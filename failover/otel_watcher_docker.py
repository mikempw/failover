#!/usr/bin/env python3
"""Keep the local OTEL collector aligned with the failover DNS record.

The watcher deliberately fails closed: an indeterminate DNS answer or Docker
state never causes a start/stop transition.  It continuously reconciles the
actual container state, so a failed action is retried even when DNS is stable.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable, Mapping, Optional, Sequence, Tuple

try:
    from common.health_server import ServiceStatus
except ModuleNotFoundError as exc:
    # Support direct source-tree execution (``python failover/...py``) while
    # keeping the container layout dependency-free.
    if exc.name != "common":
        raise
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from common.health_server import ServiceStatus


CommandRunner = Callable[..., subprocess.CompletedProcess]
GetAddrInfo = Callable[..., list]
Logger = Callable[[str, str], None]


def log(message: str, level: str = "INFO") -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] [otel-watcher] {message}", flush=True)


class ConfigurationError(ValueError):
    """Raised when watcher configuration is unsafe or incomplete."""


class DNSResolutionError(RuntimeError):
    """Raised when no resolver returns a usable, trusted answer."""


class LeaseProofError(RuntimeError):
    """Raised when the local controller cannot prove a current DNS lease."""


def _parse_bool(value: object, name: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _positive_int(value: object, name: str, *, maximum: Optional[int] = None) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if parsed <= 0 or (maximum is not None and parsed > maximum):
        suffix = f" and <= {maximum}" if maximum is not None else ""
        raise ConfigurationError(f"{name} must be > 0{suffix}")
    return parsed


def _positive_float(value: object, name: str) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if parsed <= 0:
        raise ConfigurationError(f"{name} must be > 0")
    return parsed


def _nonnegative_int(value: object, name: str) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if parsed < 0:
        raise ConfigurationError(f"{name} must be >= 0")
    return parsed


def normalize_ipv4(value: object, name: str = "IP address") -> str:
    """Validate and normalize a single IPv4 address."""
    if value is None or not str(value).strip():
        raise ConfigurationError(f"{name} is required")
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a valid IPv4 address") from exc
    if not isinstance(address, ipaddress.IPv4Address):
        raise ConfigurationError(f"{name} must be an IPv4 address")
    return str(address)


def derive_my_ip(environment: Mapping[str, str]) -> str:
    """Return MY_IP, or derive it from ROLE and the matching site IP."""
    explicit = environment.get("MY_IP", "").strip()
    role = environment.get("ROLE", "").strip().lower()

    if role and role not in {"primary", "dr"}:
        raise ConfigurationError("ROLE must be 'primary' or 'dr'")
    if explicit:
        return normalize_ipv4(explicit, "MY_IP")
    if not role:
        raise ConfigurationError(
            "MY_IP is not set; ROLE is required to select PRIMARY_IP or DR_IP"
        )

    source_name = "PRIMARY_IP" if role == "primary" else "DR_IP"
    return normalize_ipv4(environment.get(source_name), source_name)


@dataclass(frozen=True)
class WatcherConfig:
    dns_record: str
    my_ip: str
    role: str
    primary_ip: str
    dr_ip: str
    lease_proof_file: str
    max_clock_skew: int
    otel_container: str
    check_interval: float
    dns_servers: Tuple[str, ...]
    use_system_resolver: bool
    dnssec_required: bool
    consistent_answer_threshold: int
    dns_query_timeout: float
    docker_timeout: float
    stop_timeout: int
    dns_backoff_initial: float
    dns_backoff_max: float
    action_backoff_initial: float
    action_backoff_max: float
    status_host: str
    status_port: int
    readiness_max_age: float

    @classmethod
    def from_env(cls, environment: Optional[Mapping[str, str]] = None) -> "WatcherConfig":
        env = os.environ if environment is None else environment

        dns_record = env.get("DNS_RECORD", "syslog.ast.example.local").strip().rstrip(".")
        if not dns_record or any(character.isspace() for character in dns_record):
            raise ConfigurationError("DNS_RECORD must be a non-empty DNS name")

        requested_role = env.get("ROLE", "").strip().lower()
        my_ip = derive_my_ip(env)
        primary_ip = normalize_ipv4(env.get("PRIMARY_IP"), "PRIMARY_IP")
        dr_ip = normalize_ipv4(env.get("DR_IP"), "DR_IP")
        if primary_ip == dr_ip:
            raise ConfigurationError("PRIMARY_IP and DR_IP must be different")
        if my_ip == primary_ip:
            role = "primary"
        elif my_ip == dr_ip:
            role = "dr"
        else:
            raise ConfigurationError("MY_IP must equal PRIMARY_IP or DR_IP")
        if requested_role and requested_role != role:
            raise ConfigurationError("ROLE does not match the configured local site IP")

        lease_proof_file = env.get(
            "LEASE_PROOF_FILE", "/state/observed-lease.json"
        ).strip()
        if not lease_proof_file or not os.path.isabs(lease_proof_file):
            raise ConfigurationError("LEASE_PROOF_FILE must be an absolute path")
        container = env.get("OTEL_CONTAINER", "otel-collector").strip()
        if not container:
            raise ConfigurationError("OTEL_CONTAINER must not be empty")

        raw_servers = env.get("DNS_SERVERS")
        if raw_servers is None:
            raw_servers = env.get("DNS_SERVER", "")
        servers = tuple(
            server.strip().lstrip("@")
            for server in raw_servers.split(",")
            if server.strip().lstrip("@")
        )
        for server in servers:
            if any(character.isspace() for character in server):
                raise ConfigurationError(f"Invalid DNS server: {server!r}")

        use_system = _parse_bool(
            env.get("DNS_USE_SYSTEM_RESOLVER", "true"),
            "DNS_USE_SYSTEM_RESOLVER",
        )
        require_dnssec = _parse_bool(
            env.get("DNSSEC_REQUIRED", "false"), "DNSSEC_REQUIRED"
        )
        if not servers and not use_system:
            raise ConfigurationError(
                "At least one DNS_SERVERS entry or DNS_USE_SYSTEM_RESOLVER=true is required"
            )
        if require_dnssec and not servers:
            raise ConfigurationError(
                "DNSSEC_REQUIRED=true requires at least one configured DNS_SERVERS entry"
            )

        check_interval = _positive_float(
            env.get("OTEL_CHECK_INTERVAL", "15"), "OTEL_CHECK_INTERVAL"
        )
        dns_backoff_initial = _positive_float(
            env.get("DNS_BACKOFF_INITIAL", "1"), "DNS_BACKOFF_INITIAL"
        )
        dns_backoff_max = _positive_float(
            env.get("DNS_BACKOFF_MAX", "30"), "DNS_BACKOFF_MAX"
        )
        action_backoff_initial = _positive_float(
            env.get("ACTION_BACKOFF_INITIAL", "1"), "ACTION_BACKOFF_INITIAL"
        )
        action_backoff_max = _positive_float(
            env.get("ACTION_BACKOFF_MAX", "30"), "ACTION_BACKOFF_MAX"
        )
        if dns_backoff_initial > dns_backoff_max:
            raise ConfigurationError("DNS_BACKOFF_INITIAL must be <= DNS_BACKOFF_MAX")
        if action_backoff_initial > action_backoff_max:
            raise ConfigurationError("ACTION_BACKOFF_INITIAL must be <= ACTION_BACKOFF_MAX")

        readiness_default = max(check_interval * 3, 30.0)
        status_port = _positive_int(
            env.get("STATUS_PORT", "8080"), "STATUS_PORT", maximum=65535
        )

        return cls(
            dns_record=dns_record,
            my_ip=my_ip,
            role=role,
            primary_ip=primary_ip,
            dr_ip=dr_ip,
            lease_proof_file=lease_proof_file,
            max_clock_skew=_nonnegative_int(
                env.get("MAX_CLOCK_SKEW", "5"), "MAX_CLOCK_SKEW"
            ),
            otel_container=container,
            check_interval=check_interval,
            dns_servers=servers,
            use_system_resolver=use_system,
            dnssec_required=require_dnssec,
            consistent_answer_threshold=_positive_int(
                env.get("DNS_CONSISTENT_ANSWERS", "2"),
                "DNS_CONSISTENT_ANSWERS",
            ),
            dns_query_timeout=_positive_float(
                env.get("DNS_QUERY_TIMEOUT", "4"), "DNS_QUERY_TIMEOUT"
            ),
            docker_timeout=_positive_float(
                env.get("DOCKER_TIMEOUT", "30"), "DOCKER_TIMEOUT"
            ),
            stop_timeout=_positive_int(
                env.get("OTEL_STOP_TIMEOUT", "10"), "OTEL_STOP_TIMEOUT"
            ),
            dns_backoff_initial=dns_backoff_initial,
            dns_backoff_max=dns_backoff_max,
            action_backoff_initial=action_backoff_initial,
            action_backoff_max=action_backoff_max,
            status_host=env.get("STATUS_HOST", "127.0.0.1").strip() or "127.0.0.1",
            status_port=status_port,
            readiness_max_age=_positive_float(
                env.get("READINESS_MAX_AGE", str(readiness_default)),
                "READINESS_MAX_AGE",
            ),
        )


@dataclass(frozen=True)
class DNSAnswer:
    addresses: Tuple[str, ...]
    source: str
    authenticated: bool


@dataclass(frozen=True)
class LeaseProof:
    record: str
    ip: str
    owner: str
    expires_at: int
    observed_at: int


def load_lease_proof(path: str) -> LeaseProof:
    """Load the controller's strict, atomic local lease observation."""
    try:
        with open(path, "r", encoding="utf-8") as proof_file:
            raw = proof_file.read(4097)
    except (OSError, UnicodeError) as exc:
        raise LeaseProofError(f"Local lease proof is unavailable: {exc}") from exc
    if len(raw) > 4096:
        raise LeaseProofError("Local lease proof is too large")
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise LeaseProofError("Local lease proof is not valid JSON") from exc
    required_keys = {
        "version", "record", "ip", "owner", "expires_at", "observed_at"
    }
    if not isinstance(payload, dict) or set(payload) != required_keys:
        raise LeaseProofError("Local lease proof has an unexpected schema")
    if payload["version"] != 1:
        raise LeaseProofError("Local lease proof version is unsupported")
    record = payload["record"]
    owner = payload["owner"]
    if not isinstance(record, str) or not record or any(c.isspace() for c in record):
        raise LeaseProofError("Local lease proof has an invalid DNS record")
    if owner not in {"primary", "dr"}:
        raise LeaseProofError("Local lease proof has an invalid owner")
    try:
        ip = normalize_ipv4(payload["ip"], "lease proof IP")
    except ConfigurationError as exc:
        raise LeaseProofError(str(exc)) from exc
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
        raise LeaseProofError("Local lease proof has invalid timestamps")
    return LeaseProof(
        record=record.rstrip(".").lower(),
        ip=ip,
        owner=owner,
        expires_at=expires_at,
        observed_at=observed_at,
    )


class DNSResolver:
    """Resolve A records through configured resolvers, then optionally the OS."""

    def __init__(
        self,
        record: str,
        servers: Sequence[str],
        *,
        use_system_resolver: bool,
        dnssec_required: bool,
        timeout: float,
        run_command: CommandRunner = subprocess.run,
        getaddrinfo: GetAddrInfo = socket.getaddrinfo,
        logger: Logger = log,
    ) -> None:
        self.record = record
        self.servers = tuple(servers)
        self.use_system_resolver = use_system_resolver
        self.dnssec_required = dnssec_required
        self.timeout = timeout
        self.run_command = run_command
        self.getaddrinfo = getaddrinfo
        self.logger = logger

    @staticmethod
    def _normalize_answers(addresses: Sequence[object], source: str) -> Tuple[str, ...]:
        if not addresses:
            raise DNSResolutionError(f"{source} returned no A records")
        normalized = []
        for address in addresses:
            try:
                normalized.append(normalize_ipv4(address, f"A answer from {source}"))
            except ConfigurationError as exc:
                raise DNSResolutionError(str(exc)) from exc
        return tuple(sorted(set(normalized), key=lambda value: int(ipaddress.IPv4Address(value))))

    @staticmethod
    def _parse_dig_output(output: str, source: str) -> Tuple[Tuple[str, ...], bool]:
        flags = set()
        addresses = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if line.startswith(";; flags:"):
                flag_text = line.split("flags:", 1)[1].split(";", 1)[0]
                flags.update(flag.lower() for flag in flag_text.split())
                continue
            if not line or line.startswith(";"):
                continue
            fields = line.split()
            if len(fields) >= 2 and fields[-2].upper() == "A":
                addresses.append(fields[-1])
        return DNSResolver._normalize_answers(addresses, source), "ad" in flags

    def _resolve_with_dig(self, server: str) -> DNSAnswer:
        source = f"dns:{server}"
        try:
            result = self.run_command(
                [
                    "dig",
                    f"@{server}",
                    self.record,
                    "A",
                    "+dnssec",
                    "+adflag",
                    "+time=2",
                    "+tries=1",
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DNSResolutionError(f"{source} query failed: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "dig failed").strip()
            raise DNSResolutionError(f"{source} query failed: {detail}")

        addresses, authenticated = self._parse_dig_output(result.stdout, source)
        if self.dnssec_required and not authenticated:
            raise DNSResolutionError(f"{source} response did not contain the DNSSEC AD flag")
        return DNSAnswer(addresses, source, authenticated)

    def _resolve_with_system(self) -> DNSAnswer:
        source = "system"
        try:
            records = self.getaddrinfo(
                self.record,
                None,
                socket.AF_INET,
                socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise DNSResolutionError(f"system resolver failed: {exc}") from exc
        addresses = [record[4][0] for record in records]
        return DNSAnswer(self._normalize_answers(addresses, source), source, False)

    def resolve(self) -> DNSAnswer:
        failures = []
        for server in self.servers:
            try:
                return self._resolve_with_dig(server)
            except DNSResolutionError as exc:
                failures.append(str(exc))
                self.logger(str(exc), "WARN")

        if self.use_system_resolver and not self.dnssec_required:
            try:
                return self._resolve_with_system()
            except DNSResolutionError as exc:
                failures.append(str(exc))
                self.logger(str(exc), "WARN")
        elif self.use_system_resolver and self.dnssec_required:
            failures.append("system resolver skipped because it cannot prove the DNSSEC AD flag")

        detail = "; ".join(failures) or "no DNS resolvers configured"
        raise DNSResolutionError(f"Unable to resolve {self.record}: {detail}")


class AnswerConsistency:
    """Require an identical answer set for N consecutive observations."""

    def __init__(self, threshold: int) -> None:
        self.threshold = max(int(threshold), 1)
        self._last: Optional[Tuple[str, ...]] = None
        self._count = 0

    @property
    def count(self) -> int:
        return self._count

    def observe(self, addresses: Tuple[str, ...]) -> bool:
        if addresses == self._last:
            self._count = min(self._count + 1, self.threshold)
        else:
            self._last = addresses
            self._count = 1
        return self._count >= self.threshold

    def reset(self) -> None:
        self._last = None
        self._count = 0


class ContainerState(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    MISSING = "missing"
    ERROR = "error"


@dataclass(frozen=True)
class InspectResult:
    state: ContainerState
    error: str = ""


@dataclass(frozen=True)
class ReconcileResult:
    success: bool
    state: ContainerState
    changed: bool = False
    error: str = ""


class DockerManager:
    def __init__(
        self,
        container: str,
        *,
        command_timeout: float,
        stop_timeout: int,
        run_command: CommandRunner = subprocess.run,
    ) -> None:
        self.container = container
        self.command_timeout = command_timeout
        self.stop_timeout = stop_timeout
        self.run_command = run_command

    def inspect(self) -> InspectResult:
        try:
            result = self.run_command(
                ["docker", "inspect", "-f", "{{.State.Running}}", self.container],
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return InspectResult(ContainerState.ERROR, str(exc))

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "docker inspect failed").strip()
            lowered = detail.lower()
            if "no such object" in lowered or "no such container" in lowered:
                return InspectResult(ContainerState.MISSING, detail)
            return InspectResult(ContainerState.ERROR, detail)

        value = result.stdout.strip().lower()
        if value == "true":
            return InspectResult(ContainerState.RUNNING)
        if value == "false":
            return InspectResult(ContainerState.STOPPED)
        return InspectResult(
            ContainerState.ERROR,
            f"unexpected docker inspect response: {result.stdout.strip()!r}",
        )

    def _action(self, should_run: bool) -> Tuple[bool, str]:
        action = "start" if should_run else "stop"
        command = ["docker", action]
        timeout = self.command_timeout
        if not should_run:
            command.extend(["-t", str(self.stop_timeout)])
            timeout = max(timeout, float(self.stop_timeout + 5))
        command.append(self.container)

        try:
            result = self.run_command(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or f"docker {action} failed").strip()
        return True, ""

    def reconcile(self, should_run: bool) -> ReconcileResult:
        before = self.inspect()
        if before.state in {ContainerState.ERROR, ContainerState.MISSING}:
            return ReconcileResult(False, before.state, error=before.error)

        desired_state = ContainerState.RUNNING if should_run else ContainerState.STOPPED
        if before.state == desired_state:
            return ReconcileResult(True, before.state)

        action = "start" if should_run else "stop"
        action_label = "Starting" if should_run else "Stopping"
        log(f"{action_label} container: {self.container}")
        action_ok, error = self._action(should_run)
        if not action_ok:
            return ReconcileResult(False, before.state, error=error)

        after = self.inspect()
        if after.state != desired_state:
            detail = after.error or (
                f"container remained {after.state.value} after docker {action}"
            )
            return ReconcileResult(False, after.state, changed=True, error=detail)
        return ReconcileResult(True, after.state, changed=True)


class RunKind(str, Enum):
    SUCCESS = "success"
    PENDING = "pending"
    DNS_FAILURE = "dns_failure"
    ACTION_FAILURE = "action_failure"


@dataclass(frozen=True)
class RunResult:
    kind: RunKind
    message: str = ""


class OtelWatcher:
    def __init__(
        self,
        config: WatcherConfig,
        resolver: DNSResolver,
        docker: DockerManager,
        status: ServiceStatus,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        proof_reader: Optional[Callable[[], LeaseProof]] = None,
    ) -> None:
        self.config = config
        self.resolver = resolver
        self.docker = docker
        self.status = status
        self.consistency = AnswerConsistency(config.consistent_answer_threshold)
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self.proof_reader = proof_reader or (
            lambda: load_lease_proof(config.lease_proof_file)
        )

    def _validate_proof(self, address: str) -> LeaseProof:
        proof = self.proof_reader()
        now = int(self.wall_clock())
        if proof.record != self.config.dns_record.rstrip(".").lower():
            raise LeaseProofError("Local lease proof names a different DNS record")
        if proof.ip != address:
            raise LeaseProofError("Local lease proof disagrees with the DNS A record")
        expected_ip = (
            self.config.primary_ip if proof.owner == "primary" else self.config.dr_ip
        )
        if proof.ip != expected_ip:
            raise LeaseProofError("Local lease proof owner and IP are incoherent")
        if proof.observed_at > now + self.config.max_clock_skew:
            raise LeaseProofError("Local lease proof was observed in the future")
        if proof.expires_at <= now + self.config.max_clock_skew:
            raise LeaseProofError("Local lease proof is expired or inside the skew guard")
        return proof

    def _untrusted_result(
        self,
        kind: RunKind,
        message: str,
        started: float,
        **details: object,
    ) -> RunResult:
        """Immediately fence when DNS cannot prove local ownership."""
        # Unknown or ambiguous DNS is not proof of ownership. Stop locally even
        # if that sacrifices availability; retaining the current state can leave
        # both sites collecting after DR promotion.
        fenced = self.docker.reconcile(False)
        self.status.set_gauge(
            "container_running", 1 if fenced.state == ContainerState.RUNNING else 0
        )
        details.update(
            ownership_fenced=True,
            container_state=fenced.state.value,
        )
        if not fenced.success:
            fence_error = fenced.error or "could not fence collector"
            combined = f"{message}; fencing failed: {fence_error}"
            self.status.mark_failure(
                combined, self.monotonic() - started, **details
            )
            log(combined, "ERROR")
            return RunResult(RunKind.ACTION_FAILURE, combined)

        self.status.mark_failure(message, self.monotonic() - started, **details)
        if fenced.changed:
            log("Stopped collector because DNS ownership could not be proven", "WARN")
        return RunResult(kind, message)

    def run_once(self) -> RunResult:
        started = self.monotonic()
        self.status.mark_attempt(dns_record=self.config.dns_record)

        try:
            answer = self.resolver.resolve()
        except DNSResolutionError as exc:
            self.consistency.reset()
            self.status.set_gauge("dns_resolution_success", 0)
            log(str(exc), "WARN")
            return self._untrusted_result(
                RunKind.DNS_FAILURE,
                str(exc),
                started,
                dns_source="unavailable",
            )

        self.status.set_gauge("dns_resolution_success", 1)
        self.status.set_gauge("dns_answer_count", len(answer.addresses))
        if len(answer.addresses) != 1:
            self.consistency.reset()
            message = (
                f"Expected exactly one A record for {self.config.dns_record}; "
                f"received {len(answer.addresses)}: {', '.join(answer.addresses)}"
            )
            log(message, "ERROR")
            return self._untrusted_result(
                RunKind.DNS_FAILURE,
                message,
                started,
                dns_answers=answer.addresses,
                dns_source=answer.source,
            )

        try:
            proof = self._validate_proof(answer.addresses[0])
        except LeaseProofError as exc:
            self.consistency.reset()
            message = f"Ownership proof rejected: {exc}"
            log(message, "ERROR")
            return self._untrusted_result(
                RunKind.DNS_FAILURE,
                message,
                started,
                dns_answers=answer.addresses,
                dns_source=answer.source,
                lease_proof_valid=False,
            )

        desired_active = proof.owner == self.config.role and proof.ip == self.config.my_ip
        self.status.set_gauge("desired_active", 1 if desired_active else 0)

        if not self.consistency.observe(answer.addresses):
            message = (
                "Waiting for consistent DNS answer "
                f"({self.consistency.count}/{self.config.consistent_answer_threshold}): "
                f"{', '.join(answer.addresses)}"
            )
            if not desired_active:
                log(message, "WARN")
                return self._untrusted_result(
                    RunKind.PENDING,
                    message,
                    started,
                    dns_answers=answer.addresses,
                    dns_source=answer.source,
                    dns_consistency=self.consistency.count,
                )
            self.status.touch(
                dns_answers=answer.addresses,
                dns_source=answer.source,
                dns_consistency=self.consistency.count,
            )
            log(message)
            return RunResult(RunKind.PENDING, message)

        result = self.docker.reconcile(desired_active)
        self.status.set_gauge(
            "container_running", 1 if result.state == ContainerState.RUNNING else 0
        )
        details = {
            "dns_answers": answer.addresses,
            "dns_source": answer.source,
            "dnssec_authenticated": answer.authenticated,
            "lease_owner": proof.owner,
            "lease_expires_at": proof.expires_at,
            "lease_proof_valid": True,
            "desired_active": desired_active,
            "container_state": result.state.value,
        }
        if not result.success:
            message = result.error or f"container state is {result.state.value}"
            self.status.mark_failure(message, self.monotonic() - started, **details)
            log(f"Container reconciliation failed: {message}", "ERROR")
            return RunResult(RunKind.ACTION_FAILURE, message)

        if result.changed:
            verb = "started" if desired_active else "stopped"
            log(f"Container {self.config.otel_container} {verb} successfully")
        self.status.mark_success(self.monotonic() - started, **details)
        return RunResult(RunKind.SUCCESS)


class ExponentialBackoff:
    def __init__(self, initial: float, maximum: float) -> None:
        self.initial = float(initial)
        self.maximum = float(maximum)
        self._next = self.initial

    def next_delay(self) -> float:
        delay = self._next
        self._next = min(self.maximum, self._next * 2)
        return delay

    def reset(self) -> None:
        self._next = self.initial


def _build_watcher(config: WatcherConfig, status: ServiceStatus) -> OtelWatcher:
    resolver = DNSResolver(
        config.dns_record,
        config.dns_servers,
        use_system_resolver=config.use_system_resolver,
        dnssec_required=config.dnssec_required,
        timeout=config.dns_query_timeout,
    )
    docker = DockerManager(
        config.otel_container,
        command_timeout=config.docker_timeout,
        stop_timeout=config.stop_timeout,
    )
    return OtelWatcher(config, resolver, docker, status)


def _log_configuration(config: WatcherConfig) -> None:
    log("=" * 60)
    log("OTEL Collector Watcher for Docker")
    log("=" * 60)
    log(f"DNS record:       {config.dns_record}")
    log(f"Role:             {config.role}")
    log(f"Site IPv4:        {config.my_ip}")
    log(f"Check interval:   {config.check_interval:g}s")
    log(f"OTEL container:   {config.otel_container}")
    log(f"DNS servers:      {', '.join(config.dns_servers) or '(none)'}")
    log(f"System resolver:  {'enabled' if config.use_system_resolver else 'disabled'}")
    log(f"DNSSEC required:  {config.dnssec_required}")
    log(f"Answer threshold: {config.consistent_answer_threshold}")
    log(f"Lease proof:      {config.lease_proof_file}")
    log(f"Status endpoint:  {config.status_host}:{config.status_port}")
    log("=" * 60)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile OTEL with failover DNS")
    parser.add_argument(
        "--once",
        action="store_true",
        help="perform enough immediate checks to satisfy the consistency threshold, then exit",
    )
    args = parser.parse_args(argv)

    try:
        config = WatcherConfig.from_env()
    except ConfigurationError as exc:
        log(f"Invalid configuration: {exc}", "ERROR")
        return 2

    _log_configuration(config)
    status = ServiceStatus("otel_watcher", config.readiness_max_age)
    watcher = _build_watcher(config, status)

    if args.once:
        result = RunResult(RunKind.PENDING)
        for _ in range(config.consistent_answer_threshold):
            result = watcher.run_once()
            if result.kind != RunKind.PENDING:
                break
        return 0 if result.kind == RunKind.SUCCESS else 1

    try:
        status.start_http_server(config.status_port, config.status_host)
    except OSError as exc:
        log(f"Unable to start status server: {exc}", "ERROR")
        return 1

    stop_event = threading.Event()

    def shutdown(_signum: int, _frame: object) -> None:
        log("Shutdown signal received")
        stop_event.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    dns_backoff = ExponentialBackoff(
        config.dns_backoff_initial, config.dns_backoff_max
    )
    action_backoff = ExponentialBackoff(
        config.action_backoff_initial, config.action_backoff_max
    )

    while not stop_event.is_set():
        try:
            result = watcher.run_once()
        except Exception as exc:  # final containment for a long-running control loop
            status.mark_failure(exc)
            log(f"Unexpected watcher error: {exc}", "ERROR")
            result = RunResult(RunKind.ACTION_FAILURE, str(exc))

        if result.kind == RunKind.DNS_FAILURE:
            delay = dns_backoff.next_delay()
        elif result.kind == RunKind.ACTION_FAILURE:
            dns_backoff.reset()
            delay = action_backoff.next_delay()
        else:
            dns_backoff.reset()
            action_backoff.reset()
            delay = config.check_interval
        stop_event.wait(delay)

    return 0


if __name__ == "__main__":
    sys.exit(main())
