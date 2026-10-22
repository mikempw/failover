#!/usr/bin/env python3
"""Failure-safe ClickHouse partition synchronizer."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import random
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

try:
    from common.health_server import ServiceStatus
except ModuleNotFoundError:  # Allow direct execution from a source checkout.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from common.health_server import ServiceStatus


TRANSIENT_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}
STAGING_PREFIX = "_ch_sync_stage_"


def log(message: str, level: str = "INFO") -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {message}", flush=True)


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _secret(name: str) -> str:
    value = os.getenv(name)
    filename = os.getenv(f"{name}_FILE")
    if value and filename:
        raise ValueError(f"set only one of {name} and {name}_FILE")
    if filename:
        try:
            return Path(filename).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            raise ValueError(f"cannot read {name}_FILE") from exc
    return value or ""


def _require_builtin_replication() -> None:
    mode = os.getenv("DATA_REPLICATION_MODE", "builtin").strip().lower()
    if mode not in {"builtin", "external"}:
        raise ValueError(
            "DATA_REPLICATION_MODE must be either 'builtin' or 'external'"
        )
    if mode == "external":
        raise ValueError(
            "ch-sync is disabled because DATA_REPLICATION_MODE=external"
        )


def quote_identifier(value: str) -> str:
    if not value or "\x00" in value:
        raise ValueError("ClickHouse identifier must not be empty or contain NUL")
    return "`" + value.replace("\\", "\\\\").replace("`", "\\`") + "`"


def quote_literal(value: str) -> str:
    if "\x00" in value:
        raise ValueError("ClickHouse literal must not contain NUL")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


@dataclass(frozen=True)
class Endpoint:
    scheme: str
    host: str
    port: int
    user: str
    password: str
    ca_file: str = ""

    def __post_init__(self) -> None:
        if self.scheme not in {"http", "https"}:
            raise ValueError("ClickHouse scheme must be http or https")
        if not self.host or re.search(r"[/?#@]", self.host):
            raise ValueError("invalid ClickHouse host")
        if not (1 <= int(self.port) <= 65535):
            raise ValueError("invalid ClickHouse port")
        if self.password and not self.user:
            raise ValueError("a user is required when a password is configured")
        if self.ca_file and self.scheme != "https":
            raise ValueError("a CA file can only be used with https")

    @property
    def url(self) -> str:
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{self.scheme}://{host}:{self.port}/"

    @property
    def headers(self) -> Dict[str, str]:
        headers = {"X-ClickHouse-User": self.user or "default"}
        if self.password:
            headers["X-ClickHouse-Key"] = self.password
        return headers

    @property
    def ssl_context(self) -> Optional[ssl.SSLContext]:
        if self.scheme != "https":
            return None
        return ssl.create_default_context(cafile=self.ca_file or None)


@dataclass(frozen=True)
class Config:
    source: Endpoint
    destination: Endpoint
    databases: List[str]
    interval_seconds: int
    request_timeout_seconds: int
    copy_timeout_seconds: int
    max_attempts: int
    retry_initial_seconds: float
    retry_max_seconds: float
    health_port: int
    readiness_max_age_seconds: int
    guard_dns_record: str = ""
    guard_destination_ip: str = ""
    guard_lease_proof_file: str = ""
    guard_max_clock_skew: int = 5
    allow_active_destination_confirmation: str = ""

    def __post_init__(self) -> None:
        record = self.guard_dns_record.strip().rstrip(".")
        address = self.guard_destination_ip.strip()
        if bool(record) != bool(address):
            raise ValueError(
                "SYNC_GUARD_DNS_RECORD and SYNC_GUARD_DEST_IP must be set together"
            )
        if record and re.search(r"[\s/?#@]", record):
            raise ValueError("SYNC_GUARD_DNS_RECORD must be a DNS hostname")
        if address:
            try:
                address = str(ipaddress.IPv4Address(address))
            except ipaddress.AddressValueError as exc:
                raise ValueError("SYNC_GUARD_DEST_IP must be an IPv4 address") from exc
        proof_file = self.guard_lease_proof_file.strip()
        if record and (not proof_file or not os.path.isabs(proof_file)):
            raise ValueError(
                "SYNC_GUARD_LEASE_PROOF_FILE must be an absolute path when the guard is enabled"
            )
        if self.guard_max_clock_skew < 0:
            raise ValueError("MAX_CLOCK_SKEW must be non-negative")
        if self.allow_active_destination_confirmation:
            raise ValueError(
                "SYNC_ALLOW_ACTIVE_DESTINATION has been removed because it disables "
                "split-brain fencing; configure SYNC_GUARD_DEST_IP for the recovery "
                "destination instead"
            )
        object.__setattr__(self, "guard_dns_record", record)
        object.__setattr__(self, "guard_destination_ip", address)
        object.__setattr__(self, "guard_lease_proof_file", proof_file)

    @classmethod
    def from_env(cls) -> "Config":
        _require_builtin_replication()
        interval = _positive_int("SYNC_INTERVAL", 300)
        databases = [
            item.strip()
            for item in os.getenv("SYNC_DATABASES", "ast").split(",")
            if item.strip()
        ]
        if not databases:
            raise ValueError("SYNC_DATABASES must contain at least one database")
        return cls(
            source=Endpoint(
                os.getenv("SOURCE_SCHEME", "http").lower(),
                os.getenv("SOURCE_HOST", "localhost"),
                _positive_int("SOURCE_PORT", 8123),
                os.getenv("SOURCE_USER", "default"),
                _secret("SOURCE_PASSWORD"),
                os.getenv("SOURCE_CA_FILE", ""),
            ),
            destination=Endpoint(
                os.getenv("DEST_SCHEME", "http").lower(),
                os.getenv("DEST_HOST", "localhost"),
                _positive_int("DEST_PORT", 8123),
                os.getenv("DEST_USER", "default"),
                _secret("DEST_PASSWORD"),
                os.getenv("DEST_CA_FILE", ""),
            ),
            databases=databases,
            interval_seconds=interval,
            request_timeout_seconds=_positive_int("REQUEST_TIMEOUT", 120),
            copy_timeout_seconds=_positive_int("COPY_TIMEOUT", 1800),
            max_attempts=_positive_int("RETRY_MAX_ATTEMPTS", 4),
            retry_initial_seconds=float(os.getenv("RETRY_INITIAL_SECONDS", "1")),
            retry_max_seconds=float(os.getenv("RETRY_MAX_SECONDS", "30")),
            health_port=_positive_int("HEALTH_PORT", 8080),
            readiness_max_age_seconds=_positive_int(
                "READINESS_MAX_AGE", max(interval * 3, 900)
            ),
            guard_dns_record=(
                os.getenv("SYNC_GUARD_DNS_RECORD") or os.getenv("DNS_RECORD", "")
            ),
            guard_destination_ip=(
                os.getenv("SYNC_GUARD_DEST_IP") or os.getenv("DR_IP", "")
            ),
            guard_lease_proof_file=os.getenv(
                "SYNC_GUARD_LEASE_PROOF_FILE",
                "/failover-state/observed-lease.json",
            ),
            guard_max_clock_skew=int(os.getenv("MAX_CLOCK_SKEW", "5")),
            allow_active_destination_confirmation=os.getenv(
                "SYNC_ALLOW_ACTIVE_DESTINATION", ""
            ),
        )


@dataclass(frozen=True)
class Partition:
    partition_id: str
    rows: int


@dataclass(frozen=True)
class SyncResult:
    success: bool
    partitions: int = 0
    rows: int = 0
    pending_partitions: int = 0
    retries: int = 0
    error: str = ""
    skipped: bool = False


@dataclass(frozen=True)
class LeaseProof:
    record: str
    ip: str
    owner: str
    expires_at: int
    observed_at: int


def load_lease_proof(path: str) -> LeaseProof:
    try:
        raw = Path(path).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"standby lease proof is unavailable or malformed: {exc}") from exc
    required = {"version", "record", "ip", "owner", "expires_at", "observed_at"}
    if not isinstance(payload, dict) or set(payload) != required or payload["version"] != 1:
        raise RuntimeError("standby lease proof has an unexpected schema")
    if payload["owner"] not in {"primary", "dr"}:
        raise RuntimeError("standby lease proof has an invalid owner")
    try:
        ip = str(ipaddress.IPv4Address(payload["ip"]))
    except (ipaddress.AddressValueError, TypeError) as exc:
        raise RuntimeError("standby lease proof has an invalid IP") from exc
    expires_at = payload["expires_at"]
    observed_at = payload["observed_at"]
    if (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
        or expires_at <= 0
        or isinstance(observed_at, bool)
        or not isinstance(observed_at, int)
        or observed_at <= 0
        or observed_at > expires_at
    ):
        raise RuntimeError("standby lease proof has invalid timestamps")
    record = payload["record"]
    if not isinstance(record, str) or not record or any(c.isspace() for c in record):
        raise RuntimeError("standby lease proof has an invalid record")
    return LeaseProof(
        record.rstrip(".").lower(),
        ip,
        payload["owner"],
        expires_at,
        observed_at,
    )


def resolve_ipv4_addresses(hostname: str) -> set[str]:
    """Resolve every IPv4 address currently advertised for a DNS hostname."""
    try:
        answers = socket.getaddrinfo(
            hostname, None, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
    except (socket.gaierror, OSError) as exc:
        raise RuntimeError(f"DNS resolution failed for {hostname}: {exc}") from exc
    addresses = {str(ipaddress.IPv4Address(answer[4][0])) for answer in answers}
    if not addresses:
        raise RuntimeError(f"DNS resolution returned no A records for {hostname}")
    return addresses


class RetryingHttpClient:
    def __init__(
        self,
        timeout: int,
        max_attempts: int,
        retry_initial: float,
        retry_max: float,
        opener: Callable[..., object] = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.retry_initial = max(retry_initial, 0.0)
        self.retry_max = max(retry_max, self.retry_initial)
        self.opener = opener
        self.sleep = sleep
        self.random_value = random_value
        self.retries_total = 0

    def request(
        self,
        endpoint: Endpoint,
        body: bytes,
        *,
        timeout: Optional[int] = None,
        retry: bool = True,
    ) -> bytes:
        request = urllib.request.Request(
            endpoint.url,
            data=body,
            headers={**endpoint.headers, "Content-Type": "application/octet-stream"},
            method="POST",
        )
        attempts = self.max_attempts if retry else 1
        for attempt in range(1, attempts + 1):
            try:
                kwargs = {"timeout": timeout or self.timeout}
                context = endpoint.ssl_context
                if context is not None:
                    kwargs["context"] = context
                with self.opener(request, **kwargs) as response:
                    status = int(getattr(response, "status", 200))
                    if status < 200 or status >= 300:
                        raise urllib.error.HTTPError(
                            endpoint.url,
                            status,
                            f"unexpected HTTP status {status}",
                            {},
                            None,
                        )
                    return response.read()
            except urllib.error.HTTPError as exc:
                if exc.code not in TRANSIENT_HTTP_CODES or attempt >= attempts:
                    detail = ""
                    if exc.fp:
                        detail = exc.read(500).decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"ClickHouse HTTP {exc.code}: {detail or exc.reason}"
                    ) from exc
                self._backoff(attempt, exc.headers.get("Retry-After") if exc.headers else None)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt >= attempts:
                    raise RuntimeError(f"ClickHouse request failed: {exc}") from exc
                self._backoff(attempt)
        raise RuntimeError("request attempts exhausted")

    def _backoff(self, attempt: int, retry_after: Optional[str] = None) -> None:
        self.retries_total += 1
        delay: Optional[float] = None
        if retry_after:
            try:
                delay = max(float(retry_after), 0.0)
            except ValueError:
                delay = None
        if delay is None:
            base = min(self.retry_initial * (2 ** (attempt - 1)), self.retry_max)
            delay = min(base * (0.5 + self.random_value()), self.retry_max)
        self.sleep(delay)


class ClickHouseClient:
    def __init__(
        self,
        endpoint: Endpoint,
        http: RetryingHttpClient,
        copy_timeout: int,
    ) -> None:
        self.endpoint = endpoint
        self.http = http
        self.copy_timeout = copy_timeout

    def query(self, sql: str) -> List[Dict[str, object]]:
        raw = self.http.request(
            self.endpoint, f"{sql.rstrip().rstrip(';')} FORMAT JSONEachRow".encode("utf-8")
        )
        if not raw.strip():
            return []
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def execute(self, sql: str) -> None:
        self.http.request(self.endpoint, sql.encode("utf-8"))

    def export_native(self, sql: str) -> bytes:
        return self.http.request(
            self.endpoint,
            f"{sql.rstrip().rstrip(';')} FORMAT Native".encode("utf-8"),
            timeout=self.copy_timeout,
        )

    def import_native(self, database: str, table: str, payload: bytes) -> None:
        prefix = (
            f"INSERT INTO {quote_identifier(database)}.{quote_identifier(table)} "
            "FORMAT Native\n"
        ).encode("utf-8")
        # A timed-out INSERT may have committed.  Do not retry into staging and
        # risk duplicates; the next run recreates the staging table instead.
        self.http.request(
            self.endpoint,
            prefix + payload,
            timeout=self.copy_timeout,
            retry=False,
        )


class ClickHouseSync:
    def __init__(
        self,
        config: Config,
        *,
        status: Optional[ServiceStatus] = None,
        http: Optional[RetryingHttpClient] = None,
        clock: Callable[[], float] = time.time,
        resolver: Callable[[str], set[str]] = resolve_ipv4_addresses,
        proof_reader: Callable[[str], LeaseProof] = load_lease_proof,
    ) -> None:
        self.config = config
        self.clock = clock
        self.resolver = resolver
        self.proof_reader = proof_reader
        self.status = status or ServiceStatus(
            "ch_sync", config.readiness_max_age_seconds
        )
        self.http = http or RetryingHttpClient(
            config.request_timeout_seconds,
            config.max_attempts,
            config.retry_initial_seconds,
            config.retry_max_seconds,
        )
        self.source = ClickHouseClient(config.source, self.http, config.copy_timeout_seconds)
        self.destination = ClickHouseClient(
            config.destination, self.http, config.copy_timeout_seconds
        )

    def destination_is_active(self) -> bool:
        """Fail closed if a configured standby guard cannot be evaluated."""
        record = self.config.guard_dns_record
        destination_ip = self.config.guard_destination_ip
        if not record:
            self.status.set_gauge("standby_guard_active", 0)
            self.status.set_gauge("standby_guard_resolution_ok", 1)
            return False
        try:
            addresses = {
                str(ipaddress.IPv4Address(address))
                for address in self.resolver(record)
            }
            if not addresses:
                raise RuntimeError("resolver returned no A records")
            if len(addresses) != 1:
                raise RuntimeError("resolver did not return exactly one A record")
        except Exception as exc:
            self.status.set_gauge("standby_guard_resolution_ok", 0)
            raise RuntimeError(
                f"standby guard could not resolve {record}; refusing to sync: {exc}"
            ) from exc
        try:
            proof = self.proof_reader(self.config.guard_lease_proof_file)
            now = int(self.clock())
            if proof.record != record.lower():
                raise RuntimeError("lease proof names a different DNS record")
            if proof.ip != next(iter(addresses)):
                raise RuntimeError("lease proof disagrees with the DNS A record")
            if proof.observed_at > now + self.config.guard_max_clock_skew:
                raise RuntimeError("lease proof was observed in the future")
            if proof.expires_at <= now + self.config.guard_max_clock_skew:
                raise RuntimeError("lease proof is expired or inside the skew guard")
        except Exception as exc:
            self.status.set_gauge("standby_guard_resolution_ok", 0)
            raise RuntimeError(
                f"standby guard could not verify current lease ownership; refusing to sync: {exc}"
            ) from exc
        # This comparison deliberately does not depend on which site is named
        # as owner.  Normal replication protects the DR address; recovery
        # replication protects the primary address.  In both directions the
        # destination is unsafe whenever the authoritative A record and fresh
        # controller proof agree that traffic currently points at it.
        active = proof.ip == destination_ip
        self.status.set_gauge("standby_guard_active", int(active))
        self.status.set_gauge("standby_guard_resolution_ok", 1)
        return active

    @staticmethod
    def staging_table(database: str, table: str) -> str:
        digest = hashlib.sha256(f"{database}\0{table}".encode("utf-8")).hexdigest()[:16]
        return f"{STAGING_PREFIX}{digest}"

    def get_tables(self, client: ClickHouseClient, database: str) -> List[str]:
        rows = client.query(
            "SELECT name FROM system.tables "
            f"WHERE database = {quote_literal(database)} "
            "AND engine LIKE '%MergeTree' "
            f"AND NOT startsWith(name, {quote_literal(STAGING_PREFIX)}) "
            "ORDER BY name"
        )
        return [str(row["name"]) for row in rows]

    def get_create_table(self, database: str, table: str) -> str:
        rows = self.source.query(
            "SELECT create_table_query FROM system.tables "
            f"WHERE database = {quote_literal(database)} "
            f"AND name = {quote_literal(table)}"
        )
        if not rows or not rows[0].get("create_table_query"):
            raise RuntimeError(f"source DDL missing for {database}.{table}")
        return str(rows[0]["create_table_query"])

    def ensure_schema(self, database: str) -> List[str]:
        self.destination.execute(
            f"CREATE DATABASE IF NOT EXISTS {quote_identifier(database)}"
        )
        source_tables = self.get_tables(self.source, database)
        destination_tables = set(self.get_tables(self.destination, database))
        for table in source_tables:
            if table not in destination_tables:
                log(f"Creating destination table {database}.{table}")
                self.destination.execute(self.get_create_table(database, table))
        return source_tables

    def get_partitions(
        self, client: ClickHouseClient, database: str, table: str
    ) -> Dict[str, Partition]:
        rows = client.query(
            "SELECT partition_id, sum(rows) AS rows FROM system.parts "
            f"WHERE database = {quote_literal(database)} "
            f"AND table = {quote_literal(table)} AND active "
            "GROUP BY partition_id ORDER BY partition_id"
        )
        return {
            str(row["partition_id"]): Partition(
                str(row["partition_id"]), int(row["rows"])
            )
            for row in rows
        }

    def count_partition(
        self, client: ClickHouseClient, database: str, table: str, partition_id: str
    ) -> int:
        rows = client.query(
            f"SELECT count() AS rows FROM {quote_identifier(database)}."
            f"{quote_identifier(table)} WHERE _partition_id = {quote_literal(partition_id)}"
        )
        return int(rows[0]["rows"]) if rows else 0

    def sync_partition(
        self, database: str, table: str, partition_id: str, expected_rows: int
    ) -> None:
        staging = self.staging_table(database, table)
        qualified_target = f"{quote_identifier(database)}.{quote_identifier(table)}"
        qualified_staging = f"{quote_identifier(database)}.{quote_identifier(staging)}"
        self.destination.execute(f"DROP TABLE IF EXISTS {qualified_staging}")
        try:
            self.destination.execute(
                f"CREATE TABLE {qualified_staging} AS {qualified_target}"
            )
            payload = self.source.export_native(
                f"SELECT * FROM {qualified_target} "
                f"WHERE _partition_id = {quote_literal(partition_id)}"
            )
            self.destination.import_native(database, staging, payload)
            staged_rows = self.count_partition(
                self.destination, database, staging, partition_id
            )
            if staged_rows != expected_rows:
                raise RuntimeError(
                    f"staging verification failed for {database}.{table} "
                    f"partition {partition_id}: expected {expected_rows}, got {staged_rows}"
                )
            # REPLACE is the first and only operation that changes live data.
            # Re-evaluate ownership at the last possible moment so a DNS
            # transition during a long export/import cannot overwrite a site
            # that became active after the run-level guard check.
            if self.destination_is_active():
                raise RuntimeError(
                    "standby destination became active before replacing "
                    f"{database}.{table} partition {partition_id}; refusing to sync"
                )
            self.destination.execute(
                f"ALTER TABLE {qualified_target} REPLACE PARTITION ID "
                f"{quote_literal(partition_id)} FROM {qualified_staging}"
            )
            final_rows = self.count_partition(
                self.destination, database, table, partition_id
            )
            if final_rows != expected_rows:
                raise RuntimeError(
                    f"destination verification failed for {database}.{table} "
                    f"partition {partition_id}: expected {expected_rows}, got {final_rows}"
                )
        finally:
            try:
                self.destination.execute(f"DROP TABLE IF EXISTS {qualified_staging}")
            except Exception as exc:
                log(f"Could not clean staging table {database}.{staging}: {exc}", "WARN")

    def sync_database(self, database: str) -> tuple[int, int, int, int]:
        synced_partitions = 0
        synced_rows = 0
        pending_partitions = 0
        row_lag = 0
        for table in self.ensure_schema(database):
            source_partitions = self.get_partitions(self.source, database, table)
            destination_partitions = self.get_partitions(
                self.destination, database, table
            )
            for partition_id, source_partition in source_partitions.items():
                destination_rows = destination_partitions.get(
                    partition_id, Partition(partition_id, 0)
                ).rows
                if destination_rows == source_partition.rows:
                    continue
                pending_partitions += 1
                row_lag += abs(source_partition.rows - destination_rows)
                log(
                    f"Syncing {database}.{table} partition {partition_id}: "
                    f"{destination_rows} -> {source_partition.rows} rows"
                )
                self.sync_partition(
                    database, table, partition_id, source_partition.rows
                )
                synced_partitions += 1
                synced_rows += source_partition.rows
        return synced_partitions, synced_rows, pending_partitions, row_lag

    def run_once(self) -> SyncResult:
        started = self.clock()
        self.status.mark_attempt()
        retries_before = self.http.retries_total
        partitions = 0
        rows = 0
        pending = 0
        row_lag = 0
        try:
            if self.destination_is_active():
                duration = max(self.clock() - started, 0.0)
                retries = self.http.retries_total - retries_before
                self.status.set_gauge("pending_partitions", 0)
                self.status.set_gauge("row_lag", 0)
                self.status.set_gauge("retries_total", self.http.retries_total)
                self.status.mark_success(duration, 0, 0, synced_partitions=0)
                log(
                    "Skipping ClickHouse sync because the standby destination "
                    f"{self.config.guard_destination_ip} is active for "
                    f"{self.config.guard_dns_record}"
                )
                return SyncResult(True, retries=retries, skipped=True)
            for database in self.config.databases:
                result = self.sync_database(database)
                partitions += result[0]
                rows += result[1]
                pending += result[2]
                row_lag += result[3]
            duration = max(self.clock() - started, 0.0)
            retries = self.http.retries_total - retries_before
            self.status.set_gauge("pending_partitions", 0)
            self.status.set_gauge("row_lag", 0)
            self.status.set_gauge("retries_total", self.http.retries_total)
            self.status.mark_success(
                duration, rows, 0, synced_partitions=partitions
            )
            return SyncResult(True, partitions, rows, pending, retries)
        except Exception as exc:
            duration = max(self.clock() - started, 0.0)
            retries = self.http.retries_total - retries_before
            self.status.set_gauge("pending_partitions", max(pending, 1))
            self.status.set_gauge("row_lag", row_lag)
            self.status.set_gauge("retries_total", self.http.retries_total)
            self.status.mark_failure(exc, duration)
            log(f"Sync failed: {exc}", "ERROR")
            return SyncResult(False, partitions, rows, pending, retries, str(exc))


def _arguments(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one sync and exit")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _arguments(argv)
    try:
        config = Config.from_env()
    except (ValueError, OSError) as exc:
        log(f"Invalid configuration: {exc}", "ERROR")
        return 2

    status = ServiceStatus("ch_sync", config.readiness_max_age_seconds)
    status.start_http_server(config.health_port)
    service = ClickHouseSync(config, status=status)
    log(
        f"ClickHouse sync: {config.source.url} -> {config.destination.url}; "
        f"databases={','.join(config.databases)} health=:{config.health_port}"
    )

    if args.once:
        result = service.run_once()
        return 0 if result.success else 1

    next_run = time.monotonic()
    while True:
        result = service.run_once()
        if result.success:
            log(
                f"Synced {result.partitions} partitions and {result.rows} rows "
                f"({result.retries} retries)"
            )
        next_run += config.interval_seconds
        now_monotonic = time.monotonic()
        if next_run <= now_monotonic:
            next_run = now_monotonic + 1.0
        delay = next_run - now_monotonic
        status.touch(next_run_in_seconds=delay)
        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
