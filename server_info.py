import shutil
import subprocess

total, used, free = shutil.disk_usage("/")


used_percent = (used / total) * 100

print("=== Server Information ===")


print("Disk Used:", round(used_percent, 1), "%")


if used_percent > 80:
	print("WARNING: Disk usage is high!")
else:
	print("Disk usage is normal.")

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
else:
	print("Memory usage is normal.")

print("\n=== Nginx ===")


nginx_status = subprocess.run(
	["systemctl", "is-active", "nginx"],
	capture_output=True,
	text=True
).stdout.strip()


print("Nginx Status", nginx_status)


if nginx_status == "active":
	print("Nginx is running normally.")
else:
	print("WARNING: Nginx is not running!")
