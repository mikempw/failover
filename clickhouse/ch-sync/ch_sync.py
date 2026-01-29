#!/usr/bin/env python3
"""
CH-Sync - ClickHouse Data Synchronization Daemon

Ensures data parity between primary and DR ClickHouse instances.
Works with DNS-based failover to determine sync direction.

Sync Logic:
  1. Discover all tables on both nodes (exclude system/temp)
  2. Compare partition counts via system.parts
  3. Sync missing partitions using INSERT...SELECT...remote()
  4. Track state for failback readiness notification

Assumes tables are partitioned by toYYYYMMDD(timestamp).
"""

import json
import os
import re
import signal
import socket
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple, Any, Set


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
    local_ch_url: str
    remote_ch_url: str
    local_ch_user: str
    local_ch_password: str
    remote_ch_user: str
    remote_ch_password: str
    remote_ch_host: str
    remote_ch_port: int
    dns_record: str
    dns_server: Optional[str]
    primary_ip: str
    dr_ip: str
    check_interval: int
    exclude_patterns: List[str]
    connect_timeout_ms: int
    max_insert_threads: int
    failback_clean_checks: int
    state_file: str
    notify_webhook: Optional[str]
    notify_on_gap: bool
    notify_on_sync: bool
    notify_on_failback_ready: bool
    notify_on_new_table: bool
    auto_create_tables: bool

    @classmethod
    def from_env(cls):
        def get(key: str, default: Any = None) -> Any:
            return os.getenv(key, default)

        def get_int(key: str, default: int) -> int:
            return int(get(key, default))

        def get_bool(key: str, default: bool = False) -> bool:
            val = get(key, str(default))
            return str(val).lower() in ('true', '1', 'yes')

        def get_list(key: str, default: str = "") -> List[str]:
            val = get(key, default)
            if not val:
                return []
            return [p.strip() for p in val.split(',') if p.strip()]

        return cls(
            role=get('ROLE', 'primary'),
            local_ch_url=get('LOCAL_CH_URL', 'http://localhost:8123').rstrip('/'),
            remote_ch_url=get('REMOTE_CH_URL', 'http://localhost:8124').rstrip('/'),
            local_ch_user=get('LOCAL_CH_USER', 'default'),
            local_ch_password=get('LOCAL_CH_PASSWORD', ''),
            remote_ch_user=get('REMOTE_CH_USER', 'default'),
            remote_ch_password=get('REMOTE_CH_PASSWORD', ''),
            remote_ch_host=get('REMOTE_CH_HOST', 'localhost'),
            remote_ch_port=get_int('REMOTE_CH_PORT', 9000),
            dns_record=get('DNS_RECORD', 'failover.example.com'),
            dns_server=get('DNS_SERVER'),
            primary_ip=get('PRIMARY_IP', '10.10.10.10'),
            dr_ip=get('DR_IP', '10.20.20.10'),
            check_interval=get_int('CHECK_INTERVAL', 300),
            exclude_patterns=get_list('CH_EXCLUDE_PATTERNS', 'system.*,INFORMATION_SCHEMA.*,information_schema.*,_*,*_temp,*_staging'),
            connect_timeout_ms=get_int('CONNECT_TIMEOUT_MS', 2000),
            max_insert_threads=get_int('MAX_INSERT_THREADS', 4),
            failback_clean_checks=get_int('FAILBACK_CLEAN_CHECKS', 3),
            state_file=get('STATE_FILE', '/state/ch-sync-state.json'),
            notify_webhook=get('NOTIFY_WEBHOOK'),
            notify_on_gap=get_bool('NOTIFY_ON_GAP', True),
            notify_on_sync=get_bool('NOTIFY_ON_SYNC', True),
            notify_on_failback_ready=get_bool('NOTIFY_ON_FAILBACK_READY', True),
            notify_on_new_table=get_bool('NOTIFY_ON_NEW_TABLE', True),
            auto_create_tables=get_bool('AUTO_CREATE_TABLES', False),
        )

    def validate(self):
        if self.role not in ('primary', 'dr'):
            raise ValueError(f"ROLE must be 'primary' or 'dr', got '{self.role}'")
        if not self.local_ch_url:
            raise ValueError("LOCAL_CH_URL is required")
        if not self.remote_ch_url:
            raise ValueError("REMOTE_CH_URL is required")


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
    tables_checked: int = 0
    tables_with_gaps: int = 0
    partitions_synced: int = 0
    rows_synced: int = 0
    tables_created: List[str] = field(default_factory=list)
    new_tables_detected: List[str] = field(default_factory=list)
    last_error: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            'last_check': self.last_check,
            'last_sync': self.last_sync,
            'consecutive_clean': self.consecutive_clean,
            'failback_ready': self.failback_ready,
            'active_site': self.active_site,
            'tables_checked': self.tables_checked,
            'tables_with_gaps': self.tables_with_gaps,
            'partitions_synced': self.partitions_synced,
            'rows_synced': self.rows_synced,
            'tables_created': self.tables_created,
            'new_tables_detected': self.new_tables_detected,
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
            tables_checked=data.get('tables_checked', 0),
            tables_with_gaps=data.get('tables_with_gaps', 0),
            partitions_synced=data.get('partitions_synced', 0),
            rows_synced=data.get('rows_synced', 0),
            tables_created=data.get('tables_created', []),
            new_tables_detected=data.get('new_tables_detected', []),
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
# ClickHouse HTTP API
# -----------------------------

def ch_query(base_url: str, query: str, user: str = 'default', password: str = '', 
             timeout: int = 60) -> List[Dict]:
    """
    Execute a ClickHouse query and return results as list of dicts.
    """
    url = f"{base_url}/"
    
    # Add FORMAT JSON to query if not present
    # Note: Check for ' FORMAT ' with spaces to avoid matching 'INFORMATION_SCHEMA'
    if ' FORMAT ' not in query.upper():
        query = query.strip().rstrip(';') + ' FORMAT JSON'
    
    try:
        data = query.encode('utf-8')
        req = urllib.request.Request(url, data=data, method='POST')
        req.add_header('Content-Type', 'text/plain')
        
        if user and password:
            import base64
            credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
            req.add_header('Authorization', f'Basic {credentials}')
        elif user:
            req.add_header('X-ClickHouse-User', user)
        
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode())
            return result.get('data', [])
    
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        log(f"ClickHouse query error: {error_body}", "ERROR")
        return []
    except Exception as e:
        log(f"ClickHouse query exception: {e}", "ERROR")
        return []


def ch_execute(base_url: str, query: str, user: str = 'default', password: str = '',
               timeout: int = 300) -> bool:
    """
    Execute a ClickHouse command (no results expected).
    """
    url = f"{base_url}/"
    
    try:
        data = query.encode('utf-8')
        req = urllib.request.Request(url, data=data, method='POST')
        req.add_header('Content-Type', 'text/plain')
        
        if user and password:
            import base64
            credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
            req.add_header('Authorization', f'Basic {credentials}')
        elif user:
            req.add_header('X-ClickHouse-User', user)
        
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True
    
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        log(f"ClickHouse execute error: {error_body}", "ERROR")
        return False
    except Exception as e:
        log(f"ClickHouse execute exception: {e}", "ERROR")
        return False


def ch_health(base_url: str) -> bool:
    """Check if ClickHouse is healthy."""
    try:
        url = f"{base_url}/ping"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


# -----------------------------
# Table Discovery
# -----------------------------

def matches_pattern(name: str, patterns: List[str]) -> bool:
    """Check if name matches any of the glob patterns."""
    for pattern in patterns:
        # Convert glob to regex
        regex = pattern.replace('.', r'\.').replace('*', '.*').replace('?', '.')
        if re.match(f'^{regex}$', name):
            return True
    return False


def discover_tables(cfg: Config, ch_url: str, user: str, password: str) -> Dict[str, Dict]:
    """
    Discover all tables and their partition info.
    Returns: {database.table: {partition_key: ..., engine: ...}}
    """
    query = """
    SELECT 
        database,
        name as table,
        engine,
        partition_key
    FROM system.tables 
    WHERE database NOT IN ('system', 'INFORMATION_SCHEMA', 'information_schema')
      AND engine LIKE '%MergeTree%'
    """
    
    rows = ch_query(ch_url, query, user, password)
    
    tables = {}
    for row in rows:
        db = row['database']
        tbl = row['table']
        full_name = f"{db}.{tbl}"
        
        # Check exclusion patterns
        if matches_pattern(full_name, cfg.exclude_patterns):
            log(f"Excluding table: {full_name}", "DEBUG")
            continue
        
        tables[full_name] = {
            'database': db,
            'table': tbl,
            'engine': row['engine'],
            'partition_key': row['partition_key'],
        }
    
    return tables


def get_partition_counts(ch_url: str, user: str, password: str, 
                         database: str, table: str) -> Dict[str, int]:
    """
    Get row counts per partition from system.parts.
    Returns: {partition: row_count}
    """
    query = f"""
    SELECT 
        partition,
        sum(rows) as row_count
    FROM system.parts 
    WHERE database = '{database}' 
      AND table = '{table}'
      AND active = 1
    GROUP BY partition
    ORDER BY partition
    """
    
    rows = ch_query(ch_url, query, user, password)
    
    return {row['partition']: int(row['row_count']) for row in rows}


def get_create_table_ddl(ch_url: str, user: str, password: str,
                         database: str, table: str) -> Optional[str]:
    """
    Get CREATE TABLE statement from ClickHouse.
    Returns the DDL string or None if failed.
    """
    query = f"SHOW CREATE TABLE {database}.{table}"
    
    try:
        url = f"{ch_url}/"
        data = query.encode('utf-8')
        req = urllib.request.Request(url, data=data, method='POST')
        req.add_header('Content-Type', 'text/plain')
        
        if user and password:
            import base64
            credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
            req.add_header('Authorization', f'Basic {credentials}')
        elif user:
            req.add_header('X-ClickHouse-User', user)
        
        with urllib.request.urlopen(req, timeout=60) as resp:
            ddl = resp.read().decode().strip()
            return ddl
    
    except Exception as e:
        log(f"Failed to get CREATE TABLE for {database}.{table}: {e}", "ERROR")
        return None


def create_table_from_remote(cfg: Config, database: str, table: str) -> bool:
    """
    Create a table locally by copying DDL from remote (active) site.
    Returns True if successful.
    """
    full_name = f"{database}.{table}"
    log(f"Auto-creating table: {full_name}")
    
    # Get DDL from remote
    ddl = get_create_table_ddl(
        cfg.remote_ch_url, cfg.remote_ch_user, cfg.remote_ch_password,
        database, table
    )
    
    if not ddl:
        log(f"Could not get DDL for {full_name}", "ERROR")
        return False
    
    log(f"Got DDL for {full_name}", "DEBUG")
    
    # Ensure database exists locally
    create_db_query = f"CREATE DATABASE IF NOT EXISTS {database}"
    if not ch_execute(cfg.local_ch_url, create_db_query, cfg.local_ch_user, cfg.local_ch_password):
        log(f"Failed to create database {database}", "ERROR")
        return False
    
    # Execute the CREATE TABLE on local
    if ch_execute(cfg.local_ch_url, ddl, cfg.local_ch_user, cfg.local_ch_password):
        log(f"Successfully created table: {full_name}")
        return True
    else:
        log(f"Failed to create table: {full_name}", "ERROR")
        return False


# -----------------------------
# Sync Logic
# -----------------------------

def find_partition_gaps(
    source_partitions: Dict[str, int],
    dest_partitions: Dict[str, int]
) -> List[Tuple[str, int, int]]:
    """
    Find partitions where dest is missing data.
    Returns: [(partition, source_count, dest_count), ...]
    """
    gaps = []
    
    for partition, source_count in source_partitions.items():
        dest_count = dest_partitions.get(partition, 0)
        
        # If dest has fewer rows, it's a gap
        if dest_count < source_count:
            gaps.append((partition, source_count, dest_count))
    
    return gaps


def sync_partition(cfg: Config, database: str, table: str, partition: str,
                   source_host: str, source_port: int, 
                   source_user: str, source_password: str) -> Tuple[bool, int]:
    """
    Sync a single partition from source to local.
    Uses DROP + INSERT pattern to ensure exact match (no duplicates).
    
    Returns: (success, rows_synced)
    """
    log(f"Syncing partition {partition} for {database}.{table}", "DEBUG")
    
    # Step 1: DROP existing partition to avoid duplicates
    drop_query = f"ALTER TABLE {database}.{table} DROP PARTITION '{partition}'"
    
    log(f"  Dropping partition {partition}...", "DEBUG")
    drop_success = ch_execute(
        cfg.local_ch_url,
        drop_query,
        cfg.local_ch_user,
        cfg.local_ch_password,
        timeout=60
    )
    
    if not drop_success:
        # Partition might not exist locally, which is fine - continue with insert
        log(f"  Partition {partition} drop returned error (may not exist, continuing)", "DEBUG")
    
    # Step 2: INSERT from remote
    sync_query = f"""
    INSERT INTO {database}.{table}
    SELECT * FROM remote(
        '{source_host}:{source_port}',
        '{database}.{table}',
        '{source_user}',
        '{source_password}'
    )
    WHERE _partition_id = '{partition}'
    SETTINGS
        connect_timeout_with_failover_ms = {cfg.connect_timeout_ms},
        max_insert_threads = {cfg.max_insert_threads}
    """
    
    log(f"  Copying partition {partition} from remote...", "DEBUG")
    success = ch_execute(
        cfg.local_ch_url, 
        sync_query, 
        cfg.local_ch_user, 
        cfg.local_ch_password,
        timeout=600  # 10 min timeout for large partitions
    )
    
    if success:
        # Get row count for the synced partition
        count_query = f"""
        SELECT sum(rows) as cnt FROM system.parts 
        WHERE database = '{database}' AND table = '{table}' 
          AND partition = '{partition}' AND active = 1
        """
        rows = ch_query(cfg.local_ch_url, count_query, cfg.local_ch_user, cfg.local_ch_password)
        row_count = int(rows[0]['cnt']) if rows else 0
        return True, row_count
    
    return False, 0


def sync_table(cfg: Config, full_table_name: str, table_info: Dict,
               source_partitions: Dict[str, int], dest_partitions: Dict[str, int],
               active_site: str) -> Tuple[int, int]:
    """
    Sync all missing partitions for a table.
    Returns: (partitions_synced, rows_synced)
    """
    gaps = find_partition_gaps(source_partitions, dest_partitions)
    
    if not gaps:
        return 0, 0
    
    database = table_info['database']
    table = table_info['table']
    
    log(f"Table {full_table_name}: {len(gaps)} partitions need sync")
    
    # Determine source connection info based on active site
    if active_site == cfg.role:
        # We are active, sync TO remote (shouldn't happen in normal flow)
        log(f"Unexpected: we are active but trying to sync", "WARN")
        return 0, 0
    else:
        # Remote is active, sync FROM remote to local
        source_host = cfg.remote_ch_host
        source_port = cfg.remote_ch_port
        source_user = cfg.remote_ch_user
        source_password = cfg.remote_ch_password
    
    partitions_synced = 0
    rows_synced = 0
    
    for partition, source_count, dest_count in gaps:
        log(f"  Partition {partition}: source={source_count}, dest={dest_count}, delta={source_count - dest_count}")
        
        success, rows = sync_partition(
            cfg, database, table, partition,
            source_host, source_port, source_user, source_password
        )
        
        if success:
            partitions_synced += 1
            rows_synced += rows
            log(f"  Partition {partition}: synced successfully ({rows} rows total)")
        else:
            log(f"  Partition {partition}: sync FAILED", "ERROR")
    
    return partitions_synced, rows_synced


# -----------------------------
# Main Sync Check
# -----------------------------

def run_sync_check(cfg: Config, state: SyncState) -> SyncState:
    """
    Run a single sync check cycle.
    Returns updated state.
    """
    state.last_check = datetime.now(timezone.utc).isoformat()
    state.tables_with_gaps = 0
    state.new_tables_detected = []
    
    # Check CH health
    local_healthy = ch_health(cfg.local_ch_url)
    remote_healthy = ch_health(cfg.remote_ch_url)
    
    if not local_healthy:
        log(f"Local ClickHouse unhealthy: {cfg.local_ch_url}", "WARN")
        state.last_error = "Local ClickHouse unhealthy"
        return state
    
    if not remote_healthy:
        log(f"Remote ClickHouse unhealthy: {cfg.remote_ch_url}", "WARN")
        state.last_error = "Remote ClickHouse unhealthy"
        return state
    
    # Get active site from DNS
    active_site = get_active_site(cfg)
    if not active_site:
        log("Could not determine active site from DNS", "WARN")
        state.last_error = "DNS lookup failed"
        return state
    
    state.active_site = active_site
    log(f"Active site: {active_site} (we are: {cfg.role})")
    
    # If we are active, nothing to sync (we are the source of truth)
    if active_site == cfg.role:
        log("We are active site - nothing to sync")
        state.consecutive_clean += 1
        state.last_error = None
        return state
    
    # Discover tables on both sides
    log("Discovering tables...")
    local_tables = discover_tables(cfg, cfg.local_ch_url, cfg.local_ch_user, cfg.local_ch_password)
    remote_tables = discover_tables(cfg, cfg.remote_ch_url, cfg.remote_ch_user, cfg.remote_ch_password)
    
    log(f"Found {len(local_tables)} local tables, {len(remote_tables)} remote tables")
    
    # Check for new tables on remote that don't exist locally
    tables_created = []
    for table_name in remote_tables:
        if table_name not in local_tables:
            if cfg.auto_create_tables:
                # Auto-create the table
                remote_info = remote_tables[table_name]
                if create_table_from_remote(cfg, remote_info['database'], remote_info['table']):
                    tables_created.append(table_name)
                    # Add to local_tables so it gets synced this cycle
                    local_tables[table_name] = remote_info
                else:
                    log(f"NEW TABLE on active site: {table_name} (auto-create FAILED)", "WARN")
                    state.new_tables_detected.append(table_name)
            else:
                log(f"NEW TABLE on active site: {table_name} (needs manual creation)", "WARN")
                state.new_tables_detected.append(table_name)
    
    if tables_created:
        log(f"Auto-created {len(tables_created)} tables: {', '.join(tables_created)}")
    
    if state.new_tables_detected and cfg.notify_on_new_table:
        notify(cfg, "new_tables", state)
    
    # Sync tables that exist on both sides
    tables_to_check = set(local_tables.keys()) & set(remote_tables.keys())
    state.tables_checked = len(tables_to_check)
    
    total_partitions_synced = 0
    total_rows_synced = 0
    tables_with_gaps = 0
    
    for table_name in sorted(tables_to_check):
        table_info = local_tables[table_name]
        
        log(f"Checking table: {table_name}", "DEBUG")
        
        # Get partition counts from both sides
        local_partitions = get_partition_counts(
            cfg.local_ch_url, cfg.local_ch_user, cfg.local_ch_password,
            table_info['database'], table_info['table']
        )
        remote_partitions = get_partition_counts(
            cfg.remote_ch_url, cfg.remote_ch_user, cfg.remote_ch_password,
            table_info['database'], table_info['table']
        )
        
        # Remote is source (active), local is dest (passive)
        gaps = find_partition_gaps(remote_partitions, local_partitions)
        
        if gaps:
            tables_with_gaps += 1
            log(f"Table {table_name}: {len(gaps)} partitions missing")
            
            partitions_synced, rows_synced = sync_table(
                cfg, table_name, table_info,
                remote_partitions, local_partitions,
                active_site
            )
            
            total_partitions_synced += partitions_synced
            total_rows_synced += rows_synced
    
    state.tables_with_gaps = tables_with_gaps
    state.partitions_synced += total_partitions_synced
    state.rows_synced += total_rows_synced
    
    if total_partitions_synced > 0:
        state.last_sync = datetime.now(timezone.utc).isoformat()
        state.consecutive_clean = 0
        log(f"Sync complete: {total_partitions_synced} partitions, {total_rows_synced} rows")
        if cfg.notify_on_sync:
            notify(cfg, "sync_complete", state)
    elif tables_with_gaps == 0 and not state.new_tables_detected:
        state.consecutive_clean += 1
        state.last_error = None
        log(f"All tables in sync (clean check #{state.consecutive_clean})")
        
        # Check for failback readiness
        if state.consecutive_clean >= cfg.failback_clean_checks:
            if not state.failback_ready:
                state.failback_ready = True
                log("=" * 60)
                log("FAILBACK READY: All tables synced", "INFO")
                log(f"  Active site: {active_site}")
                log(f"  Tables checked: {state.tables_checked}")
                log(f"  Clean checks: {state.consecutive_clean}")
                log("  Safe to initiate failback when desired")
                log("=" * 60)
                if cfg.notify_on_failback_ready:
                    notify(cfg, "failback_ready", state)
    else:
        state.consecutive_clean = 0
        state.failback_ready = False
        if cfg.notify_on_gap:
            notify(cfg, "gap_detected", state)
    
    return state


# -----------------------------
# Notifications
# -----------------------------

def notify(cfg: Config, event: str, state: SyncState):
    """Send notification via webhook."""
    if not cfg.notify_webhook:
        return
    
    messages = {
        "gap_detected": f"⚠️ CH-Sync: {state.tables_with_gaps} tables have data gaps. Active site: {state.active_site}",
        "sync_complete": f"✅ CH-Sync: Sync complete. {state.partitions_synced} partitions synced.",
        "failback_ready": f"🟢 CH-Sync: FAILBACK READY. All {state.tables_checked} tables synced after {state.consecutive_clean} checks.",
        "new_tables": f"🆕 CH-Sync: New tables detected on active site: {', '.join(state.new_tables_detected)}. Manual creation required.",
    }
    
    message = messages.get(event, f"CH-Sync event: {event}")
    
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
    log("CH-Sync - ClickHouse Data Synchronization")
    log("=" * 60)
    
    cfg = Config.from_env()
    
    try:
        cfg.validate()
    except ValueError as e:
        log(f"Configuration error: {e}", "ERROR")
        sys.exit(1)
    
    log(f"Role:            {cfg.role}")
    log(f"Local CH:        {cfg.local_ch_url}")
    log(f"Remote CH:       {cfg.remote_ch_url}")
    log(f"Remote Host:     {cfg.remote_ch_host}:{cfg.remote_ch_port}")
    log(f"DNS Record:      {cfg.dns_record}")
    log(f"Check Interval:  {cfg.check_interval}s")
    log(f"Exclude:         {cfg.exclude_patterns}")
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
            import traceback
            traceback.print_exc()
            state.last_error = str(e)
            save_state(cfg.state_file, state)
        
        # Sleep until next check
        log(f"Next check in {cfg.check_interval}s")
        for _ in range(cfg.check_interval):
            if shutdown:
                break
            time.sleep(1)
    
    log("CH-Sync shutdown complete")


if __name__ == '__main__':
    main()
