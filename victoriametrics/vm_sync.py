#!/usr/bin/env python3
"""
VM-Sync - VictoriaMetrics Data Synchronization Daemon

Ensures data parity between primary and DR VictoriaMetrics instances.
Works with DNS-based failover to determine sync direction.

Sync Logic:
  1. Query both VMs for sample counts over 1-hour window (5-min buckets)
  2. Use DNS to determine active site (source of truth)
  3. Compare counts - sync from higher to lower
  4. Track state for failback readiness notification

Environment-driven configuration for containers.
"""

import json
import os
import signal
import socket
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple, Any


# -----------------------------
# Logging
# -----------------------------

LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
LOG_LEVELS = {'DEBUG': 0, 'INFO': 1, 'WARN': 2, 'ERROR': 3}


def log(msg: str, level: str = "INFO"):
    if LOG_LEVELS.get(level, 1) >= LOG_LEVELS.get(LOG_LEVEL, 1):
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{ts}] [{level}] {msg}", flush=True)


# -----------------------------
# Configuration
# -----------------------------

@dataclass
class Config:
    role: str
    local_vm_url: str
    remote_vm_url: str
    dns_record: str
    dns_server: Optional[str]
    primary_ip: str
    dr_ip: str
    check_interval: int
    query_window: int
    query_step: int
    gap_threshold: float
    chunk_size: int
    failback_clean_checks: int
    state_file: str
    notify_webhook: Optional[str]
    notify_on_gap: bool
    notify_on_sync: bool
    notify_on_failback_ready: bool

    @classmethod
    def from_env(cls):
        def get(key: str, default: Any = None) -> Any:
            return os.getenv(key, default)

        def get_int(key: str, default: int) -> int:
            return int(get(key, default))

        def get_float(key: str, default: float) -> float:
            return float(get(key, default))

        def get_bool(key: str, default: bool = False) -> bool:
            val = get(key, str(default))
            return str(val).lower() in ('true', '1', 'yes')

        return cls(
            role=get('ROLE', 'primary'),
            local_vm_url=get('LOCAL_VM_URL', 'http://localhost:8428').rstrip('/'),
            remote_vm_url=get('REMOTE_VM_URL', 'http://localhost:8429').rstrip('/'),
            dns_record=get('DNS_RECORD', 'failover.example.com'),
            dns_server=get('DNS_SERVER'),
            primary_ip=get('PRIMARY_IP', '10.10.10.10'),
            dr_ip=get('DR_IP', '10.20.20.10'),
            check_interval=get_int('CHECK_INTERVAL', 120),
            query_window=get_int('QUERY_WINDOW', 3600),
            query_step=get_int('QUERY_STEP', 300),
            gap_threshold=get_float('GAP_THRESHOLD', 0.9),
            chunk_size=get_int('CHUNK_SIZE', 300),
            failback_clean_checks=get_int('FAILBACK_CLEAN_CHECKS', 3),
            state_file=get('STATE_FILE', '/state/vm-sync-state.json'),
            notify_webhook=get('NOTIFY_WEBHOOK'),
            notify_on_gap=get_bool('NOTIFY_ON_GAP', True),
            notify_on_sync=get_bool('NOTIFY_ON_SYNC', True),
            notify_on_failback_ready=get_bool('NOTIFY_ON_FAILBACK_READY', True),
        )

    def validate(self):
        if self.role not in ('primary', 'dr'):
            raise ValueError(f"ROLE must be 'primary' or 'dr', got '{self.role}'")
        if not self.local_vm_url:
            raise ValueError("LOCAL_VM_URL is required")
        if not self.remote_vm_url:
            raise ValueError("REMOTE_VM_URL is required")


# -----------------------------
# State Management
# -----------------------------

@dataclass
class SyncState:
    last_check: Optional[str] = None
    last_sync: Optional[str] = None
    consecutive_clean: int = 0
    failback_ready: bool = False
    active_site: Optional[str] = None
    gaps_detected: int = 0
    samples_synced: int = 0
    last_error: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            'last_check': self.last_check,
            'last_sync': self.last_sync,
            'consecutive_clean': self.consecutive_clean,
            'failback_ready': self.failback_ready,
            'active_site': self.active_site,
            'gaps_detected': self.gaps_detected,
            'samples_synced': self.samples_synced,
            'last_error': self.last_error,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'SyncState':
        return cls(
            last_check=data.get('last_check'),
            last_sync=data.get('last_sync'),
            consecutive_clean=data.get('consecutive_clean', 0),
            failback_ready=data.get('failback_ready', False),
            active_site=data.get('active_site'),
            gaps_detected=data.get('gaps_detected', 0),
            samples_synced=data.get('samples_synced', 0),
            last_error=data.get('last_error'),
        )


def load_state(path: str) -> SyncState:
    try:
        with open(path, 'r') as f:
            return SyncState.from_dict(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return SyncState()


def save_state(path: str, state: SyncState):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(state.to_dict(), f, indent=2)


# -----------------------------
# DNS Utilities
# -----------------------------

def get_dns_ip(record: str, server: Optional[str] = None) -> Optional[str]:
    """Resolve DNS record to IP address."""
    try:
        if server:
            import subprocess
            result = subprocess.run(
                ['dig', '+short', f'@{server}', record, 'A'],
                capture_output=True, text=True, timeout=5
            )
            ip = result.stdout.strip().split('\n')[0]
            return ip if ip else None
        else:
            return socket.gethostbyname(record)
    except Exception as e:
        log(f"DNS lookup failed for {record}: {e}", "WARN")
        return None


def get_active_site(cfg: Config) -> Optional[str]:
    """Determine which site is active based on DNS."""
    ip = get_dns_ip(cfg.dns_record, cfg.dns_server)
    if ip == cfg.primary_ip:
        return 'primary'
    elif ip == cfg.dr_ip:
        return 'dr'
    else:
        log(f"DNS returned unexpected IP: {ip}", "WARN")
        return None


# -----------------------------
# VictoriaMetrics API
# -----------------------------

def vm_query(base_url: str, query: str, start: int, end: int, step: int) -> Dict[int, int]:
    """
    Query VictoriaMetrics for sample counts per time bucket.
    Returns dict of {timestamp: count}
    """
    url = f"{base_url}/api/v1/query_range"
    params = {
        'query': query,
        'start': start,
        'end': end,
        'step': f'{step}s',
    }

    try:
        full_url = f"{url}?{urllib.parse.urlencode(params)}"
        log(f"Querying: {base_url} from {start} to {end}", "DEBUG")

        req = urllib.request.Request(full_url, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        if data.get('status') != 'success':
            log(f"VM query failed: {data}", "WARN")
            return {}

        results = {}
        for result in data.get('data', {}).get('result', []):
            for ts, val in result.get('values', []):
                timestamp = int(float(ts))
                count = int(float(val))
                results[timestamp] = results.get(timestamp, 0) + count

        return results

    except urllib.error.URLError as e:
        log(f"VM query error ({base_url}): {e}", "WARN")
        return {}
    except Exception as e:
        log(f"VM query exception ({base_url}): {e}", "ERROR")
        return {}


def vm_export(base_url: str, start: int, end: int) -> bytes:
    """Export all metrics from VictoriaMetrics for a time range."""
    url = f"{base_url}/api/v1/export"
    params = {
        'match[]': '{__name__!=""}',
        'start': start,
        'end': end,
    }

    try:
        full_url = f"{url}?{urllib.parse.urlencode(params)}"
        log(f"Exporting: {base_url} [{start} -> {end}]", "DEBUG")

        req = urllib.request.Request(full_url)
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()

    except Exception as e:
        log(f"VM export error: {e}", "ERROR")
        return b''


def vm_import(base_url: str, data: bytes) -> bool:
    """Import metrics into VictoriaMetrics."""
    url = f"{base_url}/api/v1/import"

    if not data:
        return True

    try:
        log(f"Importing: {len(data)} bytes to {base_url}", "DEBUG")
        req = urllib.request.Request(
            url,
            data=data,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            if resp.status == 204 or resp.status == 200:
                return True
            log(f"VM import unexpected status: {resp.status}", "WARN")
            return False

    except Exception as e:
        log(f"VM import error: {e}", "ERROR")
        return False


def vm_health(base_url: str) -> bool:
    """Check if VictoriaMetrics is healthy."""
    try:
        url = f"{base_url}/health"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


# -----------------------------
# Sync Logic
# -----------------------------

def get_sample_counts(cfg: Config, vm_url: str, start: int, end: int) -> Dict[int, int]:
    """Get sample counts per bucket for a VM."""
    # Use 'up' metric as canary - lightweight and always present
    query = 'count(count_over_time(up[5m]))'
    return vm_query(vm_url, query, start, end, cfg.query_step)


def find_gaps(
    cfg: Config,
    source_counts: Dict[int, int],
    dest_counts: Dict[int, int]
) -> List[int]:
    """
    Find timestamps where dest has significantly fewer samples than source.
    Returns list of timestamps that need syncing.
    """
    gaps = []
    for ts, source_count in source_counts.items():
        dest_count = dest_counts.get(ts, 0)

        if source_count == 0:
            continue

        ratio = dest_count / source_count if source_count > 0 else 0

        if ratio < cfg.gap_threshold:
            log(f"Gap detected at {ts}: source={source_count}, dest={dest_count} ({ratio:.1%})", "DEBUG")
            gaps.append(ts)

    return sorted(gaps)


def merge_consecutive(timestamps: List[int], step: int) -> List[Tuple[int, int]]:
    """Merge consecutive timestamps into ranges (start, end)."""
    if not timestamps:
        return []

    ranges = []
    start = timestamps[0]
    end = timestamps[0] + step

    for ts in timestamps[1:]:
        if ts <= end:
            end = ts + step
        else:
            ranges.append((start, end))
            start = ts
            end = ts + step

    ranges.append((start, end))
    return ranges


def sync_range(source_url: str, dest_url: str, start: int, end: int) -> int:
    """Sync a time range from source to dest. Returns bytes synced."""
    data = vm_export(source_url, start, end)
    if data:
        if vm_import(dest_url, data):
            return len(data)
    return 0


def determine_sync_direction(
    cfg: Config,
    active_site: str,
    local_counts: Dict[int, int],
    remote_counts: Dict[int, int]
) -> Tuple[str, str, Dict[int, int], Dict[int, int]]:
    """
    Determine which VM is source and which is destination.
    Primary logic: Active site is source of truth.
    Secondary: If counts disagree, higher count wins.

    Returns: (source_url, dest_url, source_counts, dest_counts)
    """
    # Determine which VM is active based on DNS
    if active_site == cfg.role:
        # We are the active site - local is source
        return (cfg.local_vm_url, cfg.remote_vm_url, local_counts, remote_counts)
    else:
        # Remote is the active site - remote is source
        return (cfg.remote_vm_url, cfg.local_vm_url, remote_counts, local_counts)


def run_sync_check(cfg: Config, state: SyncState) -> SyncState:
    """
    Run a single sync check cycle.
    Returns updated state.
    """
    now = int(time.time())
    start = now - cfg.query_window
    end = now

    state.last_check = datetime.now(timezone.utc).isoformat()

    # Check VM health
    local_healthy = vm_health(cfg.local_vm_url)
    remote_healthy = vm_health(cfg.remote_vm_url)

    if not local_healthy:
        log(f"Local VM unhealthy: {cfg.local_vm_url}", "WARN")
        state.last_error = "Local VM unhealthy"
        return state

    if not remote_healthy:
        log(f"Remote VM unhealthy: {cfg.remote_vm_url}", "WARN")
        state.last_error = "Remote VM unhealthy"
        # Can't sync but local is fine - not necessarily an error state
        return state

    # Get active site from DNS
    active_site = get_active_site(cfg)
    if not active_site:
        log("Could not determine active site from DNS", "WARN")
        state.last_error = "DNS lookup failed"
        return state

    state.active_site = active_site
    log(f"Active site: {active_site} (we are: {cfg.role})")

    # Get sample counts from both VMs
    log(f"Checking data parity for last {cfg.query_window}s...")
    local_counts = get_sample_counts(cfg, cfg.local_vm_url, start, end)
    remote_counts = get_sample_counts(cfg, cfg.remote_vm_url, start, end)

    if not local_counts and not remote_counts:
        log("No data from either VM - skipping sync check", "WARN")
        return state

    # Determine sync direction
    source_url, dest_url, source_counts, dest_counts = determine_sync_direction(
        cfg, active_site, local_counts, remote_counts
    )

    source_name = "local" if source_url == cfg.local_vm_url else "remote"
    dest_name = "local" if dest_url == cfg.local_vm_url else "remote"

    # Find gaps
    gaps = find_gaps(cfg, source_counts, dest_counts)

    if not gaps:
        log("No gaps detected - data is in sync")
        state.consecutive_clean += 1
        state.gaps_detected = 0
        state.last_error = None

        # Check for failback readiness
        if state.consecutive_clean >= cfg.failback_clean_checks:
            if not state.failback_ready:
                state.failback_ready = True
                log("=" * 60)
                log("FAILBACK READY: Data parity confirmed", "INFO")
                log(f"  Active site: {active_site}")
                log(f"  Clean checks: {state.consecutive_clean}")
                log("  Safe to initiate failback when desired")
                log("=" * 60)
                notify(cfg, "failback_ready", state)

        return state

    # Gaps found - need to sync
    state.consecutive_clean = 0
    state.failback_ready = False
    state.gaps_detected = len(gaps)

    log(f"Found {len(gaps)} gaps - syncing from {source_name} to {dest_name}")
    if cfg.notify_on_gap:
        notify(cfg, "gap_detected", state)

    # Merge consecutive gaps into ranges
    ranges = merge_consecutive(gaps, cfg.query_step)
    log(f"Merged into {len(ranges)} time ranges")

    # Sync each range
    total_bytes = 0
    for range_start, range_end in ranges:
        log(f"Syncing range: {range_start} -> {range_end}")

        # Chunk large ranges
        chunk_start = range_start
        while chunk_start < range_end:
            chunk_end = min(chunk_start + cfg.chunk_size, range_end)
            bytes_synced = sync_range(source_url, dest_url, chunk_start, chunk_end)
            total_bytes += bytes_synced
            chunk_start = chunk_end

    state.last_sync = datetime.now(timezone.utc).isoformat()
    state.samples_synced += total_bytes
    state.last_error = None

    log(f"Sync complete: {total_bytes} bytes transferred")
    if cfg.notify_on_sync:
        notify(cfg, "sync_complete", state)

    return state


# -----------------------------
# Notifications
# -----------------------------

def notify(cfg: Config, event: str, state: SyncState):
    """Send notification via webhook."""
    if not cfg.notify_webhook:
        return

    if event == "gap_detected" and not cfg.notify_on_gap:
        return
    if event == "sync_complete" and not cfg.notify_on_sync:
        return
    if event == "failback_ready" and not cfg.notify_on_failback_ready:
        return

    messages = {
        "gap_detected": f"⚠️ VM-Sync: {state.gaps_detected} data gaps detected. Active site: {state.active_site}",
        "sync_complete": f"✅ VM-Sync: Sync complete. Transferred data to restore parity.",
        "failback_ready": f"🟢 VM-Sync: FAILBACK READY. Data parity confirmed after {state.consecutive_clean} checks.",
    }

    message = messages.get(event, f"VM-Sync event: {event}")

    try:
        payload = json.dumps({"text": message}).encode('utf-8')
        req = urllib.request.Request(
            cfg.notify_webhook,
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            log(f"Notification sent: {event}", "DEBUG")
    except Exception as e:
        log(f"Failed to send notification: {e}", "WARN")


# -----------------------------
# Main Loop
# -----------------------------

def main():
    log("=" * 60)
    log("VM-Sync - VictoriaMetrics Data Synchronization")
    log("=" * 60)

    cfg = Config.from_env()

    try:
        cfg.validate()
    except ValueError as e:
        log(f"Configuration error: {e}", "ERROR")
        sys.exit(1)

    log(f"Role:           {cfg.role}")
    log(f"Local VM:       {cfg.local_vm_url}")
    log(f"Remote VM:      {cfg.remote_vm_url}")
    log(f"DNS Record:     {cfg.dns_record}")
    log(f"Check Interval: {cfg.check_interval}s")
    log(f"Query Window:   {cfg.query_window}s")
    log(f"Gap Threshold:  {cfg.gap_threshold:.0%}")
    log("=" * 60)

    # Load existing state
    state = load_state(cfg.state_file)
    log(f"Loaded state: consecutive_clean={state.consecutive_clean}, failback_ready={state.failback_ready}")

    # Signal handling
    shutdown = False

    def handle_signal(signum, frame):
        nonlocal shutdown
        log("Shutdown signal received")
        shutdown = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Main loop
    while not shutdown:
        try:
            state = run_sync_check(cfg, state)
            save_state(cfg.state_file, state)

        except Exception as e:
            log(f"Sync check failed: {e}", "ERROR")
            state.last_error = str(e)
            save_state(cfg.state_file, state)

        # Sleep until next check
        log(f"Next check in {cfg.check_interval}s")
        for _ in range(cfg.check_interval):
            if shutdown:
                break
            time.sleep(1)

    log("VM-Sync shutdown complete")


if __name__ == '__main__':
    main()
