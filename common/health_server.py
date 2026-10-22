"""Small dependency-free health and Prometheus metrics server.

The services in this repository are deliberately lightweight.  This module
gives each long-running process consistent liveness, readiness, and sync-loop
metrics without adding another runtime dependency.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional


def _metric_name(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_:]", "_", value)
    if not value or not re.match(r"[a-zA-Z_:]", value):
        value = f"service_{value}"
    return value


def _prometheus_number(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return str(float(value))


class ServiceStatus:
    """Thread-safe status shared by a service loop and its HTTP server."""

    def __init__(
        self,
        service_name: str,
        readiness_max_age: float,
        liveness_max_age: Optional[float] = None,
    ) -> None:
        self.service_name = service_name
        self.metric_prefix = _metric_name(service_name.replace("-", "_"))
        self.readiness_max_age = max(float(readiness_max_age), 1.0)
        self.liveness_max_age = max(
            float(liveness_max_age or self.readiness_max_age * 2), 1.0
        )
        self.started_at = time.time()
        self.last_loop_at = self.started_at
        self.last_attempt_at = 0.0
        self.last_success_at = 0.0
        self.last_duration_seconds = 0.0
        self.consecutive_failures = 0
        self.runs_total = 0
        self.failures_total = 0
        self.items_total = 0
        self.bytes_total = 0
        self.last_error = ""
        self.details: Dict[str, Any] = {}
        self.gauges: Dict[str, float] = {}
        self._lock = threading.Lock()

    def touch(self, **details: Any) -> None:
        """Record that the main loop is alive without changing readiness."""
        with self._lock:
            self.last_loop_at = time.time()
            self.details.update(details)

    def mark_attempt(self, **details: Any) -> None:
        with self._lock:
            now = time.time()
            self.last_loop_at = now
            self.last_attempt_at = now
            self.details.update(details)

    def mark_success(
        self,
        duration_seconds: float = 0.0,
        items: int = 0,
        byte_count: int = 0,
        **details: Any,
    ) -> None:
        with self._lock:
            now = time.time()
            self.last_loop_at = now
            self.last_success_at = now
            self.last_duration_seconds = max(float(duration_seconds), 0.0)
            self.consecutive_failures = 0
            self.runs_total += 1
            self.items_total += max(int(items), 0)
            self.bytes_total += max(int(byte_count), 0)
            self.last_error = ""
            self.details.update(details)

    def mark_failure(self, error: Any, duration_seconds: float = 0.0, **details: Any) -> None:
        with self._lock:
            now = time.time()
            self.last_loop_at = now
            self.last_duration_seconds = max(float(duration_seconds), 0.0)
            self.consecutive_failures += 1
            self.runs_total += 1
            self.failures_total += 1
            self.last_error = str(error)[:500]
            self.details.update(details)

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self.gauges[_metric_name(name)] = float(value)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            live = now - self.last_loop_at <= self.liveness_max_age
            ready = (
                live
                and self.last_success_at > 0
                and now - self.last_success_at <= self.readiness_max_age
            )
            return {
                "service": self.service_name,
                "live": live,
                "ready": ready,
                "started_at": self.started_at,
                "last_loop_at": self.last_loop_at,
                "last_attempt_at": self.last_attempt_at,
                "last_success_at": self.last_success_at,
                "last_duration_seconds": self.last_duration_seconds,
                "consecutive_failures": self.consecutive_failures,
                "runs_total": self.runs_total,
                "failures_total": self.failures_total,
                "items_total": self.items_total,
                "bytes_total": self.bytes_total,
                "last_error": self.last_error,
                "details": dict(self.details),
                "gauges": dict(self.gauges),
            }

    def prometheus(self) -> str:
        snapshot = self.snapshot()
        prefix = self.metric_prefix
        now = time.time()
        values = {
            "up": 1.0 if snapshot["live"] else 0.0,
            "ready": 1.0 if snapshot["ready"] else 0.0,
            "start_timestamp_seconds": snapshot["started_at"],
            "last_loop_timestamp_seconds": snapshot["last_loop_at"],
            "last_attempt_timestamp_seconds": snapshot["last_attempt_at"],
            "last_success_timestamp_seconds": snapshot["last_success_at"],
            "seconds_since_last_success": (
                now - snapshot["last_success_at"]
                if snapshot["last_success_at"]
                else float("inf")
            ),
            "last_duration_seconds": snapshot["last_duration_seconds"],
            "consecutive_failures": snapshot["consecutive_failures"],
            "runs_total": snapshot["runs_total"],
            "failures_total": snapshot["failures_total"],
            "items_total": snapshot["items_total"],
            "bytes_total": snapshot["bytes_total"],
        }
        values.update(snapshot["gauges"])
        return "".join(
            f"{prefix}_{_metric_name(name)} {_prometheus_number(float(value))}\n"
            for name, value in sorted(values.items())
        )

    def start_http_server(self, port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
        status = self

        class StatusHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                snapshot = status.snapshot()
                if self.path == "/healthz":
                    self._json(snapshot, HTTPStatus.OK if snapshot["live"] else HTTPStatus.SERVICE_UNAVAILABLE)
                elif self.path == "/readyz":
                    self._json(snapshot, HTTPStatus.OK if snapshot["ready"] else HTTPStatus.SERVICE_UNAVAILABLE)
                elif self.path == "/metrics":
                    body = status.prometheus().encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

            def _json(self, payload: Dict[str, Any], status_code: HTTPStatus) -> None:
                body = json.dumps(payload, sort_keys=True).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args: Any) -> None:
                return

        server = ThreadingHTTPServer((host, int(port)), StatusHandler)
        thread = threading.Thread(
            target=server.serve_forever,
            name=f"{self.service_name}-health",
            daemon=True,
        )
        thread.start()
        return server
