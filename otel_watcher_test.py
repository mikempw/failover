#!/usr/bin/env python3
"""
OTEL Collector Watcher - TEST VERSION

For local Docker testing. Reads from dry-run state file instead of DNS.
In production, use otel_watcher_k8s.py which does real DNS lookups.
"""

import os
import sys
import json
import signal
import time
from datetime import datetime

# Configuration
DNS_RECORD = os.getenv('DNS_RECORD', 'syslog.example.local')
MY_IP = os.getenv('MY_IP', os.getenv('DR_IP', '10.20.20.20'))
CHECK_INTERVAL = int(os.getenv('OTEL_CHECK_INTERVAL', '5'))
STATEFILE = os.getenv('DRYRUN_STATEFILE', '/shared/zone.json')

def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] [otel-watcher] {msg}", flush=True)

def get_current_ip() -> str:
    """Read current IP from dry-run state file."""
    try:
        with open(STATEFILE, 'r') as f:
            state = json.load(f)
        return state.get('A')
    except FileNotFoundError:
        log(f"State file not found: {STATEFILE}", "WARN")
        return None
    except Exception as e:
        log(f"Error reading state: {e}", "WARN")
        return None

def get_current_owner() -> str:
    """Read current owner from dry-run state file."""
    try:
        with open(STATEFILE, 'r') as f:
            state = json.load(f)
        txt = state.get('TXT', '')
        for part in txt.split():
            if part.startswith('owner='):
                return part.split('=')[1]
    except:
        pass
    return None

def main():
    log("="*60)
    log("OTEL Collector Watcher - TEST MODE")
    log("="*60)
    log(f"DNS Record:     {DNS_RECORD}")
    log(f"My IP (DR):     {MY_IP}")
    log(f"Check Interval: {CHECK_INTERVAL}s")
    log(f"State File:     {STATEFILE}")
    log("="*60)
    log("")
    log("This simulates what happens to OTEL collector during failover.")
    log("In production, the watcher would run: kubectl scale deployment otel-collector --replicas=0/1")
    log("")
    log("="*60)
    
    def shutdown(signum, frame):
        log("Shutdown signal received")
        sys.exit(0)
    
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    
    last_state = None
    otel_running = False
    
    while True:
        try:
            current_ip = get_current_ip()
            current_owner = get_current_owner()
            
            if current_ip is None:
                log("Waiting for state file...", "WARN")
                time.sleep(CHECK_INTERVAL)
                continue
            
            should_be_active = (current_ip == MY_IP)
            
            if should_be_active != last_state:
                log("")
                log("*" * 50)
                if should_be_active:
                    log(f"DNS points to US ({current_ip}) - owner={current_owner}")
                    log("ACTION: Starting OTEL Collector!")
                    log("  (In K8s: kubectl scale deployment otel-collector --replicas=1)")
                    otel_running = True
                else:
                    log(f"DNS points to PRIMARY ({current_ip}) - owner={current_owner}")
                    log("ACTION: Stopping OTEL Collector!")
                    log("  (In K8s: kubectl scale deployment otel-collector --replicas=0)")
                    otel_running = False
                log("*" * 50)
                log("")
                last_state = should_be_active
            else:
                # Periodic status
                status = "RUNNING" if otel_running else "STOPPED"
                log(f"DNS={current_ip} owner={current_owner} | DR OTEL: {status}")
            
        except Exception as e:
            log(f"Error: {e}", "ERROR")
        
        time.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    main()
