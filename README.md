# Linux Server Monitoring

A dependency-free Linux health monitor for CPU, memory, root filesystem usage,
Nginx systemd activity, and `http://127.0.0.1/`. Automatic recovery is disabled by
default. This repository prepares deployment artifacts; it does not install them.

## Architecture

```text
systemd (restart budget, sandbox, dynamic unprivileged user)
  └─ Python foreground process
      ├─ config validation → exclusive flock → rotating file logger
      └─ sequential monitoring cycles
          ├─ disk: shutil.disk_usage("/")
          ├─ CPU: /proc/stat sampled over 0.2 seconds
          ├─ memory: free -m
          ├─ Nginx: systemctl is-active nginx
          └─ HTTP: direct loopback GET /, exact HTTP 200
```

Each check has an independent error boundary. Failed measurements become
`unknown`; other collectors still execute. Service and HTTP failures have separate
log messages. HTTP failures never initiate recovery. The configured interval is
sleep time after each cycle, so collection time adds to the cycle duration.
CPU calculations exclude duplicate guest counters and treat I/O wait as idle.
Memory retains the original used/total calculation.

```text
server_info.py                       monitoring, configuration, locking, logging
config.ini                           backward-compatible development configuration
deployment/server-monitor.service    proposed systemd system unit
tests/test_server_info.py             collection, HTTP, and recovery tests
tests/test_hardening.py               rotation, retention, paths, locking, startup
server_check.sh                       separate manual diagnostic helper
```

## Requirements and predictable paths

Linux, Python 3.10+, procps (`free`), and systemd for the service status collector.
No pip packages are required. Paths passed to the CLI may be absolute; relative
paths and defaults resolve against the script directory, never the launch cwd.

| Purpose | Development default | Proposed system service |
|---|---|---|
| Code | Repository `server_info.py` | `/opt/server-monitor/server_info.py` |
| Configuration | Beside script: `config.ini` | `/etc/server-monitor/config.ini` |
| Log | Beside script: `server_monitor.log` | `/var/log/server-monitor/server_monitor.log` |
| Lock | Beside script: `server_monitor.lock` | `/run/server-monitor/monitor.lock` |

This intentionally replaces the old cwd-relative log location. Existing logs
are not moved or deleted. Parent directories must exist and be writable where
appropriate; startup fails clearly rather than creating directories implicitly.

```bash
python3 server_info.py --config /absolute/config.ini --log-file /absolute/monitor.log --lock-file /absolute/monitor.lock
```

## Configuration

```ini
[monitoring]
disk_threshold = 80
cpu_threshold = 80
memory_threshold = 80
check_interval = 60
http_timeout = 5.0
auto_recovery = false
recovery_max_attempts = 3
recovery_cooldown = 300

[logging]
max_bytes = 5242880
backup_count = 5
```

Original configurations remain valid. Omitted logging settings use 5 MiB per file
and five backups. Thresholds must be 0–100; intervals, limits and rotation values
must be positive. HTTP timeout must be finite and positive. Configuration is read
at startup, not hot-reloaded. A malformed configuration exits nonzero.

## Rotation and retention

Python's `RotatingFileHandler` rotates on size to `.1` through `.5`; the oldest
backup is removed on rollover. Defaults retain the active file plus five backups,
roughly 30 MiB. Retention is size/count based, not age based. UTF-8 and the original
`timestamp - level - message` format are preserved. A single oversized record can
exceed the threshold, so this is not a filesystem quota. Reducing backup_count
will not automatically remove preexisting higher-numbered backups. Do not combine
external logrotate with this handler on the same file.

The flock is acquired before opening the log. A second process using the same
lock path fails without touching the log. Locks are released on exit and exceptions;
the file is deliberately not unlinked. All instances accessing the same log must
use the same lock path. Advisory locking cannot stop an operator choosing a
different lock or ignoring the protocol. Use a local Linux filesystem, not an NFS
share or Windows-mounted directory, for locks and active logs.

## Installation plan — administrator approval required

These are proposed administrator steps only. No installation, account creation,
permission changes, service activation, or Nginx operations have been performed.

1. Review the Git diff and run the validation commands below.
2. Have an administrator provision `/opt/server-monitor` and `/etc/server-monitor`,
   deploy the reviewed script and configuration, and keep both root-owned and
   unwritable by the service. Code may be 0644, configuration 0644 when it contains
   no secrets, with root-owned 0755 parent directories. Python invokes the script
   directly; executable permission on the script is unnecessary.
3. Keep `auto_recovery = false`. Do not add sudoers or polkit grants.
4. Install the reviewed unit at `/etc/systemd/system/server-monitor.service`.
   No persistent user account needs creation: systemd supplies `DynamicUser` and
   provisions the private log and runtime directories on service start.
5. Run `systemd-analyze verify /etc/systemd/system/server-monitor.service` and
   inspect `systemd-analyze security server-monitor.service` on the target host.
6. Only after separate approval, an administrator may run `systemctl daemon-reload`
   and `systemctl enable --now server-monitor.service`. This starts the monitor,
   not Nginx; the unit deliberately has no Requires/Wants dependency on Nginx.
7. Validate measurements, systemd bus access, HTTP responses, rotation, and effective
   sandbox settings under the actual service identity before production sign-off.

## Supervision and least privilege

The unit runs one foreground process under a dynamic unprivileged identity with
an empty capability set and no new privileges. It uses a fixed PATH, Python
isolated mode, read-only filesystem protection, hidden home directories, private
temporary/device views, and kernel/control-group protections. Its dedicated logs
and runtime directories are writable exceptions managed by systemd. It retains
the host network view for loopback health checks and AF_UNIX for the systemd bus;
PrivateNetwork would break the intended endpoint. `/proc/stat` remains visible.

`Restart=on-failure`, a 10-second delay, and a maximum of three starts within
300 seconds bound crash loops. Once the limit is hit, operator investigation is
required. systemd supervises a single main process per unit; flock additionally
rejects duplicates using the same runtime lock path. Stop kills the service's
control group; the kernel releases the lock even after abrupt termination.
Shutdown uses systemd's normal SIGTERM behavior; no final application shutdown
message is guaranteed. No watchdog currently detects a hung Python process.

Never run the monitor as root to work around failed checks. NoNewPrivileges and
an empty capability set do not replace reviewing local systemd/polkit policy.
The supplied unit is monitoring-only: recovery would require a separately reviewed
privilege design. Do not enable the optional recovery setting in production yet.

## Operation and troubleshooting

Read-only diagnostic commands, after installation:

```bash
systemctl status server-monitor.service
journalctl -u server-monitor.service -n 100 --no-pager
systemctl show server-monitor.service -p User -p DynamicUser -p MainPID -p NRestarts
```

Application measurements go to the rotating file. stdout/stderr and startup
failures go to journald. Journal retention is host-wide policy and is not bounded
by the application's rotation settings. Viewing protected logs may require an
administrator; do not loosen directory permissions just to inspect them.

| Symptom | Action |
|---|---|
| Invalid configuration | Check section names, numeric ranges and selected config path |
| Cannot open log/lock | Check parent paths and service-managed directory provisioning |
| Another monitor holds lock | Identify the owner; do not delete the lock file |
| Nginx status unknown / bus denied | Check systemd availability and execution policy; do not grant root |
| HTTP timeout/refused/non-200 | Inspect Nginx and default virtual-host routing independently |
| Start limit reached | Inspect journal and fix cause; any reset/start requires approval |
| Disk full or rotation errors | Inspect filesystem capacity, log ownership and retention policy |

A leftover lock file alone does not indicate a running process. Rotating-file
write errors after startup follow Python logging's error handling and may leave
collection running without durable logs; monitor stderr and disk usage externally.

## Automated validation

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
git diff --check
systemd-analyze verify deployment/server-monitor.service
```

Tests mock monitoring commands, HTTP connections and sampling sleeps. Hardening
tests exercise real rotation, backup eviction, and advisory locks only in temporary
directories. Unit verification checks syntax and dependencies without starting or
installing the service. Neither unit tests nor syntax validation prove the sandbox
works on a deployment host; that requires approved integration testing.

## Ubuntu WSL2 versus a standard Linux server

WSL2 systemd must be supported and enabled for the distribution. Check PID 1 and
systemd availability before deployment. Any changes to `/etc/wsl.conf` or WSL
shutdown/restart require separate approval; none are part of this revision.

On a standard systemd Linux server, the service starts at host boot when enabled.
In WSL2 it starts when the distribution boots, which is not a guarantee of Windows
boot startup or continuous availability. Windows sleep, shutdown, and WSL lifecycle
control can interrupt monitoring. CPU/memory/disk measurements describe the Linux
WSL environment, not full Windows host health. Loopback points to the monitor's
Linux network context; Windows-host access and external exposure depend on WSL
networking and firewall configuration. Keep deployment paths on the Linux filesystem.
Sandbox support also depends on the WSL kernel/systemd version and must be tested.

The earlier live check in this development environment could not access the systemd
bus. Preparing a unit does not resolve that restriction; do not infer Nginx state
from that failed query. WSL2 is useful for development, but validate the intended
server environment before relying on this for continuous production monitoring.

## Remaining limits and next steps

- HTTP timeout limits socket operations, not total wall-clock header receipt.
  Only headers and exact status 200 are checked; redirects are failures. No proxies
  or response bodies are used. A 200 does not prove process identity or external health.
- Optional service recovery verifies systemd active state, has a cooldown and
  process-lifetime attempt budget, but the budget resets on restart. HTTP failures
  never consume it. A durable shared budget is required before production recovery.
- No external notifications, remote availability probe, durable metrics history,
  watchdog, or maintenance-mode workflow is provided.
- Only `/` capacity is checked, without inode or additional mount monitoring.
- Retention is bounded by file count and approximate size, not a hard quota or age.
- Code/configuration ownership, effective sandboxing, service authorization and
  live operation still need target-host validation after explicit approval.

## Reference documentation

- [Python rotating file handlers](https://docs.python.org/3/library/logging.handlers.html#rotatingfilehandler)
- [systemd execution sandbox and managed directories](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)
- [systemd service supervision](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)
- [Microsoft: systemd in WSL](https://learn.microsoft.com/en-us/windows/wsl/systemd)
