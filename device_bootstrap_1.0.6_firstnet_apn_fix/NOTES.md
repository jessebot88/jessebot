# Device Bootstrap 1.0.6 — FirstNet APN profile fix

## What went wrong (from the TXBSPOTWR01 run on 2026-10-15)

The `cell_profile` step wrote the APN (`cellular 0/1/0 lte profile create 1 34287.fn none ipv4`,
"Profile 1 written to modem"), waited on the `%CELLWAN` syslog watch, then went straight
into `controller cellular 0/1/0` / `lte sim data-profile 1 attach-profile 1 slot 0`.
The controller answered:

    Error: Cellular Modem in slot 0/1 is still in reset, we recommend to re-execute this cmd after 60 seconds

The step's error regex only matched `% Invalid` / `% Error` style messages (with the `%`
prefix), so this bare `Error:` line was not recognised. The step reported success and the
pipeline moved on to Config Push with the SIM slot never bound to profile 1. Re-running the
same commands by hand after waiting worked, which is exactly what the device asked for.

Verizon passed because its modem reboot happened to be finished by the time the syslog watch
returned. On the FirstNet SIM the modem was still resetting after the watch gave up.

## What 1.0.6 changes (`jobs/steps/cell_profile.py` only, no migration)

1. **Bare `Error:` lines now count as rejections.** `_CONFIG_ERROR` matches `^Error:` as well
   as the `%`-prefixed forms, and the raised message quotes the full offending line.
2. **Modem readiness is confirmed before the attach.** After the syslog watch, `run()` calls
   `cell_firmware._wait_for_modem_ready()` (polls `show cellular X firmware` / `hardware`
   every 15 s, up to 600 s) so the controller command is not sent to a modem that is not
   answering yet.
3. **"Still in reset" is retried on the device's own advice.** `_attach_data_profile()` waits
   60 s and re-sends the attach, up to 10 times (10 minutes). Any other error raises
   immediately; a modem that never leaves reset fails the step instead of going green.

New tests: `tests/test_cell_profile.py` (12 tests) — error detection with and without `%`,
the retry/sleep sequence, the fail-after-N-attempts path, and that `run()` orders
confirm → syslog wait → modem-ready → attach. The existing cell_firmware and
cell_firmware_reset suites still pass (56 tests total).

## Files here

- `nautobot_app_device_bootstrap-1.0.6-py3-none-any.whl` — the 1.0.5 wheel rebuilt with the
  patched step, the new test file and the version bumped to 1.0.6. Install with
  `pip install --force-reinstall nautobot_app_device_bootstrap-1.0.6-py3-none-any.whl`,
  then `nautobot-server post_upgrade` and restart the workers.
- `cell_profile.py` — the patched step, drop-in for `nautobot_app_device_bootstrap/jobs/steps/cell_profile.py`.
- `test_cell_profile.py` — drop into `nautobot_app_device_bootstrap/tests/`.
- `cell_profile.diff` — unified diff against 1.0.5 for applying to the source repo at home
  (`git apply cell_profile.diff` from the repo root).

Still to do in the source repo: bump the version in `pyproject.toml`, and add a
`docs/admin/release_notes/version_1.0.6.md` entry (the wheel's bundled HTML docs were not
regenerated, so they still stop at 1.0.5).

## Not changed, worth knowing

The syslog watch (`_watch_for_modem_reboot`) is untouched. Its `Graphit 5G` pattern returns
immediately without requiring a prior MODEM_DOWN/RESTART event, which is a plausible reason
the watch ended early on this 5G module. The new modem-ready gate and the attach retry make
the step correct regardless of when the watch returns, so this was left alone rather than
risk changing the Verizon behaviour that already works.
