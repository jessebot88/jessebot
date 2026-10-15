"""Apply the carrier APN profile to the cellular modem.

This is an exec-mode command that writes the profile directly to the modem
and triggers a full modem reboot. We wait passively for the modem to come
back online before proceeding.
"""

import re
import time

from ..console import EXEC_PROMPT
from .cell_firmware import (
    _CARRIER_TAG_MAP,
    _find_cellular_modules,
    _parse_firmware_table,
    _wait_for_modem_ready,
)

# IOS-XE rejects exec/config syntax with "% Invalid input ...", but the
# cellular controller rejects a semantically-bad line with a bare
# "Error: ..." (no '%' prefix) — e.g.
#   Error: Cellular Modem in slot 0/1 is still in reset, we recommend to
#   re-execute this cmd after 60 seconds
# Both must count as rejections, or the step goes green with nothing applied.
_CONFIG_ERROR = re.compile(
    r"(?:%\s*(?:Invalid|Error|Incomplete|Ambiguous)|^\s*Error:)",
    re.IGNORECASE | re.MULTILINE,
)
_MODEM_IN_RESET = re.compile(r"still in reset|re-execute this (?:cmd|command)", re.IGNORECASE)

# The attach is retried on the device's own advice ("re-execute this cmd
# after 60 seconds"); 10 × 60s matches the modem-ready timeout used by the
# firmware step, after which a modem that is still in reset is a real failure.
_ATTACH_RETRY_WAIT = 60
_ATTACH_RETRIES = 10


def preflight(device, settings):
    issues = []
    modules = _find_cellular_modules(device)
    if not modules:
        return issues
    apn = _resolve_apn(device, settings)
    if apn is None:
        device_tags = set(device.tags.values_list("name", flat=True))
        issues.append(f"No APN configured for device tags: {', '.join(device_tags) or 'none'}")
    return issues


def applies_to(device, settings):
    """True if the device has a cellular module and a carrier APN configured."""
    if not _find_cellular_modules(device):
        return False
    return _resolve_apn(device, settings) is not None


def run(child, device, session, settings, logger):
    """Apply the carrier-specific APN profile and wait for modem reboot."""
    modules = _find_cellular_modules(device)
    if not modules:
        logger.info("No cellular modules — skipping APN profile")
        return "No cellular modules — skipped"

    apn = _resolve_apn(device, settings)
    if apn is None:
        logger.info("No carrier APN configured for this device — skipping")
        return "No carrier APN configured — skipped"

    for bay_name, module_model, subslot in modules:
        # With "Firmware Activation mode = AUTO" (lte firmware auto-sim) the
        # module re-evaluates the SIM on every modem reset and re-applies the
        # carrier image, which restores the carrier DEFAULT profiles — seen
        # live on a FirstNet router: profile 1 was written as the configured
        # APN, then read back at validation as firstnet-broadband/IPv4v6 (the
        # AT&T defaults) with the SIM never attaching. The Verizon reset step
        # already turns auto-sim off; do it here for every carrier (a no-op
        # where it is already off) BEFORE the profile write, so nothing can
        # put the defaults back afterwards.
        _disable_auto_sim(child, subslot, logger)

        cmd = f"cellular {subslot} lte profile create 1 {apn} none ipv4"
        logger.info(f"Applying APN profile: {cmd}")

        child.drain(quiet=0.3, max_wait=5)
        child.sendline(cmd)
        _handle_confirm_prompts(child, logger)
        _wait_for_modem_reboot(child, subslot, settings, logger)

        # The syslog watch above is a hint, not proof: the modem reset kicked
        # off by 'profile create' can outlive the %CELLWAN messages (seen on
        # FirstNet SIMs), and the controller then rejects the attach with
        # "Error: Cellular Modem ... is still in reset". Confirm the modem is
        # answering before touching the controller, and retry the attach on
        # the device's own advice if it still says it is in reset.
        _wait_for_modem_ready(child, subslot, logger)

        # Make the slot/profile binding explicit instead of relying on modem
        # defaults — a modem reused from a prior deployment may have stale
        # attach-profile mappings that silently override profile 1.
        _attach_data_profile(child, subslot, sim_slot=0, profile_id=1, logger=logger)
    subslots = ", ".join(subslot for _, _, subslot in modules)
    return f"APN {apn} on profile 1, attached to SIM slot 0 ({subslots})"


def _resolve_apn(device, settings):
    """Match device tags to carrier APN settings. Returns the APN string or None."""
    device_tags = set(device.tags.values_list("name", flat=True))

    for tag_name, carrier_key in _CARRIER_TAG_MAP.items():
        if tag_name in device_tags:
            apn = settings.get(f"cell_apn_{carrier_key}", "")
            if apn:
                return apn

    return None


def _first_error_line(output):
    """The first line of `output` that the controller flagged as an error, or None."""
    m = _CONFIG_ERROR.search(output or "")
    if not m:
        return None
    line_start = output.rfind("\n", 0, m.start()) + 1
    line_end = output.find("\n", m.end())
    if line_end == -1:
        line_end = len(output)
    return output[line_start:line_end].strip()


def _controller_config(
    child,
    subslot,
    lines,
    what,
    logger,
    retries=_ATTACH_RETRIES,
    retry_wait=_ATTACH_RETRY_WAIT,
):
    """Apply `lines` under `controller cellular <subslot>`, retrying while the
    modem is in reset.

    The controller refuses its lines while the modem is still resetting
    ("Error: Cellular Modem in slot 0/1 is still in reset, we recommend to
    re-execute this cmd after 60 seconds"). That is retried, waiting
    `retry_wait` between attempts, up to `retries` times; any other error, or
    a modem that never leaves reset, raises so the step fails instead of
    going green with the change not applied. `what` names the change in log
    and error text (e.g. "data-profile 1 attach").
    """
    commands = [f"controller cellular {subslot}", *lines]
    for attempt in range(1, retries + 1):
        logger.info(
            f"Applying {what} under controller cellular {subslot}"
            + (f" — attempt {attempt}/{retries}" if attempt > 1 else "")
        )
        child.drain(quiet=0.3, max_wait=5)
        output = child.send_config_set(commands, read_timeout=30)

        if _MODEM_IN_RESET.search(output or ""):
            if attempt == retries:
                raise RuntimeError(
                    f"Modem in subslot {subslot} was still in reset after {retries} "
                    f"attempts over {retries * retry_wait}s — {what} was NOT applied"
                )
            logger.warning(
                f"Modem in subslot {subslot} is still in reset — the device asks for a "
                f"re-try; waiting {retry_wait}s (attempt {attempt}/{retries})"
            )
            time.sleep(retry_wait)
            continue

        error = _first_error_line(output)
        if error:
            raise RuntimeError(f"{what} rejected under controller cellular {subslot}: {error}")
        if attempt > 1:
            logger.info(f"{what} applied on attempt {attempt}")
        return


def _disable_auto_sim(child, subslot, logger):
    """Turn off `lte firmware auto-sim` under the controller and confirm the
    module reports MANUAL activation mode.

    Auto-sim re-applies the carrier image on modem resets and, with it, the
    carrier's default profile set — which overwrites the APN profile this
    step writes. Idempotent: harmless where the Verizon reset step already
    turned it off. A module that prints no "Firmware Activation mode" line
    cannot be confirmed and is left at the command's word; one that still
    reads AUTO afterwards is logged as a warning, since the profile wipe is
    then still possible (validation will catch the end result).
    """
    logger.info(f"Disabling firmware auto-sim on subslot {subslot} so the carrier defaults cannot overwrite the APN profile")
    _controller_config(child, subslot, [" no lte firmware auto-sim"], "no lte firmware auto-sim", logger)

    child.drain(quiet=0.3, max_wait=3)
    output = child.send_command(f"show cellular {subslot} firmware", read_timeout=30)
    _, mode = _parse_firmware_table(output)
    if mode == "auto":
        logger.warning(
            f"Subslot {subslot} still reports Firmware Activation mode = AUTO after "
            "'no lte firmware auto-sim' — the carrier defaults may still overwrite the profile"
        )
    elif mode:
        logger.info(f"Subslot {subslot}: Firmware Activation mode = {mode.upper()}")


def _attach_data_profile(
    child,
    subslot,
    sim_slot,
    profile_id,
    logger,
    retries=_ATTACH_RETRIES,
    retry_wait=_ATTACH_RETRY_WAIT,
):
    """Bind the data-profile to the SIM slot in CONTROLLER config mode.

    On these modules the attach is a `controller cellular X/Y/Z` config
    command, NOT an exec command — the exec form ('cellular ... lte sim ...')
    is rejected with '% Invalid input'. Mirrors the deployment guide:
        config t
        controller cellular <subslot>
         lte sim data-profile <n> attach-profile <n> slot <s>
    (The older exec 'lte sim primary slot' line is dropped — it is not part of
    the guide and the module rejects it.) Retries while the modem is still in
    reset after the APN write — see _controller_config.
    """
    _controller_config(
        child,
        subslot,
        [f" lte sim data-profile {profile_id} attach-profile {profile_id} slot {sim_slot}"],
        f"data-profile {profile_id} attach (slot {sim_slot})",
        logger,
        retries=retries,
        retry_wait=retry_wait,
    )


def _handle_confirm_prompts(child, logger):
    """Handle the overwrite/confirm prompts that follow the profile create command."""
    start = time.time()
    while time.time() < start + 30:
        idx, _ = child.expect(
            [
                r"Do you want to overwrite\?\s*\[confirm\]",     # 0
                r"Are you sure\?\s*\[confirm\]",                 # 1
                r"Profile\s+1\s+written\s+to\s+modem",           # 2
                r"Profile\s+1\s+is\s+not\s+changed",             # 3
                r"%\s*(Invalid|Error|Incomplete|Ambiguous)",     # 4  rejected
            ],
            timeout=5,
            where="apn_confirm",
        )

        if idx in (0, 1):
            child.send_return()
            continue
        elif idx in (2, 3):
            logger.info("APN profile command accepted")
            break
        elif idx == 4:
            child.expect([EXEC_PROMPT], timeout=5, where="apn_confirm_err")
            raise RuntimeError("APN profile create rejected by the device")
        elif idx is None:
            break


def _wait_for_modem_reboot(child, subslot, settings, logger):
    """Passively wait for the modem to reboot after APN profile apply.

    This is the one step that DEPENDS on async console syslog — it watches for
    %CELLWAN-* messages. Staging turned console logging off (so syslog doesn't
    corrupt netmiko's prompt detection elsewhere), so re-enable it for the wait
    and turn it back off after, leaving the session clean for config_push.
    """
    child.send_config_set(["logging console"], read_timeout=15)
    try:
        _watch_for_modem_reboot(child, subslot, settings, logger)
    finally:
        child.send_config_set(["no logging console"], read_timeout=15)


def _watch_for_modem_reboot(child, subslot, settings, logger):
    max_wait = int(settings.get("cell_apn_reboot_wait", 360))
    logger.info(f"Waiting up to {max_wait}s for modem reboot")

    reset_seen = False
    deadline = time.time() + max_wait

    patterns = [
        r"Graphit 5G",
        r"%CELLWAN-2-MODEM_RADIO:.*turned on",
        r"%CELLWAN-2-MODEM_UP:",
        r"%CELLWAN-.-MODEM_RESTART_IND",
        r"%CELLWAN-5-FIRMWARE_SWITCH",
        r"%CELLWAN-2-MODEM_DOWN",
        EXEC_PROMPT,
    ]

    while time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break

        idx, _ = child.expect(patterns, timeout=remaining, where="apn_reboot_wait")

        if idx == 0:
            logger.info("Modem hardware recovery detected — reboot complete")
            time.sleep(5)
            return

        if idx in (1, 2):
            if reset_seen:
                logger.info("Modem back online after confirmed reset")
                return
            continue

        if idx in (3, 4, 5):
            logger.info("Modem restart/switch event detected")
            reset_seen = True
            continue

        if idx == 6:
            continue

        if idx is None:
            break

    logger.info("Modem reboot wait complete — proceeding")
