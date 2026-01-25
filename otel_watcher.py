#!/usr/bin/env python3
"""
OTEL Collector Watcher

Monitors the DNS failover record and starts/stops the OTEL collector
based on whether this site (DR) owns the DNS record.

When DNS points to DR IP → Start OTEL collector (scrape BIG-IPs)
When DNS points elsewhere → Stop OTEL collector (stay idle)

This ensures only ONE site is collecting telemetry at a time.
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

# OTEL collector command
OTEL_COMMAND = os.getenv('OTEL_COMMAND', 'otelcol-contrib --config /etc/otel/config.yaml')

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
# OTEL Collector Management
# -----------------------------

class OTELCollector:
    def __init__(self):
        self.process = None
        self.running = False
    
    def start(self):
        """Start the OTEL collector process."""
        if self.running:
            return
        
        try:
            log(f"Starting OTEL collector: {OTEL_COMMAND}")
            
            # Split command into args
            args = OTEL_COMMAND.split()
            
            self.process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid  # Create new process group for clean shutdown
            )
            self.running = True
            log(f"OTEL collector started (PID: {self.process.pid})")
            
        except Exception as e:
            log(f"Failed to start OTEL collector: {e}", "ERROR")
            self.running = False
    
    def stop(self):
        """Stop the OTEL collector process gracefully."""
        if not self.running or not self.process:
            return
        
        try:
            log("Stopping OTEL collector...")
            
            # Send SIGTERM to process group
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            
            # Wait up to 10 seconds for graceful shutdown
            try:
                self.process.wait(timeout=10)
                log("OTEL collector stopped gracefully")
            except subprocess.TimeoutExpired:
                # Force kill if still running
                log("OTEL collector didn't stop, sending SIGKILL", "WARN")
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                self.process.wait()
                log("OTEL collector killed")
            
        except Exception as e:
            log(f"Error stopping OTEL collector: {e}", "ERROR")
        finally:
            self.process = None
            self.running = False
    
    def is_running(self) -> bool:
        """Check if collector process is still running."""
        if not self.process:
            return False
        return self.process.poll() is None

# -----------------------------
# Main Loop
# -----------------------------

def main():
    log("="*60)
    log("OTEL Collector Watcher Starting")
    log("="*60)
    
    # Validate config
    if not MY_IP:
        log("ERROR: MY_IP or DR_IP environment variable must be set", "ERROR")
        log("This should be the IP address of THIS site (DR)")
        sys.exit(1)
    
    log(f"DNS Record:     {DNS_RECORD}")
    log(f"My IP (DR):     {MY_IP}")
    log(f"Check Interval: {CHECK_INTERVAL}s")
    log(f"OTEL Command:   {OTEL_COMMAND}")
    if DNS_SERVER:
        log(f"DNS Server:     {DNS_SERVER}")
    log("="*60)
    
    collector = OTELCollector()
    
    # Handle shutdown signals
    def shutdown(signum, frame):
        log("Shutdown signal received")
        collector.stop()
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
            
            # Log state changes
            if should_be_active != last_state:
                if should_be_active:
                    log(f"DNS points to us ({current_ip}) - ACTIVATING", "INFO")
                else:
                    log(f"DNS points elsewhere ({current_ip}) - DEACTIVATING", "INFO")
                last_state = should_be_active
            
            # Start or stop collector based on state
            if should_be_active:
                if not collector.is_running():
                    collector.start()
            else:
                if collector.is_running():
                    collector.stop()
            
            # Check if collector died unexpectedly while we should be active
            if should_be_active and not collector.is_running():
                log("OTEL collector died unexpectedly, restarting...", "WARN")
                collector.start()
            
        except Exception as e:
            log(f"Error in main loop: {e}", "ERROR")
        
        time.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    main()
