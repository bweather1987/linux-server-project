# Linux Server Monitoring & Recovery System

## Overview

A Python-based Linux server monitoring and recovery system built to practice real-world system administration, automation, monitoring, and service management.

The system continuously monitors server resources, checks the Nginx web service, logs monitoring results, detects failures, and automatically attempts to recover Nginx when it stops running.

## What It Monitors

* Disk usage
* CPU usage
* Memory usage
* Nginx service status
* Server health over time

## Key Features

* Continuous server monitoring
* Configurable monitoring thresholds
* Configurable monitoring interval
* Disk usage alerts
* CPU usage alerts
* Memory usage alerts
* Nginx service monitoring
* Automatic Nginx recovery
* Error handling for monitoring failures
* Structured monitoring logs
* Check numbering for monitoring cycles
* Git version control
* GitHub repository management

## Configuration

Monitoring settings are stored in `config.ini` instead of being hard-coded into the Python script.

Example:

```ini
[monitoring]
disk_threshold = 80
cpu_threshold = 80
memory_threshold = 80
check_interval = 60
```

This allows monitoring behavior to be changed without modifying the Python code.

## How It Works

The Python script collects system information from the Linux server and evaluates the results against configurable thresholds.

If disk, CPU, or memory usage exceeds the configured threshold, the system generates an alert.

The system also checks the Nginx service.

If Nginx is not running, the monitoring system:

1. Detects the service failure
2. Logs the failure
3. Attempts to restart Nginx
4. Reports whether recovery was successful

Monitoring information is also written to a log file for later review.

## Technologies Used

* Linux / Ubuntu
* Python 3
* Nginx
* Git
* GitHub
* UFW Firewall
* Python `configparser`
* Linux command-line utilities

## Project Structure

```text
cloud/
├── server_info.py
├── config.ini
├── README.md
├── .gitignore
└── server_monitor.log
```

## What I Learned

* Linux system administration
* Linux service management
* Python system automation
* CPU, memory, and disk monitoring
* Error handling
* Automatic service recovery
* Configuration management
* Log management
* Firewall configuration
* Git version control
* GitHub repository management

## Future Improvements

Planned improvements include:

* Deploying the monitoring system to AWS or Azure
* Adding cloud monitoring with CloudWatch or Azure Monitor
* Adding infrastructure security controls
* Adding automated deployment
* Adding infrastructure as code with Terraform
* Building CI/CD automation
* Expanding monitoring and alerting capabilities

## Goal

This project is part of a hands-on cloud engineering portfolio designed to demonstrate practical Linux administration, automation, monitoring, troubleshooting, and cloud infrastructure skills.

## Reliability revision

Requires Linux, Python 3.10+, `free`, and systemd for Nginx status checks.
Run with `python3 server_info.py`. Configuration is resolved beside the script;
`server_monitor.log` remains relative to the working directory, with the existing
log format and resource thresholds preserved.

Each collector has its own error boundary. Failed checks report `unknown` and do
not suppress the other checks. CPU utilization is sampled over 0.2 seconds using
aggregate `/proc/stat` deltas (idle includes I/O wait; guest counters are not
counted twice). Memory retains the existing `free -m` used/total calculation.
Subprocesses time out after 10 seconds; service start attempts after 30 seconds.

### Recovery safety

Automatic recovery is **disabled by default**. The existing `config.ini` works
without changes. Optional settings in its `[monitoring]` section are:

```ini
auto_recovery = false
recovery_max_attempts = 3
recovery_cooldown = 300
```

Do not enable recovery without operational approval. When enabled, the monitor
uses `systemctl --no-ask-password start nginx`, never sudo. Existing service
manager authorization must permit this operation; this project does not change
permissions. Only confirmed `inactive` or `failed` states trigger recovery.
Unknown and transitional states do not. Each attempt consumes the budget even
if the command fails, with at most one start per cooldown period. The budget is
per monitor process lifetime and is not replenished by successful checks;
restarting the monitor resets it. Run only one monitor instance. A durable,
cross-process recovery budget remains a deployment improvement.

After every start attempt, including timeouts and command failures, the monitor
checks Nginx status up to three times, one second apart. Only a verified `active`
state is logged as recovery success. Status-query errors report `unknown`.
Verification establishes systemd service activity, not HTTP application health.

### Automated validation

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Tests mock all subprocess calls and sleeps; they never operate on services.
Coverage includes independent check failures, command timeouts, invalid counters,
configuration validation, verified and failed recovery, cooldown, and exhaustion.

Before production deployment, add service supervision and singleton enforcement,
log rotation/retention, external HTTP probes and alert delivery, and deployment
validation under the intended unprivileged account. Live recovery validation
requires separate approval. The sequential sampling loop adds collection time to
the configured sleep interval.

## Local HTTP health check

Each monitoring cycle independently sends `GET /` to `http://127.0.0.1:80`.
Only HTTP 200 is healthy; redirects and all other status codes are logged as
`HTTP FAILURE:` events, separately from `SERVICE FAILURE:` events. Connection
errors, timeouts, and malformed HTTP responses are also logged as HTTP failures.
The existing cycle summary now includes an `HTTP` field.

Configure the socket timeout in seconds in the existing `[monitoring]` section:

```ini
http_timeout = 5.0
```

The timeout must be finite and positive. Older configurations without this key
use 5 seconds. It bounds blocking socket operations, not the total duration of a
response whose headers arrive slowly in successive chunks. The check examines
response headers without downloading the body, closes connections, bypasses
proxy settings, and does not follow redirects. No additional dependencies are
required.

HTTP failures never initiate service recovery. The separate systemd check retains
its existing opt-in, limited recovery behavior; automatic recovery remains off by
default. A local HTTP 200 establishes that an HTTP listener answered successfully,
not proof of process identity, response content, or external reachability.

HTTP tests mock network access and cover success, unexpected status codes,
timeouts, connection/protocol errors, cleanup, configuration validation, check
isolation, and the absence of HTTP-triggered recovery. Run the complete suite with
the unittest command above. Live endpoint validation is separate from unit tests.
