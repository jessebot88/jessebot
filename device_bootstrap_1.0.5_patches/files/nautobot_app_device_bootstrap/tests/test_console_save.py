"""Console ↔ driver wiring: save_config self-heal and promote().

The DBPERSIST self-heal flow was verified live on the fleet before it moved
into CiscoIosDriver — these tests pin the delegated behavior so the driver
seam can't silently change it. No DB, no real netmiko session: FakeConn
scripts the device side.
"""
from django.test import SimpleTestCase

from nautobot_app_device_bootstrap.jobs import console as console_mod
from nautobot_app_device_bootstrap.jobs import drivers


class FakeLogger:
    def __init__(self):
        self.lines = []

    def info(self, m, *a, **k):
        self.lines.append(("info", str(m)))

    def warning(self, m, *a, **k):
        self.lines.append(("warning", str(m)))

    def debug(self, m, *a, **k):
        self.lines.append(("debug", str(m)))

    def error(self, m, *a, **k):
        self.lines.append(("error", str(m)))


class FakeConn:
    """Scriptable netmiko stand-in. `script` maps a command substring to a
    list of canned outputs (consumed in order; the last one repeats).
    `save_outputs` feeds save_config(); an Exception entry raises."""

    def __init__(self, save_outputs, script=None):
        self.save_outputs = list(save_outputs)
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls = []

    def read_channel(self):
        return ""

    def write_channel(self, data):
        pass

    def send_command(self, cmd, **kwargs):
        self.calls.append(cmd)
        for key, outs in self.script.items():
            if key in cmd:
                return outs.pop(0) if len(outs) > 1 else outs[0]
        return ""

    def send_command_timing(self, cmd, **kwargs):
        self.calls.append(cmd)
        return ""

    def save_config(self):
        self.calls.append("SAVE")
        out = self.save_outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    def check_config_mode(self):
        return False

    def exit_config_mode(self):
        return ""


def make_console(fake_conn):
    """Console without ConnectHandler — wire the fields __init__ would set."""
    c = console_mod.Console.__new__(console_mod.Console)
    c.host, c.port = "192.0.2.10", 3017
    c._logger = FakeLogger()
    c.driver = drivers.default_driver()
    c.is_ios = True
    c._pending = ""
    c._tail = ""
    c.conn = fake_conn
    return c


CFG_DB_FAIL = (
    "Oct 15 16:52:23.627: %TDLDB-3-DBPERSIST_OPERATION_FAILURE: R0/0: dbm: Operation: persist, "
    "DB: /bootflash/.dbpersist/CTRL_MNG_CFG_DB, Failure reason: Is a directory"
)
# Seen live on an IR1101 on EVERY `write mem` — the NETCONF/YANG state store,
# nothing to do with startup-config. Must never fail a save.
DMI_FAIL = (
    "Oct 15 16:52:23.627: %TDLDB-3-DBPERSIST_OPERATION_FAILURE: R0/0: dbm: Operation: persist, "
    "DB: /tmp/rp/tdldb/0/DMI_STATE_DB, Failure reason: Is a directory"
)
IMAGE = "ir1101-universalk9.17.15.04.SPA.bin"
BOOT_LINE = f"boot system flash:{IMAGE}"


def _log(*lines):
    """One `show logging | include DBPERSIST` output."""
    return "\n".join(lines)


def _no_sleep(testcase):
    from unittest import mock
    from nautobot_app_device_bootstrap.jobs.drivers import cisco_ios

    patcher = mock.patch.object(cisco_ios.time, "sleep", lambda s: None)
    patcher.start()
    testcase.addCleanup(patcher.stop)


class SaveConfigDelegationTest(SimpleTestCase):
    def setUp(self):
        _no_sleep(self)

    def test_clean_save_single_attempt(self):
        fc = FakeConn(["[OK]"], {"include DBPERSIST": [""]})
        c = make_console(fc)
        self.assertEqual(c.save_config(), "[OK]")
        self.assertEqual(fc.calls.count("SAVE"), 1)
        self.assertFalse(any("delete /force" in x for x in fc.calls))

    def test_unrelated_db_persist_failure_is_ignored(self):
        # The TXLSOOTWR01 case: write mem [OK], a NEW DBPERSIST line lands in
        # the log, but it names DMI_STATE_DB — the save is fine.
        fc = FakeConn(["[OK]"], {"include DBPERSIST": ["", _log(DMI_FAIL)]})
        c = make_console(fc)
        self.assertEqual(c.save_config(), "[OK]")
        self.assertEqual(fc.calls.count("SAVE"), 1)
        self.assertFalse(any("delete /force" in x for x in fc.calls))
        self.assertFalse(any("did not persist" in m for _, m in c._logger.lines))

    def test_save_exception_with_logged_bug_heals_and_resaves(self):
        fc = FakeConn(
            [RuntimeError("netmiko pattern fail"), "[OK]"],
            {"include DBPERSIST": [_log(CFG_DB_FAIL, CFG_DB_FAIL)]},
        )
        c = make_console(fc)
        self.assertEqual(c.save_config(), "[OK]")
        self.assertTrue(any("delete /force /recursive" in x for x in fc.calls))
        self.assertTrue(any("DBPERSIST bug" in m for _, m in c._logger.lines))

    def test_save_exception_with_only_unrelated_db_lines_propagates(self):
        fc = FakeConn([RuntimeError("real failure")], {"include DBPERSIST": [_log(DMI_FAIL)]})
        c = make_console(fc)
        with self.assertRaisesMessage(RuntimeError, "real failure"):
            c.save_config()
        self.assertFalse(any("delete /force" in x for x in fc.calls))

    def test_ok_lie_detected_by_config_db_log_count_increase(self):
        # write mem prints [OK] inline while the CONFIG-DB persist fails async
        # as syslog: pre=1, post=2 → heal; after clear logging: 0, 0 → clean.
        fc = FakeConn(
            ["[OK]", "[OK]"],
            {"include DBPERSIST": [_log(CFG_DB_FAIL), _log(CFG_DB_FAIL, CFG_DB_FAIL), "", ""]},
        )
        c = make_console(fc)
        self.assertEqual(c.save_config(), "[OK]")
        self.assertTrue(any("delete /force /recursive" in x for x in fc.calls))
        self.assertTrue(any("did not persist" in m for _, m in c._logger.lines))

    def test_unrelated_save_failure_propagates(self):
        fc = FakeConn([RuntimeError("real failure")], {"include DBPERSIST": [""]})
        c = make_console(fc)
        with self.assertRaisesMessage(RuntimeError, "real failure"):
            c.save_config()

    def test_heal_that_does_not_stick_raises(self):
        fc = FakeConn(
            [RuntimeError("fail"), "[OK]"],
            # pre=1, logged → heal; post-clear pre=0, post=1 → still failing
            {"include DBPERSIST": [_log(CFG_DB_FAIL), _log(CFG_DB_FAIL), "", _log(CFG_DB_FAIL)]},
        )
        c = make_console(fc)
        with self.assertRaisesMessage(RuntimeError, "still failing"):
            c.save_config()

    def test_heal_retry_ignores_unrelated_db_lines(self):
        fc = FakeConn(
            [RuntimeError("fail"), "[OK]"],
            # pre=1, logged → heal; post-clear pre=0, post=0 (the DMI line is not counted)
            {"include DBPERSIST": [_log(CFG_DB_FAIL), _log(CFG_DB_FAIL), "", _log(DMI_FAIL)]},
        )
        c = make_console(fc)
        self.assertEqual(c.save_config(), "[OK]")
        self.assertEqual(fc.calls.count("SAVE"), 2)


class SaveConfigVerifyTest(SimpleTestCase):
    """verify_contains: the save is judged by reading startup-config back."""

    def setUp(self):
        _no_sleep(self)

    def test_verified_line_wins_over_dbpersist_noise(self):
        fc = FakeConn(
            ["[OK]"],
            {"include DBPERSIST": [_log(DMI_FAIL)], "show startup-config": [BOOT_LINE]},
        )
        c = make_console(fc)
        self.assertEqual(c.save_config(verify_contains=IMAGE), "[OK]")
        self.assertEqual(fc.calls.count("SAVE"), 1)
        self.assertIn(f"show startup-config | include {IMAGE}", fc.calls)
        self.assertFalse(any("delete /force" in x for x in fc.calls))

    def test_verified_line_wins_even_over_a_config_db_line(self):
        # Positive proof beats the heuristic: NVRAM has the line, done.
        fc = FakeConn(
            ["[OK]"],
            {"include DBPERSIST": ["", _log(CFG_DB_FAIL)], "show startup-config": [BOOT_LINE]},
        )
        c = make_console(fc)
        self.assertEqual(c.save_config(verify_contains=IMAGE), "[OK]")
        self.assertEqual(fc.calls.count("SAVE"), 1)

    def test_missing_line_heals_and_passes_once_present(self):
        fc = FakeConn(
            ["[OK]", "[OK]"],
            {
                "include DBPERSIST": [""],
                "show startup-config": ["startup-config is not present", BOOT_LINE],
            },
        )
        c = make_console(fc)
        self.assertEqual(c.save_config(verify_contains=IMAGE), "[OK]")
        self.assertEqual(fc.calls.count("SAVE"), 2)
        self.assertTrue(any("delete /force /recursive" in x for x in fc.calls))
        self.assertTrue(any("not in startup-config" in m for _, m in c._logger.lines))
        self.assertTrue(any("verified in startup-config" in m for _, m in c._logger.lines))

    def test_missing_line_after_heal_raises(self):
        fc = FakeConn(
            ["[OK]", "[OK]"],
            {"include DBPERSIST": [""], "show startup-config": ["startup-config is not present"]},
        )
        c = make_console(fc)
        with self.assertRaisesRegex(RuntimeError, rf"still failing .* '{IMAGE}' is not in startup-config"):
            c.save_config(verify_contains=IMAGE)
        self.assertEqual(fc.calls.count("SAVE"), 2)

    def test_substring_must_be_on_a_line(self):
        fc = FakeConn(["[OK]", "[OK]"], {"include DBPERSIST": [""], "show startup-config": ["boot system flash:other.bin"]})
        c = make_console(fc)
        with self.assertRaisesRegex(RuntimeError, "not in startup-config"):
            c.save_config(verify_contains=IMAGE)


class PromoteTest(SimpleTestCase):
    def test_promote_redispatches_to_driver_device_type(self):
        seen = {}
        original = console_mod.redispatch
        console_mod.redispatch = lambda conn, device_type: seen.setdefault("dt", device_type)
        try:
            c = make_console(FakeConn(["x"]))
            c.is_ios = False
            c.promote()
            self.assertEqual(seen["dt"], "cisco_ios")
            self.assertTrue(c.is_ios)
            seen.clear()
            c.promote_to_ios()  # historical alias
            self.assertEqual(seen["dt"], "cisco_ios")
        finally:
            console_mod.redispatch = original
