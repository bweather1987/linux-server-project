import shutil
import subprocess
import logging
import time


check_number = 0


logging.basicConfig(
    filename="server_monitor.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - Check #%(check_number)s - %(message)s"
)

def run_check():
	global check_number
	check_number += 1
	print(f"\n--- Server Check #{check_number}, ---")


	total, used, free = shutil.disk_usage("/")


	used_percent = (used / total) * 100

	print("=== Server Information ===")


	print("Disk Used:", round(used_percent, 1), "%")


	if used_percent > 80:
		print("WARNING: Disk usage is high!")
		logging.warning("Disk usage is high: %.1f%%", used_percent)
	else:
		print("Disk usage is normal.")
		logging.info("Disk usage is normal: %.1f%%", used_percent)

	memory = subprocess.check_output(["free", "-m"]).decode()
	memory_lines = memory.splitlines()

	mem_values = memory_lines[1].split()
	memory_total = int(mem_values[1])
	memory_used = int(mem_values[2])


	memory_percent = (memory_used / memory_total) * 100


	print("\n=== Memory ===")
	print("Memory Used:", round(memory_percent, 1), "%")


	if memory_percent > 80:
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
		print("WARNING: Nginx is not running!")
		logging.warning("Nginx is not running!")

while True:
	run_check()
	time.sleep(60)
