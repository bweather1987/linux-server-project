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
