#!/usr/bin/env python3
"""
ClickHouse Sync (ch-sync)

Append-only, idempotent gap fill from SOURCE ClickHouse into DEST ClickHouse.
Run one instance per site, each pulling from its peer, and both sites converge
on the union of their data:

  * Nothing is ever dropped or deleted on DEST.
  * For each table, rows are compared in hourly buckets over the lookback
    window (row count + order-independent content hash per bucket on each
    side). For any bucket whose fingerprint differs,
    rows from SOURCE whose content hash is not already present in that DEST
    bucket are inserted. Re-running is a no-op once both sides match.
  * This fills gaps anywhere in the window, including the failover window on
    Primary after a failback.

Environment Variables:
  SOURCE_HOST, SOURCE_PORT (HTTP, 8123), SOURCE_NATIVE_PORT (9000),
  SOURCE_USER, SOURCE_PASSWORD
  DEST_HOST, DEST_PORT (HTTP), DEST_USER, DEST_PASSWORD
  SYNC_INTERVAL        seconds between runs (default 300)
  SYNC_DATABASES       comma-separated databases (default: ast)
  SYNC_LOOKBACK_HOURS  window to reconcile (default 72, matches exporter TTL)
  TIME_COLUMNS         candidate time columns, first match wins
                       (default: TimeUnix,Timestamp)
  INSERT_TIMEOUT       seconds per bucket insert (default 1800)

Usage:
  python3 ch_sync.py          # loop forever
  python3 ch_sync.py --once   # single pass (manual recovery)

Performance: each run reads and hashes every column of every row in the
lookback window on both sides, and for each mismatched hour ships the
DEST row hashes for that hour to SOURCE (GLOBAL NOT IN). Memory per bucket is
roughly 8 bytes x rows-in-that-hour on SOURCE.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional


def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)


# -----------------------------
# SQL helpers
# -----------------------------

def quote_ident(name: str) -> str:
    """Backtick-quote a ClickHouse identifier."""
    return '`' + name.replace('\\', '\\\\').replace('`', '\\`') + '`'

def quote_str(value: str) -> str:
    """Single-quote a ClickHouse string literal."""
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"


# -----------------------------
# HTTP client
# -----------------------------

@dataclass
class CHEndpoint:
    host: str
    port: str
    user: str
    password: str

    def _request(self, query: str, timeout: int) -> str:
        # Credentials go in headers, never in the URL (keeps them out of access logs).
        req = urllib.request.Request(
            f"http://{self.host}:{self.port}/",
            data=query.encode('utf-8'),
            method='POST',
            headers={'X-ClickHouse-User': self.user, 'X-ClickHouse-Key': self.password},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'replace') if e.fp else ''
            raise RuntimeError(f"ClickHouse {self.host}: HTTP {e.code}: {body[:500]}") from None

    def query(self, sql: str, timeout: int = 120) -> List[Dict]:
        data = self._request(f"{sql} FORMAT JSONEachRow", timeout)
        return [json.loads(line) for line in data.splitlines() if line.strip()]

    def execute(self, sql: str, timeout: int = 600) -> None:
        self._request(sql, timeout)


@dataclass
class SyncConfig:
    source: CHEndpoint
    dest: CHEndpoint
    source_native_port: str
    databases: List[str]
    lookback_hours: int
    time_columns: List[str]
    insert_timeout: int
    interval: int

    @classmethod
    def from_env(cls, env=os.environ) -> 'SyncConfig':
        return cls(
            source=CHEndpoint(env.get('SOURCE_HOST', 'localhost'), env.get('SOURCE_PORT', '8123'),
                              env.get('SOURCE_USER', 'default'), env.get('SOURCE_PASSWORD', '')),
            dest=CHEndpoint(env.get('DEST_HOST', 'localhost'), env.get('DEST_PORT', '8123'),
                            env.get('DEST_USER', 'default'), env.get('DEST_PASSWORD', '')),
            source_native_port=env.get('SOURCE_NATIVE_PORT', '9000'),
            databases=[d.strip() for d in env.get('SYNC_DATABASES', 'ast').split(',') if d.strip()],
            lookback_hours=int(env.get('SYNC_LOOKBACK_HOURS', '72')),
            time_columns=[c.strip() for c in env.get('TIME_COLUMNS', 'TimeUnix,Timestamp').split(',') if c.strip()],
            insert_timeout=int(env.get('INSERT_TIMEOUT', '1800')),
            interval=int(env.get('SYNC_INTERVAL', '300')),
        )


# -----------------------------
# Sync logic
# -----------------------------

class Syncer:
    def __init__(self, cfg: SyncConfig):
        self.cfg = cfg
        self.src = cfg.source
        self.dst = cfg.dest

    # --- metadata ---

    def list_tables(self, ep: CHEndpoint, database: str) -> List[str]:
        """Data tables only: MergeTree family, excluding materialized-view inner tables."""
        rows = ep.query(
            f"SELECT name FROM system.tables WHERE database = {quote_str(database)} "
            f"AND engine LIKE '%MergeTree%' AND NOT startsWith(name, '.inner') ORDER BY name"
        )
        return [r['name'] for r in rows]

    def columns(self, ep: CHEndpoint, database: str, table: str) -> List[str]:
        """Physical (insertable) columns in table order."""
        rows = ep.query(
            f"SELECT name FROM system.columns WHERE database = {quote_str(database)} "
            f"AND table = {quote_str(table)} AND default_kind NOT IN ('MATERIALIZED', 'ALIAS', 'EPHEMERAL') "
            f"ORDER BY position"
        )
        return [r['name'] for r in rows]

    def sync_schema(self, database: str):
        self.dst.execute(f"CREATE DATABASE IF NOT EXISTS {quote_ident(database)}")
        missing = set(self.list_tables(self.src, database)) - set(self.list_tables(self.dst, database))
        for table in sorted(missing):
            rows = self.src.query(
                f"SELECT create_table_query FROM system.tables "
                f"WHERE database = {quote_str(database)} AND name = {quote_str(table)}"
            )
            if rows and rows[0].get('create_table_query'):
                log(f"  Creating table: {database}.{table}")
                self.dst.execute(rows[0]['create_table_query'])

    # --- per-table reconciliation ---

    def bucket_fingerprints(self, ep: CHEndpoint, database: str, table: str, cols: List[str],
                            time_col: str, since: int, until: Optional[int] = None) -> Dict[int, tuple]:
        """
        Per-hour (row count, order-independent content hash) for rows with time >= since.
        Count alone is not enough: around a failover both sites can hold the same
        number of *different* rows for one hour.
        """
        tc = quote_ident(time_col)
        row_hash = f"cityHash64({', '.join(quote_ident(c) for c in cols)})"
        where = f"{tc} >= toDateTime({since})"
        if until is not None:
            where += f" AND {tc} < toDateTime({until})"
        rows = ep.query(
            f"SELECT toUnixTimestamp(toStartOfHour({tc})) AS b, count() AS c, "
            f"toString(sumWithOverflow({row_hash})) AS h "
            f"FROM {quote_ident(database)}.{quote_ident(table)} WHERE {where} GROUP BY b"
        )
        return {int(r['b']): (int(r['c']), r['h']) for r in rows}

    def source_table_expr(self, database: str, table: str) -> str:
        """The source table as seen from DEST (overridable in tests)."""
        return (
            f"remote({quote_str(self.src.host + ':' + self.cfg.source_native_port)}, "
            f"{quote_str(database)}, {quote_str(table)}, "
            f"{quote_str(self.src.user)}, {quote_str(self.src.password)})"
        )

    def build_fill_query(self, database: str, table: str, cols: List[str], time_col: str, bucket: int) -> str:
        col_list = ', '.join(quote_ident(c) for c in cols)
        tc = quote_ident(time_col)
        window = f"{tc} >= toDateTime({bucket}) AND {tc} < toDateTime({bucket + 3600})"
        row_hash = f"cityHash64({col_list})"
        return (
            f"INSERT INTO {quote_ident(database)}.{quote_ident(table)} ({col_list}) "
            f"SELECT {col_list} FROM {self.source_table_expr(database, table)} "
            f"WHERE {window} AND {row_hash} GLOBAL NOT IN ("
            f"SELECT {row_hash} FROM {quote_ident(database)}.{quote_ident(table)} WHERE {window})"
        )

    def sync_table(self, database: str, table: str, now: Optional[int] = None) -> int:
        """Returns the number of buckets that were reconciled."""
        src_cols = self.columns(self.src, database, table)
        dst_cols = set(self.columns(self.dst, database, table))
        cols = [c for c in src_cols if c in dst_cols]
        if len(cols) != len(src_cols):
            log(f"  {database}.{table}: schema differs; syncing {len(cols)}/{len(src_cols)} shared columns", "WARN")

        time_col = next((c for c in self.cfg.time_columns if c in cols), None)
        if not time_col:
            log(f"  {database}.{table}: no time column ({', '.join(self.cfg.time_columns)}); skipping", "WARN")
            return 0

        now = int(time.time()) if now is None else now
        since = (now - self.cfg.lookback_hours * 3600) // 3600 * 3600
        src_buckets = self.bucket_fingerprints(self.src, database, table, cols, time_col, since)
        dst_buckets = self.bucket_fingerprints(self.dst, database, table, cols, time_col, since)

        reconciled = 0
        for bucket in sorted(src_buckets):
            if src_buckets[bucket] == dst_buckets.get(bucket):
                continue
            src_n = src_buckets[bucket][0]
            before = dst_buckets.get(bucket, (0, ''))[0]
            self.dst.execute(self.build_fill_query(database, table, cols, time_col, bucket),
                             timeout=self.cfg.insert_timeout)
            after = self.bucket_fingerprints(self.dst, database, table, cols, time_col,
                                             bucket, bucket + 3600).get(bucket, (0, ''))[0]
            hour = datetime.fromtimestamp(bucket, timezone.utc).strftime('%Y-%m-%d %H:00Z')
            log(f"    {table} {hour}: src={src_n} dst={before} -> inserted {after - before}")
            reconciled += 1
        return reconciled

    def sync_database(self, database: str):
        log(f"Syncing database: {database}")
        self.sync_schema(database)
        for table in self.list_tables(self.src, database):
            try:
                self.sync_table(database, table)
            except Exception as e:
                log(f"  Error syncing {database}.{table}: {e}", "ERROR")

    def run_once(self):
        start = time.time()
        for db in self.cfg.databases:
            try:
                self.sync_database(db)
            except Exception as e:
                log(f"Error syncing {db}: {e}", "ERROR")
        log(f"Sync pass complete in {time.time() - start:.1f}s")


def test_connections(cfg: SyncConfig) -> bool:
    ok = True
    for name, ep in (('Source', cfg.source), ('Dest', cfg.dest)):
        try:
            ep.query("SELECT 1")
            log(f"  {name} OK: {ep.host}:{ep.port}")
        except Exception as e:
            log(f"  {name} FAILED: {ep.host}:{ep.port} - {e}", "ERROR")
            ok = False
    return ok


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cfg = SyncConfig.from_env()
    log("=" * 60)
    log("ClickHouse Sync (ch-sync) - append-only")
    log(f"Source:    {cfg.source.host}:{cfg.source.port} (native: {cfg.source_native_port})")
    log(f"Dest:      {cfg.dest.host}:{cfg.dest.port}")
    log(f"Databases: {', '.join(cfg.databases)}")
    log(f"Lookback:  {cfg.lookback_hours}h, Interval: {cfg.interval}s")
    log("=" * 60)

    syncer = Syncer(cfg)
    if '--once' in argv:
        if not test_connections(cfg):
            sys.exit(1)
        syncer.run_once()
        return

    # Peer may be down at startup (that's the point of DR); keep retrying.
    while True:
        if test_connections(cfg):
            syncer.run_once()
        else:
            log("Connection test failed; will retry", "WARN")
        time.sleep(cfg.interval)


if __name__ == '__main__':
    main()
