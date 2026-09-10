import shutil
import subprocess
import logging
import time
import configparser


check_number = 0

config = configparser.ConfigParser()
config.read("/home/brandon/cloud/config.ini")

DISK_THRESHOLD = config.getint("monitoring", "disk_threshold")
CPU_THRESHOLD = config.getint("monitoring", "cpu_threshold")
MEMORY_THRESHOLD = config.getint("monitoring", "memory_threshold")
CHECK_INTERVAL = config.getint("monitoring", "check_interval")


logging.basicConfig(
    filename="server_monitor.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def run_check():
	global check_number
	check_number += 1
	print(f"\n--- Server Check #{check_number} ---")


	total, used, free = shutil.disk_usage("/")


	used_percent = (used / total) * 100

	print("=== Server Information ===")


	print("Disk Used:", round(used_percent, 1), "%")


	if used_percent > DISK_THRESHOLD:
		print("🚨 ALERT: Disk usage is high!")
		logging.warning("Disk usage is high: %.1f%%", used_percent)
	else:
		print("Disk usage is normal.")
		logging.info("Disk usage is normal: %.1f%%", used_percent)

	try:
		cpu = subprocess.check_output(
			["bash", "-c", "top -bn1 | grep 'Cpu(s)'"]
		).decode()
	except subprocess.CalledProcessError as e:
		logging.error("CPU check failed: %s", e)
		return

	cpu_idle = float(cpu.split("id,")[0].split(",")[-1])
	cpu_percent = 100 - cpu_idle


	print("\n=== CPU ===")
	print("CPU Used:", round(cpu_percent, 1), "%")


	if cpu_percent > CPU_THRESHOLD:
		print("🚨 Alert: CPU usage is high!")
		logging.warning("CPU usage is high: %.1f%%", cpu_percent)
	else:
		print("CPU usage is normal.")
		logging.info("CPU usage is normal: %.1f%%", cpu_percent)

	memory = subprocess.check_output(["free", "-m"]).decode()
	memory_lines = memory.splitlines()

	mem_values = memory_lines[1].split()
	memory_total = int(mem_values[1])
	memory_used = int(mem_values[2])


	memory_percent = (memory_used / memory_total) * 100


	print("\n=== Memory ===")
	print("Memory Used:", round(memory_percent, 1), "%")


	if memory_percent > MEMORY_THRESHOLD:
		print("WARNING: Memory usage is high!")
		logging.warning("Memory usage is high: %.1f%%", memory_percent)
	else:
		print("Memory usage is normal.")
		logging.info("Memory usage is normal: %.1f%%", memory_percent)

	print("\n=== Nginx ===")


	nginx_status = subprocess.run(
		["systemctl", "is-active", "nginx"],
		capture_output=True,
		text=True
	).stdout.strip()


	print("Nginx Status", nginx_status)


	if nginx_status == "active":
		print("Nginx is running normally.")
		logging.info("Nginx is running normally.")
	else:
		print("🚨 ALERT: Nginx is not running!")
		logging.error("SERVICE FAILURE: Nginx is not running!")
		print("Attempting to restart Nginx...")
		

		restart_result = subprocess.run(
			["sudo", "service", "nginx", "start"],
			capture_output=True,
			text=True
		)

		
		if restart_result.returncode == 0:
			print("Nginx restart successful.")
			logging.info("Nginx service recovered successfully.")
		else:
			print("🚨 Nginx restart failed!")
			logging.error("Nginx restart failed: %s", restart_result.stderr.strip())

	logging.info(
		"Summary | CPU: %.1f%% | Memory: %.1f%% | Disk: %.1f%% | Nginx: %s",
		cpu_percent,
		memory_percent,
		used_percent,
		nginx_status
	)
while True:
	run_check()
	time.sleep(CHECK_INTERVAL)
