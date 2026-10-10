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
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from datetime import datetime

# -----------------------------
# Vault Integration
# -----------------------------

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
            token = os.getenv('VAULT_TOKEN')
            if not token:
                log("VAULT_AUTH_METHOD=token but VAULT_TOKEN not set", "WARN")
                return {}
            client.token = token
            
        elif auth_method == 'approle':
            role_id = os.getenv('VAULT_ROLE_ID')
            secret_id = os.getenv('VAULT_SECRET_ID')
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
    """Get config value from Vault first, then env, then default."""
    vault_key = key.lower()
    env_key = key.upper()
    
    if vault_key in vault_data:
        return vault_data[vault_key]
    
    env_val = os.getenv(env_key)
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
    
    # F5 GTM
    f5_host: Optional[str] = None
    f5_username: Optional[str] = None
    f5_password: Optional[str] = None
    f5_partition: str = "Common"
    f5_verify_ssl: bool = True
    f5_pool_name: Optional[str] = None
    
    # Custom Script Provider
    script_set: Optional[str] = None  # Path to script that sets DNS records
    script_get: Optional[str] = None  # Path to script that gets DNS records
    
    @classmethod
    def from_env(cls):
        """Load configuration from Vault or environment variables."""
        vault_data = load_from_vault()
        
        def get(key: str, default: Any = None) -> Any:
            return get_config_value(key, vault_data, default)
        
        def get_bool(key: str, default: bool = False) -> bool:
            val = get(key, str(default))
            return str(val).lower() in ('true', '1', 'yes')
        
        def get_int(key: str, default: int = 0) -> int:
            return int(get(key, default))
        
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
            health_port=get_int('HEALTH_PORT', 8888),
            health_timeout=get_int('HEALTH_TIMEOUT', 2),
            health_mode=get('HEALTH_MODE', 'tcp'),
            health_url=get('HEALTH_URL'),
            health_metric=get('HEALTH_METRIC', 'otelcol_receiver_accepted_metric_points'),
            health_stale_count=get_int('HEALTH_STALE_COUNT', 3),
            role=get('ROLE', 'primary'),
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
            f5_host=get('F5_HOST'),
            f5_username=get('F5_USERNAME'),
            f5_password=get('F5_PASSWORD'),
            f5_partition=get('F5_PARTITION', 'Common'),
            f5_verify_ssl=get_bool('F5_VERIFY_SSL', True),
            f5_pool_name=get('F5_POOL_NAME'),
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
        
        elif self.provider == 'route53':
            if not self.aws_access_key_id:
                errors.append("AWS_ACCESS_KEY_ID required")
            if not self.aws_secret_access_key:
                errors.append("AWS_SECRET_ACCESS_KEY required")
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
        
        elif self.provider == 'f5-gtm':
            if not self.f5_host:
                errors.append("F5_HOST required")
            if not self.f5_username:
                errors.append("F5_USERNAME required")
            if not self.f5_password:
                errors.append("F5_PASSWORD required")
        
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
        if self.health_mode not in ['tcp', 'metrics']:
            errors.append(f"Invalid HEALTH_MODE: {self.health_mode}. Valid: tcp, metrics")
        
        if self.health_mode == 'metrics' and self.role == 'dr':
            if not self.health_url:
                errors.append("HEALTH_URL required when HEALTH_MODE=metrics")
            if not self.health_metric:
                errors.append("HEALTH_METRIC required when HEALTH_MODE=metrics")
        
        if self.lease_ttl <= self.update_interval:
            errors.append(f"LEASE_TTL ({self.lease_ttl}) must be > UPDATE_INTERVAL ({self.update_interval})")

        # Values below are interpolated into nsupdate scripts and API calls.
        # Reject anything that is not a plain hostname / IP so a stray newline
        # or space cannot inject extra nsupdate commands.
        for name, value in (('DNS_RECORD', self.dns_record), ('DNS_ZONE', self.dns_zone)):
            if not is_valid_dns_name(value):
                errors.append(f"Invalid {name}: {value!r}")
        if self.provider in ('bind-tsig', 'ad-gss') and not (
                is_valid_dns_name(self.dns_server) or is_valid_ip(self.dns_server)):
            errors.append(f"Invalid DNS_SERVER: {self.dns_server!r}")
        for name, value in (('PRIMARY_IP', self.primary_ip), ('DR_IP', self.dr_ip)):
            if not is_valid_ip(value):
                errors.append(f"Invalid {name}: {value!r}")
        if self.primary_ip == self.dr_ip:
            errors.append("PRIMARY_IP and DR_IP must differ")

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

def parse_txt(txt: str) -> Dict[str, str]:
    result = {}
    if not txt:
        return result
    try:
        for part in txt.replace('"', '').split():
            if '=' in part:
                k, v = part.split('=', 1)
                result[k] = v
    except Exception:
        pass
    return result

_DNS_LABEL_RE = re.compile(r'^[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?$')

def is_valid_dns_name(name: Optional[str]) -> bool:
    """True for a plain DNS name (labels of letters/digits/-/_), optional trailing dot."""
    if not name or not isinstance(name, str) or len(name) > 253:
        return False
    labels = name[:-1].split('.') if name.endswith('.') else name.split('.')
    return all(_DNS_LABEL_RE.match(label) for label in labels)

def is_valid_ip(value: Optional[str]) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except (ValueError, TypeError):
        return False


class LeaseReadError(RuntimeError):
    """The DNS provider could not be queried. Distinct from 'record absent'."""


def check_tcp(host: str, port: int, timeout: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
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

    # Exact name match only (so 'foo' does not also sum 'foo_total'), optional
    # label set, value, optional timestamp.
    pattern = re.compile(
        r'^' + re.escape(metric_name) + r'(?:\{.*\})?\s+(\S+)(?:\s+-?\d+)?\s*$'
    )
    total = 0.0
    found = False

    for line in metrics_text.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        m = pattern.match(line)
        if not m:
            continue
        try:
            total += float(m.group(1))
            found = True
        except ValueError:
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
    
    def reset(self):
        """Forget history (e.g. after the local collector was intentionally stopped)."""
        self.last_value = None
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
        
        # Counter went backwards: the collector restarted. Re-baseline without
        # counting it as stale or as healthy; the next reading decides.
        if current_value < self.last_value:
            log(f"Metrics counter reset ({self.last_value} -> {current_value}), re-baselining", "WARN")
            self.last_value = current_value
            return self.stale_checks < self.stale_count

        # Compare with last value
        if current_value > self.last_value:
            # Value increasing - healthy
            log(f"Metrics healthy: {self.metric_name}={current_value} (+{current_value - self.last_value:.0f})")
            self.last_value = current_value
            self.stale_checks = 0
            return True
        else:
            # Value flat or decreasing
            self.stale_checks += 1
            log(f"Metrics stale: {self.metric_name}={current_value} (unchanged, {self.stale_checks}/{self.stale_count})", "WARN")
            self.last_value = current_value
            return self.stale_checks < self.stale_count

# -----------------------------
# DNS Providers
# -----------------------------

class DNSProvider:
    """
    Provider contract:
      set_records(ip, owner, exp_unix) -> None; raises on failure.
      get_records() -> {'A': str|None, 'TXT': str|None}
          None means the record does not exist.
          Any failure to *query* the provider must raise (never return None),
          so callers can tell "no lease" apart from "could not read the lease".
    """
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        raise NotImplementedError

    def get_records(self) -> Dict[str, Any]:
        raise NotImplementedError


def select_lease_txt(values: List[str]) -> Optional[str]:
    """Pick our lease TXT value (owner=... exp=...) out of all TXT values on the name."""
    for v in values:
        if v and 'owner=' in v:
            return v
    return None


class DryRunProvider(DNSProvider):
    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        state = {'A': ip, 'TXT': f'owner={owner} exp={exp_unix}', 'updated_at': datetime.now().isoformat()}
        statefile = self.cfg.dryrun_statefile or '/state/zone.json'
        os.makedirs(os.path.dirname(statefile), exist_ok=True)
        with open(statefile, 'w') as f:
            json.dump(state, f, indent=2)
        log(f"[dry-run] Set A={ip}, TXT=owner={owner} exp={exp_unix}")

    def get_records(self) -> Dict[str, Any]:
        statefile = self.cfg.dryrun_statefile or '/state/zone.json'
        try:
            with open(statefile, 'r') as f:
                state = json.load(f)
            return {'A': state.get('A'), 'TXT': state.get('TXT')}
        except FileNotFoundError:
            return {'A': None, 'TXT': None}


_DIG_STATUS_RE = re.compile(r'status:\s*([A-Z]+)')
_TXT_CHUNK_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')

def dig_query(server: str, name: str, rtype: str, timeout: int = 5) -> List[str]:
    """
    Query `name` `rtype` at `server` with dig. Returns the answer values
    (empty list for NXDOMAIN / no data). Raises LeaseReadError on SERVFAIL,
    REFUSED, timeouts or dig errors.
    """
    proc = subprocess.run(
        ['dig', f'@{server}', name, rtype, '+noall', '+comments', '+answer', '+time=2', '+tries=2'],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise LeaseReadError(f"dig {rtype} failed (rc={proc.returncode}): {proc.stdout.strip() or proc.stderr.strip()}")
    m = _DIG_STATUS_RE.search(proc.stdout)
    if not m:
        raise LeaseReadError(f"dig {rtype}: no response status in output")
    status = m.group(1)
    if status == 'NXDOMAIN':
        return []
    if status != 'NOERROR':
        raise LeaseReadError(f"dig {rtype}: DNS status {status}")

    values = []
    for line in proc.stdout.splitlines():
        if not line.strip() or line.startswith(';'):
            continue
        parts = line.split(None, 4)
        # name ttl class type rdata
        if len(parts) < 5 or parts[3].upper() != rtype.upper():
            continue
        rdata = parts[4].strip()
        if rtype.upper() == 'TXT':
            chunks = _TXT_CHUNK_RE.findall(rdata)
            values.append(''.join(chunks) if chunks else rdata)
        else:
            values.append(rdata)
    return values


class NsupdateProvider(DNSProvider):
    """RFC2136 dynamic update via nsupdate; reads via dig against the same server."""
    label = 'nsupdate'

    def nsupdate_args(self) -> List[str]:
        raise NotImplementedError

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
        result = subprocess.run(self.nsupdate_args(), input=commands, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise RuntimeError(f"nsupdate failed: {result.stderr}")
        log(f"[{self.label}] Set A={ip}, TXT={txt_value}")

    def get_records(self) -> Dict[str, Any]:
        a_values = dig_query(self.cfg.dns_server, self.cfg.dns_record, 'A')
        txt_values = dig_query(self.cfg.dns_server, self.cfg.dns_record, 'TXT')
        return {'A': a_values[0] if a_values else None, 'TXT': select_lease_txt(txt_values)}


class BindTSIGProvider(NsupdateProvider):
    label = 'bind-tsig'

    def nsupdate_args(self) -> List[str]:
        return ['nsupdate', '-k', self.cfg.tsig_keyfile]


class ADGSSProvider(NsupdateProvider):
    label = 'ad-gss'

    def nsupdate_args(self) -> List[str]:
        return ['nsupdate', '-g']


class InfobloxProvider(DNSProvider):
    def __init__(self, cfg: Config):
        super().__init__(cfg)
        import requests
        self.session = requests.Session()
        self.session.auth = (cfg.infoblox_username, cfg.infoblox_password)
        self.session.verify = cfg.infoblox_verify_ssl
        self.base_url = f"https://{cfg.infoblox_host}/wapi/{cfg.infoblox_wapi_version}"

    def _search(self, record_type: str, fields: str) -> List[Dict]:
        url = f"{self.base_url}/record:{record_type}"
        params = {'name': self.cfg.dns_record, 'zone': self.cfg.dns_zone, '_return_fields': fields}
        resp = self.session.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _a_record(self) -> Optional[Dict]:
        records = self._search('a', 'ipv4addr')
        return records[0] if records else None

    def _txt_record(self) -> Optional[Dict]:
        for rec in self._search('txt', 'text'):
            if 'owner=' in (rec.get('text') or ''):
                return rec
        return None

    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        txt_value = f"owner={owner} exp={exp_unix}"
        a_rec = self._a_record()
        if a_rec:
            self.session.put(f"{self.base_url}/{a_rec['_ref']}", json={'ipv4addr': ip, 'ttl': self.cfg.dns_ttl, 'use_ttl': True}, timeout=10).raise_for_status()
        else:
            self.session.post(f"{self.base_url}/record:a", json={'name': self.cfg.dns_record, 'ipv4addr': ip, 'ttl': self.cfg.dns_ttl, 'use_ttl': True}, timeout=10).raise_for_status()

        txt_rec = self._txt_record()
        if txt_rec:
            self.session.put(f"{self.base_url}/{txt_rec['_ref']}", json={'text': txt_value, 'ttl': self.cfg.dns_ttl, 'use_ttl': True}, timeout=10).raise_for_status()
        else:
            self.session.post(f"{self.base_url}/record:txt", json={'name': self.cfg.dns_record, 'text': txt_value, 'ttl': self.cfg.dns_ttl, 'use_ttl': True}, timeout=10).raise_for_status()
        log(f"[infoblox] Set A={ip}, TXT={txt_value}")

    def get_records(self) -> Dict[str, Any]:
        a_rec = self._a_record()
        txt_rec = self._txt_record()
        return {
            'A': a_rec.get('ipv4addr') if a_rec else None,
            'TXT': txt_rec.get('text') if txt_rec else None,
        }


class CloudflareProvider(DNSProvider):
    def __init__(self, cfg: Config):
        super().__init__(cfg)
        import requests
        self.session = requests.Session()
        self.session.headers.update({'Authorization': f'Bearer {cfg.cloudflare_api_token}', 'Content-Type': 'application/json'})
        self.base_url = "https://api.cloudflare.com/client/v4"
        self.zone_id = cfg.cloudflare_zone_id

    def _list(self, record_type: str) -> List[Dict]:
        url = f"{self.base_url}/zones/{self.zone_id}/dns_records"
        params = {'type': record_type, 'name': self.cfg.dns_record}
        resp = self.session.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data.get('success'):
            raise LeaseReadError(f"Cloudflare API error: {data.get('errors')}")
        return data.get('result') or []

    def _a_record(self) -> Optional[Dict]:
        records = self._list('A')
        return records[0] if records else None

    def _txt_record(self) -> Optional[Dict]:
        # The name may carry other TXT records (SPF, verification); only touch ours.
        for rec in self._list('TXT'):
            if 'owner=' in rec.get('content', ''):
                return rec
        return None

    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        txt_value = f"owner={owner} exp={exp_unix}"
        records_url = f"{self.base_url}/zones/{self.zone_id}/dns_records"

        a_record = self._a_record()
        a_body = {'type': 'A', 'name': self.cfg.dns_record, 'content': ip, 'ttl': self.cfg.dns_ttl}
        if a_record:
            self.session.put(f"{records_url}/{a_record['id']}", json=a_body, timeout=10).raise_for_status()
        else:
            self.session.post(records_url, json=a_body, timeout=10).raise_for_status()

        txt_record = self._txt_record()
        txt_body = {'type': 'TXT', 'name': self.cfg.dns_record, 'content': txt_value, 'ttl': self.cfg.dns_ttl}
        if txt_record:
            self.session.put(f"{records_url}/{txt_record['id']}", json=txt_body, timeout=10).raise_for_status()
        else:
            self.session.post(records_url, json=txt_body, timeout=10).raise_for_status()

        log(f"[cloudflare] Set A={ip}, TXT={txt_value}")

    def get_records(self) -> Dict[str, Any]:
        a_record = self._a_record()
        txt_record = self._txt_record()
        return {
            'A': a_record['content'] if a_record else None,
            'TXT': txt_record['content'] if txt_record else None,
        }


class Route53Provider(DNSProvider):
    def __init__(self, cfg: Config):
        super().__init__(cfg)
        import boto3
        self.client = boto3.client('route53', aws_access_key_id=cfg.aws_access_key_id, aws_secret_access_key=cfg.aws_secret_access_key, region_name=cfg.aws_region)
        self.zone_id = cfg.route53_zone_id
        self.record_name = cfg.dns_record if cfg.dns_record.endswith('.') else f"{cfg.dns_record}."

    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        # Note: UPSERT replaces the whole TXT set on this name.
        txt_value = f'"owner={owner} exp={exp_unix}"'
        changes = [
            {'Action': 'UPSERT', 'ResourceRecordSet': {'Name': self.record_name, 'Type': 'A', 'TTL': self.cfg.dns_ttl, 'ResourceRecords': [{'Value': ip}]}},
            {'Action': 'UPSERT', 'ResourceRecordSet': {'Name': self.record_name, 'Type': 'TXT', 'TTL': self.cfg.dns_ttl, 'ResourceRecords': [{'Value': txt_value}]}}
        ]
        self.client.change_resource_record_sets(HostedZoneId=self.zone_id, ChangeBatch={'Changes': changes})
        log(f"[route53] Set A={ip}, TXT=owner={owner} exp={exp_unix}")

    def get_records(self) -> Dict[str, Any]:
        result = {'A': None, 'TXT': None}
        resp = self.client.list_resource_record_sets(HostedZoneId=self.zone_id, StartRecordName=self.record_name, MaxItems='10')
        for rs in resp.get('ResourceRecordSets', []):
            if rs['Name'] != self.record_name:
                continue
            values = [r['Value'] for r in rs.get('ResourceRecords', [])]
            if rs['Type'] == 'A' and values:
                result['A'] = values[0]
            elif rs['Type'] == 'TXT':
                unquoted = [''.join(_TXT_CHUNK_RE.findall(v)) or v.strip('"') for v in values]
                result['TXT'] = select_lease_txt(unquoted)
        return result


class AzureDNSProvider(DNSProvider):
    API_VERSION = "2018-05-01"
    TOKEN_REFRESH_MARGIN = 300  # seconds before expiry to refresh

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        import requests
        self._requests = requests
        self.session = requests.Session()
        self.session.headers.update({'Content-Type': 'application/json'})
        self.base_url = f"https://management.azure.com/subscriptions/{cfg.azure_subscription_id}/resourceGroups/{cfg.azure_resource_group}/providers/Microsoft.Network/dnsZones/{cfg.dns_zone}"
        self._token_expires_at = 0.0
        self._ensure_token()

    def _ensure_token(self):
        """Fetch a client-credentials token, refreshing before it expires (tokens last ~1h)."""
        if time.time() < self._token_expires_at - self.TOKEN_REFRESH_MARGIN:
            return
        url = f"https://login.microsoftonline.com/{self.cfg.azure_tenant_id}/oauth2/v2.0/token"
        data = {'grant_type': 'client_credentials', 'client_id': self.cfg.azure_client_id, 'client_secret': self.cfg.azure_client_secret, 'scope': 'https://management.azure.com/.default'}
        resp = self._requests.post(url, data=data, timeout=10)
        resp.raise_for_status()
        body = resp.json()
        self.session.headers['Authorization'] = f"Bearer {body['access_token']}"
        self._token_expires_at = time.time() + int(body.get('expires_in', 3600))

    def _get_record_name(self) -> str:
        record = self.cfg.dns_record.rstrip('.')
        zone = self.cfg.dns_zone.rstrip('.')
        if record == zone:
            return '@'
        if record.endswith('.' + zone):
            return record[:-len(zone) - 1]
        return record

    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        self._ensure_token()
        txt_value = f"owner={owner} exp={exp_unix}"
        record_name = self._get_record_name()

        self.session.put(f"{self.base_url}/A/{record_name}?api-version={self.API_VERSION}", json={'properties': {'TTL': self.cfg.dns_ttl, 'ARecords': [{'ipv4Address': ip}]}}, timeout=10).raise_for_status()
        self.session.put(f"{self.base_url}/TXT/{record_name}?api-version={self.API_VERSION}", json={'properties': {'TTL': self.cfg.dns_ttl, 'TXTRecords': [{'value': [txt_value]}]}}, timeout=10).raise_for_status()
        log(f"[azure-dns] Set A={ip}, TXT={txt_value}")

    def _get(self, rtype: str) -> Optional[Dict]:
        resp = self.session.get(f"{self.base_url}/{rtype}/{self._get_record_name()}?api-version={self.API_VERSION}", timeout=10)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get('properties', {})

    def get_records(self) -> Dict[str, Any]:
        self._ensure_token()
        result = {'A': None, 'TXT': None}
        a_props = self._get('A')
        if a_props and a_props.get('ARecords'):
            result['A'] = a_props['ARecords'][0]['ipv4Address']
        txt_props = self._get('TXT')
        if txt_props:
            values = [''.join(r.get('value') or []) for r in txt_props.get('TXTRecords', [])]
            result['TXT'] = select_lease_txt(values)
        return result


class GCPDNSProvider(DNSProvider):
    CHANGE_TIMEOUT = 120  # seconds to wait for a change to apply

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

        deadline = time.time() + self.CHANGE_TIMEOUT
        while changes.status != 'done':
            if time.time() > deadline:
                raise RuntimeError(f"GCP DNS change not applied after {self.CHANGE_TIMEOUT}s (status={changes.status})")
            time.sleep(0.5)
            changes.reload()

        log(f"[gcp-dns] Set A={ip}, TXT=owner={owner} exp={exp_unix}")

    def get_records(self) -> Dict[str, Any]:
        result = {'A': None, 'TXT': None}
        for record_set in self.zone.list_resource_record_sets():
            if record_set.name != self.record_name:
                continue
            if record_set.record_type == 'A' and record_set.rrdatas:
                result['A'] = record_set.rrdatas[0]
            elif record_set.record_type == 'TXT':
                values = [''.join(_TXT_CHUNK_RE.findall(v)) or v.strip('"') for v in record_set.rrdatas]
                result['TXT'] = select_lease_txt(values)
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

    def _datagroup_url(self) -> str:
        return f"{self.base_url}/ltm/data-group/internal/~{self.partition}~{self.datagroup_name}"

    def _ensure_datagroup(self):
        resp = self.session.get(self._datagroup_url(), timeout=10)
        if resp.status_code == 404:
            url = f"{self.base_url}/ltm/data-group/internal"
            self.session.post(url, json={'name': self.datagroup_name, 'partition': self.partition, 'type': 'string'}, timeout=10).raise_for_status()
        else:
            resp.raise_for_status()

    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        self._ensure_datagroup()
        data = {'records': [{'name': 'owner', 'data': owner}, {'name': 'exp', 'data': str(exp_unix)}, {'name': 'ip', 'data': ip}]}
        self.session.patch(self._datagroup_url(), json=data, timeout=10).raise_for_status()

        if self.cfg.f5_pool_name:
            pool_url = f"{self.base_url}/gtm/pool/a/~{self.partition}~{self.cfg.f5_pool_name}/members"
            resp = self.session.get(pool_url, timeout=10)
            resp.raise_for_status()
            # Enable the target member before disabling others so the pool is never empty.
            members = resp.json().get('items', [])
            members.sort(key=lambda m: m.get('address', '').split('%')[0] != ip)
            for member in members:
                member_url = f"{pool_url}/{member['name']}"
                member_ip = member.get('address', '').split('%')[0]
                body = {'enabled': True} if member_ip == ip else {'disabled': True}
                self.session.patch(member_url, json=body, timeout=10).raise_for_status()

        log(f"[f5-gtm] Set active={ip}, owner={owner}, exp={exp_unix}")

    def get_records(self) -> Dict[str, Any]:
        result = {'A': None, 'TXT': None}
        resp = self.session.get(self._datagroup_url(), timeout=10)
        if resp.status_code == 404:
            return result
        resp.raise_for_status()
        records = {r['name']: r.get('data', '') for r in resp.json().get('records', [])}
        result['A'] = records.get('ip')
        if 'owner' in records and 'exp' in records:
            result['TXT'] = f"owner={records['owner']} exp={records['exp']}"
        return result


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
                  Use null / omit keys when the record does not exist.
       Exit 0 on success, non-zero on failure (treated as "could not read",
       which never triggers a failover by itself)

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
            proc = subprocess.run(args, capture_output=True, text=True, timeout=30, env=env)
        except subprocess.TimeoutExpired:
            raise LeaseReadError("SCRIPT_GET timed out")

        if proc.returncode != 0:
            raise LeaseReadError(f"SCRIPT_GET failed: {proc.stderr.strip() or proc.stdout.strip() or proc.returncode}")

        output = proc.stdout.strip()
        if not output:
            return {'A': None, 'TXT': None}
        try:
            data = json.loads(output)
        except json.JSONDecodeError as e:
            raise LeaseReadError(f"SCRIPT_GET returned invalid JSON: {e}")
        if not isinstance(data, dict):
            raise LeaseReadError("SCRIPT_GET must return a JSON object")
        return {'A': data.get('A'), 'TXT': data.get('TXT')}


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

@dataclass
class Lease:
    ip: Optional[str]
    owner: Optional[str]
    exp: int  # 0 when absent / unparseable

def read_lease(provider: DNSProvider) -> Lease:
    """Read the current lease. Raises LeaseReadError if the provider can't be queried."""
    try:
        records = provider.get_records()
    except LeaseReadError:
        raise
    except Exception as e:
        raise LeaseReadError(str(e)) from e
    if not isinstance(records, dict):
        raise LeaseReadError(f"provider returned {type(records).__name__}, expected dict")
    for key in ('A', 'TXT'):
        if records.get(key) is not None and not isinstance(records.get(key), str):
            raise LeaseReadError(f"provider returned non-string {key}: {records.get(key)!r}")
    txt = parse_txt(records.get('TXT'))
    try:
        exp = int(txt.get('exp') or 0)
    except ValueError:
        exp = 0
    return Lease(ip=records.get('A'), owner=txt.get('owner'), exp=exp)


# Decisions are pure functions so they can be unit-tested without timing loops.

def decide_primary(lease: Lease, now: int, reclaim_grace: int = 0) -> str:
    """
    Primary heartbeat decision.
      'renew'   - write primary lease
      'yield'   - DR holds a live lease; leave it (manual 'failback' reclaims)
      'reclaim' - DR owns the lease but stopped renewing it (DR is down); take it back.
                  Requires the lease to be expired by more than `reclaim_grace`
                  seconds so modest clock skew between sites can't cause flapping.
    """
    if lease.owner == 'dr':
        return 'reclaim' if lease.exp + reclaim_grace < now else 'yield'
    return 'renew'

def decide_dr(lease: Lease, now: int, primary_failed: bool) -> str:
    """
    DR heartbeat decision.
      'renew'   - DR owns the lease; keep it alive until a manual failback
      'promote' - primary failed and its lease expired; take over
      'wait'    - primary failed but its lease is still live
      'idle'    - nothing to do
    """
    if lease.owner == 'dr':
        return 'renew'
    if not primary_failed:
        return 'idle'
    if lease.exp < now:
        return 'promote'
    return 'wait'


def init_dns(cfg: Config, provider: DNSProvider):
    exp = now_unix() + cfg.lease_ttl
    provider.set_records(cfg.primary_ip, 'primary', exp)
    log(f"Initialized DNS: A={cfg.primary_ip}, owner=primary")

def promote_to_dr(cfg: Config, provider: DNSProvider):
    exp = now_unix() + cfg.lease_ttl
    provider.set_records(cfg.dr_ip, 'dr', exp)
    log(f"FAILOVER: Promoted DR to active, A={cfg.dr_ip}")

def failback_to_primary(cfg: Config, provider: DNSProvider, verify_delay: Optional[float] = None, attempts: int = 3) -> bool:
    """
    Write the primary lease, then re-read after one DR interval to confirm a DR
    renewal that was in flight didn't overwrite it. Retries; returns success.
    """
    delay = cfg.update_interval + 2 if verify_delay is None else verify_delay
    for attempt in range(1, attempts + 1):
        exp = now_unix() + cfg.lease_ttl
        provider.set_records(cfg.primary_ip, 'primary', exp)
        log(f"FAILBACK: wrote primary lease (attempt {attempt}), verifying in {delay:.0f}s")
        time.sleep(delay)
        try:
            lease = read_lease(provider)
        except LeaseReadError as e:
            log(f"FAILBACK: cannot verify ({e})", "WARN")
            continue
        if lease.owner == 'primary' and lease.ip == cfg.primary_ip:
            log(f"FAILBACK: Restored primary as active, A={cfg.primary_ip}")
            return True
        log(f"FAILBACK: lease overwritten (owner={lease.owner}), retrying", "WARN")
    log("FAILBACK: could not confirm primary owns the lease", "ERROR")
    return False

def show_dns(cfg: Config, provider: DNSProvider):
    lease = read_lease(provider)
    time_remaining = lease.exp - now_unix() if lease.exp else None
    print(json.dumps({'record': cfg.dns_record, 'A': lease.ip, 'owner': lease.owner, 'expires_at': lease.exp or None, 'time_remaining': time_remaining}, indent=2))

# -----------------------------
# Heartbeat Loops
# -----------------------------

def primary_tick(cfg: Config, provider: DNSProvider, self_health_checker: Optional['MetricsHealthChecker']) -> str:
    """One primary heartbeat iteration. Returns the action taken (for logging/tests)."""
    try:
        lease = read_lease(provider)
    except LeaseReadError as e:
        # Don't write blind: if DR holds the lease we'd overwrite it and flap.
        log(f"Cannot read lease ({e}) - skipping renewal this cycle", "WARN")
        return 'read-error'
    
    action = decide_primary(lease, now_unix(), reclaim_grace=cfg.lease_ttl)
    if action == 'yield':
        log("DR currently owns lease - not overwriting (run 'failback' to reclaim)", "WARN")
        return action
    
    if action == 'reclaim':
        # Our collector is stopped while DR is active (the watcher follows DNS),
        # so the self-check can't pass yet. Reclaim, and start its history fresh.
        log(f"DR lease expired at {lease.exp} (DR stopped renewing) - reclaiming", "WARN")
        if self_health_checker:
            self_health_checker.reset()
    elif self_health_checker and not self_health_checker.check():
        log("Local OTEL unhealthy - NOT renewing lease (will expire)", "WARN")
        return 'unhealthy'
    
    try:
        exp = now_unix() + cfg.lease_ttl
        provider.set_records(cfg.primary_ip, 'primary', exp)
        log(f"Lease renewed, expires at {exp}")
    except Exception as e:
        log(f"Failed to renew lease: {e}", "ERROR")
        return 'write-error'
    return action

def heartbeat_primary(cfg: Config, provider: DNSProvider):
    log(f"Starting PRIMARY heartbeat for {cfg.dns_record}")
    log(f"  Update interval: {cfg.update_interval}s, Lease TTL: {cfg.lease_ttl}s")

    self_health_checker = None
    if cfg.health_url:
        log(f"  Self-health check enabled:")
        log(f"    URL: {cfg.health_url}")
        log(f"    Metric: {cfg.health_metric}")
        log(f"    Stale threshold: {cfg.health_stale_count}")
        self_health_checker = MetricsHealthChecker(
            cfg.health_url,
            cfg.health_metric,
            cfg.health_stale_count,
            cfg.health_timeout
        )
    else:
        log(f"  Self-health check: disabled (no HEALTH_URL)")

    while True:
        try:
            primary_tick(cfg, provider, self_health_checker)
        except Exception as e:
            log(f"Unexpected error in primary loop: {e}", "ERROR")
        time.sleep(cfg.update_interval)


class DRState:
    def __init__(self):
        self.consecutive_failures = 0

def dr_tick(cfg: Config, provider: DNSProvider, state: DRState, check_primary) -> str:
    """One DR heartbeat iteration. `check_primary()` returns True if primary is healthy."""
    if check_primary():
        if state.consecutive_failures:
            log("Primary healthy")
        state.consecutive_failures = 0
    else:
        state.consecutive_failures += 1
        log(f"Primary health check failed ({state.consecutive_failures}/{cfg.fail_threshold})", "WARN")
    primary_failed = state.consecutive_failures >= cfg.fail_threshold

    try:
        lease = read_lease(provider)
    except LeaseReadError as e:
        # Never fail over on an unreadable lease: we can't prove primary's lease expired.
        log(f"Cannot read lease ({e}) - not acting this cycle", "WARN")
        return 'read-error'

    now = now_unix()
    action = decide_dr(lease, now, primary_failed)
    try:
        if action == 'renew':
            provider.set_records(cfg.dr_ip, 'dr', now + cfg.lease_ttl)
            log("DR lease renewed")
        elif action == 'promote':
            log("Primary lease expired - initiating failover!", "WARN")
            promote_to_dr(cfg, provider)
        elif action == 'wait':
            log(f"Waiting for primary lease to expire ({lease.exp - now}s remaining)", "WARN")
    except Exception as e:
        log(f"DNS update failed during '{action}': {e}", "ERROR")
        return 'write-error'
    return action

def heartbeat_dr(cfg: Config, provider: DNSProvider):
    log(f"Starting DR heartbeat for {cfg.dns_record}")

    if cfg.health_mode == 'metrics' and cfg.health_url:
        log(f"  Health mode: metrics")
        log(f"  Metrics URL: {cfg.health_url}")
        log(f"  Metric: {cfg.health_metric}")
        log(f"  Stale threshold: {cfg.health_stale_count}")
        metrics_checker = MetricsHealthChecker(
            cfg.health_url,
            cfg.health_metric,
            cfg.health_stale_count,
            cfg.health_timeout
        )
        check_primary = metrics_checker.check
    else:
        log(f"  Health mode: tcp")
        log(f"  Monitoring: {cfg.health_host}:{cfg.health_port}")
        check_primary = lambda: check_tcp(cfg.health_host, cfg.health_port, cfg.health_timeout)

    log(f"  Fail threshold: {cfg.fail_threshold}, Check interval: {cfg.update_interval}s")

    state = DRState()
    while True:
        try:
            dr_tick(cfg, provider, state, check_primary)
        except Exception as e:
            log(f"Unexpected error in DR loop: {e}", "ERROR")
        time.sleep(cfg.update_interval)

# -----------------------------
# CLI
# -----------------------------

def main():
    parser = argparse.ArgumentParser(description='AST DNS Failover')
    parser.add_argument('command', nargs='?', default='run', choices=['run', 'init', 'promote', 'failback', 'show', 'validate'], help='Command to execute')
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
        if not failback_to_primary(cfg, provider):
            sys.exit(1)
    elif args.command == 'show':
        show_dns(cfg, provider)
    elif args.command == 'run':
        if cfg.role == 'primary':
            heartbeat_primary(cfg, provider)
        else:
            heartbeat_dr(cfg, provider)

if __name__ == '__main__':
    main()
