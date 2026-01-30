#!/usr/bin/env python3
"""
ClickHouse Sync (ch-sync)

Replicates ClickHouse data from source to destination.
Uses native port (9000) for remote() data transfer.

Environment Variables:
  SOURCE_HOST, SOURCE_PORT, SOURCE_NATIVE_PORT, SOURCE_USER, SOURCE_PASSWORD
  DEST_HOST, DEST_PORT, DEST_USER, DEST_PASSWORD
  SYNC_INTERVAL - seconds between syncs (default: 300)
  SYNC_DATABASES - comma-separated database list
"""

import os
import sys
import time
import json
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from typing import Dict, List

SOURCE_HOST = os.getenv('SOURCE_HOST', 'localhost')
SOURCE_PORT = os.getenv('SOURCE_PORT', '8123')
SOURCE_NATIVE_PORT = os.getenv('SOURCE_NATIVE_PORT', '9000')
SOURCE_USER = os.getenv('SOURCE_USER', 'default')
SOURCE_PASSWORD = os.getenv('SOURCE_PASSWORD', '')

DEST_HOST = os.getenv('DEST_HOST', 'localhost')
DEST_PORT = os.getenv('DEST_PORT', '8123')
DEST_USER = os.getenv('DEST_USER', 'default')
DEST_PASSWORD = os.getenv('DEST_PASSWORD', '')

SYNC_INTERVAL = int(os.getenv('SYNC_INTERVAL', '300'))
SYNC_DATABASES = [db.strip() for db in os.getenv('SYNC_DATABASES', 'ast').split(',')]

def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)

def ch_query(host: str, port: str, user: str, password: str, 
             query: str, format: str = "JSONEachRow") -> List[Dict]:
    url = f"http://{host}:{port}/"
    full_query = f"{query} FORMAT {format}"
    params = {'user': user, 'query': full_query}
    if password:
        params['password'] = password
    url_with_params = url + '?' + urllib.parse.urlencode(params)
    
    try:
        req = urllib.request.Request(url_with_params)
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read().decode('utf-8')
            if not data.strip():
                return []
            return [json.loads(line) for line in data.strip().split('\n') if line]
    except Exception as e:
        log(f"Query failed: {e}", "ERROR")
        raise

def ch_execute(host: str, port: str, user: str, password: str, query: str, timeout: int = 600) -> bool:
    url = f"http://{host}:{port}/"
    params = {'user': user}
    if password:
        params['password'] = password
    url_with_params = url + '?' + urllib.parse.urlencode(params)
    
    try:
        data = query.encode('utf-8')
        req = urllib.request.Request(url_with_params, data=data, method='POST')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else ''
        log(f"Execute failed: {e.code} - {error_body[:200]}", "ERROR")
        return False
    except Exception as e:
        log(f"Execute failed: {e}", "ERROR")
        return False

def source_query(query: str, fmt: str = "JSONEachRow") -> List[Dict]:
    return ch_query(SOURCE_HOST, SOURCE_PORT, SOURCE_USER, SOURCE_PASSWORD, query, fmt)

def dest_query(query: str, fmt: str = "JSONEachRow") -> List[Dict]:
    return ch_query(DEST_HOST, DEST_PORT, DEST_USER, DEST_PASSWORD, query, fmt)

def dest_execute(query: str, timeout: int = 600) -> bool:
    return ch_execute(DEST_HOST, DEST_PORT, DEST_USER, DEST_PASSWORD, query, timeout)

def get_tables(database: str, is_source: bool = True) -> List[str]:
    fn = source_query if is_source else dest_query
    rows = fn(f"SHOW TABLES FROM {database}")
    return [r['name'] for r in rows]

def get_create_table(database: str, table: str) -> str:
    rows = source_query(f"SELECT create_table_query FROM system.tables WHERE database='{database}' AND name='{table}'")
    if rows:
        return rows[0].get('create_table_query', '')
    return ""

def sync_schema(database: str):
    log(f"Syncing schema: {database}")
    dest_execute(f"CREATE DATABASE IF NOT EXISTS {database}")
    
    src_tables = set(get_tables(database, True))
    dst_tables = set(get_tables(database, False))
    
    for table in src_tables - dst_tables:
        log(f"  Creating table: {database}.{table}")
        ddl = get_create_table(database, table)
        if ddl:
            dest_execute(ddl)

def get_partitions(database: str, table: str, is_source: bool = True) -> Dict[str, int]:
    fn = source_query if is_source else dest_query
    rows = fn(f"""
        SELECT partition, sum(rows) as rows
        FROM system.parts
        WHERE database = '{database}' AND table = '{table}' AND active
        GROUP BY partition
    """)
    return {r['partition']: int(r['rows']) for r in rows}

def sync_table(database: str, table: str):
    log(f"  Syncing table: {database}.{table}")
    
    src_parts = get_partitions(database, table, True)
    dst_parts = get_partitions(database, table, False)
    
    synced = 0
    for partition, src_rows in src_parts.items():
        dst_rows = dst_parts.get(partition, 0)
        
        if src_rows != dst_rows:
            log(f"    Partition {partition}: {dst_rows} → {src_rows}")
            
            dest_execute(f"ALTER TABLE {database}.{table} DROP PARTITION '{partition}'")
            
            # Use native port 9000 for remote()
            result = dest_execute(f"""
                INSERT INTO {database}.{table}
                SELECT * FROM remote(
                    '{SOURCE_HOST}:{SOURCE_NATIVE_PORT}',
                    '{database}.{table}',
                    '{SOURCE_USER}',
                    '{SOURCE_PASSWORD}'
                ) WHERE _partition_id = '{partition}'
            """, timeout=1800)
            
            if result:
                synced += 1
            else:
                log(f"    Failed to sync partition {partition}", "ERROR")
    
    if synced:
        log(f"    Synced {synced} partitions")

def sync_database(database: str):
    log(f"Syncing database: {database}")
    sync_schema(database)
    
    for table in get_tables(database, True):
        try:
            sync_table(database, table)
        except Exception as e:
            log(f"  Error syncing {database}.{table}: {e}", "ERROR")

def test_connections() -> bool:
    log("Testing connections...")
    try:
        source_query("SELECT 1")
        log(f"  Source OK: {SOURCE_HOST}:{SOURCE_PORT}")
    except:
        log(f"  Source FAILED", "ERROR")
        return False
    
    try:
        dest_query("SELECT 1")
        log(f"  Dest OK: {DEST_HOST}:{DEST_PORT}")
    except:
        log(f"  Dest FAILED", "ERROR")
        return False
    
    return True

def main():
    log("=" * 60)
    log("ClickHouse Sync (ch-sync)")
    log("=" * 60)
    log(f"Source:    {SOURCE_HOST}:{SOURCE_PORT} (native: {SOURCE_NATIVE_PORT})")
    log(f"Dest:      {DEST_HOST}:{DEST_PORT}")
    log(f"Databases: {', '.join(SYNC_DATABASES)}")
    log(f"Interval:  {SYNC_INTERVAL}s")
    log("=" * 60)
    
    if not test_connections():
        log("Connection test failed", "ERROR")
        sys.exit(1)
    
    while True:
        start = time.time()
        for db in SYNC_DATABASES:
            if db:
                try:
                    sync_database(db)
                except Exception as e:
                    log(f"Error syncing {db}: {e}", "ERROR")
        elapsed = time.time() - start
        log(f"Sync complete in {elapsed:.1f}s. Next in {SYNC_INTERVAL}s")
        time.sleep(SYNC_INTERVAL)

if __name__ == '__main__':
    main()
