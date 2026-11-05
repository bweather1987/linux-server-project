"""Tests never invoke real subprocesses or service operations."""
import contextlib
import io
import runpy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, mock_open, patch

import server_info as monitor


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.command = patch.object(monitor.subprocess, "run").start()
        self.sleep = patch.object(monitor.time, "sleep").start()
        self.addCleanup(patch.stopall)
        self.settings = monitor.Settings(80, 80, 80, 60, auto_recovery=True)
        self.state = monitor.RecoveryState()

    def test_import_has_no_runtime_side_effects(self):
        with patch.object(monitor.logging, "basicConfig") as logging_setup:
            with patch("builtins.open", side_effect=AssertionError("unexpected file read")):
                runpy.run_path(str(Path(monitor.__file__)), run_name="import_check")
        logging_setup.assert_not_called()
        self.command.assert_not_called()
        self.sleep.assert_not_called()

    def test_command_timeout_and_noninteractive_input(self):
        self.command.side_effect = subprocess.TimeoutExpired("free", 10)
        with self.assertRaises(subprocess.TimeoutExpired):
            monitor.run_command(["free", "-m"])
        kwargs = self.command.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 10)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(kwargs["check"])
        self.assertEqual(kwargs["env"]["LC_ALL"], "C")

    def test_each_check_failure_is_isolated(self):
        names = ("collect_disk", "collect_cpu", "collect_memory", "check_nginx")
        for failed in names:
            for error in (OSError("missing"), ValueError("bad output"),
                          subprocess.TimeoutExpired("command", 10)):
                with self.subTest(check=failed, error=type(error).__name__):
                    with contextlib.ExitStack() as stack:
                        mocks = {name: stack.enter_context(patch.object(
                            monitor, name, return_value="active" if name == "check_nginx" else 10.0
                        )) for name in names}
                        mocks[failed].side_effect = error
                        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                        logs = stack.enter_context(self.assertLogs(level="INFO"))
                        results = monitor.run_check(self.settings, self.state)
                        for mock in mocks.values():
                            mock.assert_called_once()
                        self.assertEqual(list(results.values()).count(None), 1)
                        self.assertIn("unknown", logs.output[-1])

    def test_cpu_samples_deltas(self):
        with patch.object(monitor, "read_cpu_counters", side_effect=[(100, 40), (200, 70)]):
            self.assertEqual(monitor.collect_cpu(), 70)
        self.sleep.assert_called_once_with(monitor.CPU_SAMPLE_SECONDS)

    def test_cpu_excludes_guest_double_count(self):
        with patch("builtins.open", mock_open(read_data="cpu 10 20 30 40 5 6 7 8 100 200\n")):
            self.assertEqual(monitor.read_cpu_counters(), (126, 45))

    def test_bad_cpu_samples(self):
        for samples in ([(100, 40), (100, 40)], [(100, 40), (90, 30)],
                        [(100, 40), (200, 30)], [(100, 40), (110, 60)]):
            with self.subTest(samples=samples):
                with patch.object(monitor, "read_cpu_counters", side_effect=samples):
                    with self.assertRaises(ValueError):
                        monitor.collect_cpu()

    def test_memory_and_disk_validation(self):
        self.command.return_value = subprocess.CompletedProcess([], 0, "Mem: 100 25 75\n", "")
        self.assertEqual(monitor.collect_memory(), 25)
        for output in ("invalid", "Mem: 0 1", "Mem: x y", "Mem: 100"):
            self.command.return_value.stdout = output
            with self.assertRaises((ValueError, IndexError)):
                monitor.collect_memory()
        with patch.object(monitor.shutil, "disk_usage", return_value=(0, 0, 0)):
            with self.assertRaises(ValueError):
                monitor.collect_disk()

    def test_nginx_status_validation(self):
        for code, output, expected in ((0, "active", "active"), (3, "inactive", "inactive"),
                                       (3, "failed", "failed"), (3, "activating", "activating")):
            self.command.return_value = subprocess.CompletedProcess([], code, output, "")
            self.assertEqual(monitor.get_nginx_status(), expected)
        for code, output in ((1, ""), (4, "unknown"), (0, "inactive")):
            self.command.return_value = subprocess.CompletedProcess([], code, output, "error")
            with self.assertRaises(RuntimeError):
                monitor.get_nginx_status()

    def test_no_recovery_for_active_transitional_or_disabled(self):
        for status in ("active", "activating", "deactivating"):
            with patch.object(monitor, "get_nginx_status", return_value=status):
                self.assertEqual(monitor.check_nginx(self.settings, self.state), status)
        with patch.object(monitor, "get_nginx_status", return_value="inactive"):
            self.assertEqual(monitor.check_nginx(monitor.Settings(80, 80, 80, 60), self.state), "inactive")
        self.command.assert_not_called()

    def test_query_failure_never_starts_service(self):
        with patch.object(monitor, "get_nginx_status", side_effect=RuntimeError("bus unavailable")):
            with self.assertRaises(RuntimeError):
                monitor.check_nginx(self.settings, self.state)
        self.command.assert_not_called()

    def test_recovery_is_verified_and_noninteractive(self):
        with patch.object(monitor, "get_nginx_status", side_effect=["inactive", "activating", "active"]):
            with self.assertLogs(level="INFO") as logs:
                self.assertEqual(monitor.check_nginx(self.settings, self.state), "active")
        self.assertIn("recovered successfully", logs.output[-1])
        self.assertEqual(self.state.attempts, 1)
        self.assertEqual(self.command.call_args.args[0],
                         ["systemctl", "--no-ask-password", "start", "nginx"])
        self.assertEqual(self.command.call_args.kwargs["timeout"], monitor.RECOVERY_TIMEOUT)

    def test_failed_commands_still_verify(self):
        for error in (FileNotFoundError("systemctl"), subprocess.TimeoutExpired("start", 30),
                      subprocess.CalledProcessError(1, "start")):
            with self.subTest(error=type(error).__name__):
                self.command.side_effect = error
                with patch.object(monitor, "get_nginx_status", side_effect=["inactive", "active"]):
                    self.assertEqual(monitor.check_nginx(self.settings, monitor.RecoveryState()), "active")

    def test_unconfirmed_recovery_is_not_success(self):
        with patch.object(monitor, "get_nginx_status", return_value="inactive") as query:
            with self.assertLogs(level="INFO") as logs:
                self.assertEqual(monitor.check_nginx(self.settings, self.state), "inactive")
        self.assertEqual(query.call_count, 1 + monitor.VERIFY_ATTEMPTS)
        self.assertFalse(any("recovered successfully" in log for log in logs.output))

    def test_post_recovery_query_failure_is_unknown(self):
        with patch.object(monitor, "get_nginx_status", side_effect=["inactive", OSError("bus error")]):
            with self.assertLogs(level="INFO") as logs:
                result = monitor.safe_check("Nginx", lambda: monitor.check_nginx(self.settings, self.state))
        self.assertIsNone(result)
        self.assertFalse(any("recovered successfully" in log for log in logs.output))

    def test_recovery_cooldown_and_budget(self):
        with patch.object(monitor, "get_nginx_status", return_value="inactive"):
            with patch.object(monitor.time, "monotonic", return_value=1000):
                monitor.check_nginx(self.settings, self.state)
                monitor.check_nginx(self.settings, self.state)
            self.assertEqual(self.command.call_count, 1)
            with patch.object(monitor.time, "monotonic", return_value=1300):
                monitor.check_nginx(self.settings, self.state)
            with patch.object(monitor.time, "monotonic", return_value=1600):
                monitor.check_nginx(self.settings, self.state)
            with patch.object(monitor.time, "monotonic", return_value=2000):
                monitor.check_nginx(self.settings, self.state)
            self.assertEqual(self.command.call_count, 3)
            self.assertEqual(self.state.attempts, 3)

    def test_config_compatibility_and_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.ini"
            with self.assertRaises(ValueError):
                monitor.load_config(path)
            base = "[monitoring]\ndisk_threshold=80\ncpu_threshold=80\nmemory_threshold=80\ncheck_interval=60\n"
            path.write_text(base)
            self.assertFalse(monitor.load_config(path).auto_recovery)
            for old, new in (("disk_threshold=80", "disk_threshold=101"),
                             ("check_interval=60", "check_interval=0")):
                path.write_text(base.replace(old, new))
                with self.assertRaises(ValueError):
                    monitor.load_config(path)
            for extra in ("recovery_max_attempts=0", "recovery_cooldown=-1", "auto_recovery=maybe"):
                path.write_text(base + extra + "\n")
                with self.assertRaises(ValueError):
                    monitor.load_config(path)

    def test_shutdown_and_bad_startup(self):
        with patch.object(monitor.logging, "basicConfig"):
            with patch.object(monitor, "load_config", side_effect=ValueError("bad config")):
                self.assertEqual(monitor.main(), 1)
            with patch.object(monitor, "load_config", return_value=self.settings):
                with patch.object(monitor, "run_check", side_effect=KeyboardInterrupt):
                    self.assertEqual(monitor.main(), 0)
        self.command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
