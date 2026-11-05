"""Independent Linux health checks with bounded, opt-in service recovery."""

import configparser
from dataclasses import dataclass
import http.client
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import time

CONFIG_PATH = Path(__file__).resolve().with_name("config.ini")
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
    )
    for threshold in (settings.disk_threshold, settings.cpu_threshold,
                      settings.memory_threshold):
        if threshold is None or not 0 <= threshold <= 100:
            raise ValueError("Thresholds must be between 0 and 100")
    for value in (settings.check_interval, settings.recovery_max_attempts,
                  settings.recovery_cooldown):
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
        logging.warning("Nginx is in transitional state: %s", status)
        return status
    logging.error("SERVICE FAILURE: Nginx is %s", status)
    if not settings.auto_recovery:
        logging.warning("Automatic Nginx recovery is disabled")
        return status
    now = time.monotonic()
    if recovery.attempts >= settings.recovery_max_attempts:
        logging.warning("Nginx recovery attempt budget exhausted")
        return status
    if (recovery.last_attempt is not None
            and now - recovery.last_attempt < settings.recovery_cooldown):
        logging.warning("Nginx recovery cooldown is active")
        return status
    recovery.attempts += 1
    recovery.last_attempt = now
    try:
        # Never invoke sudo or prompt for interactive authorization.
        run_command(["systemctl", "--no-ask-password", "start", "nginx"],
                    timeout=RECOVERY_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        logging.error("Nginx recovery command failed: %s", exc)
    # A failed/timed-out command may still have started the service.
    for attempt in range(VERIFY_ATTEMPTS):
        status = get_nginx_status()
        if status == "active":
            logging.info("Nginx service recovered successfully.")
            return status
        if attempt < VERIFY_ATTEMPTS - 1:
            time.sleep(VERIFY_DELAY)
    logging.error("Nginx recovery not confirmed; final status: %s", status)
    return status


def check_http(settings):
    """Check local HTTP headers only; never redirect, proxy, or recover services."""
    connection = http.client.HTTPConnection("127.0.0.1", timeout=settings.http_timeout)
    try:
        connection.request("GET", "/", headers={"Connection": "close"})
        response = connection.getresponse()
        try:
            status = response.status
        finally:
            response.close()
        if status != 200:
            logging.error("HTTP FAILURE: http://127.0.0.1 returned HTTP %s", status)
            return f"HTTP {status}"
        logging.info("Nginx HTTP health is normal: HTTP 200")
        return "HTTP 200"
    except TimeoutError:
        logging.error("HTTP FAILURE: http://127.0.0.1 timed out (timeout=%ss)",
                      settings.http_timeout)
        return "timeout"
    except (OSError, http.client.HTTPException) as exc:
        logging.error("HTTP FAILURE: http://127.0.0.1 request failed: %s", exc)
        return "request failed"
    finally:
        connection.close()


def safe_check(name, collector):
    try:
        return collector()
    except Exception:
        # Isolation boundary: even unexpected collector bugs must not skip peers.
        logging.exception("%s check failed", name)
        return None


def format_percent(value):
    return "unknown" if value is None else f"{value:.1f}%"


def run_check(settings, recovery):
    results = {}
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
                logging.warning("%s usage is high: %.1f%%", name, value)
            else:
                logging.info("%s usage is normal: %.1f%%", name, value)
    results["nginx"] = safe_check("Nginx", lambda: check_nginx(settings, recovery))
    results["http"] = safe_check("HTTP", lambda: check_http(settings))
    logging.info(
        "Summary | CPU: %s | Memory: %s | Disk: %s | Nginx: %s | HTTP: %s",
        format_percent(results["cpu"]), format_percent(results["memory"]),
        format_percent(results["disk"]), results["nginx"] or "unknown",
        results["http"] or "unknown",
    )
    return results


def main():
    logging.basicConfig(
        filename="server_monitor.log", level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    try:
        settings = load_config()
    except (OSError, ValueError, KeyError, configparser.Error):
        logging.exception("Invalid monitoring configuration")
        return 1
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


if __name__ == "__main__":
    raise SystemExit(main())
