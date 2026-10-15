"""Cellular firmware step: modem readiness, carrier-image gate, post-flash verify.

Pins the three behaviours that let a modem go unflashed under a green run:
a modem still initialising after the IOS-XE reload must be waited for (and a
modem that never answers must FAIL, not skip); the skip-upgrade gate must look
at the active carrier image as well as the base firmware version; and
validate must fail the pipeline when the modem isn't on the carrier firmware.
No DB, no console: a scripted fake stands in for the netmiko session.
"""
from unittest import mock

from django.test import SimpleTestCase

from nautobot_app_device_bootstrap.jobs.steps import cell_firmware as cf
from nautobot_app_device_bootstrap.jobs.steps import validate as val_step


# `show cellular 0/1/0 firmware`, as printed by a multi-carrier Sierra module.
def _fw_table(active, verizon_fw="02.17.08.00", generic_fw="02.15.01.00"):
    def status(name):
        return "Active" if name == active else "Inactive"

    return (
        "Load for five secs: 0%/0%; one minute: 0%; five minutes: 0%\n"
        "Time source is NTP, *10:26:50.649 cst Thu Feb 26 2026\n"
        "\n"
        "Idx  Carrier         FwVersion        PriVersion     Status\n"
        f"1    ATT             02.15.01.00      030.059_000    {status('ATT')}\n"
        f"2    GENERIC         {generic_fw}      030.054_001    {status('GENERIC')}\n"
        f"3    TMO             02.15.01.00      030.017_000    {status('TMO')}\n"
        f"4    VERIZON         {verizon_fw}      030.092_001    {status('VERIZON')}\n"
        "\n"
        "Firmware Activation mode  =  MANUAL\n"
    )


FW_VERIZON = _fw_table("VERIZON")                                   # on target
FW_VERIZON_OLD = _fw_table("VERIZON", verizon_fw="02.15.01.00")     # right image, old version
FW_GENERIC_SAME_VERSION = _fw_table("GENERIC", generic_fw="02.17.08.00")  # right version, wrong image
TARGET = "02.17.08.00"

# `show cellular X hardware` — the fallback for modules that print no table.
HW_EM9293 = """\
Modem Firmware Version = 01.07.03.00
Host Firmware Version = 01.07.03.00
Device Model ID = EM9293
Modem Status = Modem Online
PRI version = 1.03, Carrier = Verizon
OEM PRI version = 01.07.03.00
"""
HW_EM7455 = """\
Modem Firmware Version = SWI9X30C_02.24.05.06
Modem Firmware built = 2017/02/22 21:58:03
Device Model ID: EM7455
Package Identifier ID: 1102700_9904609_EM7455_02.24.05.06_00_Cisco_002.016_000
PRI SKU ID = 1102700, PRI version = 002.016, Carrier = Generic
"""
HW_NO_CARRIER = "Modem Firmware Version = 01.07.03.00\nDevice Model ID = EM9293\n"
NOT_READY = "Modem is not ready\n"
NOT_PRESENT = "Cellular 0/1/0 not present\n"


class FakeLogger:
    def __init__(self):
        self.lines = []

    def _log(self, level, m):
        self.lines.append((level, str(m)))

    def info(self, m, *a, **k):
        self._log("info", m)

    def warning(self, m, *a, **k):
        self._log("warning", m)

    def debug(self, m, *a, **k):
        self._log("debug", m)

    def error(self, m, *a, **k):
        self._log("error", m)

    def text(self, level=None):
        return "\n".join(m for lvl, m in self.lines if level is None or lvl == level)


class FakeChild:
    """send_command() answers by command keyword ("firmware" / "hardware");
    each keyword's outputs are consumed in order and the last one repeats."""

    def __init__(self, firmware=(), hardware=(NOT_READY,)):
        self.script = {"firmware": list(firmware), "hardware": list(hardware)}
        self.calls = []

    def send_command(self, cmd, **kwargs):
        self.calls.append(cmd)
        for key, outs in self.script.items():
            if key in cmd and outs:
                return outs.pop(0) if len(outs) > 1 else outs[0]
        return ""


class FakeClock:
    """Deterministic time.time()/time.sleep(): sleep advances the clock."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _clock(testcase, module=cf):
    clock = FakeClock()
    for name in ("time", "sleep"):
        patcher = mock.patch.object(module.time, name, getattr(clock, name))
        patcher.start()
        testcase.addCleanup(patcher.stop)
    return clock


class ParseFirmwareTableTest(SimpleTestCase):
    def test_rows_and_mode(self):
        rows, mode = cf._parse_firmware_table(FW_VERIZON)
        self.assertEqual(mode, "manual")
        self.assertEqual([r["carrier"] for r in rows], ["att", "generic", "tmo", "verizon"])
        self.assertEqual([r["idx"] for r in rows], [1, 2, 3, 4])
        active = [r for r in rows if r["active"]]
        self.assertEqual(len(active), 1)
        self.assertEqual((active[0]["carrier"], active[0]["fw"], active[0]["pri"]), ("verizon", "02.17.08.00", "030.092_001"))

    def test_no_table(self):
        self.assertEqual(cf._parse_firmware_table(NOT_READY), ([], None))
        self.assertEqual(cf._parse_firmware_table(""), ([], None))


class ReadModemStateTest(SimpleTestCase):
    def test_active_row_of_the_firmware_table_wins(self):
        child = FakeChild(firmware=[FW_VERIZON], hardware=[HW_EM9293])
        self.assertEqual(cf._read_modem_state(child, "0/1/0"), ("02.17.08.00", "verizon"))
        self.assertEqual(child.calls, ["show cellular 0/1/0 firmware"])

    def test_falls_back_to_hardware_when_no_table(self):
        child = FakeChild(firmware=[""], hardware=[HW_EM9293])
        self.assertEqual(cf._read_modem_state(child, "0/1/0"), ("01.07.03.00", "verizon"))
        self.assertEqual(child.calls, ["show cellular 0/1/0 firmware", "show cellular 0/1/0 hardware"])

    def test_not_ready(self):
        child = FakeChild(firmware=[NOT_READY], hardware=[NOT_READY])
        self.assertEqual(cf._read_modem_state(child, "0/1/0"), (None, None))


class ParseHardwareStateTest(SimpleTestCase):
    def test_5g_module_reports_version_and_carrier(self):
        self.assertEqual(cf._parse_modem_state(HW_EM9293), ("01.07.03.00", "verizon"))

    def test_4g_module_version_comes_from_the_firmware_line_not_the_package_id(self):
        self.assertEqual(cf._parse_modem_state(HW_EM7455), ("02.24.05.06", "generic"))

    def test_platform_without_carrier_field(self):
        self.assertEqual(cf._parse_modem_state(HW_NO_CARRIER), ("01.07.03.00", None))

    def test_not_ready_outputs(self):
        for output in (NOT_READY, NOT_PRESENT, "% Invalid input detected", ""):
            self.assertEqual(cf._parse_modem_state(output), (None, None), output)


class MatchesTargetTest(SimpleTestCase):
    def test_version_and_carrier_match(self):
        ok, _ = cf._modem_matches_target(TARGET, "verizon", "verizon", TARGET)
        self.assertTrue(ok)

    def test_version_mismatch(self):
        ok, reason = cf._modem_matches_target("02.15.01.00", "verizon", "verizon", TARGET)
        self.assertFalse(ok)
        self.assertIn("02.15.01.00", reason)

    def test_right_version_wrong_carrier_image_needs_upgrade(self):
        ok, reason = cf._modem_matches_target(TARGET, "generic", "verizon", TARGET)
        self.assertFalse(ok)
        self.assertIn("carrier PRI 'generic'", reason)

    def test_firstnet_accepts_att_image(self):
        ok, _ = cf._modem_matches_target(TARGET, "att", "firstnet", TARGET)
        self.assertTrue(ok)

    def test_no_carrier_reported_gates_on_version_only(self):
        ok, _ = cf._modem_matches_target(TARGET, None, "verizon", TARGET)
        self.assertTrue(ok)


class CarrierKeyFromTagsTest(SimpleTestCase):
    def _device(self, *tags):
        device = mock.Mock()
        device.tags.values_list.return_value = list(tags)
        return device

    def test_tags(self):
        self.assertEqual(cf._carrier_key_from_tags(self._device("Verizon Wireless", "Router")), "verizon")
        self.assertEqual(cf._carrier_key_from_tags(self._device("FirstNet")), "firstnet")
        self.assertIsNone(cf._carrier_key_from_tags(self._device("Router")))


MODEM_STILL_DOWN = "Modem is still down, please wait for modem to come up\n"


class ReadModemStateTest(SimpleTestCase):
    def test_still_down_from_firmware_command_is_not_ready_even_if_hardware_says_online(self):
        # Seen live on an EM9293 right after the APN write.
        child = FakeChild(firmware=[MODEM_STILL_DOWN], hardware=[HW_EM9293])
        self.assertEqual(cf._read_modem_state(child, "0/1/0"), (None, None))
        self.assertFalse(any("hardware" in c for c in child.calls))

    def test_no_firmware_table_command_still_falls_back_to_hardware(self):
        child = FakeChild(firmware=["% Invalid input detected at '^' marker.\n"], hardware=[HW_EM9293])
        self.assertEqual(cf._read_modem_state(child, "0/1/0"), ("01.07.03.00", "verizon"))


class WaitForModemReadyTest(SimpleTestCase):
    def test_waits_through_not_ready_then_returns(self):
        clock = _clock(self)
        child = FakeChild(firmware=[NOT_READY, "", FW_VERIZON], hardware=[NOT_READY])
        logger = FakeLogger()

        result = cf._wait_for_modem_ready(child, "0/1/0", logger, timeout=600, interval=15)

        self.assertEqual(result, ("02.17.08.00", "verizon"))
        self.assertEqual(clock.sleeps, [15, 15])
        self.assertIn("not ready yet", logger.text("info"))

    def test_never_ready_raises_instead_of_skipping(self):
        clock = _clock(self)
        child = FakeChild(firmware=[NOT_READY], hardware=[NOT_READY])
        with self.assertRaisesRegex(RuntimeError, "did not report a firmware version within 600s"):
            cf._wait_for_modem_ready(child, "0/1/0", FakeLogger(), timeout=600, interval=60)
        self.assertEqual(len(clock.sleeps), 10)


class PollModemOnlineTest(SimpleTestCase):
    def test_succeeds_once_version_and_carrier_match(self):
        _clock(self)
        child = FakeChild(firmware=[NOT_PRESENT, FW_VERIZON_OLD, FW_VERIZON])
        logger = FakeLogger()

        cf._poll_modem_online(
            child, "0/1/0", logger, target_version=TARGET, carrier_key="verizon",
            timeout=900, interval=15,
        )
        self.assertIn("back on target firmware 02.17.08.00 (carrier PRI verizon)", logger.text("info"))

    def test_right_version_wrong_carrier_fails(self):
        _clock(self)
        child = FakeChild(firmware=[FW_GENERIC_SAME_VERSION])
        with self.assertRaisesRegex(RuntimeError, "carrier PRI 'generic' is not verizon"):
            cf._poll_modem_online(
                child, "0/1/0", FakeLogger(), target_version=TARGET,
                carrier_key="verizon", timeout=60, interval=15,
            )

    def test_never_returns_fails(self):
        _clock(self)
        child = FakeChild(firmware=[NOT_PRESENT])
        with self.assertRaisesRegex(RuntimeError, "didn't come back after 60s"):
            cf._poll_modem_online(
                child, "0/1/0", FakeLogger(), target_version=TARGET,
                carrier_key="verizon", timeout=60, interval=15,
            )


def _carrier():
    return {
        "key": "verizon",
        "cwe_file": f"modem_{TARGET}.cwe",
        "nvu_file": "modem_verizon.nvu",
        "version": TARGET,
        "router_dir": "em9293_ver",
    }


class RunTest(SimpleTestCase):
    """run() end to end with the console-side helpers stubbed."""

    def _run(self, child):
        logger = FakeLogger()
        patches = {
            "_find_cellular_modules": mock.Mock(return_value=[("PIM 1", "P-5GS6-GL", "0/1/0")]),
            "_resolve_carrier": mock.Mock(return_value=_carrier()),
            "get_ei_scheme": mock.Mock(return_value="scp"),
            "get_ei_creds": mock.Mock(return_value=("u", "p")),
            "get_ei_host_and_base_path": mock.Mock(return_value=("fs", "/fw")),
            "_ensure_fw_directory": mock.Mock(),
            "_download_if_missing": mock.Mock(),
            "_run_microcode_reload": mock.Mock(),
            "_poll_modem_online": mock.Mock(),
        }
        with mock.patch.multiple(cf, **patches):
            _clock(self)
            cf.run(child, device=object(), session=None, settings={"file_server_ei": object()}, logger=logger)
        return patches, logger

    def test_on_target_skips_flash(self):
        patches, logger = self._run(FakeChild(firmware=[FW_VERIZON]))
        patches["_run_microcode_reload"].assert_not_called()
        self.assertIn("skipping upgrade", logger.text("info"))

    def test_wrong_carrier_image_flashes_even_on_matching_version(self):
        child = FakeChild(firmware=[FW_GENERIC_SAME_VERSION])
        patches, logger = self._run(child)
        patches["_run_microcode_reload"].assert_called_once()
        patches["_poll_modem_online"].assert_called_once_with(
            child, "0/1/0", logger, target_version=TARGET, carrier_key="verizon"
        )
        self.assertIn("Upgrade needed: carrier PRI 'generic'", logger.text("info"))

    def test_modem_initialising_is_waited_for_then_flashed(self):
        patches, _ = self._run(FakeChild(firmware=[NOT_READY, NOT_READY, FW_VERIZON_OLD]))
        patches["_run_microcode_reload"].assert_called_once()

    def test_modem_never_answering_fails_the_step(self):
        with self.assertRaisesRegex(RuntimeError, "did not report a firmware version"):
            self._run(FakeChild(firmware=[NOT_READY]))


class ValidateCellFirmwareTest(SimpleTestCase):
    def _check(self, child, carrier):
        logger = FakeLogger()
        with mock.patch.object(cf, "_find_cellular_modules", return_value=[("PIM 1", "P-5GS6-GL", "0/1/0")]), \
                mock.patch.object(cf, "_resolve_carrier", return_value=carrier):
            _clock(self)
            failures = val_step._check_cell_firmware(child, object(), {}, logger)
        return failures, logger

    def test_on_target_passes(self):
        failures, logger = self._check(FakeChild(firmware=[FW_VERIZON]), _carrier())
        self.assertEqual(failures, [])
        self.assertIn("Cellular firmware verified", logger.text("info"))

    def test_wrong_firmware_fails_validation(self):
        failures, _ = self._check(FakeChild(firmware=[FW_VERIZON_OLD]), _carrier())
        self.assertEqual(len(failures), 1)
        self.assertIn("02.15.01.00", failures[0])

    def test_wrong_carrier_image_fails_validation(self):
        failures, _ = self._check(FakeChild(firmware=[FW_GENERIC_SAME_VERSION]), _carrier())
        self.assertEqual(len(failures), 1)
        self.assertIn("carrier PRI 'generic'", failures[0])

    def test_unresponsive_modem_fails_validation(self):
        failures, _ = self._check(FakeChild(firmware=[NOT_READY]), _carrier())
        self.assertEqual(len(failures), 1)
        self.assertIn("did not report a firmware version within 300s", failures[0])

    def test_no_carrier_firmware_configured_is_advisory(self):
        failures, logger = self._check(FakeChild(firmware=[NOT_READY]), None)
        self.assertEqual(failures, [])
        self.assertIn("advisory", logger.text("warning"))
