# AST DNS Failover - Technical Documentation

## Overview

AST DNS Failover provides automated active/passive failover for OpenTelemetry collectors using DNS-based coordination. It ensures only one collector scrapes BIG-IP devices at any time, preventing API contention issues inherent to BIG-IP's iControl REST API.

**Problem Statement:** BIG-IP's iControl REST API cannot handle concurrent scraping from multiple collectors. Running collectors at both primary and DR sites simultaneously causes API timeouts, incomplete data, and device performance degradation.

**Solution:** DNS-based lease mechanism coordinates which site is active. Only the active site runs its OTEL collector.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              PRIMARY SITE                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────────────┐      ┌──────────────────────┐                     │
│  │   dns-failover.py    │      │   OTEL Collector     │                     │
│  │   (ROLE=primary)     │      │   (always running)   │                     │
│  │                      │      │                      │                     │
│  │  - Renews DNS lease  │      │  - Scrapes BIG-IPs   │                     │
│  │    every N seconds   │      │  - Exports metrics   │                     │
│  │  - Writes A + TXT    │      │  - Port 8888 metrics │                     │
│  │    records           │      │                      │                     │
│  └──────────┬───────────┘      └──────────────────────┘                     │
│             │                                                                │
└─────────────┼────────────────────────────────────────────────────────────────┘
              │
              │  DNS Updates (A record + TXT lease record)
              ▼
     ┌─────────────────┐
     │   DNS Server    │
     │                 │
     │  Route53 /      │
     │  Cloudflare /   │
     │  Infoblox /     │
     │  BIND / etc.    │
     └────────┬────────┘
              │
              │  DNS Queries + Updates
              │
┌─────────────┼────────────────────────────────────────────────────────────────┐
│             ▼                        DR SITE                                 │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────────────┐      ┌──────────────────────┐                     │
│  │   dns-failover.py    │      │   otel-watcher.py    │                     │
│  │   (ROLE=dr)          │      │                      │                     │
│  │                      │      │  - Watches DNS       │                     │
│  │  - Health checks     │      │  - Compares to MY_IP │                     │
│  │    primary           │      │  - Controls OTEL     │                     │
│  │  - Monitors lease    │      │    container         │                     │
│  │  - Takes over if     │      │                      │                     │
│  │    primary fails     │      │                      │                     │
│  └──────────────────────┘      └──────────┬───────────┘                     │
│                                           │                                  │
│                                           │ docker start/stop                │
│                                           ▼                                  │
│                                ┌──────────────────────┐                     │
│                                │   OTEL Collector     │                     │
│                                │   (controlled)       │                     │
│                                │                      │                     │
│                                │  - Starts on         │                     │
│                                │    failover          │                     │
│                                │  - Stops on          │                     │
│                                │    failback          │                     │
│                                └──────────────────────┘                     │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Components

### 1. dns-failover.py

Core failover coordination daemon. Runs on both primary and DR sites with different roles.

**Primary Role (`ROLE=primary`):**
- Continuously renews DNS lease by updating A and TXT records
- A record: Points to primary IP
- TXT record: Contains `owner=primary exp=<unix_timestamp>`
- Runs until stopped; no health checking of DR

**DR Role (`ROLE=dr`):**
- Performs health checks against primary
- Monitors DNS lease expiration
- Takes over DNS if: health check fails AND lease expired
- Renews its own lease once active

### 2. otel-watcher.py (DR site only)

Controls OTEL collector based on DNS state.

**Variants:**
- `otel_watcher_docker.py` - Controls Docker containers via `docker start/stop`
- `otel_watcher_k8s.py` - Controls Kubernetes deployments via `kubectl scale`

**Logic:**
```python
while True:
    current_ip = dns_lookup(DNS_RECORD)
    
    if current_ip == MY_IP:
        start_otel_collector()
    else:
        stop_otel_collector()
    
    sleep(CHECK_INTERVAL)
```

### 3. DNS Provider Abstraction

Pluggable DNS backend system. All providers implement:

```python
class DNSProvider:
    def set_records(self, ip: str, owner: str, exp_unix: int) -> None:
        """Create/update A and TXT records"""
        
    def get_records(self) -> Dict[str, Any]:
        """Query current A and TXT record values"""
```

---

## DNS Record Structure

**A Record:**
```
failover.example.com.  30  IN  A  192.168.100.144
```

**TXT Record (lease metadata):**
```
failover.example.com.  30  IN  TXT  "owner=primary exp=1769571600"
```

| Field | Description |
|-------|-------------|
| `owner` | Current lease holder: `primary` or `dr` |
| `exp` | Unix timestamp when lease expires |

**Lease Semantics:**
- Lease is valid if `exp > current_time`
- Owner must renew before expiration
- If lease expires, any node can claim ownership
- DR will only claim if health check also fails (prevents split-brain during network partition)

---

## Failover State Machine

```
                    ┌─────────────────┐
                    │     START       │
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
           ┌───────►│  PRIMARY ACTIVE │◄──────┐
           │        │                 │       │
           │        │ - Lease valid   │       │
           │        │ - Renewing      │       │
           │        └────────┬────────┘       │
           │                 │                │
           │     Primary fails                │
           │     (health + lease)             │
           │                 │                │
           │                 ▼                │
           │        ┌─────────────────┐       │
           │        │  DR TAKEOVER    │       │
           │        │                 │       │
           │        │ - Claims lease  │       │
           │        │ - Updates DNS   │       │
           │        └────────┬────────┘       │
           │                 │                │
           │                 ▼                │
           │        ┌─────────────────┐       │
           │        │   DR ACTIVE     │       │
           │        │                 │       │
           │        │ - Lease valid   │       │
           │        │ - Renewing      │       │
           │        └────────┬────────┘       │
           │                 │                │
           │        Manual failback           │
           │        command executed          │
           │                 │                │
           └─────────────────┘                │
                                              │
                    ┌─────────────────┐       │
                    │    FAILBACK     │───────┘
                    │                 │
                    │ - Primary       │
                    │   reclaims      │
                    └─────────────────┘
```

---

## Health Check Modes

### TCP Mode (Default)

Simple port connectivity check.

**Configuration:**
```bash
HEALTH_MODE=tcp
HEALTH_HOST=192.168.100.144
HEALTH_PORT=4317
HEALTH_TIMEOUT=2
```

**Logic:**
```python
def check_tcp(host, port, timeout):
    socket.create_connection((host, port), timeout)
    return True  # Port is open
```

**Catches:**
- Process crashed
- Port not listening
- Network unreachable

**Does NOT catch:**
- Hung process
- Process running but not processing data
- Upstream failures (BIG-IP unreachable)

### Metrics Mode (Recommended)

Verifies OTEL collector is actively receiving and processing data.

**Configuration:**
```bash
HEALTH_MODE=metrics
HEALTH_URL=http://192.168.100.144:8888/metrics
HEALTH_METRIC=otelcol_receiver_accepted_metric_points_total
HEALTH_STALE_COUNT=3
HEALTH_TIMEOUT=5
```

**Logic:**
```python
class MetricsHealthChecker:
    def check(self):
        metrics = http_get(HEALTH_URL)
        current_value = parse_metric(metrics, HEALTH_METRIC)
        
        if current_value > self.last_value:
            self.stale_count = 0
            return HEALTHY
        else:
            self.stale_count += 1
            if self.stale_count >= HEALTH_STALE_COUNT:
                return UNHEALTHY
            return HEALTHY  # Still within tolerance
```

**Catches:**
- Process crashed
- Process hung
- No data flowing
- BIG-IP unreachable
- Network issues to BIG-IP
- Collector misconfiguration

**Metric Selection:**

| Metric | What it indicates |
|--------|-------------------|
| `otelcol_receiver_accepted_metric_points_total` | Data received from scrapers |
| `otelcol_exporter_sent_metric_points_total` | Data exported to backends |
| `otelcol_processor_batch_batch_send_size_sum` | Data processed through pipeline |

---

## Timing Parameters

### Relationship Diagram

```
                    UPDATE_INTERVAL (e.g., 30s)
                    ◄──────────────────────────►
    
Check 1             Check 2             Check 3             Check 4
   │                   │                   │                   │
   ▼                   ▼                   ▼                   ▼
┌─────┐             ┌─────┐             ┌─────┐             ┌─────┐
│Fetch│             │Fetch│             │Fetch│             │Fetch│
│Metric             │Metric             │Metric             │Metric
│=100 │             │=100 │             │=100 │             │=100 │
└─────┘             └─────┘             └─────┘             └─────┘
   │                   │                   │                   │
Baseline            Stale 1/3          Stale 2/3          Stale 3/3
                                                           UNHEALTHY
                                                               │
                                                               ▼
                                                          Fail 1/2
                                                               │
                    ┌──────────────────────────────────────────┘
                    │              UPDATE_INTERVAL
                    ▼
                 Check 5
                    │
                    ▼
               ┌─────┐
               │Fetch│
               │Metric
               │=100 │
               └─────┘
                    │
                Fail 2/2
                    │
                    ▼
           ┌───────────────┐
           │ Check lease   │
           │ expiration    │
           └───────┬───────┘
                   │
         ┌─────────┴─────────┐
         │                   │
    Lease valid         Lease expired
         │                   │
         ▼                   ▼
    Wait & retry       FAILOVER!
```

### Parameter Reference

| Parameter | Default | Description | Recommendation |
|-----------|---------|-------------|----------------|
| `UPDATE_INTERVAL` | 10 | Seconds between health checks | 30-60 for production |
| `LEASE_TTL` | 60 | Seconds until lease expires if not renewed | 2-3x UPDATE_INTERVAL |
| `FAIL_THRESHOLD` | 3 | Consecutive failures before considering failover | 2-3 |
| `HEALTH_STALE_COUNT` | 3 | Flat metric readings before marking unhealthy | 2-3 |
| `HEALTH_TIMEOUT` | 2 | Seconds to wait for health check response | 2-5 |

### Failover Timing Calculation

```
Time to failover = (HEALTH_STALE_COUNT + FAIL_THRESHOLD - 1) × UPDATE_INTERVAL + LEASE_TTL

Example with defaults:
= (3 + 3 - 1) × 10 + 60
= 50 + 60
= 110 seconds worst case
```

**Aggressive settings (faster failover):**
```bash
UPDATE_INTERVAL=15
LEASE_TTL=45
FAIL_THRESHOLD=2
HEALTH_STALE_COUNT=2

Time = (2 + 2 - 1) × 15 + 45 = 45 + 45 = 90 seconds
```

**Conservative settings (fewer false positives):**
```bash
UPDATE_INTERVAL=60
LEASE_TTL=180
FAIL_THRESHOLD=3
HEALTH_STALE_COUNT=3

Time = (3 + 3 - 1) × 60 + 180 = 300 + 180 = 480 seconds (8 minutes)
```

---

## DNS Providers

### Supported Providers

| Provider | ID | Auth Method | Notes |
|----------|-----|-------------|-------|
| Dry Run | `dry-run` | None | Local JSON file, testing only |
| BIND | `bind-tsig` | TSIG key file | RFC2136 dynamic updates |
| Active Directory | `ad-gss` | Kerberos/GSS-TSIG | Requires krb5 config |
| Infoblox | `infoblox` | Username/password | WAPI REST API |
| Cloudflare | `cloudflare` | API token | Zone ID required |
| AWS Route53 | `route53` | Access key/secret | Hosted zone ID required |
| Azure DNS | `azure-dns` | Service principal | 5 credential fields |
| Google Cloud DNS | `gcp-dns` | Service account | Project + managed zone |
| F5 GTM | `f5-gtm` | Username/password | Uses data-group, not DNS |
| Custom Script | `script` | User-defined | Any DNS system |

### Provider Configuration

**Route53 Example:**
```bash
DNS_PROVIDER=route53
DNS_ZONE=example.com
DNS_RECORD=failover.example.com
DNS_TTL=30

AWS_ACCESS_KEY_ID=AKIAXXXXXXXX
AWS_SECRET_ACCESS_KEY=xxxxxxxx
AWS_REGION=us-east-1
ROUTE53_ZONE_ID=Z0123456789
```

**Cloudflare Example:**
```bash
DNS_PROVIDER=cloudflare
DNS_ZONE=example.com
DNS_RECORD=failover.example.com
DNS_TTL=30

CLOUDFLARE_API_TOKEN=xxxxxxxxxxxx
CLOUDFLARE_ZONE_ID=xxxxxxxxxxxx
```

### Script Provider

For unsupported DNS systems, implement two scripts:

**set_dns.sh** - Called to update DNS:
```bash
#!/bin/bash
# Arguments: $1=record $2=ip $3=owner $4=expiry $5=ttl $6=zone
RECORD="$1"
IP="$2"
OWNER="$3"
EXPIRY="$4"

# Call your DNS API here
curl -X PUT "https://your-dns/api/record/$RECORD" \
  -d '{"ip":"'$IP'","txt":"owner='$OWNER' exp='$EXPIRY'"}'

exit 0
```

**get_dns.sh** - Called to query DNS:
```bash
#!/bin/bash
# Arguments: $1=record $2=zone
# Must output JSON to stdout

echo '{"A": "10.10.10.10", "TXT": "owner=primary exp=1699567890"}'
exit 0
```

---

## OTEL Watcher Operation

### Docker Mode

**Container Control:**
```python
def start_container():
    subprocess.run(['docker', 'start', OTEL_CONTAINER])

def stop_container():
    subprocess.run(['docker', 'stop', '-t', '10', OTEL_CONTAINER])
```

**Requirements:**
- Docker socket mounted: `-v /var/run/docker.sock:/var/run/docker.sock`
- OTEL container must exist (created, can be stopped)
- Container name must match `OTEL_CONTAINER` env var

### Kubernetes Mode

**Deployment Scaling:**
```python
def scale_deployment(replicas):
    subprocess.run([
        'kubectl', 'scale', 'deployment', OTEL_DEPLOYMENT,
        f'--replicas={replicas}',
        '-n', OTEL_NAMESPACE
    ])
```

**Requirements:**
- kubectl available in container
- RBAC permissions to scale deployments
- Deployment must exist in specified namespace

### DNS Resolution

The watcher resolves DNS to determine active site:

```python
current_ip = socket.gethostbyname(DNS_RECORD)

if current_ip == MY_IP:
    # We are active
    ensure_collector_running()
else:
    # Other site is active
    ensure_collector_stopped()
```

**DNS Caching Consideration:**
- System resolver may cache DNS
- Use `DNS_SERVER` env var to query authoritative server directly
- Set low TTL (30s) on DNS records

---

## Configuration Reference

### Complete Environment Variables

**Core Settings:**
```bash
ROLE=primary|dr                    # Site role
DNS_PROVIDER=route53               # DNS provider ID
DNS_SERVER=10.10.1.53             # DNS server (for queries)
DNS_ZONE=example.com              # DNS zone
DNS_RECORD=failover.example.com   # FQDN to manage
DNS_TTL=30                        # Record TTL in seconds
```

**Failover Settings:**
```bash
PRIMARY_IP=192.168.100.144        # Primary site IP
DR_IP=192.168.100.143             # DR site IP
LEASE_TTL=60                      # Lease duration in seconds
UPDATE_INTERVAL=10                # Health check interval
FAIL_THRESHOLD=3                  # Failures before failover
```

**Health Check - TCP Mode:**
```bash
HEALTH_MODE=tcp                   # Health check mode
HEALTH_HOST=192.168.100.144       # Host to check
HEALTH_PORT=4317                  # Port to check
HEALTH_TIMEOUT=2                  # Connection timeout
```

**Health Check - Metrics Mode:**
```bash
HEALTH_MODE=metrics               # Health check mode
HEALTH_URL=http://192.168.100.144:8888/metrics
HEALTH_METRIC=otelcol_receiver_accepted_metric_points_total
HEALTH_STALE_COUNT=3              # Flat readings before unhealthy
HEALTH_TIMEOUT=5                  # HTTP timeout
```

**OTEL Watcher (Docker):**
```bash
DNS_RECORD=failover.example.com   # DNS record to watch
MY_IP=192.168.100.143             # This site's IP
OTEL_CONTAINER=otel-collector     # Container name to control
OTEL_CHECK_INTERVAL=15            # DNS check interval
DNS_SERVER=8.8.8.8                # Optional: specific DNS server
```

**OTEL Watcher (Kubernetes):**
```bash
DNS_RECORD=failover.example.com
MY_IP=192.168.100.143
OTEL_NAMESPACE=monitoring
OTEL_DEPLOYMENT=otel-collector
OTEL_REPLICAS_ACTIVE=1
OTEL_REPLICAS_INACTIVE=0
OTEL_CHECK_INTERVAL=15
```

---

## Deployment

### Primary Site

```bash
# 1. Configure
cat > .env.primary << 'EOF'
ROLE=primary
DNS_PROVIDER=route53
DNS_ZONE=example.com
DNS_RECORD=failover.example.com
DNS_TTL=30
PRIMARY_IP=192.168.100.144
DR_IP=192.168.100.143
LEASE_TTL=60
UPDATE_INTERVAL=30
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
ROUTE53_ZONE_ID=Z0123...
EOF

# 2. Build and run
docker build -t dns-failover .
docker run -d --name dns-failover \
  --restart unless-stopped \
  --env-file .env.primary \
  dns-failover

# 3. Initialize DNS (first time only)
docker exec dns-failover python3 /app/dns_failover.py init
```

### DR Site

```bash
# 1. Configure dns-failover
cat > .env.dr << 'EOF'
ROLE=dr
DNS_PROVIDER=route53
DNS_ZONE=example.com
DNS_RECORD=failover.example.com
DNS_TTL=30
PRIMARY_IP=192.168.100.144
DR_IP=192.168.100.143
LEASE_TTL=60
UPDATE_INTERVAL=30
FAIL_THRESHOLD=2
HEALTH_MODE=metrics
HEALTH_URL=http://192.168.100.144:8888/metrics
HEALTH_METRIC=otelcol_receiver_accepted_metric_points_total
HEALTH_STALE_COUNT=2
HEALTH_TIMEOUT=5
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
ROUTE53_ZONE_ID=Z0123...
EOF

# 2. Build and run dns-failover
docker build -t dns-failover .
docker run -d --name dns-failover \
  --restart unless-stopped \
  --env-file .env.dr \
  dns-failover

# 3. Build and run otel-watcher
docker build -t otel-watcher -f Dockerfile.otel-watcher-docker .
docker run -d --name otel-watcher \
  --restart unless-stopped \
  -e DNS_RECORD=failover.example.com \
  -e MY_IP=192.168.100.143 \
  -e OTEL_CONTAINER=otel-collector \
  -e DNS_SERVER=8.8.8.8 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  otel-watcher

# 4. Ensure OTEL collector exists but is stopped
docker stop otel-collector
```

---

## Operations

### View Current State

```bash
docker exec dns-failover python3 /app/dns_failover.py show
```

Output:
```json
{
  "record": "failover.example.com",
  "A": "192.168.100.144",
  "owner": "primary",
  "expires_at": "1769571600",
  "time_remaining": 45
}
```

### Manual Failover (Force DR Active)

```bash
# On DR site
docker exec dns-failover python3 /app/dns_failover.py promote
```

### Manual Failback (Restore Primary)

```bash
# On Primary site (after primary is stable)
docker exec dns-failover python3 /app/dns_failover.py failback
```

### Validate Configuration

```bash
docker exec dns-failover python3 /app/dns_failover.py validate
```

---

## Monitoring & Alerting

### Key Metrics to Monitor

**On Primary:**
- DNS lease renewal success rate
- OTEL collector `otelcol_receiver_accepted_metric_points_total` increasing

**On DR:**
- Health check results
- Failover events
- OTEL collector state (running/stopped)

### Log Analysis

**Healthy Primary:**
```
[INFO] Lease renewed, expires at 1769571600
[INFO] Lease renewed, expires at 1769571630
```

**Healthy DR (Standby):**
```
[INFO] Metrics healthy: otelcol_receiver_accepted_metric_points_total=12400 (+55)
[INFO] Primary healthy
```

**Failover in Progress:**
```
[WARN] Metrics stale: otelcol_receiver_accepted_metric_points_total=12400 (unchanged, 1/3)
[WARN] Metrics stale: otelcol_receiver_accepted_metric_points_total=12400 (unchanged, 2/3)
[WARN] Metrics stale: otelcol_receiver_accepted_metric_points_total=12400 (unchanged, 3/3)
[WARN] Primary health check failed (1/2)
[WARN] Primary health check failed (2/2)
[WARN] Waiting for primary lease to expire (45s remaining)
[WARN] Primary lease expired - initiating failover!
[INFO] FAILOVER: Promoted DR to active, A=192.168.100.143
```

### Recommended Alerts

| Alert | Condition | Severity |
|-------|-----------|----------|
| Failover occurred | Log contains "FAILOVER: Promoted" | Critical |
| Primary unhealthy | Log contains "health check failed" for >5min | Warning |
| Lease renewal failing | Log contains "Failed to renew lease" | Critical |
| Metrics endpoint down | HEALTH_URL not responding | Warning |
| OTEL data stopped | Metric not incrementing for >10min | Warning |

---

## Troubleshooting

### DNS Not Updating

**Symptoms:** `show` command returns stale data

**Checks:**
1. Verify credentials: `docker exec dns-failover python3 /app/dns_failover.py validate`
2. Check DNS provider logs in cloud console
3. Verify network connectivity to DNS API
4. Check IAM/permissions for DNS updates

### Health Check Always Fails

**Symptoms:** DR constantly shows "health check failed"

**Checks:**
1. TCP mode: `nc -zv <PRIMARY_IP> <HEALTH_PORT>`
2. Metrics mode: `curl http://<PRIMARY_IP>:8888/metrics`
3. Verify firewall rules between sites
4. Check OTEL collector is running and port is published

### OTEL Watcher Not Controlling Container

**Symptoms:** Watcher logs show state changes but container doesn't start/stop

**Checks:**
1. Verify docker socket mounted: `-v /var/run/docker.sock:/var/run/docker.sock`
2. Verify container name matches: `docker ps -a | grep <OTEL_CONTAINER>`
3. Check watcher has permissions: `docker exec otel-watcher docker ps`

### Split-Brain Prevention

**Scenario:** Both sites think they are active

**Built-in protections:**
1. DR only takes over if BOTH health fails AND lease expired
2. Lease mechanism ensures orderly handoff
3. Short DNS TTL ensures clients follow DNS quickly

**If split-brain occurs:**
1. Stop dns-failover on DR: `docker stop dns-failover`
2. Wait for lease to expire
3. On Primary: `docker exec dns-failover python3 /app/dns_failover.py init`
4. Restart DR: `docker start dns-failover`

---

## Security Considerations

### Credentials Management

| Method | Security Level | Use Case |
|--------|---------------|----------|
| Environment variables | Low | Development/testing |
| .env file | Low | Simple deployments |
| Docker secrets | Medium | Docker Swarm |
| Kubernetes secrets | Medium | Kubernetes |
| HashiCorp Vault | High | Enterprise production |

**Vault Integration:**
```bash
VAULT_ADDR=https://vault.example.com:8200
VAULT_AUTH_METHOD=approle
VAULT_ROLE_ID=xxx
VAULT_SECRET_ID=xxx
VAULT_MOUNT=secret
VAULT_KEY=dns-failover
```

### Network Security

- DNS API credentials should use least-privilege access
- Health check traffic should be on management network
- Docker socket access grants root-equivalent permissions
- Consider mTLS for cross-site communication

### DNS Security

- Use API tokens with minimal scope (DNS zone write only)
- Enable DNS audit logging
- Monitor for unauthorized DNS changes
- Consider DNSSEC for production

---

## Appendix: File Structure

```
dns-failover/
├── dns_failover.py              # Main failover daemon
├── otel_watcher_docker.py       # Docker container controller
├── otel_watcher_k8s.py          # Kubernetes deployment controller
├── Dockerfile                   # Main container image
├── Dockerfile.otel-watcher-docker
├── requirements.txt             # Python dependencies
├── setup.py                     # Interactive configuration wizard
├── .env.primary                 # Primary site config template
├── .env.dr                      # DR site config template
├── k8s/
│   ├── dns-failover.yaml        # Kubernetes manifests
│   └── otel-watcher.yaml
├── USER_GUIDE.md                # End-user documentation
├── SCRIPT_PROVIDER_GUIDE.md     # Custom DNS provider guide
└── TECHNICAL_DOCUMENTATION.md   # This document
```
