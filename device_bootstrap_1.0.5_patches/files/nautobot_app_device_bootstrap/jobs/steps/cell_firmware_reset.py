"""Cellular carrier-firmware factory reset.

Vendor procedure for modems that will not connect after provisioning. It
began as the Verizon workaround and now runs for EVERY carrier-tagged router
with a cellular module: on FirstNet, auto-sim left on meant a later modem
reset restored the AT&T default profiles over the APN the pipeline had
written, and one deterministic, bench-tested path for all carriers beats a
per-carrier special case. The carrier image re-activated in step 4 comes
from _CARRIER_FW_NAMES (Verizon → VERIZON, FirstNet → ATT).

    1. controller Cellular X: `no lte firmware auto-sim`,
       `lte cellular-profile-cleanup`
    2. `cellular X lte firmware-activate <GENERIC idx>` (from `show cellular
       X firmware`), wait for the modem
    3. save config, reload the router
    4. confirm GENERIC is Active, `cellular X lte firmware-activate <carrier
       idx>`, wait for the modem on the carrier image
    5-6. re-create the APN profile and attach it to the SIM slot — that is
       the existing `cell_profile` step, which must follow this one in the
       pipeline.

The reload reuses the reload step's confirm/boot-wait machinery (it is the
same reload, mid-pipeline, with the staging config saved first).
"""

import re
import time

from ..console import EXEC_PROMPT
from . import reload as _reload
from .cell_firmware import (
    _CARRIER_FW_NAMES,
    _carrier_key_from_tags,
    _find_cellular_modules,
    _modem_matches_target,
    _parse_firmware_table,
    _parse_modem_state,
    _resolve_carrier,
)

# Carriers whose provisioning includes the factory-reset procedure — every
# carrier the pipeline knows (see _CARRIER_TAG_MAP). A router with a modem
# but no carrier tag is skipped: there is no image to re-activate for it.
RESET_CARRIERS = ("verizon", "firstnet")

_GENERIC = "generic"
_CONFIG_ERROR = re.compile(r"%\s*(Invalid|Error|Incomplete|Ambiguous)", re.IGNORECASE)

# A firmware switch resets the modem; it is back on the new image within a
# few minutes. Same budget as the firmware step's readiness wait.
_SWITCH_TIMEOUT = 600
_SWITCH_INTERVAL = 15


def preflight(device, settings):
    return []


def applies_to(device, settings):
    """Carrier-tagged devices with a cellular module."""
    if not _find_cellular_modules(device):
        return False
    return _carrier_key_from_tags(device) in RESET_CARRIERS


def run(child, device, session, settings, logger):
    modules = _find_cellular_modules(device)
    if not modules:
        logger.info("No cellular modules installed — skipping")
        return "No cellular modules — skipped"

    carrier_key = _carrier_key_from_tags(device)
    if carrier_key not in RESET_CARRIERS:
        logger.info(f"Carrier '{carrier_key or 'none'}' does not use the firmware reset — skipping")
        return f"Not required for carrier {(carrier_key or 'none').upper()} — skipped"
    carrier_image = _CARRIER_FW_NAMES[carrier_key]

    target = _resolve_carrier(device, settings)
    target_version = target["version"] if target else None

    subslots = [subslot for _, _, subslot in modules]

    # Steps 1-2: clear modem profile state, switch the active image to GENERIC.
    for subslot in subslots:
        _controller_cleanup(child, subslot, logger)
        rows = _wait_for_firmware_table(child, subslot, logger)
        if _active_row(rows)["carrier"] == _GENERIC:
            logger.info(f"Subslot {subslot}: GENERIC image already active")
            continue
        _activate_firmware(child, subslot, _index_of(rows, _GENERIC, subslot), logger)
        _wait_for_active_carrier(child, subslot, _GENERIC, logger)

    # Step 3: save and reload the router.
    logger.info("Saving config and reloading the router to complete the modem reset")
    child.save_config()
    _reload._initiate_reload(child, logger)
    child = _reload._wait_for_boot(child, settings, logger)

    # Step 4: confirm GENERIC survived the reload, re-activate the carrier image.
    results = []
    for subslot in subslots:
        rows = _wait_for_firmware_table(child, subslot, logger)
        active = _active_row(rows)
        if active["carrier"] != _GENERIC:
            raise RuntimeError(
                f"Subslot {subslot}: expected the GENERIC image active after the reload, "
                f"found {active['carrier'].upper()} — the modem reset did not take"
            )
        _activate_firmware(child, subslot, _index_of(rows, carrier_image, subslot), logger)
        version, carrier = _wait_for_active_carrier(child, subslot, carrier_image, logger)

        if target_version:
            on_target, reason = _modem_matches_target(version, carrier, carrier_key, target_version)
            if not on_target:
                raise RuntimeError(
                    f"Subslot {subslot}: after re-activating the carrier image, {reason}"
                )
        logger.info(
            f"Subslot {subslot}: {carrier_image.upper()} image active on firmware {version} — "
            "the APN profile step re-applies the data profile"
        )
        results.append(f"Modem {subslot}: reset via GENERIC, {carrier_image.upper()} re-activated on {version}")
    return "; ".join(results)


def _controller_cleanup(child, subslot, logger):
    """Step 1: disable auto-sim and clean the modem's profile state."""
    logger.info(f"Subslot {subslot}: disabling auto-sim and cleaning cellular profiles")
    child.drain(quiet=0.3, max_wait=5)
    output = child.send_config_set(
        [
            f"controller Cellular {subslot}",
            " no lte firmware auto-sim",
            " lte cellular-profile-cleanup",
        ],
        read_timeout=60,
    )
    match = _CONFIG_ERROR.search(output)
    if match:
        raise RuntimeError(
            f"Controller cleanup rejected under controller Cellular {subslot}: {match.group(0).strip()}"
        )


def _activate_firmware(child, subslot, index, logger):
    """`cellular X lte firmware-activate <index>`; the modem resets afterwards."""
    logger.info(f"Subslot {subslot}: activating firmware index {index}")
    child.drain(quiet=0.3, max_wait=5)
    child.sendline(f"cellular {subslot} lte firmware-activate {index}")
    idx, _ = child.expect(
        [r"\[confirm\]", r"%\s*(Invalid|Error|Incomplete|Ambiguous)", EXEC_PROMPT],
        timeout=30,
        where="cell_fw_activate",
    )
    if idx == 0:
        child.send_return()
        child.expect([EXEC_PROMPT], timeout=30, where="cell_fw_activate_done")
    elif idx == 1:
        child.expect([EXEC_PROMPT], timeout=10, where="cell_fw_activate_err")
        raise RuntimeError(f"firmware-activate {index} rejected on subslot {subslot}")
    elif idx is None:
        raise RuntimeError(
            f"No response to firmware-activate {index} on subslot {subslot} — "
            f"console showed: {child.tail()}"
        )


def _read_firmware_table(child, subslot):
    output = child.send_command(f"show cellular {subslot} firmware", read_timeout=30)
    rows, _ = _parse_firmware_table(output)
    return rows


def _active_row(rows):
    return next(row for row in rows if row["active"])


def _index_of(rows, carrier, subslot):
    for row in rows:
        if row["carrier"] == carrier:
            return row["idx"]
    available = ", ".join(f"{row['idx']}={row['carrier'].upper()}" for row in rows) or "none"
    raise RuntimeError(
        f"No {carrier.upper()} image in the firmware table for subslot {subslot} "
        f"(available: {available})"
    )


def _wait_for_firmware_table(
    child, subslot, logger, timeout=_SWITCH_TIMEOUT, interval=_SWITCH_INTERVAL
):
    """Poll until `show cellular X firmware` lists an Active image."""
    from netmiko.exceptions import ReadTimeout

    deadline = time.time() + timeout
    announced = False
    while True:
        try:
            rows = _read_firmware_table(child, subslot)
        except ReadTimeout:
            rows = []
        if any(row["active"] for row in rows):
            return rows
        if time.time() >= deadline:
            raise RuntimeError(
                f"Subslot {subslot}: no firmware table with an active image within {timeout}s"
            )
        if not announced:
            logger.info(f"Subslot {subslot}: waiting for the modem's firmware table (up to {timeout}s)")
            announced = True
        time.sleep(interval)


def _wait_for_active_carrier(
    child, subslot, carrier, logger, timeout=_SWITCH_TIMEOUT, interval=_SWITCH_INTERVAL
):
    """Wait until `carrier` is the Active image AND the modem answers again.

    The table can show the new Active row while the modem is still resetting,
    so "back" means the hardware query reports a firmware version too.
    Returns (version, carrier) as reported by the modem.
    """
    from netmiko.exceptions import ReadTimeout

    logger.info(f"Subslot {subslot}: waiting for the modem on the {carrier.upper()} image (up to {timeout}s)")
    deadline = time.time() + timeout
    last_active = None
    while time.time() < deadline:
        time.sleep(interval)
        try:
            rows = _read_firmware_table(child, subslot)
            active = next((row for row in rows if row["active"]), None)
            if active is None:
                continue
            last_active = active["carrier"]
            if active["carrier"] != carrier:
                continue
            output = child.send_command(f"show cellular {subslot} hardware", read_timeout=30)
        except ReadTimeout:
            continue
        version, _ = _parse_modem_state(output)
        if version:
            logger.info(f"Subslot {subslot}: modem back on {carrier.upper()} image, firmware {version}")
            return version, active["carrier"]

    raise RuntimeError(
        f"Subslot {subslot}: modem did not come back on the {carrier.upper()} image within "
        f"{timeout}s (last active image: {(last_active or 'none').upper()})"
    )
