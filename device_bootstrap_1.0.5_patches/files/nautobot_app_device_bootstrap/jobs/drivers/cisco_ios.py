"""Cisco IOS / IOS-XE platform driver.

Source of truth for the Cisco console personality: prompt regexes, the
netmiko promotion target, router/switch classification, and the save-config
flow with its IOS-XE DBPERSIST self-heal. console.py re-exports the names
steps have always imported (EXEC_PROMPT, DBPERSIST helpers, ...), so step
modules keep importing from ..console.
"""

import re
import time

from .base import PlatformDriver

EXEC_PROMPT = r"(?m)^[A-Za-z0-9][\w\-]*#\s*$"
CFG_PROMPT = r"(?m)^[A-Za-z0-9][\w\-]*\([^)]+\)#\s*$"
ANY_PROMPT = r"(?m)^[A-Za-z0-9][\w\-]*(#|\([^)]+\)#)\s*$"

# IOS-XE TDLDB persist bug: a stale CTRL_MNG_CFG_DB directory under
# bootflash:/.dbpersist blocks config save AND reload:
#   %TDLDB-3-DBPERSIST_OPERATION_FAILURE: R0/0: dbm: Operation: persist,
#       DB: /bootflash/.dbpersist/CTRL_MNG_CFG_DB, Failure reason: Is a directory
#   %IOSXE_RP_CFG_NOT-3-MCP_DBPERSIST_ERROR: System configuration update
#       could not complete
#
# Only a failure of the CONFIG database means the save did not persist. The
# same %TDLDB-3-DBPERSIST_OPERATION_FAILURE line is logged for unrelated
# databases too — seen live on an IR1101 on EVERY `write mem`:
#   ... DB: /tmp/rp/tdldb/0/DMI_STATE_DB, Failure reason: Is a directory
# (the NETCONF/YANG state store). Those are noise for provisioning: the
# save printed [OK], startup-config was written, and matching on the bare
# word DBPERSIST failed the OS Image step on a router that was fine. So the
# signature names the config DB explicitly.
DBPERSIST_ERROR = re.compile(
    r"CTRL_MNG_CFG_DB|MCP_DBPERSIST_ERROR|"
    r"configuration update could not complete|startup-config file open failed",
    re.IGNORECASE,
)
# Any persist-failure line, for operator-facing evidence only (never a gate).
_ANY_DBPERSIST_LINE = re.compile(r"%\S*DBPERSIST")
_DBPERSIST_DB_PATH = "bootflash:/.dbpersist/CTRL_MNG_CFG_DB/"


def _dbpersist_log_lines(child):
    """Every DBPERSIST line currently in the log buffer (raises on error)."""
    child.drain(quiet=0.3, max_wait=3)
    out = child.send_command("show logging | include DBPERSIST", read_timeout=30)
    return [ln.strip() for ln in out.splitlines() if "DBPERSIST" in ln]


def dbpersist_log_count(child):
    """Count CONFIG-DB persist failures in the log buffer, or None if unreadable.

    The bug's failure is ASYNC: `write mem` prints [OK] while the persist
    fails seconds later as syslog (seen live). Comparing counts before/after
    a save is how an inline-clean-but-actually-failed save is caught.

    Lines are pulled with `| include DBPERSIST` and filtered in Python
    against DBPERSIST_ERROR, so a DMI_STATE_DB (or any other non-config DB)
    failure does not move the count."""
    try:
        return sum(1 for ln in _dbpersist_log_lines(child) if DBPERSIST_ERROR.search(ln))
    except Exception:
        return None


def dbpersist_bug_logged(child):
    """True if the log buffer shows the CONFIG-DB persist failure. With
    console logging off, the buffer is where the syslog signature lands."""
    try:
        return any(DBPERSIST_ERROR.search(ln) for ln in _dbpersist_log_lines(child))
    except Exception:
        return False


def startup_config_contains(child, text):
    """True if a line of startup-config contains `text` (positive proof the
    save persisted — independent of any syslog heuristic).

    `text` is handed to IOS `| include` as-is, so keep it to plain text
    (filenames, keywords); the Python side does the exact substring check.
    "startup-config is not present" and an unreadable NVRAM both read as
    False. Raises only if the console itself fails."""
    child.drain(quiet=0.3, max_wait=3)
    out = child.send_command(f"show startup-config | include {text}", read_timeout=60)
    return any(text in ln for ln in out.splitlines())


def clear_log_buffer(child):
    """Clear the device log buffer (best effort).

    Used after DBPERSIST remediation so the retry save is judged from a zero
    baseline: the bug's syslog entries land seconds after the save's [OK], so
    a straggler from the FIRST failed save can otherwise drift in between the
    retry's before/after samples and get blamed on a save that succeeded.
    Failure here is fine — the count-based comparison still works, just with
    the old race window.
    """
    try:
        child.drain(quiet=0.3, max_wait=3)
        child.send_command_timing("clear logging", last_read=1.0, read_timeout=15)
        child.send_command_timing("", last_read=1.0, read_timeout=10)  # answer [confirm]
    except Exception:
        pass


def dbpersist_log_lines(child, limit=3):
    """The last few DBPERSIST log lines (any DB), for failure evidence. Empty on error."""
    try:
        return " | ".join(_dbpersist_log_lines(child)[-limit:])
    except Exception:
        return ""


def clear_dbpersist_db(child, logger):
    """Delete the stale DBPERSIST directory that blocks save/reload."""
    logger.warning(
        f"Hit the IOS-XE DBPERSIST bug — deleting stale {_DBPERSIST_DB_PATH} and retrying"
    )
    child.drain(quiet=0.3, max_wait=3)
    # /force /recursive suppresses per-file confirms; timing-based send in
    # case a platform still prompts something unexpected.
    out = child.send_command_timing(
        f"delete /force /recursive {_DBPERSIST_DB_PATH}", last_read=2.0, read_timeout=60
    )
    return out


class CiscoIosDriver(PlatformDriver):
    """Cisco IOS / IOS-XE — routers and switches, the original device family."""

    network_drivers = ("cisco_ios", "cisco_xe")
    netmiko_device_type = "cisco_ios"

    exec_prompt = EXEC_PROMPT
    cfg_prompt = CFG_PROMPT
    any_prompt = ANY_PROMPT

    pipeline_keys = {
        "router": "pipeline_router",
        "switch": "pipeline_switch",
    }

    def classify(self, device):
        """Router vs switch by role name — router wins when a role matches both."""
        role_name = device.role.name.lower() if device.role else ""
        if "router" in role_name:
            return "router"
        if any(k in role_name for k in ("switch", "access", "dist")):
            return "switch"
        return None

    def save_config(self, console, verify_contains=None):
        """Save config, self-healing the IOS-XE TDLDB persist bug.

        A stale CTRL_MNG_CFG_DB *directory* under bootflash:/.dbpersist makes
        every save (and save-on-reload) fail with
        %TDLDB-3-DBPERSIST_OPERATION_FAILURE ... Is a directory. The fix is
        deleting that directory and saving again. Seen live on the fleet;
        `no logging console` means the syslog signature may only be in the
        log buffer, so an ambiguous save failure is confirmed there before
        remediating.

        `verify_contains`: text that must appear on a startup-config line
        once the save has persisted (e.g. the image filename of the `boot
        system` line the caller just configured). When given, the save is
        judged by READING STARTUP-CONFIG BACK, not by the syslog heuristic:
        the line present means success whatever the log buffer says; absent
        means the save did not persist, even if `write mem` printed [OK].
        Without it, only CONFIG-DB persist failures (DBPERSIST_ERROR) count
        — unrelated DBPERSIST lines such as DMI_STATE_DB are ignored.
        """
        logger = console._logger
        pre = dbpersist_log_count(console)
        try:
            output = console.conn.save_config()
        except Exception:
            if not dbpersist_bug_logged(console):
                raise
            output = None
        if output is not None and not DBPERSIST_ERROR.search(output):
            if verify_contains is not None:
                if startup_config_contains(console, verify_contains):
                    logger.debug(f"Save verified: '{verify_contains}' is in startup-config")
                    return output
                logger.warning(
                    f"Save printed [OK] but '{verify_contains}' is not in startup-config "
                    "— the save did not persist"
                )
            else:
                # Inline output can show [OK] while the persist fails async as
                # syslog (seen live) — a NEW config-DB DBPERSIST entry means
                # the save lied.
                post = dbpersist_log_count(console)
                logger.debug(f"Config-DB DBPERSIST log count around save: before={pre} after={post}")
                if pre is None or post is None or post <= pre:
                    return output
                logger.warning(
                    "Save printed [OK] but a new config-DB DBPERSIST failure hit the log "
                    "— the save did not persist"
                )
        clear_dbpersist_db(console, logger)
        # Zero the baseline so stragglers from the FIRST save's async syslog
        # can't be blamed on the retry (they otherwise land mid-comparison and
        # fail a save that actually persisted).
        clear_log_buffer(console)
        pre = dbpersist_log_count(console)
        output = console.conn.save_config()
        # The bug's syslog arrives seconds AFTER the [OK] — give it a settle
        # window before sampling, or a genuinely failed retry can pass.
        time.sleep(6)
        if verify_contains is not None:
            if startup_config_contains(console, verify_contains):
                logger.info(
                    f"Config saved successfully after DBPERSIST cleanup "
                    f"('{verify_contains}' verified in startup-config)"
                )
                return output
            raise RuntimeError(
                f"Config save still failing after deleting the stale DBPERSIST "
                f"directory — '{verify_contains}' is not in startup-config — "
                f"DBPERSIST log: {dbpersist_log_lines(console) or 'n/a'} — "
                f"device said: {' '.join(output.split())[-300:]}"
            )
        post = dbpersist_log_count(console)
        logger.debug(f"Config-DB DBPERSIST log count around retry save (post log-clear): before={pre} after={post}")
        if DBPERSIST_ERROR.search(output) or (pre is not None and post is not None and post > pre):
            evidence = dbpersist_log_lines(console)
            raise RuntimeError(
                f"Config save still failing after deleting the stale DBPERSIST "
                f"directory — new DBPERSIST log: {evidence or 'n/a'} — "
                f"device said: {' '.join(output.split())[-300:]}"
            )
        logger.info("Config saved successfully after DBPERSIST cleanup")
        return output
