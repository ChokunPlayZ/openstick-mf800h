import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from cellular_recovery import ACTIVE, MODEMS, RADIO, RESTART, Paths, check


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.paths = Paths(*(root / name for name in ("model", "qmi", "uptime", "state.json")))
        self.paths.model.write_text("Qualcomm MSM8916 MF800H\0")
        self.paths.qmi.touch()
        self.set_time(100)
        self.calls = []
        self.modems = []
        self.radio = "enabled"
        self.active = True
        self.fail = None
        self.modem_output = None

    def set_time(self, now):
        self.paths.uptime.write_text(f"{now}.0 0.0\n")

    def runner(self, args):
        self.calls.append(args)
        output = ""
        code = 0
        if args == ACTIVE:
            code = 0 if self.active else 3
        elif args == RADIO:
            output = self.radio
        elif args == MODEMS:
            output = self.modem_output or json.dumps({"modem-list": self.modems})
        elif args != RESTART:
            self.failTest(f"Unexpected command: {args}")
        if args == self.fail:
            code = 1
        return subprocess.CompletedProcess(args, code, output, "test failure" if code else "")

    def check(self):
        return check(self.paths, self.runner)

    def test_restarts_only_after_discovery_grace(self):
        self.assertEqual(self.check(), "waiting")
        self.set_time(129)
        self.assertEqual(self.check(), "waiting")
        self.assertNotIn(RESTART, self.calls)
        self.set_time(130)
        self.assertEqual(self.check(), "restarted")
        self.assertEqual(self.calls.count(RESTART), 1)

    def test_cooldown_survives_separate_checks(self):
        self.check()
        self.set_time(130)
        self.check()
        self.set_time(160)
        self.assertEqual(self.check(), "waiting")
        self.set_time(190)
        self.assertEqual(self.check(), "cooldown")
        self.set_time(430)
        self.assertEqual(self.check(), "restarted")
        self.assertEqual(self.calls.count(RESTART), 2)

    def test_failed_restart_is_rate_limited(self):
        self.check()
        self.set_time(130)
        self.fail = RESTART
        with self.assertRaises(RuntimeError):
            self.check()
        self.set_time(160)
        self.check()
        self.set_time(190)
        self.assertEqual(self.check(), "cooldown")
        self.assertEqual(self.calls.count(RESTART), 1)

    def test_healthy_modem_is_never_restarted(self):
        self.modems = ["/org/freedesktop/ModemManager1/Modem/7"]
        self.assertEqual(self.check(), "healthy")
        self.assertFalse(self.paths.state.exists())
        self.assertNotIn(RESTART, self.calls)

    def test_discovery_recovery_resets_missing_streak(self):
        self.check()
        self.modems = ["/org/freedesktop/ModemManager1/Modem/0"]
        self.set_time(130)
        self.assertEqual(self.check(), "healthy")
        self.modems = []
        self.set_time(160)
        self.assertEqual(self.check(), "waiting")
        self.assertNotIn(RESTART, self.calls)

    def test_skips_other_hardware(self):
        self.paths.model.write_text("UZ801")
        self.assertEqual(self.check(), "skipped")
        self.assertEqual(self.calls, [])

    def test_skips_missing_qmi_port(self):
        self.paths.qmi.unlink()
        self.assertEqual(self.check(), "skipped")
        self.assertEqual(self.calls, [])

    def test_allows_boot_initialization(self):
        self.set_time(44)
        self.assertEqual(self.check(), "skipped")
        self.assertEqual(self.calls, [])

    def test_respects_stopped_modem_manager(self):
        self.active = False
        self.assertEqual(self.check(), "skipped")
        self.assertEqual(self.calls, [ACTIVE])

    def test_respects_disabled_wwan(self):
        self.radio = "disabled"
        self.assertEqual(self.check(), "skipped")
        self.assertNotIn(MODEMS, self.calls)
        self.assertNotIn(RESTART, self.calls)

    def test_failed_diagnostics_do_not_restart(self):
        for command in (RADIO, MODEMS):
            with self.subTest(command=command):
                self.fail = command
                with self.assertRaises(RuntimeError):
                    self.check()
                self.assertNotIn(RESTART, self.calls)

    def test_malformed_diagnostics_do_not_restart(self):
        for output in ("bad JSON", "{}", "[]", '{"modem-list": null}', '{"modem-list": [1]}'):
            with self.subTest(output=output):
                self.modem_output = output
                with self.assertRaises(ValueError):
                    self.check()
                self.assertNotIn(RESTART, self.calls)

    def test_corrupt_state_does_not_restart(self):
        for state in ("bad JSON", "[]", '{"last_restart": "bad"}', '{"missing_since": -1}'):
            with self.subTest(state=state):
                self.paths.state.write_text(state)
                with self.assertRaises(ValueError):
                    self.check()
                self.assertNotIn(RESTART, self.calls)

    def test_timeout_does_not_restart(self):
        def timeout(args):
            raise subprocess.TimeoutExpired(args, 30)
        with self.assertRaises(subprocess.TimeoutExpired):
            check(self.paths, timeout)
        self.assertNotIn(RESTART, self.calls)

    def test_recovery_commands_are_carrier_neutral_and_non_destructive(self):
        self.check()
        self.set_time(130)
        self.check()
        self.assertEqual(self.calls, [ACTIVE, RADIO, MODEMS, ACTIVE, RADIO, MODEMS, RESTART])


if __name__ == "__main__":
    unittest.main()
