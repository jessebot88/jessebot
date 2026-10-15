# Device Bootstrap 1.0.5 (patched) — three fixes from the 2026-10-15 runs

Version stays 1.0.5. Install the wheel with
`pip install --force-reinstall --no-deps nautobot_app_device_bootstrap-1.0.5-py3-none-any.whl`,
then `nautobot-server post_upgrade` and restart the workers (pip skips a same-version install
without `--force-reinstall`).

Contents:

- `nautobot_app_device_bootstrap-1.0.5-py3-none-any.whl` — the shipped 1.0.5 wheel rebuilt with
  both fixes and their tests.
- `patch.diff` — one unified diff against the shipped 1.0.5 source, covering every changed file.
  From the source repo root: `git apply patch.diff` (verified to apply cleanly to a pristine 1.0.5
  tree with `patch -p1`).
- `files/` — full copies of the changed files, laid out at their package paths, for dropping in.

No model changes, no migration, no settings changes.

---

## Fix 1 — APN profile attach on FirstNet SIMs (`jobs/steps/cell_profile.py`)

**Symptom (TXBSPOTWR01).** The step wrote the APN (`Profile 1 written to modem`), waited on the
`%CELLWAN` syslog watch, then sent `controller cellular 0/1/0` /
`lte sim data-profile 1 attach-profile 1 slot 0`. The router answered:

    Error: Cellular Modem in slot 0/1 is still in reset, we recommend to re-execute this cmd after 60 seconds

The step's error regex only matched `%`-prefixed errors, so this was missed, the step went green,
and Config Push ran with the SIM slot never bound. Re-running by hand after a wait worked.
Verizon passed only because that modem had finished rebooting before the watch returned.

**Change.**
1. Bare `Error:` lines count as rejections; the failure message quotes the offending line.
2. After the syslog watch, `run()` calls `cell_firmware._wait_for_modem_ready()` (polls
   `show cellular X firmware` / `hardware` every 15 s, up to 600 s) before touching the controller.
3. `_attach_data_profile()` retries on "still in reset" every 60 s, up to 10 times. Any other
   error raises at once; a modem that never leaves reset fails the step.

4. **Auto-sim is turned off before the profile write (added after the TXMNDOTWR01 run).**
   That router's module reported `Firmware Activation mode = AUTO`. Auto-sim re-applies the
   carrier image on modem resets and with it the carrier's DEFAULT profile set: profile 1 was
   written as the configured APN, then read back at validation as `firstnet-broadband`/IPv4v6
   (the AT&T defaults, with `attm2mglobal` on profile 4) and the SIM never attached. The Verizon
   reset step already sends `no lte firmware auto-sim`; FirstNet routers skip that step. The APN
   step now sends `no lte firmware auto-sim` under `controller cellular <subslot>` for every
   carrier (a no-op where already off), with the same in-reset retry as the attach, then reads
   `show cellular X firmware` and logs the activation mode (a module still reporting AUTO is a
   warning, not a failure, since validation catches the end result). The Verizon-only
   `lte cellular-profile-cleanup` is NOT sent here: it wipes profiles and resets the modem.
   The controller attach and the auto-sim line share one helper (`_controller_config`).

Tests: `tests/test_cell_profile.py` (17 new): error detection, retry/sleep sequences, fail-after-N,
auto-sim command + mode read-back, and `run()` ordering auto-sim-off → confirm → syslog wait →
modem-ready → attach.

The syslog watch itself is untouched. Its `Graphit 5G` pattern returns without a prior
MODEM_DOWN/RESTART event, which is a plausible reason it ended early on this 5G module, but the
readiness gate and retry make the step correct either way, so the Verizon-tested behaviour was
left alone.

---

## Fix 2 — DBPERSIST false positive on config save (`jobs/drivers/cisco_ios.py`, `jobs/steps/os_image.py`)

**Symptom (TXLSOOTWR01, OS Image step).** `write mem` printed `[OK]` twice, yet the step failed
with "Config save still failing after deleting the stale DBPERSIST directory". The log line that
tripped it names a different database:

    %TDLDB-3-DBPERSIST_OPERATION_FAILURE: R0/0: dbm: Operation: persist,
    DB: /tmp/rp/tdldb/0/DMI_STATE_DB, Failure reason: Is a directory

The self-heal was built for `CTRL_MNG_CFG_DB` (the config database; its failure really does mean
startup-config was not written). `DMI_STATE_DB` is the NETCONF/YANG state store and is unrelated to
NVRAM. On this IR1101 that line is logged on every `write mem`, so the before/after count of
`show logging | count DBPERSIST` always rose by one and the step could never pass. The delete of
`bootflash:/.dbpersist/CTRL_MNG_CFG_DB/` returning "No such file" confirms the real bug was not
present. The router is noisy; the provisioner was wrong to call that a failed save.

**Change.**
1. `DBPERSIST_ERROR` now matches only config-DB signatures: `CTRL_MNG_CFG_DB`,
   `MCP_DBPERSIST_ERROR`, "configuration update could not complete",
   "startup-config file open failed". `dbpersist_log_count()` and `dbpersist_bug_logged()` pull
   `show logging | include DBPERSIST` and filter on that, so DMI (or any other DB) lines no longer
   move the count or trigger the heal. The same regex gates the reload step's boot-variable
   repair path, so that is narrowed consistently. Evidence strings in error messages still show
   every DBPERSIST line.
2. Positive verification. `save_config()` takes `verify_contains=<text>`; when given, the save is
   judged by `show startup-config | include <text>` instead of the syslog heuristic. Present means
   success whatever the log says; absent means the save did not persist even if `[OK]` printed
   (heal, retry, then fail with a message naming the missing line). The OS Image step passes the
   image filename, so its save is proven by the `boot system` line being in startup-config.
   Other steps keep the (now narrowed) heuristic.

Tests: `tests/test_console_save.py` rewritten for the new semantics (13 tests), including the
exact TXLSOOTWR01 case (DMI line after a clean save → single attempt, no heal) and the
startup-config read-back paths.

---

## Test run

Executed here against the patched tree with nautobot/django/netmiko stubbed out:
test_console_save, test_cell_profile, test_cell_firmware, test_cell_firmware_reset, test_drivers,
test_console_ports, test_transcript, test_ndes_resolution — 116 tests, all pass. The remaining
modules (test_validate_checks, test_pipeline_consistency, test_batch) need `netutils`,
`django_filters` and real models and were not runnable in this environment; nothing in them
touches the changed code paths. Run the full suite at home with `nautobot-server test`.
