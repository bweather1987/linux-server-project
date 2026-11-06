"""Independent Linux health checks with bounded, opt-in service recovery."""

import argparse
import configparser
from contextlib import contextmanager
import fcntl
from dataclasses import dataclass
import http.client
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.ini"
COMMAND_TIMEOUT = 10
RECOVERY_TIMEOUT = 30
CPU_SAMPLE_SECONDS = 0.2
VERIFY_ATTEMPTS = 3
VERIFY_DELAY = 1


@dataclass(frozen=True)
class Settings:
    disk_threshold: int
    cpu_threshold: int
    memory_threshold: int
    check_interval: int
    auto_recovery: bool = False
    recovery_max_attempts: int = 3
    recovery_cooldown: int = 300
    http_timeout: float = 5.0
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 5


def load_config(path=CONFIG_PATH):
    config = configparser.ConfigParser()
    if not config.read(path):
        raise ValueError(f"Cannot read configuration: {path}")
    section = config["monitoring"]
    settings = Settings(
        **{key: section.getint(key) for key in (
            "disk_threshold", "cpu_threshold", "memory_threshold", "check_interval"
        )},
        auto_recovery=section.getboolean("auto_recovery", fallback=False),
        recovery_max_attempts=section.getint("recovery_max_attempts", fallback=3),
        recovery_cooldown=section.getint("recovery_cooldown", fallback=300),
        http_timeout=section.getfloat("http_timeout", fallback=5.0),
        log_max_bytes=config.getint("logging", "max_bytes", fallback=5 * 1024 * 1024),
        log_backup_count=config.getint("logging", "backup_count", fallback=5),
    )
    for threshold in (settings.disk_threshold, settings.cpu_threshold,
                      settings.memory_threshold):
        if threshold is None or not 0 <= threshold <= 100:
            raise ValueError("Thresholds must be between 0 and 100")
    for value in (settings.check_interval, settings.recovery_max_attempts,
                  settings.recovery_cooldown, settings.log_max_bytes, settings.log_backup_count):
        if value is None or value <= 0:
            raise ValueError("Intervals and recovery limits must be positive")
    if not math.isfinite(settings.http_timeout) or settings.http_timeout <= 0:
        raise ValueError("http_timeout must be finite and positive")
    return settings


def run_command(args, *, timeout=COMMAND_TIMEOUT, check=True):
    return subprocess.run(
        args, capture_output=True, text=True, stdin=subprocess.DEVNULL,
        timeout=timeout, check=check,
        env={**os.environ, "LC_ALL": "C"},
    )


def percentage(used, total):
    if total <= 0 or not 0 <= used <= total:
        raise ValueError("Invalid resource counters")
    result = 100 * used / total
    if not math.isfinite(result):
        raise ValueError("Non-finite resource measurement")
    return result


def send_alert(message, *args, level=logging.WARNING, exc_info=False):
    """Emit one local console/log alert; this sends no external notifications."""
    text = message % args if args else message
    print(f"ALERT: {text}")
    logging.log(level, "ALERT: %s", text, exc_info=exc_info)


def collect_uptime():
    with open("/proc/uptime", encoding="ascii") as source:
        fields = source.readline().split()
    if not fields:
        raise ValueError("Missing uptime counter")
    seconds = float(fields[0])
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Invalid uptime counter")
    return seconds


def format_uptime(seconds):
    if seconds is None:
        return "unknown"
    days, remainder = divmod(int(seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    return f"{days}d {hours}h {remainder // 60}m"


def collect_disk():
    total, used, _ = shutil.disk_usage("/")
    return percentage(used, total)


def read_cpu_counters():
    with open("/proc/stat", encoding="ascii") as source:
        fields = source.readline().split()
    if not fields or fields[0] != "cpu" or len(fields) < 5:
        raise ValueError("Missing aggregate CPU counters")
    # guest and guest_nice are already included in user/nice; exclude them.
    counters = [int(value) for value in fields[1:9]]
    if any(value < 0 for value in counters):
        raise ValueError("Negative CPU counter")
    idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
    return sum(counters), idle


def collect_cpu():
    before_total, before_idle = read_cpu_counters()
    time.sleep(CPU_SAMPLE_SECONDS)
    after_total, after_idle = read_cpu_counters()
    elapsed = after_total - before_total
    idle = after_idle - before_idle
    if idle < 0:
        raise ValueError("CPU counters moved backwards")
    return percentage(elapsed - idle, elapsed)


def collect_memory():
    output = run_command(["free", "-m"]).stdout
    for line in output.splitlines():
        fields = line.split()
        if fields and fields[0] == "Mem:":
            return percentage(int(fields[2]), int(fields[1]))
    raise ValueError("Missing memory counters")


def get_nginx_status():
    result = run_command(["systemctl", "is-active", "nginx"], check=False)
    status = result.stdout.strip()
    if result.returncode == 0 and status == "active":
        return status
    if result.returncode == 3 and status in {
        "inactive", "failed", "activating", "deactivating", "reloading",
    }:
        return status
    raise RuntimeError(
        f"Cannot determine Nginx status: exit={result.returncode}, "
        f"stderr={result.stderr.strip()}"
    )


@dataclass
class RecoveryState:
    """Process-lifetime budget; successful checks do not replenish attempts."""

    attempts: int = 0
    last_attempt: float | None = None


def check_nginx(settings, recovery):
    status = get_nginx_status()
    if status == "active":
        logging.info("Nginx is running normally.")
        return status
    if status not in {"inactive", "failed"}:
        send_alert("Nginx is in transitional state: %s", status)
        return status
    send_alert("SERVICE FAILURE: Nginx is %s", status, level=logging.ERROR)
    if not settings.auto_recovery:
        send_alert("Automatic Nginx recovery is disabled")
        return status
    now = time.monotonic()
    if recovery.attempts >= settings.recovery_max_attempts:
        send_alert("Nginx recovery attempt budget exhausted")
        return status
    if (recovery.last_attempt is not None
            and now - recovery.last_attempt < settings.recovery_cooldown):
        send_alert("Nginx recovery cooldown is active")
        return status
    recovery.attempts += 1
    recovery.last_attempt = now
    try:
        # Never invoke sudo or prompt for interactive authorization.
        run_command(["systemctl", "--no-ask-password", "start", "nginx"],
                    timeout=RECOVERY_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        send_alert("Nginx recovery command failed: %s", exc, level=logging.ERROR)
    # A failed/timed-out command may still have started the service.
    for attempt in range(VERIFY_ATTEMPTS):
        status = get_nginx_status()
        if status == "active":
            logging.info("Nginx service recovered successfully.")
            return status
        if attempt < VERIFY_ATTEMPTS - 1:
            time.sleep(VERIFY_DELAY)
    send_alert("Nginx recovery not confirmed; final status: %s", status, level=logging.ERROR)
    return status


@dataclass(frozen=True)
class HttpResult:
    """HTTP outcome and optional completed request-to-headers latency."""

    status: str
    response_time_ms: float | None = None


def check_http(settings):
    """Measure request-to-headers latency; never redirect, proxy, or recover."""
    connection = http.client.HTTPConnection("127.0.0.1", timeout=settings.http_timeout)
    started = time.monotonic()
    try:
        connection.request("GET", "/", headers={"Connection": "close"})
        response = connection.getresponse()
        try:
            elapsed_ms = (time.monotonic() - started) * 1000
            status = response.status
        finally:
            response.close()
        print(f"HTTP status: {status} | Response time: {elapsed_ms:.2f} ms")
        if status != 200:
            send_alert("HTTP FAILURE: http://127.0.0.1 returned HTTP %s | Response time: %.2f ms",
                       status, elapsed_ms, level=logging.ERROR)
        else:
            logging.info("Nginx HTTP health is normal: HTTP 200 | Response time: %.2f ms",
                         elapsed_ms)
        return HttpResult(f"HTTP {status}", elapsed_ms)
    except TimeoutError:
        send_alert("HTTP FAILURE: http://127.0.0.1 timed out (timeout=%ss)",
                   settings.http_timeout, level=logging.ERROR)
        return HttpResult("timeout")
    except (OSError, http.client.HTTPException) as exc:
        send_alert("HTTP FAILURE: http://127.0.0.1 request failed: %s", exc, level=logging.ERROR)
        return HttpResult("request failed")
    finally:
        connection.close()


def safe_check(name, collector):
    try:
        return collector()
    except Exception:
        # Isolation boundary: even unexpected collector bugs must not skip peers.
        send_alert("%s check failed", name, level=logging.ERROR, exc_info=True)
        return None


def format_percent(value):
    return "unknown" if value is None else f"{value:.1f}%"


def run_check(settings, recovery):
    uptime = safe_check("Uptime", collect_uptime)
    results = {"uptime_seconds": uptime}
    print(f"Server Uptime: {format_uptime(uptime)}")
    if uptime is not None:
        logging.info("Server uptime: %s", format_uptime(uptime))
    for name, collector, threshold in (
        ("Disk", collect_disk, settings.disk_threshold),
        ("CPU", collect_cpu, settings.cpu_threshold),
        ("Memory", collect_memory, settings.memory_threshold),
    ):
        value = safe_check(name, collector)
        results[name.lower()] = value
        print(f"{name} Used: {format_percent(value)}")
        if value is not None:
            if value > threshold:
                send_alert("%s usage is high: %.1f%%", name, value)
            else:
                logging.info("%s usage is normal: %.1f%%", name, value)
    results["nginx"] = safe_check("Nginx", lambda: check_nginx(settings, recovery))
    http = safe_check("HTTP", lambda: check_http(settings))
    results["http"] = http.status if http is not None else None
    results["http_response_ms"] = http.response_time_ms if http is not None else None
    logging.info(
        "Summary | CPU: %s | Memory: %s | Disk: %s | Nginx: %s | HTTP: %s "
        "| Uptime: %s | HTTP response: %s",
        format_percent(results["cpu"]), format_percent(results["memory"]),
        format_percent(results["disk"]), results["nginx"] or "unknown",
        results["http"] or "unknown", format_uptime(uptime),
        ("unknown" if results["http_response_ms"] is None
         else f"{results['http_response_ms']:.2f} ms"),
    )
    return results


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--log-file", type=Path, default=PROJECT_DIR / "server_monitor.log")
    parser.add_argument("--lock-file", type=Path, default=PROJECT_DIR / "server_monitor.lock")
    args = parser.parse_args(argv)
    # Explicit relative arguments are anchored to the script, never the caller's cwd.
    for name in ("config", "log_file", "lock_file"):
        value = getattr(args, name)
        if not value.is_absolute():
            value = PROJECT_DIR / value
        setattr(args, name, value.resolve())
    return args


@contextmanager
def instance_lock(path):
    """Keep the inode in place: unlinking lock files can permit duplicate owners."""
    with open(path, "a", encoding="ascii") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Another monitor holds the lock: {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def configure_logging(path, settings):
    handler = RotatingFileHandler(
        path, maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s"
    ))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def monitor_loop(settings):
    recovery = RecoveryState()
    check_number = 0
    try:
        while True:
            check_number += 1
            print(f"\n--- Server Check #{check_number} ---")
            run_check(settings, recovery)
            time.sleep(settings.check_interval)
    except KeyboardInterrupt:
        logging.info("Monitoring stopped")
        return 0


def main(argv=None):
    args = parse_args(argv)
    # Startup failures go to stderr/journald even before the file logger exists.
    try:
        settings = load_config(args.config)
    except (OSError, ValueError, KeyError, configparser.Error) as exc:
        print(f"Invalid monitoring configuration: {exc}", file=sys.stderr)
        return 1
    try:
        with instance_lock(args.lock_file):
            configure_logging(args.log_file, settings)
            logging.info("Monitor starting | Config: %s | Log: %s | Recovery: %s",
                         args.config, args.log_file, settings.auto_recovery)
            return monitor_loop(settings)
    except (OSError, RuntimeError) as exc:
        print(f"Monitor startup/runtime failure: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
