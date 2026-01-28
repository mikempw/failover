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
import json
import os
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
            health_port=get_int('HEALTH_PORT', 6514),
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
    
    total = 0.0
    found = False
    
    for line in metrics_text.split('\n'):
        line = line.strip()
        # Skip comments and empty lines
        if not line or line.startswith('#'):
            continue
        
        # Check if line starts with metric name
        if line.startswith(metric_name):
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
    def __init__(self, cfg: Config):
        self.cfg = cfg
    
    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        raise NotImplementedError
    
    def get_records(self) -> Dict[str, Any]:
        raise NotImplementedError


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
            if proc.returncode == 0 and proc.stdout.strip():
                result['A'] = proc.stdout.strip().split('\n')[0]
        except Exception as e:
            log(f"Failed to query A record: {e}", "WARN")
        try:
            proc = subprocess.run(['dig', f'@{self.cfg.dns_server}', self.cfg.dns_record, 'TXT', '+short'], capture_output=True, text=True, timeout=5)
            if proc.returncode == 0 and proc.stdout.strip():
                result['TXT'] = proc.stdout.strip().replace('"', '')
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
            if proc.returncode == 0 and proc.stdout.strip():
                result['A'] = proc.stdout.strip().split('\n')[0]
        except Exception as e:
            log(f"Failed to query A record: {e}", "WARN")
        try:
            proc = subprocess.run(['dig', f'@{self.cfg.dns_server}', self.cfg.dns_record, 'TXT', '+short'], capture_output=True, text=True, timeout=5)
            if proc.returncode == 0 and proc.stdout.strip():
                result['TXT'] = proc.stdout.strip().replace('"', '')
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
        return records[0]['_ref'] if records else None
    
    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        txt_value = f"owner={owner} exp={exp_unix}"
        a_ref = self._find_record('A')
        if a_ref:
            self.session.put(f"{self.base_url}/{a_ref}", json={'ipv4addr': ip, 'ttl': self.cfg.dns_ttl}, timeout=10).raise_for_status()
        else:
            self.session.post(f"{self.base_url}/record:a", json={'name': self.cfg.dns_record, 'ipv4addr': ip, 'ttl': self.cfg.dns_ttl, 'zone': self.cfg.dns_zone}, timeout=10).raise_for_status()
        
        txt_ref = self._find_record('TXT')
        if txt_ref:
            self.session.put(f"{self.base_url}/{txt_ref}", json={'text': txt_value, 'ttl': self.cfg.dns_ttl}, timeout=10).raise_for_status()
        else:
            self.session.post(f"{self.base_url}/record:txt", json={'name': self.cfg.dns_record, 'text': txt_value, 'ttl': self.cfg.dns_ttl, 'zone': self.cfg.dns_zone}, timeout=10).raise_for_status()
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
            return data['result'][0]
        return None
    
    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        txt_value = f"owner={owner} exp={exp_unix}"
        
        a_record = self._find_record('A')
        if a_record:
            url = f"{self.base_url}/zones/{self.zone_id}/dns_records/{a_record['id']}"
            self.session.put(url, json={'type': 'A', 'name': self.cfg.dns_record, 'content': ip, 'ttl': self.cfg.dns_ttl}, timeout=10).raise_for_status()
        else:
            url = f"{self.base_url}/zones/{self.zone_id}/dns_records"
            self.session.post(url, json={'type': 'A', 'name': self.cfg.dns_record, 'content': ip, 'ttl': self.cfg.dns_ttl}, timeout=10).raise_for_status()
        
        txt_record = self._find_record('TXT')
        if txt_record:
            url = f"{self.base_url}/zones/{self.zone_id}/dns_records/{txt_record['id']}"
            self.session.put(url, json={'type': 'TXT', 'name': self.cfg.dns_record, 'content': txt_value, 'ttl': self.cfg.dns_ttl}, timeout=10).raise_for_status()
        else:
            url = f"{self.base_url}/zones/{self.zone_id}/dns_records"
            self.session.post(url, json={'type': 'TXT', 'name': self.cfg.dns_record, 'content': txt_value, 'ttl': self.cfg.dns_ttl}, timeout=10).raise_for_status()
        
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
        self.client = boto3.client('route53', aws_access_key_id=cfg.aws_access_key_id, aws_secret_access_key=cfg.aws_secret_access_key, region_name=cfg.aws_region)
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
                        result['A'] = rs['ResourceRecords'][0]['Value']
                    elif rs['Type'] == 'TXT':
                        result['TXT'] = rs['ResourceRecords'][0]['Value'].strip('"')
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
    
    def _get_record_name(self) -> str:
        if self.cfg.dns_record.endswith(self.cfg.dns_zone):
            return self.cfg.dns_record[:-len(self.cfg.dns_zone)-1]
        return self.cfg.dns_record
    
    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        txt_value = f"owner={owner} exp={exp_unix}"
        record_name = self._get_record_name()
        api_version = "2018-05-01"
        
        self.session.put(f"{self.base_url}/A/{record_name}?api-version={api_version}", json={'properties': {'TTL': self.cfg.dns_ttl, 'ARecords': [{'ipv4Address': ip}]}}, timeout=10).raise_for_status()
        self.session.put(f"{self.base_url}/TXT/{record_name}?api-version={api_version}", json={'properties': {'TTL': self.cfg.dns_ttl, 'TXTRecords': [{'value': [txt_value]}]}}, timeout=10).raise_for_status()
        log(f"[azure-dns] Set A={ip}, TXT={txt_value}")
    
    def get_records(self) -> Dict[str, Any]:
        result = {'A': None, 'TXT': None}
        record_name = self._get_record_name()
        api_version = "2018-05-01"
        try:
            resp = self.session.get(f"{self.base_url}/A/{record_name}?api-version={api_version}", timeout=10)
            if resp.status_code == 200:
                records = resp.json().get('properties', {}).get('ARecords', [])
                if records:
                    result['A'] = records[0]['ipv4Address']
        except Exception as e:
            log(f"Failed to query A record: {e}", "WARN")
        try:
            resp = self.session.get(f"{self.base_url}/TXT/{record_name}?api-version={api_version}", timeout=10)
            if resp.status_code == 200:
                records = resp.json().get('properties', {}).get('TXTRecords', [])
                if records and records[0].get('value'):
                    result['TXT'] = records[0]['value'][0]
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
        
        while changes.status != 'done':
            time.sleep(0.5)
            changes.reload()
        
        log(f"[gcp-dns] Set A={ip}, TXT=owner={owner} exp={exp_unix}")
    
    def get_records(self) -> Dict[str, Any]:
        result = {'A': None, 'TXT': None}
        try:
            for record_set in self.zone.list_resource_record_sets():
                if record_set.name == self.record_name:
                    if record_set.record_type == 'A':
                        result['A'] = record_set.rrdatas[0]
                    elif record_set.record_type == 'TXT':
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
    
    def _ensure_datagroup(self):
        url = f"{self.base_url}/ltm/data-group/internal/~{self.partition}~{self.datagroup_name}"
        resp = self.session.get(url, timeout=10)
        if resp.status_code == 404:
            url = f"{self.base_url}/ltm/data-group/internal"
            self.session.post(url, json={'name': self.datagroup_name, 'partition': self.partition, 'type': 'string'}, timeout=10).raise_for_status()
    
    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        self._ensure_datagroup()
        url = f"{self.base_url}/ltm/data-group/internal/~{self.partition}~{self.datagroup_name}"
        data = {'records': [{'name': 'owner', 'data': owner}, {'name': 'exp', 'data': str(exp_unix)}, {'name': 'ip', 'data': ip}]}
        self.session.patch(url, json=data, timeout=10).raise_for_status()
        
        if self.cfg.f5_pool_name:
            pool_url = f"{self.base_url}/gtm/pool/a/~{self.partition}~{self.cfg.f5_pool_name}/members"
            resp = self.session.get(pool_url, timeout=10)
            if resp.status_code == 200:
                for member in resp.json().get('items', []):
                    member_url = f"{self.base_url}/gtm/pool/a/~{self.partition}~{self.cfg.f5_pool_name}/members/{member['name']}"
                    member_ip = member.get('address', '').split('%')[0]
                    if member_ip == ip:
                        self.session.patch(member_url, json={'enabled': True}, timeout=10)
                    else:
                        self.session.patch(member_url, json={'disabled': True}, timeout=10)
        
        log(f"[f5-gtm] Set active={ip}, owner={owner}, exp={exp_unix}")
    
    def get_records(self) -> Dict[str, Any]:
        result = {'A': None, 'TXT': None}
        try:
            self._ensure_datagroup()
            url = f"{self.base_url}/ltm/data-group/internal/~{self.partition}~{self.datagroup_name}"
            resp = self.session.get(url, timeout=10)
            resp.raise_for_status()
            records = {r['name']: r.get('data', '') for r in resp.json().get('records', [])}
            result['A'] = records.get('ip')
            if 'owner' in records and 'exp' in records:
                result['TXT'] = f"owner={records['owner']} exp={records['exp']}"
        except Exception as e:
            log(f"Failed to query F5 data group: {e}", "WARN")
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
    exp = now_unix() + cfg.lease_ttl
    provider.set_records(cfg.primary_ip, 'primary', exp)
    log(f"Initialized DNS: A={cfg.primary_ip}, owner=primary")

def promote_to_dr(cfg: Config, provider: DNSProvider):
    exp = now_unix() + cfg.lease_ttl
    provider.set_records(cfg.dr_ip, 'dr', exp)
    log(f"FAILOVER: Promoted DR to active, A={cfg.dr_ip}")

def failback_to_primary(cfg: Config, provider: DNSProvider):
    exp = now_unix() + cfg.lease_ttl
    provider.set_records(cfg.primary_ip, 'primary', exp)
    log(f"FAILBACK: Restored primary as active, A={cfg.primary_ip}")

def show_dns(cfg: Config, provider: DNSProvider):
    records = provider.get_records()
    txt_data = parse_txt(records.get('TXT'))
    exp_unix = int(txt_data.get('exp', '0')) if txt_data.get('exp') else 0
    time_remaining = exp_unix - now_unix() if exp_unix else None
    print(json.dumps({'record': cfg.dns_record, 'A': records.get('A'), 'owner': txt_data.get('owner'), 'expires_at': txt_data.get('exp'), 'time_remaining': time_remaining}, indent=2))

# -----------------------------
# Heartbeat Loops
# -----------------------------

def heartbeat_primary(cfg: Config, provider: DNSProvider):
    log(f"Starting PRIMARY heartbeat for {cfg.dns_record}")
    log(f"  Update interval: {cfg.update_interval}s, Lease TTL: {cfg.lease_ttl}s")
    while True:
        try:
            exp = now_unix() + cfg.lease_ttl
            provider.set_records(cfg.primary_ip, 'primary', exp)
            log(f"Lease renewed, expires at {exp}")
        except Exception as e:
            log(f"Failed to renew lease: {e}", "ERROR")
        time.sleep(cfg.update_interval)

def heartbeat_dr(cfg: Config, provider: DNSProvider):
    log(f"Starting DR heartbeat for {cfg.dns_record}")
    
    # Initialize health checker based on mode
    if cfg.health_mode == 'metrics':
        if not cfg.health_url:
            log("HEALTH_MODE=metrics but HEALTH_URL not set, falling back to tcp", "WARN")
            cfg.health_mode = 'tcp'
        else:
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
    
    if cfg.health_mode == 'tcp':
        log(f"  Health mode: tcp")
        log(f"  Monitoring: {cfg.health_host}:{cfg.health_port}")
    
    log(f"  Fail threshold: {cfg.fail_threshold}, Check interval: {cfg.update_interval}s")
    
    consecutive_failures = 0
    while True:
        # Perform health check based on mode
        if cfg.health_mode == 'metrics':
            primary_healthy = metrics_checker.check()
        else:
            primary_healthy = check_tcp(cfg.health_host, cfg.health_port, cfg.health_timeout)
            if primary_healthy:
                log("Primary healthy")
        
        if primary_healthy:
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            if cfg.health_mode == 'tcp':
                log(f"Primary health check failed ({consecutive_failures}/{cfg.fail_threshold})", "WARN")
            # metrics mode logs its own messages
            
            if consecutive_failures >= cfg.fail_threshold:
                records = provider.get_records()
                txt_data = parse_txt(records.get('TXT'))
                current_owner = txt_data.get('owner')
                exp_unix = int(txt_data.get('exp', '0')) if txt_data.get('exp') else 0
                if current_owner == 'dr':
                    exp = now_unix() + cfg.lease_ttl
                    provider.set_records(cfg.dr_ip, 'dr', exp)
                    log("DR lease renewed")
                elif exp_unix < now_unix():
                    log("Primary lease expired - initiating failover!", "WARN")
                    promote_to_dr(cfg, provider)
                else:
                    remaining = exp_unix - now_unix()
                    log(f"Waiting for primary lease to expire ({remaining}s remaining)", "WARN")
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
        failback_to_primary(cfg, provider)
    elif args.command == 'show':
        show_dns(cfg, provider)
    elif args.command == 'run':
        if cfg.role == 'primary':
            heartbeat_primary(cfg, provider)
        else:
            heartbeat_dr(cfg, provider)

if __name__ == '__main__':
    main()
