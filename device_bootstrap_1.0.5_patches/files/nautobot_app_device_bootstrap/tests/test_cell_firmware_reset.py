"""Cellular firmware reset step (the Verizon modem workaround).

Pins the vendor procedure's order and gates: controller cleanup, GENERIC
activation (skipped when already active), save + reload, GENERIC confirmed
after the reload, carrier re-activation, and the version check on the way
out. Console-level helpers are exercised against a scripted fake; run() is
exercised with those helpers stubbed so the ORDER is what's asserted.
"""
from unittest import mock

from django.test import SimpleTestCase

from nautobot_app_device_bootstrap.jobs.steps import cell_firmware_reset as cfr
from nautobot_app_device_bootstrap.jobs.steps import reload as reload_step

from .test_cell_firmware import (
    FW_GENERIC_SAME_VERSION,
    FW_VERIZON,
    FW_VERIZON_OLD,
    HW_NO_CARRIER,
    NOT_READY,
    TARGET,
    FakeChild,
    FakeLogger,
    _clock,
)

FW_GENERIC = FW_GENERIC_SAME_VERSION
SUBSLOT = "0/1/0"


class ExpectChild(FakeChild):
    """Adds the raw-console surface _activate_firmware drives."""

    def __init__(self, expect_results=(), **kwargs):
        super().__init__(**kwargs)
        self.expect_results = list(expect_results)
        self.sent = []
        self.returns = 0

    def drain(self, **kwargs):
        pass

    def sendline(self, line):
        self.sent.append(line)

    def send_return(self):
        self.returns += 1

    def expect(self, patterns, timeout=None, where=None):
        return self.expect_results.pop(0), ""

    def tail(self):
        return "<console tail>"


def _device(*tags):
    device = mock.Mock()
    device.tags.values_list.return_value = list(tags)
    return device


class IndexLookupTest(SimpleTestCase):
    def test_index_of(self):
        rows, _ = cfr._parse_firmware_table(FW_VERIZON)
        self.assertEqual(cfr._index_of(rows, "generic", SUBSLOT), 2)
        self.assertEqual(cfr._index_of(rows, "verizon", SUBSLOT), 4)

    def test_missing_carrier_lists_what_is_there(self):
        rows, _ = cfr._parse_firmware_table(FW_VERIZON)
        with self.assertRaisesRegex(RuntimeError, r"No SPRINT image .* 1=ATT, 2=GENERIC, 3=TMO, 4=VERIZON"):
            cfr._index_of(rows, "sprint", SUBSLOT)


class ActivateFirmwareTest(SimpleTestCase):
    def test_plain_prompt(self):
        child = ExpectChild(expect_results=[2])
        cfr._activate_firmware(child, SUBSLOT, 2, FakeLogger())
        self.assertEqual(child.sent, ["cellular 0/1/0 lte firmware-activate 2"])
        self.assertEqual(child.returns, 0)

    def test_confirm_prompt_is_answered(self):
        child = ExpectChild(expect_results=[0, 0])
        cfr._activate_firmware(child, SUBSLOT, 4, FakeLogger())
        self.assertEqual(child.returns, 1)

    def test_rejected(self):
        child = ExpectChild(expect_results=[1, 0])
        with self.assertRaisesRegex(RuntimeError, "firmware-activate 4 rejected"):
            cfr._activate_firmware(child, SUBSLOT, 4, FakeLogger())

    def test_no_response(self):
        child = ExpectChild(expect_results=[None])
        with self.assertRaisesRegex(RuntimeError, "No response to firmware-activate"):
            cfr._activate_firmware(child, SUBSLOT, 4, FakeLogger())


class WaitForActiveCarrierTest(SimpleTestCase):
    def test_waits_for_table_switch_and_modem_online(self):
        clock = _clock(self, module=cfr)
        # 1st poll: table still shows VERIZON; 2nd: GENERIC but modem not back;
        # 3rd: GENERIC and the hardware query answers.
        child = FakeChild(firmware=[FW_VERIZON, FW_GENERIC, FW_GENERIC], hardware=[NOT_READY, HW_NO_CARRIER])
        result = cfr._wait_for_active_carrier(child, SUBSLOT, "generic", FakeLogger(), timeout=600, interval=15)
        self.assertEqual(result, ("01.07.03.00", "generic"))
        self.assertEqual(len(clock.sleeps), 3)

    def test_timeout_reports_last_active_image(self):
        _clock(self, module=cfr)
        child = FakeChild(firmware=[FW_VERIZON])
        with self.assertRaisesRegex(RuntimeError, r"GENERIC image within 60s \(last active image: VERIZON\)"):
            cfr._wait_for_active_carrier(child, SUBSLOT, "generic", FakeLogger(), timeout=60, interval=15)


class AppliesToTest(SimpleTestCase):
    def test_verizon_with_modem(self):
        with mock.patch.object(cfr, "_find_cellular_modules", return_value=[("PIM 1", "P-5GS6-GL", SUBSLOT)]):
            self.assertTrue(cfr.applies_to(_device("Verizon Wireless"), {}))
            self.assertTrue(cfr.applies_to(_device("FirstNet"), {}))
            self.assertFalse(cfr.applies_to(_device("Router"), {}))  # modem, no carrier tag

    def test_no_modem(self):
        with mock.patch.object(cfr, "_find_cellular_modules", return_value=[]):
            self.assertFalse(cfr.applies_to(_device("Verizon Wireless"), {}))


class RunTest(SimpleTestCase):
    """Order of operations, with the console helpers stubbed and the firmware
    table scripted: `before` is what the first session sees, `after` what the
    post-reload session sees."""

    def _run(self, before, after, device=None, carrier_version=TARGET, reactivated=(TARGET, "verizon")):
        child1 = ExpectChild(firmware=list(before))
        child1.save_config = mock.Mock()
        child2 = ExpectChild(firmware=list(after))
        logger = FakeLogger()
        calls = []

        def record(name):
            def _f(*args, **kwargs):
                calls.append((name,) + tuple(args[1:3]))  # (name, subslot, idx/carrier)
                if name == "wait_active":
                    return reactivated
                if name == "wait_boot":
                    return child2
            return _f

        patches = {
            "_find_cellular_modules": mock.Mock(return_value=[("PIM 1", "P-5GS6-GL", SUBSLOT)]),
            "_resolve_carrier": mock.Mock(return_value={"key": "verizon", "version": carrier_version} if carrier_version is not None else None),
            "_controller_cleanup": record("cleanup"),
            "_activate_firmware": record("activate"),
            "_wait_for_active_carrier": record("wait_active"),
        }
        with mock.patch.multiple(cfr, **patches), \
                mock.patch.object(reload_step, "_initiate_reload", record("reload")), \
                mock.patch.object(reload_step, "_wait_for_boot", record("wait_boot")):
            _clock(self, module=cfr)
            cfr.run(child1, device or _device("Verizon Wireless"), None, {}, logger)
        return calls, child1, logger

    def test_full_procedure_order(self):
        calls, child1, logger = self._run(before=[FW_VERIZON], after=[FW_GENERIC])
        self.assertEqual(
            [c[0] for c in calls],
            ["cleanup", "activate", "wait_active", "reload", "wait_boot", "activate", "wait_active"],
        )
        self.assertEqual(calls[1], ("activate", SUBSLOT, 2))          # GENERIC before the reload
        self.assertEqual(calls[2], ("wait_active", SUBSLOT, "generic"))
        self.assertEqual(calls[5], ("activate", SUBSLOT, 4))          # VERIZON after it
        self.assertEqual(calls[6], ("wait_active", SUBSLOT, "verizon"))
        child1.save_config.assert_called_once()
        self.assertIn("APN profile step re-applies", logger.text("info"))

    def test_generic_already_active_skips_the_first_activation(self):
        calls, _, logger = self._run(before=[FW_GENERIC], after=[FW_GENERIC])
        self.assertEqual([c[0] for c in calls], ["cleanup", "reload", "wait_boot", "activate", "wait_active"])
        self.assertIn("GENERIC image already active", logger.text("info"))

    def test_generic_not_active_after_reload_fails(self):
        with self.assertRaisesRegex(RuntimeError, "expected the GENERIC image active after the reload, found VERIZON"):
            self._run(before=[FW_VERIZON], after=[FW_VERIZON])

    def test_wrong_version_after_reactivation_fails(self):
        with self.assertRaisesRegex(RuntimeError, "after re-activating the carrier image, firmware 02.15.01.00"):
            self._run(before=[FW_VERIZON_OLD], after=[FW_GENERIC], reactivated=("02.15.01.00", "verizon"))

    def test_no_carrier_firmware_configured_skips_version_check(self):
        calls, _, _ = self._run(before=[FW_VERIZON], after=[FW_GENERIC], carrier_version=None, reactivated=("02.15.01.00", "verizon"))
        self.assertEqual(calls[-1][0], "wait_active")

    def test_untagged_device_skips(self):
        calls, child1, logger = self._run(before=[FW_VERIZON], after=[FW_GENERIC], device=_device("Router"))
        self.assertEqual(calls, [])
        child1.save_config.assert_not_called()
        self.assertIn("does not use the firmware reset", logger.text("info"))

    def test_firstnet_runs_the_same_procedure_on_the_att_image(self):
        calls, child1, logger = self._run(
            before=[FW_VERIZON], after=[FW_GENERIC], device=_device("FirstNet"), reactivated=(TARGET, "att"),
        )
        self.assertEqual(
            [c[0] for c in calls],
            ["cleanup", "activate", "wait_active", "reload", "wait_boot", "activate", "wait_active"],
        )
        self.assertEqual(calls[1], ("activate", SUBSLOT, 2))        # GENERIC before the reload
        self.assertEqual(calls[5], ("activate", SUBSLOT, 1))        # ATT after it
        self.assertEqual(calls[6], ("wait_active", SUBSLOT, "att"))
        child1.save_config.assert_called_once()
