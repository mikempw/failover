#!/usr/bin/env python3
"""
AST DNS Failover - Container Edition
Simple DNS-based failover using A + TXT records with lease mechanism.

Supports 9 DNS providers:
  - dry-run      (local JSON for testing)
  - bind-tsig    (RFC2136 via nsupdate + TSIG)
  - ad-gss       (RFC2136 + GSS-TSIG / Kerberos)
  - infoblox     (Infoblox WAPI REST API)
  - cloudflare   (Cloudflare API)
  - route53      (AWS Route 53)
  - azure-dns    (Azure DNS)
  - gcp-dns      (Google Cloud DNS)
  - f5-gtm       (F5 BIG-IP DNS / GTM)

Configuration sources (in priority order):
  1. HashiCorp Vault (if configured)
  2. Environment variables / .env file

Environment-driven configuration for containers.
"""

import argparse
import ipaddress
import json
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from datetime import datetime

from common.health_server import ServiceStatus

# -----------------------------
# Secret and Vault Integration
# -----------------------------

def read_secret_file(path: str, key: str) -> str:
    """Read a mounted secret without ever including its contents in errors."""
    try:
        with open(path, 'r', encoding='utf-8') as secret_file:
            return secret_file.read().rstrip('\r\n')
    except OSError as exc:
        raise ValueError(f"Unable to read {key}_FILE '{path}': {exc}") from exc


def read_bootstrap_secret(key: str) -> Optional[str]:
    """Read KEY_FILE or KEY, rejecting ambiguous configuration."""
    file_path = os.getenv(f'{key}_FILE')
    value = os.getenv(key)
    if file_path and value:
        raise ValueError(f"Set only one of {key} or {key}_FILE")
    return read_secret_file(file_path, key) if file_path else value

def load_from_vault() -> Dict[str, Any]:
    """
    Load secrets from HashiCorp Vault if configured.
    Returns empty dict if Vault is not configured or unavailable.
    """
    vault_addr = os.getenv('VAULT_ADDR')
    
    if not vault_addr:
        return {}
    
    try:
        import hvac
    except ImportError:
        log("VAULT_ADDR set but hvac not installed. Falling back to env.", "WARN")
        return {}
    
    try:
        client = hvac.Client(url=vault_addr)
        auth_method = os.getenv('VAULT_AUTH_METHOD', 'token').lower()
        
        if auth_method == 'token':
            token = read_bootstrap_secret('VAULT_TOKEN')
            if not token:
                log("VAULT_AUTH_METHOD=token but VAULT_TOKEN not set", "WARN")
                return {}
            client.token = token
            
        elif auth_method == 'approle':
            role_id = os.getenv('VAULT_ROLE_ID')
            secret_id = read_bootstrap_secret('VAULT_SECRET_ID')
            if not role_id or not secret_id:
                log("VAULT_AUTH_METHOD=approle but VAULT_ROLE_ID/VAULT_SECRET_ID not set", "WARN")
                return {}
            client.auth.approle.login(role_id=role_id, secret_id=secret_id)
            
        elif auth_method == 'kubernetes':
            role = os.getenv('VAULT_K8S_ROLE')
            jwt_path = os.getenv('VAULT_K8S_JWT_PATH', '/var/run/secrets/kubernetes.io/serviceaccount/token')
            if not role:
                log("VAULT_AUTH_METHOD=kubernetes but VAULT_K8S_ROLE not set", "WARN")
                return {}
            with open(jwt_path, 'r') as f:
                jwt = f.read()
            client.auth.kubernetes.login(role=role, jwt=jwt)
        else:
            log(f"Unknown VAULT_AUTH_METHOD: {auth_method}", "WARN")
            return {}
        
        if not client.is_authenticated():
            log("Vault authentication failed", "WARN")
            return {}
        
        vault_mount = os.getenv('VAULT_MOUNT', 'secret')
        vault_key = os.getenv('VAULT_KEY', 'dns-failover')
        
        try:
            secret = client.secrets.kv.v2.read_secret_version(path=vault_key, mount_point=vault_mount)
            data = secret['data']['data']
        except Exception:
            secret = client.secrets.kv.v1.read_secret(path=vault_key, mount_point=vault_mount)
            data = secret['data']
        
        log(f"Loaded {len(data)} settings from Vault")
        return data
        
    except Exception as e:
        log(f"Vault error: {e}. Falling back to env.", "WARN")
        return {}


def get_config_value(key: str, vault_data: Dict[str, Any], default: Any = None) -> Any:
    """Get config from Vault, a mounted secret file, env, then default."""
    vault_key = key.lower()
    env_key = key.upper()
    
    if vault_key in vault_data:
        return vault_data[vault_key]
    
    file_path = os.getenv(f'{env_key}_FILE')
    env_val = os.getenv(env_key)
    if file_path and env_val is not None:
        raise ValueError(f"Set only one of {env_key} or {env_key}_FILE")
    if file_path:
        return read_secret_file(file_path, env_key)
    if env_val is not None:
        return env_val
    
    return default

# -----------------------------
# Configuration
# -----------------------------

VALID_PROVIDERS = [
    'dry-run', 'bind-tsig', 'ad-gss', 'infoblox',
    'cloudflare', 'route53', 'azure-dns', 'gcp-dns', 'f5-gtm',
    'script'  # Custom script provider for unsupported DNS platforms
]

@dataclass
class Config:
    # Core settings
    provider: str
    dns_server: str
    dns_zone: str
    dns_record: str
    dns_ttl: int
    primary_ip: str
    dr_ip: str
    lease_ttl: int
    update_interval: int
    fail_threshold: int
    health_host: str
    health_port: int
    health_timeout: int
    health_mode: str  # 'tcp' or 'metrics'
    health_url: Optional[str]  # For metrics mode: http://host:8888/metrics
    health_metric: Optional[str]  # Metric name to check, e.g., otelcol_receiver_accepted_metric_points
    health_stale_count: int  # Number of flat readings before unhealthy
    role: str

    # Safety, failback, and observability
    local_health_url: Optional[str] = None
    failback_health_url: Optional[str] = None
    auto_failback: bool = False
    failback_threshold: int = 3
    max_clock_skew: int = 5
    retry_max: int = 60
    status_port: int = 8080
    write_verify_attempts: int = 3
    write_verify_delay: float = 0.5
    lease_proof_file: Optional[str] = None
    
    # Provider-specific
    dryrun_statefile: Optional[str] = None
    tsig_keyfile: Optional[str] = None
    
    # Infoblox
    infoblox_host: Optional[str] = None
    infoblox_username: Optional[str] = None
    infoblox_password: Optional[str] = None
    infoblox_wapi_version: str = "v2.11"
    infoblox_verify_ssl: bool = True
    
    # Cloudflare
    cloudflare_api_token: Optional[str] = None
    cloudflare_zone_id: Optional[str] = None
    
    # AWS Route53
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    aws_region: str = "us-east-1"
    route53_zone_id: Optional[str] = None
    
    # Azure DNS
    azure_subscription_id: Optional[str] = None
    azure_resource_group: Optional[str] = None
    azure_tenant_id: Optional[str] = None
    azure_client_id: Optional[str] = None
    azure_client_secret: Optional[str] = None
    
    # GCP DNS
    gcp_project_id: Optional[str] = None
    gcp_credentials_file: Optional[str] = None
    gcp_managed_zone: Optional[str] = None
    gcp_change_timeout: float = 30.0
    
    # F5 GTM
    f5_host: Optional[str] = None
    f5_username: Optional[str] = None
    f5_password: Optional[str] = None
    f5_partition: str = "Common"
    f5_verify_ssl: bool = True
    f5_pool_name: Optional[str] = None
    f5_primary_member: Optional[str] = None
    f5_dr_member: Optional[str] = None
    
    # Custom Script Provider
    script_set: Optional[str] = None  # Path to script that sets DNS records
    script_get: Optional[str] = None  # Path to script that gets DNS records
    
    @classmethod
    def from_env(cls):
        """Load configuration from Vault or environment variables."""
        vault_data = load_from_vault()
        if (
            os.getenv('VAULT_ADDR')
            and os.getenv('VAULT_REQUIRED', 'false').lower() in ('true', '1', 'yes')
            and not vault_data
        ):
            raise ValueError("VAULT_REQUIRED is enabled but no Vault data was loaded")
        
        def get(key: str, default: Any = None) -> Any:
            return get_config_value(key, vault_data, default)
        
        def get_bool(key: str, default: bool = False) -> bool:
            val = get(key, str(default))
            normalized = str(val).strip().lower()
            if normalized in ('true', '1', 'yes', 'on'):
                return True
            if normalized in ('false', '0', 'no', 'off'):
                return False
            raise ValueError(f"{key} must be true or false")
        
        def get_int(key: str, default: int = 0) -> int:
            return int(get(key, default))

        def get_float(key: str, default: float = 0.0) -> float:
            return float(get(key, default))
        
        return cls(
            provider=get('DNS_PROVIDER', 'dry-run'),
            dns_server=get('DNS_SERVER', '127.0.0.1'),
            dns_zone=get('DNS_ZONE', 'example.local'),
            dns_record=get('DNS_RECORD', 'syslog.ast.example.local'),
            dns_ttl=get_int('DNS_TTL', 30),
            primary_ip=get('PRIMARY_IP', '10.10.10.10'),
            dr_ip=get('DR_IP', '10.20.20.10'),
            lease_ttl=get_int('LEASE_TTL', 60),
            update_interval=get_int('UPDATE_INTERVAL', 10),
            fail_threshold=get_int('FAIL_THRESHOLD', 3),
            health_host=get('HEALTH_HOST', '10.10.10.10'),
            health_port=get_int('HEALTH_PORT', 6514),
            health_timeout=get_int('HEALTH_TIMEOUT', 2),
            health_mode=get('HEALTH_MODE', 'tcp'),
            health_url=get('HEALTH_URL'),
            health_metric=get('HEALTH_METRIC', 'otelcol_receiver_accepted_metric_points'),
            health_stale_count=get_int('HEALTH_STALE_COUNT', 3),
            role=get('ROLE', 'primary'),
            local_health_url=get('LOCAL_HEALTH_URL'),
            failback_health_url=get('FAILBACK_HEALTH_URL'),
            auto_failback=get_bool('AUTO_FAILBACK', False),
            failback_threshold=get_int('FAILBACK_THRESHOLD', 3),
            max_clock_skew=get_int('MAX_CLOCK_SKEW', 5),
            retry_max=get_int('DNS_RETRY_MAX', 60),
            status_port=get_int('STATUS_PORT', 8080),
            write_verify_attempts=get_int('WRITE_VERIFY_ATTEMPTS', 3),
            write_verify_delay=get_float('WRITE_VERIFY_DELAY', 0.5),
            lease_proof_file=get('LEASE_PROOF_FILE', '/state/observed-lease.json'),
            dryrun_statefile=get('DRYRUN_STATEFILE', '/state/zone.json'),
            tsig_keyfile=get('TSIG_KEYFILE', '/secrets/tsig.key'),
            infoblox_host=get('INFOBLOX_HOST'),
            infoblox_username=get('INFOBLOX_USERNAME'),
            infoblox_password=get('INFOBLOX_PASSWORD'),
            infoblox_wapi_version=get('INFOBLOX_WAPI_VERSION', 'v2.11'),
            infoblox_verify_ssl=get_bool('INFOBLOX_VERIFY_SSL', True),
            cloudflare_api_token=get('CLOUDFLARE_API_TOKEN'),
            cloudflare_zone_id=get('CLOUDFLARE_ZONE_ID'),
            aws_access_key_id=get('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=get('AWS_SECRET_ACCESS_KEY'),
            aws_region=get('AWS_REGION', 'us-east-1'),
            route53_zone_id=get('ROUTE53_ZONE_ID'),
            azure_subscription_id=get('AZURE_SUBSCRIPTION_ID'),
            azure_resource_group=get('AZURE_RESOURCE_GROUP'),
            azure_tenant_id=get('AZURE_TENANT_ID'),
            azure_client_id=get('AZURE_CLIENT_ID'),
            azure_client_secret=get('AZURE_CLIENT_SECRET'),
            gcp_project_id=get('GCP_PROJECT_ID'),
            gcp_credentials_file=get('GCP_CREDENTIALS_FILE'),
            gcp_managed_zone=get('GCP_MANAGED_ZONE'),
            gcp_change_timeout=get_float('GCP_CHANGE_TIMEOUT', 30.0),
            f5_host=get('F5_HOST'),
            f5_username=get('F5_USERNAME'),
            f5_password=get('F5_PASSWORD'),
            f5_partition=get('F5_PARTITION', 'Common'),
            f5_verify_ssl=get_bool('F5_VERIFY_SSL', True),
            f5_pool_name=get('F5_POOL_NAME'),
            f5_primary_member=get('F5_PRIMARY_MEMBER'),
            f5_dr_member=get('F5_DR_MEMBER'),
            script_set=get('SCRIPT_SET'),
            script_get=get('SCRIPT_GET'),
        )
    
    def validate(self):
        """Validate configuration."""
        errors = []
        
        if self.role not in ['primary', 'dr']:
            errors.append(f"Invalid ROLE: {self.role}")
        
        if self.provider not in VALID_PROVIDERS:
            errors.append(f"Invalid DNS_PROVIDER: {self.provider}. Valid: {', '.join(VALID_PROVIDERS)}")

        for name, value in [('PRIMARY_IP', self.primary_ip), ('DR_IP', self.dr_ip)]:
            try:
                address = ipaddress.ip_address(value)
                if not isinstance(address, ipaddress.IPv4Address):
                    errors.append(f"{name} must be IPv4 because this service manages an A record")
                elif (
                    address.is_unspecified
                    or address.is_multicast
                    or address.is_loopback
                    or address.is_link_local
                ):
                    errors.append(f"{name} must be a usable unicast address")
            except ValueError:
                errors.append(f"Invalid {name}: {value}")
        if self.primary_ip == self.dr_ip:
            errors.append("PRIMARY_IP and DR_IP must be different")

        dns_name_re = re.compile(
            r'^(?=.{1,253}\.?$)(?:[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?\.)*'
            r'[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?\.?$'
        )
        record = self.dns_record.rstrip('.').lower()
        zone = self.dns_zone.rstrip('.').lower()
        if not dns_name_re.fullmatch(self.dns_record):
            errors.append(f"Invalid DNS_RECORD: {self.dns_record}")
        if not dns_name_re.fullmatch(self.dns_zone):
            errors.append(f"Invalid DNS_ZONE: {self.dns_zone}")
        if zone and record != zone and not record.endswith(f'.{zone}'):
            errors.append(f"DNS_RECORD {self.dns_record} is not within DNS_ZONE {self.dns_zone}")
        if any(char.isspace() for char in self.dns_server):
            errors.append("DNS_SERVER must not contain whitespace")
        
        if self.provider == 'bind-tsig':
            if not os.path.exists(self.tsig_keyfile or ''):
                errors.append(f"TSIG keyfile not found: {self.tsig_keyfile}")
        
        elif self.provider == 'infoblox':
            if not self.infoblox_host:
                errors.append("INFOBLOX_HOST required")
            if not self.infoblox_username:
                errors.append("INFOBLOX_USERNAME required")
            if not self.infoblox_password:
                errors.append("INFOBLOX_PASSWORD required")
        
        elif self.provider == 'cloudflare':
            if not self.cloudflare_api_token:
                errors.append("CLOUDFLARE_API_TOKEN required")
            if not self.cloudflare_zone_id:
                errors.append("CLOUDFLARE_ZONE_ID required")
            if self.dns_ttl != 1 and not 60 <= self.dns_ttl <= 86400:
                errors.append(
                    "Cloudflare DNS_TTL must be 1 (automatic) or between 60 and 86400"
                )
        
        elif self.provider == 'route53':
            if bool(self.aws_access_key_id) != bool(self.aws_secret_access_key):
                errors.append(
                    "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set together; "
                    "omit both to use the standard AWS credential chain"
                )
            if not self.route53_zone_id:
                errors.append("ROUTE53_ZONE_ID required")
        
        elif self.provider == 'azure-dns':
            for field in ['azure_subscription_id', 'azure_resource_group', 'azure_tenant_id', 'azure_client_id', 'azure_client_secret']:
                if not getattr(self, field):
                    errors.append(f"{field.upper()} required")
        
        elif self.provider == 'gcp-dns':
            if not self.gcp_project_id:
                errors.append("GCP_PROJECT_ID required")
            if not self.gcp_managed_zone:
                errors.append("GCP_MANAGED_ZONE required")
            if not (0 < self.gcp_change_timeout <= 300):
                errors.append("GCP_CHANGE_TIMEOUT must be greater than 0 and at most 300 seconds")
        
        elif self.provider == 'f5-gtm':
            if not self.f5_host:
                errors.append("F5_HOST required")
            if not self.f5_username:
                errors.append("F5_USERNAME required")
            if not self.f5_password:
                errors.append("F5_PASSWORD required")
            if not self.f5_pool_name:
                errors.append("F5_POOL_NAME required")
            if not self.f5_primary_member:
                errors.append("F5_PRIMARY_MEMBER required")
            if not self.f5_dr_member:
                errors.append("F5_DR_MEMBER required")
            if self.f5_primary_member and self.f5_dr_member:
                if self.f5_primary_member == self.f5_dr_member:
                    errors.append("F5_PRIMARY_MEMBER and F5_DR_MEMBER must be different")
                for name, selector in (
                    ('F5_PRIMARY_MEMBER', self.f5_primary_member),
                    ('F5_DR_MEMBER', self.f5_dr_member),
                ):
                    expected_prefix = f"/{self.f5_partition}/"
                    if (
                        not selector.startswith(expected_prefix)
                        or any(char.isspace() for char in selector)
                        or any(char in selector for char in ('?', '#'))
                    ):
                        errors.append(
                            f"{name} must be a fullPath beginning with {expected_prefix}"
                        )
            safe_f5_name = re.compile(r'^[A-Za-z0-9_.-]+$')
            if not safe_f5_name.fullmatch(self.f5_partition):
                errors.append("F5_PARTITION contains unsupported characters")
            if self.f5_pool_name and not safe_f5_name.fullmatch(self.f5_pool_name):
                errors.append("F5_POOL_NAME contains unsupported characters")
            if (
                self.f5_host
                and (
                    any(char.isspace() for char in self.f5_host)
                    or any(char in self.f5_host for char in ('/', '?', '#', '@'))
                )
            ):
                errors.append("F5_HOST must be a hostname or host:port")
        
        elif self.provider == 'script':
            if not self.script_set:
                errors.append("SCRIPT_SET required (path to script that sets DNS records)")
            elif not os.path.exists(self.script_set):
                errors.append(f"SCRIPT_SET not found: {self.script_set}")
            elif not os.access(self.script_set, os.X_OK):
                errors.append(f"SCRIPT_SET not executable: {self.script_set}")
            
            if not self.script_get:
                errors.append("SCRIPT_GET required (path to script that gets DNS records)")
            elif not os.path.exists(self.script_get):
                errors.append(f"SCRIPT_GET not found: {self.script_get}")
            elif not os.access(self.script_get, os.X_OK):
                errors.append(f"SCRIPT_GET not executable: {self.script_get}")
        
        # Health check validation
        if self.health_mode not in ['tcp', 'http', 'metrics']:
            errors.append(f"Invalid HEALTH_MODE: {self.health_mode}. Valid: tcp, http, metrics")
        
        if self.health_mode in ('http', 'metrics'):
            if not self.health_url:
                errors.append(f"HEALTH_URL required when HEALTH_MODE={self.health_mode}")
            else:
                parsed_health_url = urllib.parse.urlsplit(self.health_url)
                if (
                    parsed_health_url.scheme not in ('http', 'https')
                    or not parsed_health_url.hostname
                ):
                    errors.append("HEALTH_URL must be an absolute http or https URL")
            if self.health_mode == 'metrics' and not self.health_metric:
                errors.append("HEALTH_METRIC required when HEALTH_MODE=metrics")
        elif not self.health_host:
            errors.append("HEALTH_HOST required when HEALTH_MODE=tcp")

        if self.local_health_url:
            parsed_local_url = urllib.parse.urlsplit(self.local_health_url)
            if parsed_local_url.scheme not in ('http', 'https') or not parsed_local_url.hostname:
                errors.append("LOCAL_HEALTH_URL must be an absolute http or https URL")

        if self.failback_health_url:
            parsed_failback_url = urllib.parse.urlsplit(self.failback_health_url)
            if (
                parsed_failback_url.scheme not in ('http', 'https')
                or not parsed_failback_url.hostname
            ):
                errors.append("FAILBACK_HEALTH_URL must be an absolute http or https URL")
        if self.auto_failback and not self.failback_health_url:
            errors.append("FAILBACK_HEALTH_URL is required when AUTO_FAILBACK is enabled")
        
        if self.lease_ttl <= self.update_interval:
            errors.append(f"LEASE_TTL ({self.lease_ttl}) must be > UPDATE_INTERVAL ({self.update_interval})")
        if self.lease_ttl <= self.update_interval + (2 * self.max_clock_skew):
            errors.append("LEASE_TTL must exceed UPDATE_INTERVAL plus twice MAX_CLOCK_SKEW")

        positive_values = {
            'DNS_TTL': self.dns_ttl,
            'UPDATE_INTERVAL': self.update_interval,
            'FAIL_THRESHOLD': self.fail_threshold,
            'FAILBACK_THRESHOLD': self.failback_threshold,
            'HEALTH_TIMEOUT': self.health_timeout,
            'HEALTH_STALE_COUNT': self.health_stale_count,
            'DNS_RETRY_MAX': self.retry_max,
            'WRITE_VERIFY_ATTEMPTS': self.write_verify_attempts,
        }
        for name, value in positive_values.items():
            if value <= 0:
                errors.append(f"{name} must be greater than zero")
        if self.max_clock_skew < 0:
            errors.append("MAX_CLOCK_SKEW cannot be negative")
        if self.write_verify_delay < 0:
            errors.append("WRITE_VERIFY_DELAY cannot be negative")
        if self.lease_proof_file and not os.path.isabs(self.lease_proof_file):
            errors.append("LEASE_PROOF_FILE must be an absolute path")
        for name, value in [('HEALTH_PORT', self.health_port), ('STATUS_PORT', self.status_port)]:
            if not 1 <= value <= 65535:
                errors.append(f"{name} must be between 1 and 65535")
        
        if errors:
            raise ValueError(f"Configuration errors: {'; '.join(errors)}")

# -----------------------------
# Utilities
# -----------------------------

def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)

def now_unix() -> int:
    return int(time.time())


class LeaseReadError(RuntimeError):
    """The DNS control plane did not return one coherent, trusted lease."""


@dataclass(frozen=True)
class Lease:
    ip: str
    owner: str
    expires_at: int

    def is_expired(self, at_time: int, max_clock_skew: int = 0) -> bool:
        """Wait through the skew allowance so a fast clock cannot steal early."""
        return self.expires_at + max_clock_skew < at_time

def parse_txt(txt: str) -> Dict[str, str]:
    """Parse exactly one canonical owner/expiry lease value."""
    if not isinstance(txt, str) or not txt.strip():
        raise LeaseReadError("DNS lease TXT value is empty")
    normalized = txt.strip()
    if normalized.startswith('"') and normalized.endswith('"'):
        normalized = normalized[1:-1]
    parts = normalized.split()
    if len(parts) != 2:
        raise LeaseReadError("DNS lease TXT must contain exactly owner and exp")
    result: Dict[str, str] = {}
    for part in parts:
        match = re.fullmatch(r'(owner|exp)=([^\s=]+)', part)
        if not match:
            raise LeaseReadError(f"DNS lease TXT contains an invalid token: {part!r}")
        key, value = match.groups()
        if key in result:
            raise LeaseReadError(f"DNS lease TXT contains duplicate {key}")
        result[key] = value
    if set(result) != {'owner', 'exp'}:
        raise LeaseReadError("DNS lease TXT must contain owner and exp exactly once")
    return result


def parse_lease(records: Dict[str, Any], cfg: Config) -> Lease:
    """Strictly validate that A and TXT records describe the same lease."""
    if not isinstance(records, dict):
        raise LeaseReadError("DNS provider returned a non-object response")
    raw_ip = records.get('A')
    raw_txt = records.get('TXT')
    if not raw_ip or not raw_txt:
        raise LeaseReadError("DNS lease is incomplete (both A and TXT are required)")
    try:
        address = ipaddress.ip_address(str(raw_ip).strip())
    except ValueError as exc:
        raise LeaseReadError(f"DNS A record is not a valid address: {raw_ip}") from exc
    if not isinstance(address, ipaddress.IPv4Address):
        raise LeaseReadError("DNS lease must contain an IPv4 A record")

    txt_data = parse_txt(str(raw_txt))
    owner = txt_data.get('owner')
    if owner not in ('primary', 'dr'):
        raise LeaseReadError(f"DNS lease has invalid owner: {owner!r}")
    try:
        expires_at = int(txt_data['exp'])
    except (KeyError, TypeError, ValueError) as exc:
        raise LeaseReadError("DNS lease has a missing or invalid expiry") from exc
    if expires_at <= 0:
        raise LeaseReadError("DNS lease expiry must be positive")

    expected_ip = cfg.primary_ip if owner == 'primary' else cfg.dr_ip
    if str(address) != expected_ip:
        raise LeaseReadError(
            f"Incoherent DNS lease: owner {owner} requires A={expected_ip}, got {address}"
        )
    return Lease(str(address), owner, expires_at)


def read_lease(provider: 'DNSProvider', cfg: Config) -> Lease:
    try:
        lease = parse_lease(provider.get_records(), cfg)
        persist_lease_proof(cfg, lease)
        return lease
    except LeaseReadError:
        invalidate_lease_proof(cfg)
        raise
    except Exception as exc:
        invalidate_lease_proof(cfg)
        raise LeaseReadError(f"DNS lease read failed: {exc}") from exc


def invalidate_lease_proof(cfg: Config) -> None:
    """Remove local ownership evidence after an untrusted control-plane read."""
    if not cfg.lease_proof_file:
        return
    try:
        os.unlink(cfg.lease_proof_file)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log(f"Unable to invalidate local lease proof: {exc}", "ERROR")


def persist_lease_proof(cfg: Config, lease: Lease) -> None:
    """Atomically publish a non-secret, coherent lease observation locally."""
    if not cfg.lease_proof_file:
        return
    path = os.path.abspath(cfg.lease_proof_file)
    directory = os.path.dirname(path)
    temporary_path: Optional[str] = None
    payload = {
        'version': 1,
        'record': cfg.dns_record.rstrip('.').lower(),
        'ip': lease.ip,
        'owner': lease.owner,
        'expires_at': lease.expires_at,
        'observed_at': now_unix(),
    }
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix='.observed-lease.', dir=directory, text=True
        )
        with os.fdopen(descriptor, 'w', encoding='utf-8') as proof_file:
            json.dump(payload, proof_file, sort_keys=True, separators=(',', ':'))
            proof_file.write('\n')
            proof_file.flush()
            os.fsync(proof_file.fileno())
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        raise LeaseReadError(f"Unable to publish local lease proof: {exc}") from exc
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def set_lease_verified(
    provider: 'DNSProvider',
    cfg: Config,
    owner: str,
    expected: Optional[Lease] = None,
) -> Lease:
    """Optimistically update a lease, then require coherent read-after-write.

    Providers with transactional record-set APIs update A and TXT atomically.
    Other providers commit TXT first and A last; readback verification detects a
    partial update.  This is not a replacement for provider-side compare-and-set,
    so operators should run one process per role.
    """
    if expected is not None:
        current = read_lease(provider, cfg)
        if current != expected:
            raise RuntimeError("DNS lease changed before update; refusing stale write")

    ip = cfg.primary_ip if owner == 'primary' else cfg.dr_ip
    expires_at = now_unix() + cfg.lease_ttl
    provider.set_records(ip, owner, expires_at)
    last_error: Optional[Exception] = None
    for attempt in range(cfg.write_verify_attempts):
        try:
            lease = read_lease(provider, cfg)
            if lease.owner == owner and lease.ip == ip and lease.expires_at == expires_at:
                return lease
            last_error = RuntimeError(f"unexpected lease after write: {lease}")
        except Exception as exc:
            last_error = exc
        if attempt + 1 < cfg.write_verify_attempts:
            time.sleep(cfg.write_verify_delay)
    raise RuntimeError(f"DNS write could not be verified: {last_error}")


def retry_delay(base: int, failures: int, maximum: int) -> float:
    """Capped exponential backoff with small jitter for provider failures."""
    delay = min(maximum, base * (2 ** max(failures - 1, 0)))
    return max(1.0, delay * random.uniform(0.9, 1.1))

def check_tcp(host: str, port: int, timeout: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def check_http(url: str, timeout: int) -> bool:
    """Return true only for a successful HTTP readiness response."""
    try:
        import urllib.request
        request = urllib.request.Request(url, headers={'User-Agent': 'dns-failover'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception as exc:
        log(f"HTTP health check failed for {url}: {exc}", "WARN")
        return False

# -----------------------------
# Metrics-based Health Check
# -----------------------------

def fetch_metrics(url: str, timeout: int) -> Optional[str]:
    """Fetch Prometheus metrics from URL."""
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={'User-Agent': 'dns-failover'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode('utf-8')
    except Exception as e:
        log(f"Failed to fetch metrics from {url}: {e}", "WARN")
        return None

def parse_metric_value(metrics_text: str, metric_name: str) -> Optional[float]:
    """
    Parse a Prometheus metric value from text format.
    Handles metrics with labels, summing all label combinations.
    
    Example input:
        otelcol_receiver_accepted_metric_points{receiver="prometheus"} 12345
        otelcol_receiver_accepted_metric_points{receiver="otlp"} 6789
    
    Returns: sum of all matching metric values (12345 + 6789 = 19134)
    """
    if not metrics_text:
        return None
    
    total = 0.0
    found = False
    
    for line in metrics_text.split('\n'):
        line = line.strip()
        # Skip comments and empty lines
        if not line or line.startswith('#'):
            continue
        
        sample_name = re.split(r'[\s{]', line, maxsplit=1)[0]
        if sample_name == metric_name:
            try:
                # Handle metrics with labels: metric_name{label="value"} 123
                # Or without labels: metric_name 123
                if '{' in line:
                    # metric_name{labels} value
                    value_part = line.split('}')[-1].strip()
                else:
                    # metric_name value
                    parts = line.split()
                    if len(parts) >= 2:
                        value_part = parts[1]
                    else:
                        continue
                
                total += float(value_part)
                found = True
            except (ValueError, IndexError):
                continue
    
    return total if found else None


class MetricsHealthChecker:
    """
    Health checker that monitors if OTEL metrics are incrementing.
    
    Healthy = metric value is increasing
    Unhealthy = metric value flat/decreasing for N consecutive checks
    """
    
    def __init__(self, url: str, metric_name: str, stale_count: int, timeout: int):
        self.url = url
        self.metric_name = metric_name
        self.stale_count = stale_count
        self.timeout = timeout
        self.last_value: Optional[float] = None
        self.stale_checks = 0
    
    def check(self) -> bool:
        """
        Check if metrics are healthy (incrementing).
        
        Returns:
            True if healthy (metrics incrementing)
            False if unhealthy (can't fetch, or flat for stale_count checks)
        """
        metrics_text = fetch_metrics(self.url, self.timeout)
        if metrics_text is None:
            # Can't reach endpoint
            self.stale_checks += 1
            log(f"Metrics endpoint unreachable ({self.stale_checks}/{self.stale_count})", "WARN")
            return self.stale_checks < self.stale_count
        
        current_value = parse_metric_value(metrics_text, self.metric_name)
        if current_value is None:
            log(f"Metric '{self.metric_name}' not found in response", "WARN")
            self.stale_checks += 1
            return self.stale_checks < self.stale_count
        
        # First check - just record the value
        if self.last_value is None:
            self.last_value = current_value
            log(f"Metrics baseline: {self.metric_name}={current_value}")
            return True
        
        # Compare with last value
        if current_value > self.last_value:
            # Value increasing - healthy
            log(f"Metrics healthy: {self.metric_name}={current_value} (+{current_value - self.last_value:.0f})")
            self.last_value = current_value
            self.stale_checks = 0
            return True
        elif current_value < self.last_value:
            # Counter reset means the endpoint and collector are alive.
            log(f"Metrics counter reset: {self.metric_name}={current_value}")
            self.last_value = current_value
            self.stale_checks = 0
            return True
        else:
            # Value is flat. This data-freshness mode intentionally treats a
            # prolonged idle source as stale; use HEALTH_MODE=http for pure
            # service readiness checks.
            self.stale_checks += 1
            log(f"Metrics stale: {self.metric_name}={current_value} (unchanged, {self.stale_checks}/{self.stale_count})", "WARN")
            self.last_value = current_value
            return self.stale_checks < self.stale_count


class TCPHealthChecker:
    def __init__(self, host: str, port: int, timeout: int):
        self.host = host
        self.port = port
        self.timeout = timeout

    def check(self) -> bool:
        return check_tcp(self.host, self.port, self.timeout)


class HTTPHealthChecker:
    def __init__(self, url: str, timeout: int):
        self.url = url
        self.timeout = timeout

    def check(self) -> bool:
        return check_http(self.url, self.timeout)


def build_health_checker(cfg: Config):
    if cfg.health_mode == 'metrics':
        return MetricsHealthChecker(
            cfg.health_url or '', cfg.health_metric or '', cfg.health_stale_count, cfg.health_timeout
        )
    if cfg.health_mode == 'http':
        return HTTPHealthChecker(cfg.health_url or '', cfg.health_timeout)
    return TCPHealthChecker(cfg.health_host, cfg.health_port, cfg.health_timeout)

# -----------------------------
# DNS Providers
# -----------------------------

class DNSProvider:
    def __init__(self, cfg: Config):
        self.cfg = cfg
    
    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        raise NotImplementedError
    
    def get_records(self) -> Dict[str, Any]:
        raise NotImplementedError


def _single_dig_answer(output: str, record_type: str) -> Optional[str]:
    answers = [line.strip() for line in output.splitlines() if line.strip()]
    if not answers:
        return None
    if len(answers) != 1:
        raise RuntimeError(
            f"DNS returned {len(answers)} {record_type} answers; exactly one is required"
        )
    value = answers[0]
    if record_type == 'TXT' and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value


class DryRunProvider(DNSProvider):
    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        state = {'A': ip, 'TXT': f'owner={owner} exp={exp_unix}', 'updated_at': datetime.now().isoformat()}
        statefile = self.cfg.dryrun_statefile or '/state/zone.json'
        state_dir = os.path.dirname(statefile) or '.'
        os.makedirs(state_dir, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(prefix='.lease-', dir=state_dir, text=True)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as state_handle:
                json.dump(state, state_handle, indent=2)
                state_handle.flush()
                os.fsync(state_handle.fileno())
            os.replace(temporary_path, statefile)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)
        log(f"[dry-run] Set A={ip}, TXT=owner={owner} exp={exp_unix}")
    
    def get_records(self) -> Dict[str, Any]:
        statefile = self.cfg.dryrun_statefile or '/state/zone.json'
        try:
            with open(statefile, 'r') as f:
                state = json.load(f)
            return {'A': state.get('A'), 'TXT': state.get('TXT')}
        except FileNotFoundError:
            return {'A': None, 'TXT': None}


class BindTSIGProvider(DNSProvider):
    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        txt_value = f"owner={owner} exp={exp_unix}"
        commands = f"""server {self.cfg.dns_server}
zone {self.cfg.dns_zone}
update delete {self.cfg.dns_record} A
update delete {self.cfg.dns_record} TXT
update add {self.cfg.dns_record} {self.cfg.dns_ttl} A {ip}
update add {self.cfg.dns_record} {self.cfg.dns_ttl} TXT "{txt_value}"
send
"""
        result = subprocess.run(['nsupdate', '-k', self.cfg.tsig_keyfile], input=commands, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise RuntimeError(f"nsupdate failed: {result.stderr}")
        log(f"[bind-tsig] Set A={ip}, TXT={txt_value}")
    
    def get_records(self) -> Dict[str, Any]:
        result = {'A': None, 'TXT': None}
        try:
            proc = subprocess.run(['dig', f'@{self.cfg.dns_server}', self.cfg.dns_record, 'A', '+short'], capture_output=True, text=True, timeout=5)
            if proc.returncode == 0:
                result['A'] = _single_dig_answer(proc.stdout, 'A')
        except Exception as e:
            log(f"Failed to query A record: {e}", "WARN")
        try:
            proc = subprocess.run(['dig', f'@{self.cfg.dns_server}', self.cfg.dns_record, 'TXT', '+short'], capture_output=True, text=True, timeout=5)
            if proc.returncode == 0:
                result['TXT'] = _single_dig_answer(proc.stdout, 'TXT')
        except Exception as e:
            log(f"Failed to query TXT record: {e}", "WARN")
        return result


class ADGSSProvider(DNSProvider):
    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        txt_value = f"owner={owner} exp={exp_unix}"
        commands = f"""server {self.cfg.dns_server}
zone {self.cfg.dns_zone}
update delete {self.cfg.dns_record} A
update delete {self.cfg.dns_record} TXT
update add {self.cfg.dns_record} {self.cfg.dns_ttl} A {ip}
update add {self.cfg.dns_record} {self.cfg.dns_ttl} TXT "{txt_value}"
send
"""
        result = subprocess.run(['nsupdate', '-g'], input=commands, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise RuntimeError(f"nsupdate (GSS) failed: {result.stderr}")
        log(f"[ad-gss] Set A={ip}, TXT={txt_value}")
    
    def get_records(self) -> Dict[str, Any]:
        result = {'A': None, 'TXT': None}
        try:
            proc = subprocess.run(['dig', f'@{self.cfg.dns_server}', self.cfg.dns_record, 'A', '+short'], capture_output=True, text=True, timeout=5)
            if proc.returncode == 0:
                result['A'] = _single_dig_answer(proc.stdout, 'A')
        except Exception as e:
            log(f"Failed to query A record: {e}", "WARN")
        try:
            proc = subprocess.run(['dig', f'@{self.cfg.dns_server}', self.cfg.dns_record, 'TXT', '+short'], capture_output=True, text=True, timeout=5)
            if proc.returncode == 0:
                result['TXT'] = _single_dig_answer(proc.stdout, 'TXT')
        except Exception as e:
            log(f"Failed to query TXT record: {e}", "WARN")
        return result


class InfobloxProvider(DNSProvider):
    def __init__(self, cfg: Config):
        super().__init__(cfg)
        import requests
        self.session = requests.Session()
        self.session.auth = (cfg.infoblox_username, cfg.infoblox_password)
        self.session.verify = cfg.infoblox_verify_ssl
        self.base_url = f"https://{cfg.infoblox_host}/wapi/{cfg.infoblox_wapi_version}"
    
    def _find_record(self, record_type: str) -> Optional[str]:
        url = f"{self.base_url}/record:{record_type.lower()}"
        params = {'name': self.cfg.dns_record, 'zone': self.cfg.dns_zone}
        resp = self.session.get(url, params=params, timeout=10)
        resp.raise_for_status()
        records = resp.json()
        if len(records) > 1:
            raise RuntimeError(
                f"Infoblox returned multiple {record_type} records for {self.cfg.dns_record}"
            )
        return records[0]['_ref'] if records else None
    
    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        txt_value = f"owner={owner} exp={exp_unix}"
        # Commit metadata first and the A record last. Watchers act on A, so a
        # failed two-step update cannot activate the new site prematurely.
        txt_ref = self._find_record('TXT')
        if txt_ref:
            self.session.put(f"{self.base_url}/{txt_ref}", json={'text': txt_value, 'ttl': self.cfg.dns_ttl}, timeout=10).raise_for_status()
        else:
            self.session.post(f"{self.base_url}/record:txt", json={'name': self.cfg.dns_record, 'text': txt_value, 'ttl': self.cfg.dns_ttl, 'zone': self.cfg.dns_zone}, timeout=10).raise_for_status()
        a_ref = self._find_record('A')
        if a_ref:
            self.session.put(f"{self.base_url}/{a_ref}", json={'ipv4addr': ip, 'ttl': self.cfg.dns_ttl}, timeout=10).raise_for_status()
        else:
            self.session.post(f"{self.base_url}/record:a", json={'name': self.cfg.dns_record, 'ipv4addr': ip, 'ttl': self.cfg.dns_ttl, 'zone': self.cfg.dns_zone}, timeout=10).raise_for_status()
        log(f"[infoblox] Set A={ip}, TXT={txt_value}")
    
    def get_records(self) -> Dict[str, Any]:
        result = {'A': None, 'TXT': None}
        try:
            a_ref = self._find_record('A')
            if a_ref:
                resp = self.session.get(f"{self.base_url}/{a_ref}", params={'_return_fields': 'ipv4addr'}, timeout=10)
                resp.raise_for_status()
                result['A'] = resp.json().get('ipv4addr')
        except Exception as e:
            log(f"Failed to query A record: {e}", "WARN")
        try:
            txt_ref = self._find_record('TXT')
            if txt_ref:
                resp = self.session.get(f"{self.base_url}/{txt_ref}", params={'_return_fields': 'text'}, timeout=10)
                resp.raise_for_status()
                result['TXT'] = resp.json().get('text')
        except Exception as e:
            log(f"Failed to query TXT record: {e}", "WARN")
        return result


class CloudflareProvider(DNSProvider):
    def __init__(self, cfg: Config):
        super().__init__(cfg)
        import requests
        self.session = requests.Session()
        self.session.headers.update({'Authorization': f'Bearer {cfg.cloudflare_api_token}', 'Content-Type': 'application/json'})
        self.base_url = "https://api.cloudflare.com/client/v4"
        self.zone_id = cfg.cloudflare_zone_id
    
    def _find_record(self, record_type: str) -> Optional[Dict]:
        url = f"{self.base_url}/zones/{self.zone_id}/dns_records"
        params = {'type': record_type, 'name': self.cfg.dns_record}
        resp = self.session.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data['success'] and data['result']:
            if len(data['result']) != 1:
                raise RuntimeError(
                    f"Cloudflare returned multiple {record_type} records for {self.cfg.dns_record}"
                )
            return data['result'][0]
        return None
    
    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        txt_value = f"owner={owner} exp={exp_unix}"
        txt_record = self._find_record('TXT')
        if txt_record:
            url = f"{self.base_url}/zones/{self.zone_id}/dns_records/{txt_record['id']}"
            self.session.put(url, json={'type': 'TXT', 'name': self.cfg.dns_record, 'content': txt_value, 'ttl': self.cfg.dns_ttl}, timeout=10).raise_for_status()
        else:
            url = f"{self.base_url}/zones/{self.zone_id}/dns_records"
            self.session.post(url, json={'type': 'TXT', 'name': self.cfg.dns_record, 'content': txt_value, 'ttl': self.cfg.dns_ttl}, timeout=10).raise_for_status()
        a_record = self._find_record('A')
        if a_record:
            url = f"{self.base_url}/zones/{self.zone_id}/dns_records/{a_record['id']}"
            self.session.put(url, json={'type': 'A', 'name': self.cfg.dns_record, 'content': ip, 'ttl': self.cfg.dns_ttl}, timeout=10).raise_for_status()
        else:
            url = f"{self.base_url}/zones/{self.zone_id}/dns_records"
            self.session.post(url, json={'type': 'A', 'name': self.cfg.dns_record, 'content': ip, 'ttl': self.cfg.dns_ttl}, timeout=10).raise_for_status()
        log(f"[cloudflare] Set A={ip}, TXT={txt_value}")
    
    def get_records(self) -> Dict[str, Any]:
        result = {'A': None, 'TXT': None}
        try:
            a_record = self._find_record('A')
            if a_record:
                result['A'] = a_record['content']
        except Exception as e:
            log(f"Failed to query A record: {e}", "WARN")
        try:
            txt_record = self._find_record('TXT')
            if txt_record:
                result['TXT'] = txt_record['content']
        except Exception as e:
            log(f"Failed to query TXT record: {e}", "WARN")
        return result


class Route53Provider(DNSProvider):
    def __init__(self, cfg: Config):
        super().__init__(cfg)
        import boto3
        client_options = {'region_name': cfg.aws_region}
        if cfg.aws_access_key_id and cfg.aws_secret_access_key:
            client_options.update({
                'aws_access_key_id': cfg.aws_access_key_id,
                'aws_secret_access_key': cfg.aws_secret_access_key,
            })
        self.client = boto3.client('route53', **client_options)
        self.zone_id = cfg.route53_zone_id
        self.record_name = cfg.dns_record if cfg.dns_record.endswith('.') else f"{cfg.dns_record}."
    
    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        txt_value = f'"owner={owner} exp={exp_unix}"'
        changes = [
            {'Action': 'UPSERT', 'ResourceRecordSet': {'Name': self.record_name, 'Type': 'A', 'TTL': self.cfg.dns_ttl, 'ResourceRecords': [{'Value': ip}]}},
            {'Action': 'UPSERT', 'ResourceRecordSet': {'Name': self.record_name, 'Type': 'TXT', 'TTL': self.cfg.dns_ttl, 'ResourceRecords': [{'Value': txt_value}]}}
        ]
        self.client.change_resource_record_sets(HostedZoneId=self.zone_id, ChangeBatch={'Changes': changes})
        log(f"[route53] Set A={ip}, TXT=owner={owner} exp={exp_unix}")
    
    def get_records(self) -> Dict[str, Any]:
        result = {'A': None, 'TXT': None}
        try:
            resp = self.client.list_resource_record_sets(HostedZoneId=self.zone_id, StartRecordName=self.record_name, MaxItems='10')
            for rs in resp.get('ResourceRecordSets', []):
                if rs['Name'] == self.record_name:
                    if rs['Type'] == 'A':
                        values = rs.get('ResourceRecords', [])
                        if len(values) != 1 or result['A'] is not None:
                            raise RuntimeError("Route53 lease must contain exactly one A value")
                        result['A'] = values[0]['Value']
                    elif rs['Type'] == 'TXT':
                        values = rs.get('ResourceRecords', [])
                        if len(values) != 1 or result['TXT'] is not None:
                            raise RuntimeError("Route53 lease must contain exactly one TXT value")
                        result['TXT'] = values[0]['Value'].strip('"')
        except Exception as e:
            log(f"Failed to query records: {e}", "WARN")
        return result


class AzureDNSProvider(DNSProvider):
    def __init__(self, cfg: Config):
        super().__init__(cfg)
        import requests
        self.session = requests.Session()
        self.cfg = cfg
        self.base_url = f"https://management.azure.com/subscriptions/{cfg.azure_subscription_id}/resourceGroups/{cfg.azure_resource_group}/providers/Microsoft.Network/dnsZones/{cfg.dns_zone}"
        self._authenticate()
    
    def _authenticate(self):
        import requests
        url = f"https://login.microsoftonline.com/{self.cfg.azure_tenant_id}/oauth2/v2.0/token"
        data = {'grant_type': 'client_credentials', 'client_id': self.cfg.azure_client_id, 'client_secret': self.cfg.azure_client_secret, 'scope': 'https://management.azure.com/.default'}
        resp = requests.post(url, data=data, timeout=10)
        resp.raise_for_status()
        token = resp.json()['access_token']
        self.session.headers.update({'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})

    def _request(self, method: str, url: str, **kwargs):
        """Refresh the Azure access token once when a long-running process gets 401."""
        response = self.session.request(method, url, **kwargs)
        if response.status_code == 401:
            response.close()
            self._authenticate()
            response = self.session.request(method, url, **kwargs)
        return response
    
    def _get_record_name(self) -> str:
        record = self.cfg.dns_record.rstrip('.')
        zone = self.cfg.dns_zone.rstrip('.')
        if record.lower() == zone.lower():
            return '@'
        if record.lower().endswith(f'.{zone.lower()}'):
            return record[:-(len(zone) + 1)]
        return self.cfg.dns_record
    
    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        txt_value = f"owner={owner} exp={exp_unix}"
        record_name = self._get_record_name()
        api_version = "2018-05-01"
        
        self._request('PUT', f"{self.base_url}/TXT/{record_name}?api-version={api_version}", json={'properties': {'TTL': self.cfg.dns_ttl, 'TXTRecords': [{'value': [txt_value]}]}}, timeout=10).raise_for_status()
        self._request('PUT', f"{self.base_url}/A/{record_name}?api-version={api_version}", json={'properties': {'TTL': self.cfg.dns_ttl, 'ARecords': [{'ipv4Address': ip}]}}, timeout=10).raise_for_status()
        log(f"[azure-dns] Set A={ip}, TXT={txt_value}")
    
    def get_records(self) -> Dict[str, Any]:
        result = {'A': None, 'TXT': None}
        record_name = self._get_record_name()
        api_version = "2018-05-01"
        try:
            resp = self._request('GET', f"{self.base_url}/A/{record_name}?api-version={api_version}", timeout=10)
            if resp.status_code == 200:
                records = resp.json().get('properties', {}).get('ARecords', [])
                if len(records) > 1:
                    raise RuntimeError("Azure DNS lease must contain exactly one A value")
                if records:
                    result['A'] = records[0]['ipv4Address']
            elif resp.status_code != 404:
                resp.raise_for_status()
        except Exception as e:
            log(f"Failed to query A record: {e}", "WARN")
        try:
            resp = self._request('GET', f"{self.base_url}/TXT/{record_name}?api-version={api_version}", timeout=10)
            if resp.status_code == 200:
                records = resp.json().get('properties', {}).get('TXTRecords', [])
                if len(records) > 1:
                    raise RuntimeError("Azure DNS lease must contain exactly one TXT value")
                if records:
                    values = records[0].get('value', [])
                    if len(values) != 1:
                        raise RuntimeError("Azure DNS lease TXT must have exactly one chunk")
                    result['TXT'] = values[0]
            elif resp.status_code != 404:
                resp.raise_for_status()
        except Exception as e:
            log(f"Failed to query TXT record: {e}", "WARN")
        return result


class GCPDNSProvider(DNSProvider):
    def __init__(self, cfg: Config):
        super().__init__(cfg)
        from google.cloud import dns
        from google.oauth2 import service_account
        
        if cfg.gcp_credentials_file:
            credentials = service_account.Credentials.from_service_account_file(cfg.gcp_credentials_file)
            self.client = dns.Client(project=cfg.gcp_project_id, credentials=credentials)
        else:
            self.client = dns.Client(project=cfg.gcp_project_id)
        
        self.zone = self.client.zone(cfg.gcp_managed_zone)
        self.record_name = cfg.dns_record if cfg.dns_record.endswith('.') else f"{cfg.dns_record}."
    
    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        txt_value = f'"owner={owner} exp={exp_unix}"'
        changes = self.zone.changes()
        
        for record_set in self.zone.list_resource_record_sets():
            if record_set.name == self.record_name and record_set.record_type in ('A', 'TXT'):
                changes.delete_record_set(record_set)
        
        changes.add_record_set(self.zone.resource_record_set(self.record_name, 'A', self.cfg.dns_ttl, [ip]))
        changes.add_record_set(self.zone.resource_record_set(self.record_name, 'TXT', self.cfg.dns_ttl, [txt_value]))
        changes.create()

        deadline = time.monotonic() + self.cfg.gcp_change_timeout
        while changes.status != 'done':
            if changes.status != 'pending':
                raise RuntimeError(
                    f"GCP DNS change entered unexpected status: {changes.status!r}"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "GCP DNS change did not complete within "
                    f"{self.cfg.gcp_change_timeout:g} seconds"
                )
            time.sleep(min(0.5, remaining))
            changes.reload()
        
        log(f"[gcp-dns] Set A={ip}, TXT=owner={owner} exp={exp_unix}")
    
    def get_records(self) -> Dict[str, Any]:
        result = {'A': None, 'TXT': None}
        try:
            for record_set in self.zone.list_resource_record_sets():
                if record_set.name == self.record_name:
                    if record_set.record_type == 'A':
                        if len(record_set.rrdatas) != 1 or result['A'] is not None:
                            raise RuntimeError("GCP DNS lease must contain exactly one A value")
                        result['A'] = record_set.rrdatas[0]
                    elif record_set.record_type == 'TXT':
                        if len(record_set.rrdatas) != 1 or result['TXT'] is not None:
                            raise RuntimeError("GCP DNS lease must contain exactly one TXT value")
                        result['TXT'] = record_set.rrdatas[0].strip('"')
        except Exception as e:
            log(f"Failed to query records: {e}", "WARN")
        return result


class F5GTMProvider(DNSProvider):
    def __init__(self, cfg: Config):
        super().__init__(cfg)
        import requests
        self.session = requests.Session()
        self.session.auth = (cfg.f5_username, cfg.f5_password)
        self.session.verify = cfg.f5_verify_ssl
        self.session.headers.update({'Content-Type': 'application/json'})
        self.base_url = f"https://{cfg.f5_host}/mgmt/tm"
        self.partition = cfg.f5_partition
        self.datagroup_name = "dns_failover_lease"

        quoted_partition = urllib.parse.quote(self.partition, safe='')
        quoted_pool = urllib.parse.quote(cfg.f5_pool_name or '', safe='')
        self.datagroup_url = (
            f"{self.base_url}/ltm/data-group/internal/"
            f"~{quoted_partition}~{urllib.parse.quote(self.datagroup_name, safe='')}"
        )
        self.pool_members_url = (
            f"{self.base_url}/gtm/pool/a/~{quoted_partition}~{quoted_pool}/members"
        )
    
    def _ensure_datagroup(self):
        resp = self.session.get(self.datagroup_url, timeout=10)
        if resp.status_code == 404:
            url = f"{self.base_url}/ltm/data-group/internal"
            self.session.post(
                url,
                json={
                    'name': self.datagroup_name,
                    'partition': self.partition,
                    'type': 'string',
                },
                timeout=10,
            ).raise_for_status()
        else:
            resp.raise_for_status()

    def _member_url(self, member: Dict[str, Any]) -> str:
        """Use a member selfLink path while pinning requests to the configured F5."""
        self_link = member.get('selfLink')
        if not isinstance(self_link, str) or not self_link:
            raise RuntimeError("F5 GTM member is missing selfLink")
        parsed = urllib.parse.urlsplit(self_link)
        expected_prefix = urllib.parse.urlsplit(self.pool_members_url).path.rstrip('/') + '/'
        if (
            not parsed.path.startswith(expected_prefix)
            or '/..' in parsed.path
            or '\\' in parsed.path
        ):
            raise RuntimeError("F5 GTM member selfLink is outside the configured pool")
        query = f"?{parsed.query}" if parsed.query else ''
        origin = f"https://{self.cfg.f5_host}"
        return f"{origin}{parsed.path}{query}"

    def _site_members(self) -> Dict[str, Dict[str, Any]]:
        resp = self.session.get(self.pool_members_url, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get('items')
        if not isinstance(items, list):
            raise RuntimeError("F5 GTM pool response is missing its member list")

        by_full_path: Dict[str, Dict[str, Any]] = {}
        for member in items:
            if not isinstance(member, dict):
                raise RuntimeError("F5 GTM pool returned an invalid member object")
            full_path = member.get('fullPath')
            if not isinstance(full_path, str) or not full_path:
                raise RuntimeError("F5 GTM pool member is missing fullPath")
            if full_path in by_full_path:
                raise RuntimeError(f"F5 GTM pool returned duplicate member {full_path}")
            by_full_path[full_path] = member

        selectors = {
            'primary': self.cfg.f5_primary_member,
            'dr': self.cfg.f5_dr_member,
        }
        missing = [selector for selector in selectors.values() if selector not in by_full_path]
        if missing:
            raise RuntimeError(
                "F5 GTM pool is missing configured member(s): " + ', '.join(missing)
            )
        unexpected = sorted(set(by_full_path) - set(selectors.values()))
        if unexpected:
            raise RuntimeError(
                "F5 GTM pool contains unconfigured member(s); refusing to change traffic: "
                + ', '.join(unexpected)
            )
        return {role: by_full_path[selector] for role, selector in selectors.items()}

    @staticmethod
    def _member_is_enabled(member: Dict[str, Any]) -> bool:
        enabled = member.get('enabled') is True
        disabled = member.get('disabled') is True
        if enabled == disabled:
            full_path = member.get('fullPath', '<unknown>')
            raise RuntimeError(f"F5 GTM member has ambiguous enabled state: {full_path}")
        return enabled

    def _active_pool_role(self, members: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
        members = members or self._site_members()
        active_roles = [
            role for role, member in members.items() if self._member_is_enabled(member)
        ]
        if len(active_roles) != 1:
            raise RuntimeError(
                "F5 GTM pool must have exactly one enabled primary/DR member"
            )
        return active_roles[0]

    def _set_active_pool_role(self, role: str) -> None:
        members = self._site_members()
        # Disable the peer before enabling the target. A failed second write
        # therefore causes a fail-closed outage instead of a split-brain window
        # in which GTM can answer with both sites.
        peer_role = 'dr' if role == 'primary' else 'primary'
        for member_role in (peer_role, role):
            member = members[member_role]
            body = {'enabled': True} if member_role == role else {'disabled': True}
            self.session.patch(
                self._member_url(member), json=body, timeout=10
            ).raise_for_status()

        verified_role = self._active_pool_role()
        if verified_role != role:
            raise RuntimeError(
                f"F5 GTM pool verification failed: expected {role}, got {verified_role}"
            )
    
    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        role_by_ip = {self.cfg.primary_ip: 'primary', self.cfg.dr_ip: 'dr'}
        role = role_by_ip.get(ip)
        if role is None or owner != role:
            raise ValueError("F5 GTM writes require a matching primary or DR IP/owner")

        self._ensure_datagroup()
        self._set_active_pool_role(role)
        data = {
            'records': [
                {'name': 'owner', 'data': owner},
                {'name': 'exp', 'data': str(exp_unix)},
                {'name': 'ip', 'data': ip},
            ]
        }
        self.session.patch(
            self.datagroup_url, json=data, timeout=10
        ).raise_for_status()
        
        log(f"[f5-gtm] Set active={ip}, owner={owner}, exp={exp_unix}")
    
    def get_records(self) -> Dict[str, Any]:
        self._ensure_datagroup()
        resp = self.session.get(self.datagroup_url, timeout=10)
        resp.raise_for_status()
        raw_records = resp.json().get('records')
        if not isinstance(raw_records, list):
            raise RuntimeError("F5 lease data group is missing records")
        records: Dict[str, str] = {}
        for record in raw_records:
            if not isinstance(record, dict) or not isinstance(record.get('name'), str):
                raise RuntimeError("F5 lease data group contains an invalid record")
            name = record['name']
            if name in records:
                raise RuntimeError(f"F5 lease data group contains duplicate {name}")
            records[name] = str(record.get('data', ''))
        if not all(records.get(key) for key in ('ip', 'owner', 'exp')):
            raise RuntimeError("F5 lease data group is incomplete")

        active_role = self._active_pool_role()
        active_ip = self.cfg.primary_ip if active_role == 'primary' else self.cfg.dr_ip
        if records['ip'] != active_ip or records['owner'] != active_role:
            raise RuntimeError(
                "F5 lease data group disagrees with the enabled GTM pool member"
            )
        return {
            'A': active_ip,
            'TXT': f"owner={records['owner']} exp={records['exp']}",
        }


class ScriptProvider(DNSProvider):
    """
    Custom script provider for unsupported DNS platforms.
    
    Allows users to integrate ANY DNS system by providing two scripts:
    
    1. SCRIPT_SET - Called when DNS needs to be updated
       Arguments: $1=record $2=ip $3=owner $4=expiry_unix $5=ttl $6=zone
       Example:   ./set_dns.sh syslog.example.com 10.10.10.10 primary 1699567890 30 example.com
       Exit 0 on success, non-zero on failure
    
    2. SCRIPT_GET - Called to query current DNS state  
       Arguments: $1=record $2=zone
       Example:   ./get_dns.sh syslog.example.com example.com
       Output:    JSON on stdout: {"A": "10.10.10.10", "TXT": "owner=primary exp=1699567890"}
       Exit 0 on success, non-zero on failure
    
    Scripts can be written in any language (bash, python, powershell, etc.)
    """
    
    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        """Call the set script with DNS update parameters."""
        args = [
            self.cfg.script_set,
            self.cfg.dns_record,      # $1 - FQDN to update
            ip,                        # $2 - IP address
            owner,                     # $3 - owner (primary or dr)
            str(exp_unix),            # $4 - lease expiry (unix timestamp)
            str(self.cfg.dns_ttl),    # $5 - TTL in seconds
            self.cfg.dns_zone         # $6 - DNS zone
        ]
        
        # Also pass as environment variables for convenience
        env = os.environ.copy()
        env.update({
            'DNS_RECORD': self.cfg.dns_record,
            'DNS_IP': ip,
            'DNS_OWNER': owner,
            'DNS_EXPIRY': str(exp_unix),
            'DNS_TTL': str(self.cfg.dns_ttl),
            'DNS_ZONE': self.cfg.dns_zone,
            'DNS_SERVER': self.cfg.dns_server,
        })
        
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30,
            env=env
        )
        
        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip() or f"Exit code {result.returncode}"
            raise RuntimeError(f"SCRIPT_SET failed: {error_msg}")
        
        log(f"[script] Set A={ip}, owner={owner}, exp={exp_unix}")
    
    def get_records(self) -> Dict[str, Any]:
        """Call the get script to query current DNS state."""
        result = {'A': None, 'TXT': None}
        
        args = [
            self.cfg.script_get,
            self.cfg.dns_record,      # $1 - FQDN to query
            self.cfg.dns_zone         # $2 - DNS zone
        ]
        
        env = os.environ.copy()
        env.update({
            'DNS_RECORD': self.cfg.dns_record,
            'DNS_ZONE': self.cfg.dns_zone,
            'DNS_SERVER': self.cfg.dns_server,
        })
        
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=30,
                env=env
            )
            
            if proc.returncode != 0:
                log(f"SCRIPT_GET failed: {proc.stderr.strip() or proc.stdout.strip()}", "WARN")
                return result
            
            # Parse JSON output
            output = proc.stdout.strip()
            if output:
                data = json.loads(output)
                result['A'] = data.get('A')
                result['TXT'] = data.get('TXT')
        
        except json.JSONDecodeError as e:
            log(f"SCRIPT_GET returned invalid JSON: {e}", "WARN")
        except subprocess.TimeoutExpired:
            log("SCRIPT_GET timed out", "WARN")
        except Exception as e:
            log(f"SCRIPT_GET error: {e}", "WARN")
        
        return result


def build_provider(cfg: Config) -> DNSProvider:
    providers = {
        'dry-run': DryRunProvider,
        'bind-tsig': BindTSIGProvider,
        'ad-gss': ADGSSProvider,
        'infoblox': InfobloxProvider,
        'cloudflare': CloudflareProvider,
        'route53': Route53Provider,
        'azure-dns': AzureDNSProvider,
        'gcp-dns': GCPDNSProvider,
        'f5-gtm': F5GTMProvider,
        'script': ScriptProvider,
    }
    if cfg.provider not in providers:
        raise ValueError(f"Unknown provider: {cfg.provider}")
    return providers[cfg.provider](cfg)

# -----------------------------
# Core Operations
# -----------------------------

def init_dns(cfg: Config, provider: DNSProvider):
    lease = set_lease_verified(provider, cfg, 'primary')
    log(f"Initialized DNS: A={lease.ip}, owner=primary, expires={lease.expires_at}")

def promote_to_dr(cfg: Config, provider: DNSProvider):
    lease = set_lease_verified(provider, cfg, 'dr')
    log(f"FAILOVER: Promoted DR to active, A={lease.ip}, expires={lease.expires_at}")

def failback_to_primary(cfg: Config, provider: DNSProvider):
    lease = set_lease_verified(provider, cfg, 'primary')
    log(f"FAILBACK: Restored primary as active, A={lease.ip}, expires={lease.expires_at}")

def show_dns(cfg: Config, provider: DNSProvider):
    lease = read_lease(provider, cfg)
    print(json.dumps({
        'record': cfg.dns_record,
        'A': lease.ip,
        'owner': lease.owner,
        'expires_at': lease.expires_at,
        'time_remaining': lease.expires_at - now_unix(),
    }, indent=2))

# -----------------------------
# Heartbeat Loops
# -----------------------------

@dataclass
class PrimaryLoopState:
    healthy_streak: int = 0
    control_failures: int = 0
    observed_owner: Optional[str] = None
    observed_health_phase: Optional[str] = None


@dataclass
class DRLoopState:
    primary_failures: int = 0
    control_failures: int = 0


def _set_owner_metrics(status: ServiceStatus, owner: str) -> None:
    status.set_gauge('lease_owner_primary', 1 if owner == 'primary' else 0)
    status.set_gauge('lease_owner_dr', 1 if owner == 'dr' else 0)


def run_primary_iteration(
    cfg: Config,
    provider: DNSProvider,
    health_checker,
    state: PrimaryLoopState,
    status: ServiceStatus,
    failback_health_checker=None,
) -> float:
    """Run one fail-closed primary heartbeat iteration and return next delay."""
    started = time.monotonic()
    status.mark_attempt(role='primary')
    try:
        lease = read_lease(provider, cfg)
    except Exception as exc:
        state.healthy_streak = 0
        state.observed_owner = None
        state.observed_health_phase = None
        state.control_failures += 1
        delay = retry_delay(cfg.update_interval, state.control_failures, cfg.retry_max)
        log(f"Lease state unavailable; failing closed for {delay:.1f}s: {exc}", "ERROR")
        status.mark_failure(exc, time.monotonic() - started, role='primary', action='read-failed')
        return delay

    _set_owner_metrics(status, lease.owner)
    if state.observed_owner is not None and state.observed_owner != lease.owner:
        state.healthy_streak = 0
    state.observed_owner = lease.owner
    now = now_unix()
    checker = health_checker
    health_label = 'active service'
    health_phase = 'active'
    if lease.owner == 'dr':
        checker = failback_health_checker
        health_label = 'primary recovery'
        health_phase = 'recovery'
    elif lease.is_expired(now, cfg.max_clock_skew):
        state.healthy_streak = 0
        state.observed_health_phase = 'expired-self'
        message = (
            'Primary-owned lease is already expired; refusing an automatic renewal '
            'that could race DR promotion'
        )
        log(message, "ERROR")
        status.mark_failure(
            message,
            time.monotonic() - started,
            role='primary',
            owner='primary',
            action='expired-self-blocked',
        )
        return cfg.update_interval
    if checker is None:
        state.healthy_streak = 0
        state.observed_health_phase = health_phase
        message = f'{health_label} requires FAILBACK_HEALTH_URL'
        log(f"{message}; automatic lease update is blocked", "WARN")
        status.mark_failure(
            message,
            time.monotonic() - started,
            role='primary',
            owner=lease.owner,
            action='failback-blocked',
        )
        return cfg.update_interval
    if (
        state.observed_health_phase is not None
        and state.observed_health_phase != health_phase
    ):
        state.healthy_streak = 0
    state.observed_health_phase = health_phase

    local_healthy = checker.check()
    status.set_gauge('local_health', 1 if local_healthy else 0)
    if lease.owner == 'dr':
        status.set_gauge('failback_health', 1 if local_healthy else 0)
    if not local_healthy:
        state.healthy_streak = 0
        message = f"{health_label} unhealthy - not renewing or reclaiming the lease"
        log(message, "WARN")
        status.mark_failure(
            message, time.monotonic() - started, role='primary', owner=lease.owner
        )
        return cfg.update_interval
    state.healthy_streak += 1

    if lease.owner == 'dr':
        expired = lease.is_expired(now, cfg.max_clock_skew)
        stable = state.healthy_streak >= cfg.failback_threshold
        if not stable or not expired:
            reason = 'health stabilization' if not stable else 'unexpired DR lease'
            log(f"DR owns lease; waiting for {reason}", "WARN")
            state.control_failures = 0
            status.mark_success(
                time.monotonic() - started, role='primary', owner='dr', action='wait'
            )
            return cfg.update_interval
        log(
            "DR lease expired - reclaiming for healthy primary",
            "WARN",
        )

    try:
        new_lease = set_lease_verified(provider, cfg, 'primary', expected=lease)
    except Exception as exc:
        state.control_failures += 1
        delay = retry_delay(cfg.update_interval, state.control_failures, cfg.retry_max)
        log(f"Primary lease update failed; retrying in {delay:.1f}s: {exc}", "ERROR")
        status.mark_failure(exc, time.monotonic() - started, role='primary', action='write-failed')
        return delay

    state.control_failures = 0
    _set_owner_metrics(status, 'primary')
    status.set_gauge('lease_expires_timestamp_seconds', new_lease.expires_at)
    status.mark_success(
        time.monotonic() - started, role='primary', owner='primary', action='renew'
    )
    log(f"Primary lease active until {new_lease.expires_at}")
    return cfg.update_interval


def run_dr_iteration(
    cfg: Config,
    provider: DNSProvider,
    primary_health_checker,
    local_health_checker,
    state: DRLoopState,
    status: ServiceStatus,
    failback_health_checker=None,
) -> float:
    """Run one DR monitor iteration; an unknown lease can never promote DR."""
    started = time.monotonic()
    status.mark_attempt(role='dr')
    try:
        lease = read_lease(provider, cfg)
    except Exception as exc:
        state.primary_failures = 0
        state.control_failures += 1
        delay = retry_delay(cfg.update_interval, state.control_failures, cfg.retry_max)
        log(f"Lease state unavailable; DR promotion is blocked for {delay:.1f}s: {exc}", "ERROR")
        status.mark_failure(exc, time.monotonic() - started, role='dr', action='read-failed')
        return delay

    _set_owner_metrics(status, lease.owner)
    if lease.owner == 'dr':
        state.primary_failures = 0
        if lease.is_expired(now_unix(), cfg.max_clock_skew):
            message = (
                'DR-owned lease is already expired; refusing an automatic renewal '
                'that could race primary reclamation'
            )
            log(message, "ERROR")
            status.mark_failure(
                message,
                time.monotonic() - started,
                role='dr',
                owner='dr',
                action='expired-self-blocked',
            )
            return cfg.update_interval
        if cfg.auto_failback:
            if failback_health_checker is None:
                message = 'AUTO_FAILBACK is enabled but no primary recovery checker exists'
                log(message, "ERROR")
                status.mark_failure(
                    message,
                    time.monotonic() - started,
                    role='dr',
                    owner='dr',
                    action='failback-blocked',
                )
                return cfg.update_interval
            recovery_healthy = failback_health_checker.check()
            status.set_gauge('failback_health', 1 if recovery_healthy else 0)
            if recovery_healthy:
                state.control_failures = 0
                log(
                    "Primary recovery is healthy; yielding DR lease for automatic failback",
                    "WARN",
                )
                status.mark_success(
                    time.monotonic() - started, role='dr', owner='dr', action='yield'
                )
                return cfg.update_interval

        if local_health_checker is not None and not local_health_checker.check():
            status.set_gauge('local_health', 0)
            log("DR backing services are unhealthy - lease renewal stopped", "ERROR")
            status.mark_failure(
                'DR backing services unhealthy',
                time.monotonic() - started,
                role='dr',
                owner='dr',
            )
            return cfg.update_interval
        status.set_gauge('local_health', 1)
    else:
        primary_healthy = primary_health_checker.check()
        status.set_gauge('primary_health', 1 if primary_healthy else 0)
        if primary_healthy:
            state.primary_failures = 0
            state.control_failures = 0
            status.mark_success(
                time.monotonic() - started,
                role='dr',
                owner='primary',
                action='monitor',
            )
            log("Primary healthy")
            return cfg.update_interval

        state.primary_failures += 1
        log(
            f"Primary health check failed ({state.primary_failures}/{cfg.fail_threshold})",
            "WARN",
        )
        if state.primary_failures < cfg.fail_threshold:
            status.mark_success(
                time.monotonic() - started,
                role='dr',
                owner='primary',
                action='threshold-wait',
            )
            return cfg.update_interval

        if local_health_checker is not None and not local_health_checker.check():
            status.set_gauge('local_health', 0)
            log("DR backing services are unhealthy - refusing promotion", "ERROR")
            status.mark_failure(
                'DR backing services unhealthy',
                time.monotonic() - started,
                role='dr',
                owner='primary',
            )
            return cfg.update_interval
        status.set_gauge('local_health', 1)

    if lease.owner == 'primary' and not lease.is_expired(now_unix(), cfg.max_clock_skew):
        remaining = lease.expires_at + cfg.max_clock_skew - now_unix()
        log(f"Waiting for primary lease and clock-skew guard ({remaining}s remaining)", "WARN")
        state.control_failures = 0
        status.mark_success(time.monotonic() - started, role='dr', owner='primary', action='lease-wait')
        return cfg.update_interval

    try:
        new_lease = set_lease_verified(provider, cfg, 'dr', expected=lease)
    except Exception as exc:
        state.control_failures += 1
        delay = retry_delay(cfg.update_interval, state.control_failures, cfg.retry_max)
        log(f"DR lease update failed; retrying in {delay:.1f}s: {exc}", "ERROR")
        status.mark_failure(exc, time.monotonic() - started, role='dr', action='write-failed')
        return delay

    state.control_failures = 0
    _set_owner_metrics(status, 'dr')
    status.set_gauge('lease_expires_timestamp_seconds', new_lease.expires_at)
    action = 'renew' if lease.owner == 'dr' else 'promote'
    status.mark_success(time.monotonic() - started, role='dr', owner='dr', action=action)
    log("DR lease renewed" if action == 'renew' else "Primary lease expired - DR promoted", "WARN")
    return cfg.update_interval


def heartbeat_primary(
    cfg: Config,
    provider: DNSProvider,
    status: ServiceStatus,
    health_checker=None,
    failback_health_checker=None,
):
    log(f"Starting PRIMARY heartbeat for {cfg.dns_record}")
    log(f"  Update interval: {cfg.update_interval}s, Lease TTL: {cfg.lease_ttl}s")
    checker = health_checker or build_health_checker(cfg)
    recovery_checker = failback_health_checker
    if recovery_checker is None and cfg.failback_health_url:
        recovery_checker = HTTPHealthChecker(cfg.failback_health_url, cfg.health_timeout)
    state = PrimaryLoopState()
    while True:
        time.sleep(
            run_primary_iteration(
                cfg, provider, checker, state, status, recovery_checker
            )
        )


def heartbeat_dr(
    cfg: Config,
    provider: DNSProvider,
    status: ServiceStatus,
    primary_health_checker=None,
    local_health_checker=None,
    failback_health_checker=None,
):
    log(f"Starting DR heartbeat for {cfg.dns_record}")
    checker = primary_health_checker or build_health_checker(cfg)
    local_checker = local_health_checker
    if local_checker is None and cfg.local_health_url:
        local_checker = HTTPHealthChecker(cfg.local_health_url, cfg.health_timeout)
    recovery_checker = failback_health_checker
    if recovery_checker is None and cfg.failback_health_url:
        recovery_checker = HTTPHealthChecker(cfg.failback_health_url, cfg.health_timeout)
    log(f"  Health mode: {cfg.health_mode}; fail threshold: {cfg.fail_threshold}")
    state = DRLoopState()
    while True:
        time.sleep(
            run_dr_iteration(
                cfg,
                provider,
                checker,
                local_checker,
                state,
                status,
                recovery_checker,
            )
        )

# -----------------------------
# CLI
# -----------------------------

def main():
    parser = argparse.ArgumentParser(description='AST DNS Failover')
    parser.add_argument('command', nargs='?', default='run', choices=['run', 'init', 'promote', 'failback', 'show', 'validate'], help='Command to execute')
    parser.add_argument('--once', action='store_true', help='Run one heartbeat iteration and exit')
    args = parser.parse_args()
    
    cfg = Config.from_env()
    
    if args.command == 'validate':
        try:
            cfg.validate()
            print("Configuration valid")
            print(json.dumps({'role': cfg.role, 'provider': cfg.provider, 'dns_record': cfg.dns_record, 'primary_ip': cfg.primary_ip, 'dr_ip': cfg.dr_ip}, indent=2))
            sys.exit(0)
        except ValueError as e:
            print(f"Configuration invalid: {e}")
            sys.exit(1)
    
    cfg.validate()
    provider = build_provider(cfg)
    
    if args.command == 'init':
        init_dns(cfg, provider)
    elif args.command == 'promote':
        promote_to_dr(cfg, provider)
    elif args.command == 'failback':
        failback_to_primary(cfg, provider)
    elif args.command == 'show':
        show_dns(cfg, provider)
    elif args.command == 'run':
        status = ServiceStatus(
            'dns_failover',
            readiness_max_age=max(cfg.update_interval * 3, cfg.lease_ttl + cfg.max_clock_skew),
        )
        status.start_http_server(cfg.status_port)
        if cfg.role == 'primary':
            checker = build_health_checker(cfg)
            recovery_checker = (
                HTTPHealthChecker(cfg.failback_health_url, cfg.health_timeout)
                if cfg.failback_health_url else None
            )
            if args.once:
                run_primary_iteration(
                    cfg,
                    provider,
                    checker,
                    PrimaryLoopState(),
                    status,
                    recovery_checker,
                )
            else:
                heartbeat_primary(cfg, provider, status, checker, recovery_checker)
        else:
            checker = build_health_checker(cfg)
            local_checker = (
                HTTPHealthChecker(cfg.local_health_url, cfg.health_timeout)
                if cfg.local_health_url else None
            )
            recovery_checker = (
                HTTPHealthChecker(cfg.failback_health_url, cfg.health_timeout)
                if cfg.failback_health_url else None
            )
            if args.once:
                run_dr_iteration(
                    cfg,
                    provider,
                    checker,
                    local_checker,
                    DRLoopState(),
                    status,
                    recovery_checker,
                )
            else:
                heartbeat_dr(
                    cfg,
                    provider,
                    status,
                    checker,
                    local_checker,
                    recovery_checker,
                )

if __name__ == '__main__':
    main()
