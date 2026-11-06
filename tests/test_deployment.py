"""Validate the shipped unit's security contract without invoking systemd."""
import configparser
from pathlib import Path
import shlex
import unittest

import server_info as monitor


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.unit = configparser.ConfigParser(interpolation=None, strict=False)
        self.unit.optionxform = str
        text = (Path(monitor.__file__).parent / 'deployment/server-monitor.service').read_text()
        # Environment may repeat in systemd units. Reject ambiguous duplicates
        # of security-sensitive scalar settings rather than silently ignoring them.
        seen = set()
        for line in text.splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key = line.split('=', 1)[0]
                if key != 'Environment':
                    self.assertNotIn(key, seen)
                seen.add(key)
        self.unit.read_string(text)
        self.service = self.unit['Service']

    def test_static_identity_has_no_privilege_escalation(self):
        self.assertEqual(self.service['DynamicUser'], 'no')
        self.assertEqual(self.service['User'], 'server-monitor-check')
        self.assertEqual(self.service['Group'], 'server-monitor-check')
        self.assertNotIn('SupplementaryGroups', self.service)
        self.assertEqual(self.service['CapabilityBoundingSet'], '')
        self.assertEqual(self.service['AmbientCapabilities'], '')
        for command in ('ExecStartPre', 'ExecStartPost', 'ExecStop', 'ExecStopPost', 'ExecReload'):
            self.assertNotIn(command, self.service)

    def test_sandbox_contract_preserves_dynamic_user_protections(self):
        for key in ('RemoveIPC', 'NoNewPrivileges', 'PrivateTmp', 'PrivateDevices',
                    'ProtectHome', 'ProtectKernelTunables', 'ProtectKernelModules',
                    'ProtectKernelLogs', 'ProtectControlGroups', 'RestrictSUIDSGID',
                    'RestrictRealtime', 'LockPersonality'):
            with self.subTest(directive=key):
                self.assertEqual(self.service[key], 'yes')
        self.assertEqual(self.service['ProtectSystem'], 'strict')
        self.assertEqual(set(self.service['RestrictAddressFamilies'].split()), {'AF_UNIX', 'AF_INET'})
        self.assertNotIn('PrivateNetwork', self.service)
        self.assertNotIn('ReadWritePaths', self.service)

    def test_managed_paths_and_permissions(self):
        for key in ('RuntimeDirectory', 'LogsDirectory'):
            self.assertEqual(self.service[key], 'server-monitor')
            self.assertEqual(int(self.service[key + 'Mode'], 8), 0o700)
        self.assertEqual(int(self.service['UMask'], 8), 0o077)
        self.assertEqual(self.service['RuntimeDirectoryPreserve'], 'restart')
        command = shlex.split(self.service['ExecStart'])
        self.assertEqual(command[:5], ['/usr/bin/python3', '-I', '-B', '-u',
                                      '/opt/server-monitor/server_info.py'])
        self.assertEqual(dict(zip(command[5::2], command[6::2])), {
            '--config': '/etc/server-monitor/config.ini',
            '--log-file': '/var/log/server-monitor/server_monitor.log',
            '--lock-file': '/run/server-monitor/monitor.lock',
        })
        self.assertEqual(self.service['WorkingDirectory'], '/opt/server-monitor')

    def test_restart_budget_and_nginx_independence(self):
        self.assertEqual(self.service['Type'], 'simple')
        self.assertEqual(self.service['Restart'], 'on-failure')
        self.assertEqual(self.service['RestartSec'], '10')
        self.assertEqual(self.service['KillMode'], 'control-group')
        self.assertEqual(self.unit['Unit']['StartLimitIntervalSec'], '300')
        self.assertEqual(self.unit['Unit']['StartLimitBurst'], '3')
        for key in ('After', 'Wants', 'Requires', 'BindsTo', 'PartOf'):
            self.assertNotIn('nginx', self.unit['Unit'].get(key, ''))
        self.assertFalse(monitor.load_config().auto_recovery)


if __name__ == '__main__':
    unittest.main()
