"""Exercise real rotation and advisory locks only in temporary directories."""
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server_info as monitor


class HardeningTests(unittest.TestCase):
    def test_rotation_retains_only_configured_backups(self):
        settings = monitor.Settings(80, 80, 80, 60, log_max_bytes=180, log_backup_count=2)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'monitor.log'
            with patch.object(monitor.logging, 'basicConfig') as setup:
                monitor.configure_logging(path, settings)
            handler = setup.call_args.kwargs['handlers'][0]
            try:
                for number in range(30):
                    handler.handle(logging.LogRecord('test', logging.INFO, '', 0,
                                                      f'entry {number:02d} ' + 'x' * 40, (), None))
                handler.flush()
                self.assertEqual({p.name for p in Path(folder).iterdir()},
                                 {'monitor.log', 'monitor.log.1', 'monitor.log.2'})
                self.assertTrue(all(p.stat().st_size < 180 for p in Path(folder).iterdir()))
                self.assertIn('entry 29', path.read_text())
                self.assertNotIn('entry 00', ''.join(p.read_text() for p in Path(folder).iterdir()))
            finally:
                handler.close()

    def test_retention_remains_bounded_after_logger_reopen(self):
        # Simulate process restarts against existing logs, with one retained backup.
        settings = monitor.Settings(80, 80, 80, 60, log_max_bytes=180, log_backup_count=1)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'monitor.log'
            for batch in range(3):
                with patch.object(monitor.logging, 'basicConfig') as setup:
                    monitor.configure_logging(path, settings)
                handler = setup.call_args.kwargs['handlers'][0]
                try:
                    for index in range(20):
                        handler.handle(logging.LogRecord(
                            'test', logging.INFO, '', 0,
                            f'batch {batch} entry {index:02d} ' + 'x' * 40, (), None))
                finally:
                    handler.close()
                self.assertEqual({item.name for item in Path(folder).iterdir()},
                                 {'monitor.log', 'monitor.log.1'})
            self.assertIn('batch 2 entry 19', path.read_text())
            self.assertNotIn('batch 0', ''.join(item.read_text() for item in Path(folder).iterdir()))

    def test_duplicate_lock_rejected_and_released(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'monitor.lock'
            with monitor.instance_lock(path):
                with self.assertRaisesRegex(RuntimeError, 'Another monitor'):
                    with monitor.instance_lock(path):
                        self.fail('duplicate acquired lock')
            self.assertTrue(path.exists())
            with monitor.instance_lock(path):
                pass

    def test_lock_released_on_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'monitor.lock'
            with self.assertRaises(ValueError):
                with monitor.instance_lock(path):
                    raise ValueError('test')
            with monitor.instance_lock(path):
                pass

    def test_paths_do_not_depend_on_working_directory(self):
        before = Path.cwd()
        with tempfile.TemporaryDirectory() as folder:
            try:
                os.chdir(folder)
                defaults = monitor.parse_args([])
                custom = monitor.parse_args(['--config', 'custom.ini', '--log-file', 'logs/custom.log'])
                self.assertEqual(defaults.config, monitor.CONFIG_PATH)
                self.assertEqual(defaults.log_file, monitor.PROJECT_DIR / 'server_monitor.log')
                self.assertEqual(defaults.lock_file, monitor.PROJECT_DIR / 'server_monitor.lock')
                self.assertEqual(custom.config, monitor.PROJECT_DIR / 'custom.ini')
                self.assertEqual(custom.log_file, monitor.PROJECT_DIR / 'logs/custom.log')
            finally:
                os.chdir(before)

    def test_logging_config_defaults_and_invalid_limits(self):
        base = '[monitoring]\ndisk_threshold=80\ncpu_threshold=80\nmemory_threshold=80\ncheck_interval=60\n'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.ini'
            path.write_text(base)
            self.assertEqual(monitor.load_config(path).log_backup_count, 5)
            self.assertEqual(monitor.load_config(path).log_max_bytes, 5242880)
            for config in ('max_bytes=0', 'backup_count=0', 'backup_count=-1', 'max_bytes=bad'):
                path.write_text(base + '\n[logging]\n' + config)
                with self.assertRaises(ValueError):
                    monitor.load_config(path)
            path.write_text(base + '\n[logging]\nmax_bytes=1024\nbackup_count=2\n')
            self.assertEqual(monitor.load_config(path).log_max_bytes, 1024)

    def test_main_lock_and_logging_failures_prevent_collection(self):
        settings = monitor.Settings(80, 80, 80, 60)
        with tempfile.TemporaryDirectory() as folder:
            args = ['--lock-file', str(Path(folder) / 'lock'),
                    '--log-file', str(Path(folder) / 'missing' / 'log')]
            with patch.object(monitor, 'load_config', return_value=settings):
                with patch.object(monitor, 'monitor_loop') as loop:
                    self.assertEqual(monitor.main(args), 1)
                    with monitor.instance_lock(Path(folder) / 'lock'):
                        self.assertEqual(monitor.main(args), 1)
                    loop.assert_not_called()

    def test_main_runs_under_lock_with_loaded_settings(self):
        settings = monitor.Settings(80, 80, 80, 60)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'lock'
            def loop(value):
                self.assertEqual(value, settings)
                with self.assertRaises(RuntimeError):
                    with monitor.instance_lock(path):
                        pass
                return 0
            with patch.object(monitor, 'load_config', return_value=settings):
                with patch.object(monitor, 'configure_logging'):
                    with patch.object(monitor, 'monitor_loop', side_effect=loop):
                        self.assertEqual(monitor.main(['--lock-file', str(path)]), 0)

    def test_checked_in_config_keeps_recovery_disabled(self):
        self.assertFalse(monitor.load_config().auto_recovery)


if __name__ == '__main__':
    unittest.main()
