"""Regression tests for features merged from origin/main; no live operations."""
import contextlib
import io
import logging
import subprocess
import unittest
from unittest.mock import mock_open, patch

import server_info as monitor


class IntegratedFeatureTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        self.command = patch.object(monitor.subprocess, 'run').start()
        self.http = patch.object(monitor.http.client, 'HTTPConnection').start()
        self.http.return_value.getresponse.return_value.status = 200
        self.sleep = patch.object(monitor.time, 'sleep').start()
        self.console = io.StringIO()
        self.output = contextlib.redirect_stdout(self.console)
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        self.settings = monitor.Settings(80, 80, 80, 60)

    def test_uptime_parsing_and_formatting(self):
        for raw, value, formatted in (
            ('90061.5 180000.0\n', 90061.5, '1d 1h 1m'),
            ('0.0 0.0\n', 0.0, '0d 0h 0m'),
            ('3599.99 30000\n', 3599.99, '0d 0h 59m'),
        ):
            with self.subTest(raw=raw):
                with patch('builtins.open', mock_open(read_data=raw)) as source:
                    self.assertEqual(monitor.collect_uptime(), value)
                source.assert_called_once_with('/proc/uptime', encoding='ascii')
                self.assertEqual(monitor.format_uptime(value), formatted)
        self.assertEqual(monitor.format_uptime(None), 'unknown')
        self.command.assert_not_called()

    def test_invalid_uptime_is_not_a_healthy_zero(self):
        for raw in ('', 'bad counters', '-1 0', 'nan 0', 'inf 0', '-inf 0'):
            with self.subTest(raw=raw):
                with patch('builtins.open', mock_open(read_data=raw)):
                    with self.assertRaises(ValueError):
                        monitor.collect_uptime()

    def test_missing_uptime_file_reports_unknown(self):
        with patch('builtins.open', side_effect=FileNotFoundError('/proc/uptime')):
            with self.assertLogs(level='ERROR') as logs:
                self.assertIsNone(monitor.safe_check('Uptime', monitor.collect_uptime))
        self.assertIn('ALERT: Uptime check failed', self.console.getvalue())
        self.assertIsNotNone(logs.records[0].exc_info)

    def test_http_latency_uses_monotonic_clock_and_closes_headers(self):
        with patch.object(monitor.time, 'monotonic', side_effect=[50.0, 50.125]):
            with self.assertLogs(level='INFO') as logs:
                result = monitor.check_http(self.settings)
        self.assertEqual(result, monitor.HttpResult('HTTP 200', 125.0))
        self.assertIn('Response time: 125.00 ms', self.console.getvalue())
        self.assertIn('Response time: 125.00 ms', logs.output[-1])
        response = self.http.return_value.getresponse.return_value
        response.read.assert_not_called()
        response.close.assert_called_once()
        self.http.return_value.close.assert_called_once()
        self.command.assert_not_called()

    def test_non_200_has_latency_and_error_alert_without_recovery(self):
        self.http.return_value.getresponse.return_value.status = 503
        with patch.object(monitor.time, 'monotonic', side_effect=[10.0, 10.5]):
            with self.assertLogs(level='ERROR') as logs:
                result = monitor.check_http(self.settings)
        self.assertEqual(result, monitor.HttpResult('HTTP 503', 500.0))
        self.assertIn('ALERT: HTTP FAILURE:', logs.output[0])
        self.assertNotIn('SERVICE FAILURE:', logs.output[0])
        self.command.assert_not_called()

    def test_http_timeout_and_connection_failure_have_no_completed_latency(self):
        for error, status in ((TimeoutError('slow'), 'timeout'),
                              (ConnectionRefusedError('refused'), 'request failed')):
            with self.subTest(error=error):
                self.http.return_value.request.side_effect = error
                with self.assertLogs(level='ERROR'):
                    result = monitor.check_http(self.settings)
                self.assertEqual(result, monitor.HttpResult(status, None))
        self.command.assert_not_called()

    def test_local_alert_formats_once_and_preserves_severity(self):
        with self.assertLogs(level='WARNING') as logs:
            monitor.send_alert('Disk usage is high: %.1f%%', 90.5)
            monitor.send_alert('HTTP FAILURE: %s', 'refused', level=logging.ERROR)
        self.assertEqual(self.console.getvalue().splitlines(), [
            'ALERT: Disk usage is high: 90.5%', 'ALERT: HTTP FAILURE: refused'])
        self.assertEqual([record.levelno for record in logs.records],
                         [logging.WARNING, logging.ERROR])
        self.assertEqual(len(logs.records), 2)
        self.command.assert_not_called()

    def test_summary_includes_uptime_and_latency_without_losing_old_fields(self):
        with contextlib.ExitStack() as stack:
            for name, value in (('collect_uptime', 90061.0), ('collect_cpu', 30.0),
                                ('collect_memory', 40.0), ('collect_disk', 20.0),
                                ('check_nginx', 'active'),
                                ('check_http', monitor.HttpResult('HTTP 200', 12.5))):
                stack.enter_context(patch.object(monitor, name, return_value=value))
            logs = stack.enter_context(self.assertLogs(level='INFO'))
            result = monitor.run_check(self.settings, monitor.RecoveryState())
        self.assertEqual(result, {'uptime_seconds': 90061.0, 'cpu': 30.0, 'memory': 40.0,
                                  'disk': 20.0, 'nginx': 'active', 'http': 'HTTP 200',
                                  'http_response_ms': 12.5})
        for value in ('CPU: 30.0%', 'Memory: 40.0%', 'Disk: 20.0%', 'Nginx: active',
                      'HTTP: HTTP 200', 'Uptime: 1d 1h 1m', 'HTTP response: 12.50 ms'):
            self.assertIn(value, logs.output[-1])

    def test_resource_thresholds_use_local_alerts(self):
        with contextlib.ExitStack() as stack:
            for name, value in (('collect_uptime', 0.0), ('collect_cpu', 81.0),
                                ('collect_memory', 90.0), ('collect_disk', 99.0),
                                ('check_nginx', 'active'),
                                ('check_http', monitor.HttpResult('HTTP 200', 1.0))):
                stack.enter_context(patch.object(monitor, name, return_value=value))
            alert = stack.enter_context(patch.object(monitor, 'send_alert'))
            monitor.run_check(self.settings, monitor.RecoveryState())
        self.assertEqual(alert.call_count, 3)
        self.assertEqual([call.args[1] for call in alert.call_args_list], ['Disk', 'CPU', 'Memory'])

    def test_repeated_inactive_service_with_default_config_never_recovers(self):
        def commands(args, **kwargs):
            if args == ['free', '-m']:
                return subprocess.CompletedProcess(args, 0, 'Mem: 100 25 75\n', '')
            if args == ['systemctl', 'is-active', 'nginx']:
                return subprocess.CompletedProcess(args, 3, 'inactive', '')
            self.fail(f'Unexpected service operation: {args}')
        self.command.side_effect = commands
        state = monitor.RecoveryState()
        with contextlib.ExitStack() as stack:
            for name in ('collect_uptime', 'collect_cpu', 'collect_disk'):
                stack.enter_context(patch.object(monitor, name, return_value=10.0))
            stack.enter_context(self.assertLogs(level='INFO'))
            for _ in range(3):
                monitor.run_check(monitor.load_config(), state)
        self.assertEqual(state.attempts, 0)
        for call in self.command.call_args_list:
            self.assertEqual(call.kwargs['timeout'], monitor.COMMAND_TIMEOUT)
            self.assertEqual(call.kwargs['stdin'], subprocess.DEVNULL)
            self.assertNotIn('sudo', call.args[0])


if __name__ == '__main__':
    unittest.main()
