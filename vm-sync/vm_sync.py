#!/usr/bin/env python3
"""
VictoriaMetrics Sync (vm-sync)

Syncs metrics from source VictoriaMetrics to destination.
Uses VM's native export/import APIs for efficient transfer.

Environment Variables:
  SOURCE_URL - Source VictoriaMetrics URL
  DEST_URL - Destination VictoriaMetrics URL
  SYNC_INTERVAL - Seconds between sync runs (default: 300)
  SYNC_LOOKBACK - How far back to sync in minutes (default: 30)
"""

import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
import json
from datetime import datetime

SOURCE_URL = os.getenv('SOURCE_URL', 'http://localhost:8428')
DEST_URL = os.getenv('DEST_URL', 'http://localhost:8428')
SYNC_INTERVAL = int(os.getenv('SYNC_INTERVAL', '300'))
SYNC_LOOKBACK = int(os.getenv('SYNC_LOOKBACK', '30'))

def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)

def test_connection(url: str, name: str) -> bool:
    try:
        req = urllib.request.Request(f"{url}/health")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                log(f"  {name} OK: {url}")
                return True
    except Exception as e:
        log(f"  {name} FAILED: {url} - {e}", "ERROR")
    return False

def get_metric_names(url: str) -> list:
    try:
        req = urllib.request.Request(f"{url}/api/v1/label/__name__/values")
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return data.get('data', [])
    except Exception as e:
        log(f"Failed to get metric names: {e}", "ERROR")
        return []

def sync_metrics():
    now = int(time.time())
    start_time = now - (SYNC_LOOKBACK * 60)
    
    log(f"Syncing last {SYNC_LOOKBACK} minutes of data...")
    
    # Get all metric names from source
    metrics = get_metric_names(SOURCE_URL)
    if not metrics:
        log("No metrics found on source")
        return
    
    log(f"Found {len(metrics)} metrics to sync")
    
    total_series = 0
    total_bytes = 0
    errors = 0
    
    for i, metric in enumerate(metrics):
        try:
            # Export this metric with match[] parameter
            export_url = f"{SOURCE_URL}/api/v1/export?match[]={urllib.parse.quote(metric)}&start={start_time}&end={now}"
            export_req = urllib.request.Request(export_url)
            
            with urllib.request.urlopen(export_req, timeout=120) as export_resp:
                data = export_resp.read()
                
                if not data or not data.strip():
                    continue
                
                lines = len(data.strip().split(b'\n'))
                
                # Import to destination
                import_url = f"{DEST_URL}/api/v1/import"
                import_req = urllib.request.Request(import_url, data=data, method='POST')
                import_req.add_header('Content-Type', 'application/x-ndjson')
                
                with urllib.request.urlopen(import_req, timeout=120) as import_resp:
                    total_series += lines
                    total_bytes += len(data)
                    
        except Exception as e:
            errors += 1
            if errors <= 3:
                log(f"Error syncing {metric}: {e}", "WARN")
            continue
        
        # Progress every 100 metrics
        if (i + 1) % 100 == 0:
            log(f"  Progress: {i+1}/{len(metrics)} metrics, {total_series} series")
    
    log(f"Synced {total_series} series ({total_bytes} bytes) from {len(metrics)} metrics ({errors} errors)")

def main():
    log("=" * 60)
    log("VictoriaMetrics Sync (vm-sync)")
    log("=" * 60)
    log(f"Source:     {SOURCE_URL}")
    log(f"Dest:       {DEST_URL}")
    log(f"Interval:   {SYNC_INTERVAL}s")
    log(f"Lookback:   {SYNC_LOOKBACK} min")
    log("=" * 60)
    
    log("Testing connections...")
    if not test_connection(SOURCE_URL, "Source"):
        log("Source connection failed", "ERROR")
        sys.exit(1)
    if not test_connection(DEST_URL, "Dest"):
        log("Dest connection failed", "ERROR")
        sys.exit(1)
    
    while True:
        start = time.time()
        
        try:
            sync_metrics()
        except Exception as e:
            log(f"Sync error: {e}", "ERROR")
        
        elapsed = time.time() - start
        log(f"Sync complete in {elapsed:.1f}s. Next in {SYNC_INTERVAL}s")
        
        time.sleep(SYNC_INTERVAL)

if __name__ == '__main__':
    main()
