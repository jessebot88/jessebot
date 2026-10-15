"""Cellular modem firmware upgrade from the staging file server.

Pushes both CWE (base firmware) and NVU (carrier PRI) files to a subdirectory
on flash, then runs `microcode reload cellular` to apply them.
"""

import re
import time

from ..console import (
    copy_image,
    get_ei_creds,
    get_ei_host_and_base_path,
    get_ei_scheme,
    remote_file_check,
    FILE_MISSING,
    EXEC_PROMPT,
)

_CELL_KEYWORDS = {"lte", "4g", "5g", "cellular", "cat18", "cat12", "cat6", "nim-ltea"}

_CARRIER_TAG_MAP = {
    "FirstNet": "firstnet",
    "Verizon Wireless": "verizon",
}

# Carrier PRI names, as `show cellular <subslot> hardware` prints them in its
# "Carrier = X" field (compared lower-cased), accepted for each carrier key. A
# modem whose PRI is not in the tuple needs the NVU flashed even when its base
# firmware version already matches. FirstNet runs on AT&T's network and the
# module may report the PRI as the AT&T family — tighten once real output is
# on file. A platform that prints no Carrier field is gated on version only.
_CARRIER_PRI_NAMES = {
    "verizon": ("verizon",),
    "firstnet": ("firstnet", "att"),
}

# The carrier image row (lower-cased) in `show cellular X firmware` that each
# carrier key activates. FirstNet rides the AT&T image.
_CARRIER_FW_NAMES = {
    "verizon": "verizon",
    "firstnet": "att",
}

# How long to wait for a modem to answer `show cellular ... hardware` before
# treating it as a failure. After an IOS-XE reload the router is back on the
# console (and past the connectivity step) minutes before a 5G module finishes
# initialising.
_MODEM_READY_TIMEOUT = 600
_MODEM_READY_INTERVAL = 15

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+\.\d+)")
_CARRIER_RE = re.compile(r"Carrier\s*=\s*([^\s,]+)", re.IGNORECASE)
_NOT_READY_RE = re.compile(r"% Invalid|not ready|not present", re.IGNORECASE)
# `show cellular X firmware` while the modem is (re)initialising: "Modem is
# still down, please wait for modem to come up" / "Modem is not ready". Seen
# live on an EM9293 right after the APN write WHILE `show cellular X
# hardware` already said "Modem Online" — so this answer must short-circuit
# the hardware fallback, or the readiness gate passes a modem the controller
# still rejects as "in reset". (A "% Invalid" from a module with no firmware
# table command is NOT in here: that one must fall back to hardware.)
_MODEM_DOWN_RE = re.compile(r"still down|not ready|wait for modem", re.IGNORECASE)
# `show cellular X firmware` rows: "4    VERIZON    02.17.08.00    030.092_001    Active"
_FW_ROW_RE = re.compile(
    r"^\s*(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(Active|Inactive)\s*$", re.IGNORECASE | re.MULTILINE
)
_FW_MODE_RE = re.compile(r"Firmware Activation mode\s*=\s*(\S+)", re.IGNORECASE)


def preflight(device, settings):
    issues = []
    modules = _find_cellular_modules(device)
    if not modules:
        return issues

    if settings.get("file_server_ei") is None:
        issues.append("No file server ExternalIntegration selected")
        return issues

    carrier = _resolve_carrier(device, settings)
    if carrier is None:
        device_tags = set(device.tags.values_list("name", flat=True))
        issues.append(f"No carrier firmware match — device tags: {', '.join(device_tags) or 'none'}")
        return issues

    for label, key in [("CWE", "cwe_file"), ("NVU", "nvu_file")]:
        filename = carrier.get(key, "")
        if not filename:
            issues.append(f"No {label} file configured for {carrier['key']}")
            continue
        if remote_file_check(settings.get("file_server_ei"), filename) == FILE_MISSING:
            issues.append(f"{label} file '{filename}' not found on the file server")

    if carrier.get("cwe_file") and not carrier.get("version"):
        issues.append(
            f"Cannot read a firmware version from the CWE filename '{carrier['cwe_file']}' "
            "— expected a.b.c.d in the name"
        )

    return issues


def applies_to(device, settings):
    """True if the device has a cellular module and a matching carrier firmware configured.

    Do NOT gate on file_server_ei here: applicability is a property of the
    DEVICE (has a modem + a carrier match), and it must resolve identically
    during the job run and during progress-page rendering. The job injects
    file_server_ei into settings but the page/API don't, so checking it here
    made the step vanish from the displayed pipeline while still executing —
    desyncing the progress cards. A missing file server is caught by preflight
    and by run() instead (a real error, not a silent skip).
    """
    if not _find_cellular_modules(device):
        return False
    return _resolve_carrier(device, settings) is not None


def run(child, device, session, settings, logger):
    """Detect installed cellular modules and upgrade firmware for the device's carrier."""
    modules = _find_cellular_modules(device)
    if not modules:
        logger.info("No cellular modules installed — skipping")
        return "No cellular modules — skipped"

    carrier = _resolve_carrier(device, settings)
    if carrier is None:
        logger.info("No carrier tag matches firmware settings — skipping")
        return "No carrier firmware configured for this device — skipped"

    carrier_key = carrier["key"]
    cwe_file = carrier["cwe_file"]
    nvu_file = carrier["nvu_file"]
    target_version = carrier["version"]
    router_dir = carrier["router_dir"]

    ei = settings.get("file_server_ei")
    if ei is None:
        raise RuntimeError("No file server ExternalIntegration configured")
    scheme = get_ei_scheme(ei)
    user, password = get_ei_creds(ei, required=(scheme == "scp"))
    host, base_path = get_ei_host_and_base_path(ei)

    logger.info(f"Carrier: {carrier_key} — CWE: {cwe_file}, NVU: {nvu_file}")
    if not target_version:
        raise RuntimeError(
            f"Cannot read a firmware version from the CWE filename '{cwe_file}' — "
            "the upgrade cannot be verified, so it is not attempted"
        )

    results = []
    for bay_name, module_model, subslot in modules:
        logger.info(f"Found {module_model} in bay {bay_name} (subslot {subslot})")

        # A modem that never answers is a failure, not a skip: the old
        # single-query "not responding — skipping" path let the pipeline go
        # green with the modem unflashed whenever the router booted faster
        # than the module.
        version, pri = _wait_for_modem_ready(child, subslot, logger)

        on_target, reason = _modem_matches_target(version, pri, carrier_key, target_version)
        if on_target:
            logger.info(
                f"Modem already on target firmware {version} "
                f"(carrier PRI {pri or 'not reported'}) — skipping upgrade"
            )
            results.append(f"Modem {subslot}: already on {version} ({(pri or carrier_key).upper()})")
            continue
        logger.info(f"Upgrade needed: {reason}")

        _ensure_fw_directory(child, router_dir, logger)

        _download_if_missing(child, scheme, user, password, host, base_path, router_dir, cwe_file, logger)
        _download_if_missing(child, scheme, user, password, host, base_path, router_dir, nvu_file, logger)

        _run_microcode_reload(child, subslot, router_dir, logger)
        _poll_modem_online(
            child, subslot, logger, target_version=target_version, carrier_key=carrier_key
        )
        results.append(
            f"Modem {subslot}: {version} ({(pri or 'unknown').upper()}) → "
            f"{target_version} ({carrier_key.upper()})"
        )
    return "; ".join(results)


def _parse_version_from_cwe(cwe_filename):
    """Extract version from CWE filename like 'modem_01.02.03.04.cwe' -> '01.02.03.04'."""
    if not cwe_filename:
        return ""
    match = re.search(r"(\d+\.\d+\.\d+\.\d+)", cwe_filename)
    return match.group(1) if match else ""


def _carrier_key_from_tags(device):
    """The carrier key ('verizon', 'firstnet') from the device's tags, or None."""
    device_tags = set(device.tags.values_list("name", flat=True))
    for tag_name, carrier_key in _CARRIER_TAG_MAP.items():
        if tag_name in device_tags:
            return carrier_key
    return None


def _resolve_carrier(device, settings):
    """Match device tags against carrier settings. Returns a dict or None."""
    device_tags = set(device.tags.values_list("name", flat=True))
    cwe = settings.get("cell_firmware_cwe", "")

    for tag_name, carrier_key in _CARRIER_TAG_MAP.items():
        if tag_name not in device_tags:
            continue
        nvu = settings.get(f"cell_firmware_{carrier_key}_nvu", "")
        if not nvu:
            continue
        return {
            "key": carrier_key,
            "cwe_file": cwe,
            "nvu_file": nvu,
            "version": _parse_version_from_cwe(cwe),
            "router_dir": f"em9293_{carrier_key[:3]}",
        }

    return None


_CELLULAR_IFACE_PATTERN = re.compile(r"^[Cc]ellular\s*(\d+(?:/\d+)+)$")


def _find_cellular_modules(device):
    """Return list of (bay_name, module_model, subslot) for installed cellular modules."""
    results = []
    for bay in device.module_bays.all():
        try:
            module = bay.installed_module
        except bay._meta.model.installed_module.RelatedObjectDoesNotExist:
            continue

        model_lower = module.module_type.model.lower()
        if not any(kw in model_lower for kw in _CELL_KEYWORDS):
            continue

        subslot = _resolve_cellular_subslot(module)
        results.append((bay.name, module.module_type.model, subslot))

    return results


def _resolve_cellular_subslot(module):
    """Return the IOS subslot (e.g. '0/2/0') for a cellular module.

    Reads the subslot from the module's own Cellular interface name rather
    than the bay position, because bay names are platform-dependent (e.g.
    'PIM 1' on IR1101, '0/1/0' on C8200L) and don't always match the IOS
    subslot used in cellular commands. The Cellular interface on the module
    is the authoritative source.
    """
    for iface in module.interfaces.all():
        match = _CELLULAR_IFACE_PATTERN.match(iface.name)
        if match:
            return match.group(1)
    raise RuntimeError(
        f"Module '{module.module_type.model}' has no 'CellularN/N/N' interface — "
        f"cannot determine IOS subslot for cellular commands"
    )


def _read_modem_state(child, subslot):
    """Return (version, carrier) for the modem's ACTIVE firmware image.

    Primary source is the carrier table from `show cellular <subslot>
    firmware` (Idx / Carrier / FwVersion / PriVersion / Status): the Active
    row's FwVersion and Carrier, lower-cased. Modules that print no table fall
    back to `show cellular <subslot> hardware` — the "Modem Firmware Version"
    line, plus a "Carrier = X" field when the platform prints one.

    (None, None) while the modem isn't answering.
    """
    output = child.send_command(f"show cellular {subslot} firmware", read_timeout=30)
    if _MODEM_DOWN_RE.search(output or ""):
        return None, None
    rows, _ = _parse_firmware_table(output)
    for row in rows:
        if row["active"]:
            match = _VERSION_RE.search(row["fw"])
            return (match.group(1) if match else row["fw"]), row["carrier"]

    output = child.send_command(f"show cellular {subslot} hardware", read_timeout=30)
    return _parse_modem_state(output)


def _parse_firmware_table(output):
    """Parse `show cellular X firmware` into (rows, activation_mode).

    rows: [{"idx": int, "carrier": "verizon", "fw": "02.17.08.00",
            "pri": "030.092_001", "active": bool}, ...]
    activation_mode: "manual" / "auto" / None.
    """
    rows = [
        {
            "idx": int(m.group(1)),
            "carrier": m.group(2).lower(),
            "fw": m.group(3),
            "pri": m.group(4),
            "active": m.group(5).lower() == "active",
        }
        for m in _FW_ROW_RE.finditer(output or "")
    ]
    mode = _FW_MODE_RE.search(output or "")
    return rows, (mode.group(1).lower() if mode else None)


def _parse_modem_state(output):
    if _NOT_READY_RE.search(output):
        return None, None

    version = None
    firmware_lines = [line for line in output.splitlines() if "Firmware" in line]
    firmware_lines.sort(key=lambda line: 0 if "Modem Firmware Version" in line else 1)
    for line in firmware_lines:
        match = _VERSION_RE.search(line)
        if match:
            version = match.group(1)
            break

    match = _CARRIER_RE.search(output)
    carrier = match.group(1).lower() if match else None
    return version, carrier


def _modem_matches_target(version, carrier, carrier_key, target_version):
    """Return (on_target, reason) for a modem's reported firmware + carrier PRI.

    Both must match: the base firmware version (from the CWE filename) AND the
    carrier PRI (from the NVU). A modem on the right base firmware with the
    wrong PRI — a reused unit, or one shipped with the generic image — still
    needs the flash. The carrier check only applies when the platform reports
    a Carrier field.
    """
    if target_version and version != target_version:
        return False, f"firmware {version or 'unknown'} is not the target {target_version}"
    accepted = _CARRIER_PRI_NAMES.get(carrier_key, (carrier_key,))
    if carrier is not None and carrier not in accepted:
        return False, f"carrier PRI '{carrier}' is not {'/'.join(accepted)}"
    return True, "firmware and carrier PRI on target"


def _wait_for_modem_ready(
    child, subslot, logger, timeout=_MODEM_READY_TIMEOUT, interval=_MODEM_READY_INTERVAL
):
    """Block until the modem reports a firmware version; raise if it never does.

    Returns (version, carrier) from `_read_modem_state`.
    """
    from netmiko.exceptions import ReadTimeout

    deadline = time.time() + timeout
    announced = False
    while True:
        try:
            version, carrier = _read_modem_state(child, subslot)
        except ReadTimeout:
            version, carrier = None, None
        if version:
            logger.info(f"Current modem firmware: {version}, carrier PRI: {carrier or 'not reported'}")
            return version, carrier
        if time.time() >= deadline:
            raise RuntimeError(
                f"Modem in subslot {subslot} did not report a firmware version within "
                f"{timeout}s — cannot verify or upgrade it (module still initialising, "
                "or the subslot from its Cellular interface is wrong)"
            )
        if not announced:
            logger.info(f"Modem in subslot {subslot} not ready yet — waiting up to {timeout}s")
            announced = True
        time.sleep(interval)


def _ensure_fw_directory(child, router_dir, logger):
    output = child.send_command_timing(f"mkdir flash:/{router_dir}", last_read=2.0)
    if "Create directory" in output:
        child.send_command_timing("", last_read=2.0)  # accept the default name


def _download_if_missing(child, scheme, user, password, host, base_path, router_dir, filename, logger):
    """Download a firmware file to flash:/<router_dir>/ if not already present."""
    if not filename:
        return

    # netmiko strips the command echo, so a bare filename match is safe here.
    output = child.send_command(f"dir flash:/{router_dir}/ | include {filename}", read_timeout=30)
    if filename in output:
        logger.info(f"{filename} already on flash — skipping")
        return

    remote_path = f"{base_path}/{filename}" if base_path else f"/{filename}"
    copy_image(
        child,
        scheme=scheme,
        user=user,
        password=password,
        host=host,
        remote_path=remote_path,
        local_dest=f"flash:/{router_dir}/{filename}",
        logger=logger,
        timeout=600,
        where="cell_copy",
    )


def _run_microcode_reload(child, subslot, router_dir, logger):
    """Flash the modem firmware using microcode reload.

    The `microcode reload cellular <slot> <bay>` syntax takes the first two
    components of the IOS subslot (chassis/slot/port → chassis bay), so we
    derive them from the subslot parsed out of the Nautobot module bay rather
    than hardcoding.
    """
    parts = subslot.split("/")
    if len(parts) < 2:
        raise RuntimeError(f"Unexpected subslot format '{subslot}' — expected 'chassis/slot[/port]'")
    micro_target = f"{parts[0]} {parts[1]}"

    logger.info(f"Starting modem firmware upgrade from flash:/{router_dir}")
    child.drain(quiet=0.3, max_wait=5)
    child.sendline(f"microcode reload cellular {micro_target} modem-provision flash:/{router_dir}")

    idx, _ = child.expect(
        [r"Reload microcode\?\s*\[confirm\]", r"% Invalid", EXEC_PROMPT],
        timeout=60,
        where="cell_micro_confirm",
    )
    if idx == 0:
        child.send_return()
    elif idx == 1:
        child.expect([EXEC_PROMPT], timeout=10, where="cell_micro_err")
        raise RuntimeError("microcode reload command not recognized — check platform")

    idx, _ = child.expect(
        [r"Log status.*\[confirm\]", r"send FW Upgrade command"],
        timeout=60,
        where="cell_micro_log",
    )
    if idx == 0:
        child.send_return()

    logger.info("Firmware upgrade in progress — waiting for completion")
    idx, _ = child.expect(
        [
            r"Firmware Upgrade has completed successfully",   # 0: synchronous done (older LTE NIM)
            r"F/W Upgrade.*fail",                             # 1: synchronous failure
            r"(?i)Microcode Reload Process launched",         # 2: async launch (5G/EM9293 — runs in background)
            EXEC_PROMPT,                                      # 3: returned to prompt (also async)
        ],
        timeout=600,
        where="cell_micro_wait",
    )

    if idx == 0:
        child.expect([EXEC_PROMPT], timeout=60, where="cell_micro_done")
        logger.info("Modem firmware upgrade completed (synchronous)")
    elif idx == 1:
        child.expect([EXEC_PROMPT], timeout=60, where="cell_micro_fail")
        raise RuntimeError("Modem firmware upgrade failed")
    elif idx in (2, 3):
        # 5G modules (EM9293/Graphit) launch the upgrade as a BACKGROUND
        # process and return to the prompt immediately — there is no
        # synchronous "completed successfully" banner. Completion is reported
        # asynchronously via syslog and, authoritatively for us, confirmed by
        # _poll_modem_online polling the modem firmware version. So this is a
        # successful LAUNCH, not a failure — let the poll verify the outcome.
        logger.info(
            "Firmware upgrade launched in the background — the modem will drop and "
            "reflash; confirming completion by polling the modem firmware version"
        )
    else:
        raise RuntimeError(
            "Modem firmware upgrade produced no recognizable response after launch — "
            f"verify the modem firmware manually. Console showed: {child.tail()}"
        )


def _poll_modem_online(
    child, subslot, logger, target_version=None, carrier_key=None, timeout=900, interval=15
):
    """Wait for the modem to return, then confirm it's on the target firmware.

    Coming back online is necessary but not sufficient — a flash that silently
    no-op'd leaves the modem online on the OLD firmware or the OLD carrier PRI.
    When target_version is known, the poll only succeeds once the reported
    firmware matches it AND the carrier PRI (when reported) is the carrier's;
    a modem that returns on the wrong version or PRI raises rather than
    reporting success.

    On 5G modules the `microcode reload` runs in the background: the modem
    drops, reflashes, and reboots over several minutes, so this is the step
    that actually confirms the async upgrade landed.
    """
    from netmiko.exceptions import ReadTimeout

    logger.info(f"Waiting for modem to come back online (up to {timeout}s)")
    start = time.time()
    last_version = last_carrier = None

    while time.time() - start < timeout:
        time.sleep(interval)
        try:
            version, carrier = _read_modem_state(child, subslot)
        except ReadTimeout:
            # Modem/CLI unresponsive mid-flash — keep polling rather than fail.
            continue
        if version is None:
            continue

        last_version, last_carrier = version, carrier
        if target_version is None:
            logger.info(f"Modem is back — firmware: {version}")
            return
        on_target, reason = _modem_matches_target(version, carrier, carrier_key, target_version)
        if on_target:
            logger.info(
                f"Modem is back on target firmware {target_version} "
                f"(carrier PRI {carrier or 'not reported'})"
            )
            return
        # Online but not yet on the target — keep polling in case the modem
        # is still finalizing; a persistent mismatch fails below.
        logger.info(f"Modem online but {reason} — waiting")

    if last_version and target_version:
        _, reason = _modem_matches_target(last_version, last_carrier, carrier_key, target_version)
        raise RuntimeError(
            f"Modem in subslot {subslot} came back but {reason} — upgrade did not take"
        )
    raise RuntimeError(f"Modem in subslot {subslot} didn't come back after {timeout}s")
