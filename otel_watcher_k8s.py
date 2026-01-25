#!/usr/bin/env python3
"""
OTEL Collector Watcher for Kubernetes/K3s

Monitors the DNS failover record and scales the OTEL collector deployment
based on whether this site (DR) owns the DNS record.

When DNS points to DR IP → Scale OTEL deployment to 1 replica
When DNS points elsewhere → Scale OTEL deployment to 0 replicas

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

# Kubernetes settings
OTEL_NAMESPACE = os.getenv('OTEL_NAMESPACE', 'monitoring')
OTEL_DEPLOYMENT = os.getenv('OTEL_DEPLOYMENT', 'otel-collector')
OTEL_REPLICAS_ACTIVE = int(os.getenv('OTEL_REPLICAS_ACTIVE', '1'))
OTEL_REPLICAS_INACTIVE = int(os.getenv('OTEL_REPLICAS_INACTIVE', '0'))

# Optional: DNS server to query (uses system resolver if not set)
DNS_SERVER = os.getenv('DNS_SERVER', '')

# Method: 'kubectl' or 'client' (kubernetes python client)
K8S_METHOD = os.getenv('K8S_METHOD', 'kubectl')

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
# Kubernetes Operations
# -----------------------------

def scale_with_kubectl(replicas: int) -> bool:
    """Scale deployment using kubectl."""
    try:
        cmd = [
            'kubectl', 'scale', 'deployment', OTEL_DEPLOYMENT,
            f'--replicas={replicas}',
            '-n', OTEL_NAMESPACE
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            log(f"Scaled {OTEL_DEPLOYMENT} to {replicas} replicas")
            return True
        else:
            log(f"kubectl scale failed: {result.stderr}", "ERROR")
            return False
    except Exception as e:
        log(f"kubectl error: {e}", "ERROR")
        return False

def scale_with_client(replicas: int) -> bool:
    """Scale deployment using kubernetes Python client."""
    try:
        from kubernetes import client, config
        
        # Load in-cluster config (when running as pod) or kubeconfig
        try:
            config.load_incluster_config()
        except:
            config.load_kube_config()
        
        apps_v1 = client.AppsV1Api()
        
        # Patch the deployment
        body = {'spec': {'replicas': replicas}}
        apps_v1.patch_namespaced_deployment_scale(
            name=OTEL_DEPLOYMENT,
            namespace=OTEL_NAMESPACE,
            body=body
        )
        
        log(f"Scaled {OTEL_DEPLOYMENT} to {replicas} replicas")
        return True
        
    except ImportError:
        log("kubernetes client not installed, falling back to kubectl", "WARN")
        return scale_with_kubectl(replicas)
    except Exception as e:
        log(f"Kubernetes client error: {e}", "ERROR")
        return False

def scale_deployment(replicas: int) -> bool:
    """Scale the OTEL collector deployment."""
    if K8S_METHOD == 'client':
        return scale_with_client(replicas)
    else:
        return scale_with_kubectl(replicas)

def get_current_replicas() -> int:
    """Get current replica count."""
    try:
        cmd = [
            'kubectl', 'get', 'deployment', OTEL_DEPLOYMENT,
            '-n', OTEL_NAMESPACE,
            '-o', 'jsonpath={.spec.replicas}'
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip())
    except Exception as e:
        log(f"Failed to get replicas: {e}", "WARN")
    return -1

# -----------------------------
# Main Loop
# -----------------------------

def main():
    log("="*60)
    log("OTEL Collector Watcher for K3s/Kubernetes")
    log("="*60)
    
    # Validate config
    if not MY_IP:
        log("ERROR: MY_IP or DR_IP environment variable must be set", "ERROR")
        log("This should be the IP address of THIS site (DR)")
        sys.exit(1)
    
    log(f"DNS Record:       {DNS_RECORD}")
    log(f"My IP (DR):       {MY_IP}")
    log(f"Check Interval:   {CHECK_INTERVAL}s")
    log(f"K8s Namespace:    {OTEL_NAMESPACE}")
    log(f"K8s Deployment:   {OTEL_DEPLOYMENT}")
    log(f"Active Replicas:  {OTEL_REPLICAS_ACTIVE}")
    log(f"Inactive Replicas:{OTEL_REPLICAS_INACTIVE}")
    log(f"K8s Method:       {K8S_METHOD}")
    if DNS_SERVER:
        log(f"DNS Server:       {DNS_SERVER}")
    log("="*60)
    
    # Verify kubectl access
    current = get_current_replicas()
    if current < 0:
        log("WARNING: Could not connect to Kubernetes. Check permissions.", "WARN")
    else:
        log(f"Current {OTEL_DEPLOYMENT} replicas: {current}")
    
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
            
            # Log state changes
            if should_be_active != last_state:
                if should_be_active:
                    log(f"DNS points to us ({current_ip}) - ACTIVATING OTEL", "INFO")
                    scale_deployment(OTEL_REPLICAS_ACTIVE)
                else:
                    log(f"DNS points elsewhere ({current_ip}) - DEACTIVATING OTEL", "INFO")
                    scale_deployment(OTEL_REPLICAS_INACTIVE)
                last_state = should_be_active
            
        except Exception as e:
            log(f"Error in main loop: {e}", "ERROR")
        
        time.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    main()
