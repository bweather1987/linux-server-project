#!/bin/bash

echo "===server check ==="
echo "user: $(whoami)"
echo "hostname: $(hostname)"
echo "uptime:"
uptime
echo "disk space:"
df -h
echo "memory:"
free -h
echo "Nginx Status"
systemctl is-active nginx
