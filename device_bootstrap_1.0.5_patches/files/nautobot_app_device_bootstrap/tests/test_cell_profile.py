"""APN profile step: the data-profile attach must not go green on a rejection.

The controller rejects the attach with a bare "Error: ... still in reset"
(no '%' prefix) while the modem is still rebooting after the APN write. Pins
that this is retried on the device's own advice, that any other error fails
the step, and that run() confirms the modem answers before the attach.
No DB, no console: scripted fakes stand in for the netmiko session.
"""
from unittest import mock

from django.test import SimpleTestCase

from nautobot_app_device_bootstrap.jobs.steps import cell_profile as cp

from .test_cell_firmware import FakeLogger, _clock

SUBSLOT = "0/1/0"
ATTACH = [
    "controller cellular 0/1/0",
    " lte sim data-profile 1 attach-profile 1 slot 0",
]

IN_RESET = (
    "configure terminal\n"
    "Enter configuration commands, one per line.  End with CNTL/Z.\n"
    "TXBSPOTWR01(config)#controller cellular 0/1/0\n"
    "TXBSPOTWR01(config-controller)# lte sim data-profile 1 attach-profile 1 slot 0\n"
    "Error: Cellular Modem in slot 0/1 is still in reset, we recommend to re-execute "
    "this cmd after 60 seconds\n"
    "TXBSPOTWR01(config-controller)#end\n"
)
ACCEPTED = (
    "configure terminal\n"
    "TXBSPOTWR01(config)#controller cellular 0/1/0\n"
    "TXBSPOTWR01(config-controller)# lte sim data-profile 1 attach-profile 1 slot 0\n"
    "TXBSPOTWR01(config-controller)#end\n"
)
INVALID = (
    "TXBSPOTWR01(config-controller)# lte sim data-profile 1 attach-profile 1 slot 0\n"
    "                                    ^\n"
    "% Invalid input detected at '^' marker.\n"
)
BARE_ERROR = (
    "TXBSPOTWR01(config-controller)# lte sim data-profile 1 attach-profile 1 slot 0\n"
    "Error: SIM slot 0 is not present\n"
)


FW_MANUAL = (
    "Idx  Carrier         FwVersion        PriVersion     Status\n"
    "1    ATT             02.17.08.00      030.094_001    Active\n"
    "2    GENERIC         02.15.01.00      030.054_001    Inactive\n"
    "\n"
    "Firmware Activation mode  =  MANUAL\n"
)
FW_AUTO = FW_MANUAL.replace("MANUAL", "AUTO")
AUTO_SIM_OFF = ["controller cellular 0/1/0", " no lte firmware auto-sim"]


STILL_DOWN = "Modem is still down, please wait for modem to come up\n"


class ConfigChild:
    """send_config_set() answers from a script; the last output repeats.
    send_command() answers `show cellular X firmware` from `firmware`
    (a list consumed in order, last repeats)."""

    def __init__(self, outputs, firmware=(FW_MANUAL,)):
        self.outputs = list(outputs)
        self.config_sets = []
        self.commands = []
        self.firmware = list(firmware) if not isinstance(firmware, str) else [firmware]

    def drain(self, **kwargs):
        pass

    def send_config_set(self, commands, **kwargs):
        self.config_sets.append(list(commands))
        return self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]

    def send_command(self, cmd, **kwargs):
        self.commands.append(cmd)
        if "firmware" in cmd:
            return self.firmware.pop(0) if len(self.firmware) > 1 else self.firmware[0]
        return ""


class ErrorDetectionTest(SimpleTestCase):
    def test_bare_error_without_percent_is_an_error(self):
        self.assertEqual(
            cp._first_error_line(IN_RESET),
            "Error: Cellular Modem in slot 0/1 is still in reset, we recommend to re-execute "
            "this cmd after 60 seconds",
        )

    def test_percent_invalid_is_an_error(self):
        self.assertEqual(cp._first_error_line(INVALID), "% Invalid input detected at '^' marker.")

    def test_clean_output_is_not_an_error(self):
        self.assertIsNone(cp._first_error_line(ACCEPTED))
        self.assertIsNone(cp._first_error_line(""))

    def test_in_reset_is_recognised(self):
        self.assertTrue(cp._MODEM_IN_RESET.search(IN_RESET))
        self.assertFalse(cp._MODEM_IN_RESET.search(ACCEPTED))


class AttachDataProfileTest(SimpleTestCase):
    def test_accepted_first_time_sends_controller_commands_once(self):
        clock = _clock(self, module=cp)
        child = ConfigChild([ACCEPTED])
        cp._attach_data_profile(child, SUBSLOT, 0, 1, FakeLogger())
        self.assertEqual(child.config_sets, [ATTACH])
        self.assertEqual(clock.sleeps, [])

    def test_still_in_reset_is_retried_after_the_advised_wait(self):
        clock = _clock(self, module=cp)
        child = ConfigChild([IN_RESET, IN_RESET, ACCEPTED])
        logger = FakeLogger()

        cp._attach_data_profile(child, SUBSLOT, 0, 1, logger, retries=10, retry_wait=60)

        self.assertEqual(child.config_sets, [ATTACH, ATTACH, ATTACH])
        self.assertEqual(clock.sleeps, [60, 60])
        self.assertIn("still in reset", logger.text("warning"))
        self.assertIn("attach (slot 0) applied on attempt 3", logger.text("info"))

    def test_never_leaving_reset_fails_the_step(self):
        clock = _clock(self, module=cp)
        child = ConfigChild([IN_RESET])
        with self.assertRaisesRegex(RuntimeError, r"still in reset after 3 attempts over 180s — data-profile 1 attach"):
            cp._attach_data_profile(child, SUBSLOT, 0, 1, FakeLogger(), retries=3, retry_wait=60)
        self.assertEqual(len(child.config_sets), 3)
        self.assertEqual(clock.sleeps, [60, 60])

    def test_percent_invalid_raises_without_retry(self):
        clock = _clock(self, module=cp)
        child = ConfigChild([INVALID])
        with self.assertRaisesRegex(RuntimeError, r"attach \(slot 0\) rejected .*% Invalid input"):
            cp._attach_data_profile(child, SUBSLOT, 0, 1, FakeLogger())
        self.assertEqual(len(child.config_sets), 1)
        self.assertEqual(clock.sleeps, [])

    def test_other_bare_error_raises_without_retry(self):
        _clock(self, module=cp)
        child = ConfigChild([BARE_ERROR])
        with self.assertRaisesRegex(RuntimeError, r"Error: SIM slot 0 is not present"):
            cp._attach_data_profile(child, SUBSLOT, 0, 1, FakeLogger())
        self.assertEqual(len(child.config_sets), 1)


class DisableAutoSimTest(SimpleTestCase):
    def setUp(self):
        # One clock: cell_profile and cell_firmware share the same `time`
        # module, so patching it once covers _wait_for_modem_ready too.
        self.clock = _clock(self, module=cp)

    def test_already_manual_sends_nothing(self):
        # The normal case: the reset step already turned auto-sim off. The
        # change would reset the modem, so it must NOT be re-sent.
        child = ConfigChild([ACCEPTED], firmware=[FW_MANUAL])
        logger = FakeLogger()
        cp._disable_auto_sim(child, SUBSLOT, logger)
        self.assertEqual(child.config_sets, [])
        self.assertIn("auto-sim already off", logger.text("info"))

    def test_auto_sends_command_and_waits_out_the_modem_reset(self):
        # pre-check AUTO → command → modem drops → comes back → post-check MANUAL
        child = ConfigChild([ACCEPTED], firmware=[FW_AUTO, STILL_DOWN, FW_MANUAL])
        logger = FakeLogger()
        cp._disable_auto_sim(child, SUBSLOT, logger)
        self.assertEqual(child.config_sets, [AUTO_SIM_OFF])
        self.assertIn("modem went down for the auto-sim change", logger.text("info"))
        self.assertIn("Firmware Activation mode = MANUAL", logger.text("info"))
        self.assertEqual(logger.text("warning"), "")

    def test_auto_with_no_reset_observed_continues_after_the_window(self):
        child = ConfigChild([ACCEPTED], firmware=[FW_AUTO, FW_MANUAL])
        logger = FakeLogger()
        cp._disable_auto_sim(child, SUBSLOT, logger)
        self.assertEqual(child.config_sets, [AUTO_SIM_OFF])
        self.assertIn("no modem reset observed within 90s", logger.text("info"))
        self.assertEqual(self.clock.sleeps, [10] * 9)

    def test_still_auto_afterwards_is_warned_not_fatal(self):
        child = ConfigChild([ACCEPTED], firmware=[FW_AUTO, STILL_DOWN, FW_AUTO])
        logger = FakeLogger()
        cp._disable_auto_sim(child, SUBSLOT, logger)
        self.assertIn("still reports Firmware Activation mode = AUTO", logger.text("warning"))

    def test_no_mode_line_sends_command_and_is_quiet(self):
        # A module without the firmware table command cannot be confirmed either way.
        child = ConfigChild([ACCEPTED], firmware=["% Invalid input detected at '^' marker.\n"])
        logger = FakeLogger()
        cp._disable_auto_sim(child, SUBSLOT, logger)
        self.assertEqual(child.config_sets, [AUTO_SIM_OFF])
        self.assertEqual(logger.text("warning"), "")

    def test_in_reset_is_retried_then_applied(self):
        child = ConfigChild([IN_RESET, ACCEPTED], firmware=[FW_AUTO, STILL_DOWN, FW_MANUAL])
        logger = FakeLogger()
        cp._disable_auto_sim(child, SUBSLOT, logger)
        self.assertEqual(child.config_sets, [AUTO_SIM_OFF, AUTO_SIM_OFF])
        self.assertIn(60, self.clock.sleeps)
        self.assertIn("no lte firmware auto-sim applied on attempt 2", logger.text("info"))

    def test_rejection_raises(self):
        child = ConfigChild([INVALID], firmware=[FW_AUTO])
        with self.assertRaisesRegex(RuntimeError, r"no lte firmware auto-sim rejected .*% Invalid input"):
            cp._disable_auto_sim(child, SUBSLOT, FakeLogger())


class RunOrderTest(SimpleTestCase):
    """run() must turn auto-sim off BEFORE the profile write, and confirm the
    modem answers BEFORE the controller attach."""

    def test_modem_ready_gate_precedes_attach(self):
        calls = []
        device = mock.Mock()
        device.tags.values_list.return_value = ["FirstNet"]
        settings = {"cell_apn_firstnet": "34287.fn", "cell_apn_reboot_wait": 360}
        child = mock.Mock()

        def autosim(c, subslot, logger):
            calls.append("auto_sim_off")
            c.sendline("<auto-sim marker>")  # proves it ran before the profile write

        with mock.patch.object(cp, "_find_cellular_modules", return_value=[("0/1/0", "P-5GS6-GL", SUBSLOT)]), \
             mock.patch.object(cp, "_disable_auto_sim", side_effect=autosim), \
             mock.patch.object(cp, "_handle_confirm_prompts", side_effect=lambda *a, **k: calls.append("confirm")), \
             mock.patch.object(cp, "_wait_for_modem_reboot", side_effect=lambda *a, **k: calls.append("syslog_wait")), \
             mock.patch.object(cp, "_wait_for_modem_ready", side_effect=lambda *a, **k: calls.append("modem_ready")), \
             mock.patch.object(cp, "_attach_data_profile", side_effect=lambda *a, **k: calls.append("attach")):
            result = cp.run(child, device, None, settings, FakeLogger())

        self.assertEqual(
            [c.args[0] for c in child.sendline.call_args_list],
            ["<auto-sim marker>", "cellular 0/1/0 lte profile create 1 34287.fn none ipv4"],
        )
        self.assertEqual(calls, ["auto_sim_off", "confirm", "syslog_wait", "modem_ready", "attach"])
        self.assertEqual(result, "APN 34287.fn on profile 1, attached to SIM slot 0 (0/1/0)")

    def test_modem_never_ready_fails_before_attach(self):
        device = mock.Mock()
        device.tags.values_list.return_value = ["FirstNet"]
        settings = {"cell_apn_firstnet": "34287.fn"}

        with mock.patch.object(cp, "_find_cellular_modules", return_value=[("0/1/0", "P-5GS6-GL", SUBSLOT)]), \
             mock.patch.object(cp, "_disable_auto_sim"), \
             mock.patch.object(cp, "_handle_confirm_prompts"), \
             mock.patch.object(cp, "_wait_for_modem_reboot"), \
             mock.patch.object(cp, "_wait_for_modem_ready", side_effect=RuntimeError("did not report a firmware version")), \
             mock.patch.object(cp, "_attach_data_profile") as attach:
            with self.assertRaisesRegex(RuntimeError, "did not report a firmware version"):
                cp.run(mock.Mock(), device, None, settings, FakeLogger())
        attach.assert_not_called()
