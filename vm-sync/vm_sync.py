#!/usr/bin/env python3
"""Resilient VictoriaMetrics gap synchronizer.

The service copies bounded time windows from a source VictoriaMetrics instance
to a destination.  A durable watermark makes catch-up independent of the
configured polling interval and prevents an extended outage from becoming a
permanent data gap.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional

try:
    from common.health_server import ServiceStatus
except ModuleNotFoundError:  # Allow direct execution from a source checkout.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from common.health_server import ServiceStatus


TRANSIENT_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}


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


def _nonnegative_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def _optional_nonnegative_int(name: str) -> Optional[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer Unix timestamp") from exc
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def _secret(name: str) -> str:
    """Read NAME or NAME_FILE, never both."""
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
            "vm-sync is disabled because DATA_REPLICATION_MODE=external"
        )


@dataclass(frozen=True)
class Endpoint:
    url: str
    username: str = ""
    password: str = ""
    ca_file: str = ""

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"invalid HTTP endpoint: {self.url!r}")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("credentials must not be embedded in endpoint URLs")
        if self.password and not self.username:
            raise ValueError("a username is required when a password is configured")
        if self.ca_file and parsed.scheme != "https":
            raise ValueError("a CA file can only be used with an https endpoint")

    @property
    def headers(self) -> Dict[str, str]:
        if not self.username:
            return {}
        token = base64.b64encode(
            f"{self.username}:{self.password}".encode("utf-8")
        ).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    @property
    def ssl_context(self) -> Optional[ssl.SSLContext]:
        if not self.url.startswith("https://"):
            return None
        return ssl.create_default_context(cafile=self.ca_file or None)


@dataclass(frozen=True)
class Config:
    source: Endpoint
    destination: Endpoint
    interval_seconds: int
    initial_lookback_seconds: int
    chunk_seconds: int
    overlap_seconds: int
    settle_delay_seconds: int
    state_file: Path
    request_timeout_seconds: int
    max_attempts: int
    retry_initial_seconds: float
    retry_max_seconds: float
    health_port: int
    readiness_max_age_seconds: int
    max_rows_per_line: int
    initial_start_timestamp: Optional[int] = None

    def __post_init__(self) -> None:
        if (
            self.initial_start_timestamp is not None
            and self.initial_start_timestamp < 0
        ):
            raise ValueError("initial_start_timestamp must not be negative")

    @classmethod
    def from_env(cls) -> "Config":
        _require_builtin_replication()
        interval = _positive_int("SYNC_INTERVAL", 300)
        lookback_minutes = _positive_int("SYNC_LOOKBACK", 30)
        chunk = _positive_int("SYNC_CHUNK_SECONDS", 300)
        overlap = _nonnegative_int("SYNC_OVERLAP_SECONDS", 30)
        settle = _nonnegative_int("SYNC_SETTLE_DELAY_SECONDS", 15)
        return cls(
            source=Endpoint(
                os.getenv("SOURCE_URL", "http://localhost:8428").rstrip("/"),
                os.getenv("SOURCE_USER", ""),
                _secret("SOURCE_PASSWORD"),
                os.getenv("SOURCE_CA_FILE", ""),
            ),
            destination=Endpoint(
                os.getenv("DEST_URL", "http://localhost:8428").rstrip("/"),
                os.getenv("DEST_USER", ""),
                _secret("DEST_PASSWORD"),
                os.getenv("DEST_CA_FILE", ""),
            ),
            interval_seconds=interval,
            initial_lookback_seconds=lookback_minutes * 60,
            chunk_seconds=chunk,
            overlap_seconds=overlap,
            settle_delay_seconds=settle,
            state_file=Path(os.getenv("SYNC_STATE_FILE", "/state/vm-sync.json")),
            request_timeout_seconds=_positive_int("REQUEST_TIMEOUT", 120),
            max_attempts=_positive_int("RETRY_MAX_ATTEMPTS", 4),
            retry_initial_seconds=float(os.getenv("RETRY_INITIAL_SECONDS", "1")),
            retry_max_seconds=float(os.getenv("RETRY_MAX_SECONDS", "30")),
            health_port=_positive_int("HEALTH_PORT", 8080),
            readiness_max_age_seconds=_positive_int(
                "READINESS_MAX_AGE", max(interval * 3, 900)
            ),
            max_rows_per_line=_positive_int("MAX_ROWS_PER_LINE", 10000),
            initial_start_timestamp=_optional_nonnegative_int(
                "SYNC_START_TIMESTAMP"
            ),
        )


@dataclass(frozen=True)
class SyncResult:
    success: bool
    watermark: int
    windows: int = 0
    series: int = 0
    byte_count: int = 0
    retries: int = 0
    error: str = ""


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
        path: str,
        *,
        method: str = "GET",
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> bytes:
        url = f"{endpoint.url}{path}"
        request_headers = dict(endpoint.headers)
        request_headers.update(headers or {})
        request = urllib.request.Request(
            url, data=data, headers=request_headers, method=method
        )

        for attempt in range(1, self.max_attempts + 1):
            try:
                kwargs = {"timeout": self.timeout}
                context = endpoint.ssl_context
                if context is not None:
                    kwargs["context"] = context
                with self.opener(request, **kwargs) as response:
                    status = int(getattr(response, "status", 200))
                    if status < 200 or status >= 300:
                        raise urllib.error.HTTPError(
                            url, status, f"unexpected HTTP status {status}", {}, None
                        )
                    return response.read()
            except urllib.error.HTTPError as exc:
                if exc.code not in TRANSIENT_HTTP_CODES or attempt >= self.max_attempts:
                    raise
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                self._backoff(attempt, retry_after)
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt >= self.max_attempts:
                    raise
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


class VictoriaMetricsSync:
    def __init__(
        self,
        config: Config,
        *,
        status: Optional[ServiceStatus] = None,
        http: Optional[RetryingHttpClient] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.clock = clock
        self.status = status or ServiceStatus(
            "vm_sync", config.readiness_max_age_seconds
        )
        self.http = http or RetryingHttpClient(
            config.request_timeout_seconds,
            config.max_attempts,
            config.retry_initial_seconds,
            config.retry_max_seconds,
        )

    def load_watermark(self, now: Optional[int] = None) -> int:
        current = int(self.clock()) if now is None else int(now)
        try:
            payload = json.loads(self.config.state_file.read_text(encoding="utf-8"))
            watermark = int(payload["successful_through"])
        except FileNotFoundError:
            # Anchor the first attempted window before touching the network.
            # Otherwise a prolonged first-start outage would slide this value
            # forward on every retry and silently abandon old data.
            if self.config.initial_start_timestamp is not None:
                watermark = min(self.config.initial_start_timestamp, current)
            else:
                watermark = max(0, current - self.config.initial_lookback_seconds)
            self.save_watermark(watermark)
            return watermark
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError("invalid vm-sync state file") from exc
        return min(max(watermark, 0), current)

    def save_watermark(self, watermark: int) -> None:
        state_file = self.config.state_file
        state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(state_file.parent),
                prefix=f".{state_file.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                json.dump(
                    {"successful_through": int(watermark)},
                    temporary,
                    sort_keys=True,
                )
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, state_file)
        finally:
            if temporary_name and os.path.exists(temporary_name):
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass

    def sync_window(self, start: int, end: int) -> tuple[int, int]:
        query = urllib.parse.urlencode(
            [
                ("match[]", '{__name__!=""}'),
                ("start", str(start)),
                ("end", str(end)),
                ("max_rows_per_line", str(self.config.max_rows_per_line)),
            ]
        )
        exported = self.http.request(
            self.config.source, f"/api/v1/export?{query}"
        )
        if not exported.strip():
            return 0, 0
        self.http.request(
            self.config.destination,
            "/api/v1/import",
            method="POST",
            data=exported,
            headers={"Content-Type": "application/json"},
        )
        series = len(exported.rstrip(b"\n").split(b"\n"))
        return series, len(exported)

    def run_once(self) -> SyncResult:
        started = self.clock()
        self.status.mark_attempt()
        retries_before = self.http.retries_total
        target = max(0, int(started) - self.config.settle_delay_seconds)
        original_watermark = 0
        windows = 0
        series = 0
        byte_count = 0

        try:
            original_watermark = self.load_watermark(int(started))
            cursor = min(original_watermark, target)
            while cursor < target:
                chunk_end = min(cursor + self.config.chunk_seconds, target)
                export_start = max(0, cursor - self.config.overlap_seconds)
                window_series, window_bytes = self.sync_window(export_start, chunk_end)
                windows += 1
                series += window_series
                byte_count += window_bytes
                cursor = chunk_end

            # Commit once, after the whole run.  A partial run never advances state.
            self.save_watermark(target)
            duration = max(self.clock() - started, 0.0)
            retries = self.http.retries_total - retries_before
            self.status.set_gauge("watermark_timestamp_seconds", target)
            self.status.set_gauge("lag_seconds", max(self.clock() - target, 0.0))
            self.status.set_gauge("retries_total", self.http.retries_total)
            self.status.mark_success(
                duration, series, byte_count, windows=windows, watermark=target
            )
            return SyncResult(
                True, target, windows, series, byte_count, retries
            )
        except Exception as exc:
            duration = max(self.clock() - started, 0.0)
            retries = self.http.retries_total - retries_before
            self.status.set_gauge("watermark_timestamp_seconds", original_watermark)
            self.status.set_gauge(
                "lag_seconds", max(self.clock() - original_watermark, 0.0)
            )
            self.status.set_gauge("retries_total", self.http.retries_total)
            self.status.mark_failure(exc, duration, windows=windows)
            log(f"Sync failed: {exc}", "ERROR")
            return SyncResult(
                False,
                original_watermark,
                windows,
                series,
                byte_count,
                retries,
                str(exc),
            )


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

    status = ServiceStatus("vm_sync", config.readiness_max_age_seconds)
    status.start_http_server(config.health_port)
    service = VictoriaMetricsSync(config, status=status)
    log(f"VictoriaMetrics sync: {config.source.url} -> {config.destination.url}")
    log(
        f"interval={config.interval_seconds}s chunk={config.chunk_seconds}s "
        f"state={config.state_file} health=:{config.health_port} "
        "initial_start="
        f"{config.initial_start_timestamp if config.initial_start_timestamp is not None else 'lookback'}"
    )

    if args.once:
        result = service.run_once()
        return 0 if result.success else 1

    next_run = time.monotonic()
    while True:
        result = service.run_once()
        if result.success:
            log(
                f"Synced {result.windows} windows, {result.series} series, "
                f"{result.byte_count} bytes; watermark={result.watermark}"
            )
        next_run += config.interval_seconds
        now_monotonic = time.monotonic()
        if next_run <= now_monotonic:
            # Catch up promptly without entering a hot loop when a run takes
            # longer than the configured interval.
            next_run = now_monotonic + 1.0
        delay = next_run - now_monotonic
        status.touch(next_run_in_seconds=delay)
        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
