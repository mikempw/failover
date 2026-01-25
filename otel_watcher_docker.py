#!/usr/bin/env python3
"""
OTEL Collector Watcher for Docker

Monitors the DNS failover record and starts/stops the OTEL collector container
based on whether this site (DR) owns the DNS record.

When DNS points to DR IP → docker start otel-collector
When DNS points elsewhere → docker stop otel-collector

This ensures only ONE site is collecting telemetry at a time.

Requirements:
  - Mount Docker socket: -v /var/run/docker.sock:/var/run/docker.sock
  - OTEL collector container must exist (created but can be stopped)
"""

import os
import sys
import socket
import subprocess
import signal
import time
from datetime import datetime

# -----------------------------
# Configuration (from env)
# -----------------------------

# DNS record to watch (same as dns_failover uses)
DNS_RECORD = os.getenv('DNS_RECORD', 'syslog.ast.example.local')

# This site's IP - when DNS points here, we're active
MY_IP = os.getenv('DR_IP', os.getenv('MY_IP'))

# How often to check DNS (seconds)
CHECK_INTERVAL = int(os.getenv('OTEL_CHECK_INTERVAL', '15'))

# OTEL collector container name
OTEL_CONTAINER = os.getenv('OTEL_CONTAINER', 'otel-collector')

# Optional: DNS server to query (uses system resolver if not set)
DNS_SERVER = os.getenv('DNS_SERVER', '')

# -----------------------------
# Logging
# -----------------------------

def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] [otel-watcher] {msg}", flush=True)

# -----------------------------
# DNS Lookup
# -----------------------------

def get_dns_ip() -> str:
    """Resolve the failover DNS record to an IP address."""
    try:
        if DNS_SERVER:
            # Use dig if specific DNS server is configured
            result = subprocess.run(
                ['dig', f'@{DNS_SERVER}', DNS_RECORD, 'A', '+short'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().split('\n')[0]
        else:
            # Use system resolver
            ip = socket.gethostbyname(DNS_RECORD)
            return ip
    except Exception as e:
        log(f"DNS lookup failed: {e}", "WARN")
    return None

# -----------------------------
# Docker Container Management
# -----------------------------

def container_exists() -> bool:
    """Check if the OTEL container exists."""
    try:
        result = subprocess.run(
            ['docker', 'inspect', OTEL_CONTAINER],
            capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0
    except Exception as e:
        log(f"Error checking container: {e}", "ERROR")
        return False

def container_is_running() -> bool:
    """Check if the OTEL container is currently running."""
    try:
        result = subprocess.run(
            ['docker', 'inspect', '-f', '{{.State.Running}}', OTEL_CONTAINER],
            capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0 and result.stdout.strip() == 'true'
    except Exception as e:
        log(f"Error checking container state: {e}", "WARN")
        return False

def start_container() -> bool:
    """Start the OTEL collector container."""
    try:
        if container_is_running():
            log(f"Container {OTEL_CONTAINER} is already running")
            return True
        
        log(f"Starting container: {OTEL_CONTAINER}")
        result = subprocess.run(
            ['docker', 'start', OTEL_CONTAINER],
            capture_output=True, text=True, timeout=30
        )
        
        if result.returncode == 0:
            log(f"Container {OTEL_CONTAINER} started successfully")
            return True
        else:
            log(f"Failed to start container: {result.stderr}", "ERROR")
            return False
    except Exception as e:
        log(f"Error starting container: {e}", "ERROR")
        return False

def stop_container() -> bool:
    """Stop the OTEL collector container gracefully."""
    try:
        if not container_is_running():
            log(f"Container {OTEL_CONTAINER} is already stopped")
            return True
        
        log(f"Stopping container: {OTEL_CONTAINER}")
        result = subprocess.run(
            ['docker', 'stop', '-t', '10', OTEL_CONTAINER],  # 10s grace period
            capture_output=True, text=True, timeout=30
        )
        
        if result.returncode == 0:
            log(f"Container {OTEL_CONTAINER} stopped successfully")
            return True
        else:
            log(f"Failed to stop container: {result.stderr}", "ERROR")
            return False
    except Exception as e:
        log(f"Error stopping container: {e}", "ERROR")
        return False

# -----------------------------
# Main Loop
# -----------------------------

def main():
    log("="*60)
    log("OTEL Collector Watcher for Docker")
    log("="*60)
    
    # Validate config
    if not MY_IP:
        log("ERROR: MY_IP or DR_IP environment variable must be set", "ERROR")
        log("This should be the IP address of THIS site (DR)")
        sys.exit(1)
    
    log(f"DNS Record:      {DNS_RECORD}")
    log(f"My IP (DR):      {MY_IP}")
    log(f"Check Interval:  {CHECK_INTERVAL}s")
    log(f"OTEL Container:  {OTEL_CONTAINER}")
    if DNS_SERVER:
        log(f"DNS Server:      {DNS_SERVER}")
    log("="*60)
    
    # Verify Docker access and container exists
    if not container_exists():
        log(f"ERROR: Container '{OTEL_CONTAINER}' not found!", "ERROR")
        log("Create the container first (it can be stopped):")
        log(f"  docker create --name {OTEL_CONTAINER} otel/opentelemetry-collector-contrib ...")
        sys.exit(1)
    
    running = container_is_running()
    log(f"Container {OTEL_CONTAINER} is currently: {'RUNNING' if running else 'STOPPED'}")
    
    # Handle shutdown signals
    def shutdown(signum, frame):
        log("Shutdown signal received")
        sys.exit(0)
    
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    
    # Track state for logging
    last_state = None
    
    while True:
        try:
            # Check DNS
            current_ip = get_dns_ip()
            
            if current_ip is None:
                log("Could not resolve DNS - keeping current state", "WARN")
                time.sleep(CHECK_INTERVAL)
                continue
            
            # Determine if we should be active
            should_be_active = (current_ip == MY_IP)
            
            # Log state changes and take action
            if should_be_active != last_state:
                if should_be_active:
                    log(f"DNS points to us ({current_ip}) - ACTIVATING", "INFO")
                    start_container()
                else:
                    log(f"DNS points elsewhere ({current_ip}) - DEACTIVATING", "INFO")
                    stop_container()
                last_state = should_be_active
            
            # Check if container died unexpectedly while we should be active
            if should_be_active and not container_is_running():
                log("OTEL container stopped unexpectedly, restarting...", "WARN")
                start_container()
            
        except Exception as e:
            log(f"Error in main loop: {e}", "ERROR")
        
        time.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    main()
