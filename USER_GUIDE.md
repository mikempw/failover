# DNS Failover + OTEL Collector - User Flow

## Overview

You have TWO sites. Only ONE should collect telemetry at a time.

```
┌─────────────────────────────────┐       ┌─────────────────────────────────┐
│         PRIMARY SITE            │       │           DR SITE               │
│         (Datacenter 1)          │       │         (Datacenter 2)          │
├─────────────────────────────────┤       ├─────────────────────────────────┤
│                                 │       │                                 │
│  ┌─────────────────────────┐    │       │  ┌─────────────────────────┐    │
│  │   OTEL Collector        │    │       │  │   OTEL Collector        │    │
│  │   (ALWAYS RUNNING)      │    │       │  │   (CONTROLLED BY        │    │
│  │                         │    │       │  │    otel_watcher.py)     │    │
│  └───────────┬─────────────┘    │       │  └───────────┬─────────────┘    │
│              │                  │       │              │                  │
│              ▼                  │       │              ▼                  │
│  ┌─────────────────────────┐    │       │  ┌─────────────────────────┐    │
│  │   VictoriaMetrics       │◄───────────────►   VictoriaMetrics      │    │
│  │   (replicates to DR)    │    │       │  │   (replica)             │    │
│  └─────────────────────────┘    │       │  └─────────────────────────┘    │
│                                 │       │                                 │
│  ┌─────────────────────────┐    │       │  ┌─────────────────────────┐    │
│  │   ClickHouse            │◄───────────────►   ClickHouse           │    │
│  │   (distributed)         │    │       │  │   (distributed)         │    │
│  └─────────────────────────┘    │       │  └─────────────────────────┘    │
│                                 │       │                                 │
│  ┌─────────────────────────┐    │       │  ┌─────────────────────────┐    │
│  │   dns_failover.py       │    │       │  │   dns_failover.py       │    │
│  │   ROLE=primary          │    │       │  │   ROLE=dr               │    │
│  │   (renews DNS lease)    │    │       │  │   (monitors primary)    │    │
│  └─────────────────────────┘    │       │  └─────────────────────────┘    │
│                                 │       │                                 │
│                                 │       │  ┌─────────────────────────┐    │
│                                 │       │  │   otel_watcher.py       │    │
│                                 │       │  │   (starts/stops OTEL    │    │
│                                 │       │  │    based on DNS)        │    │
│                                 │       │  └─────────────────────────┘    │
└─────────────────────────────────┘       └─────────────────────────────────┘
            │                                           │
            └──────────────┬────────────────────────────┘
                           │
                    ┌──────▼──────┐
                    │  BIG-IPs    │  (scraped by whichever
                    │             │   OTEL collector is active)
                    └─────────────┘
```

---

## What Each Component Does

| Component | Where | What it does |
|-----------|-------|--------------|
| `dns_failover.py` (primary) | Primary | Renews DNS lease every 10s |
| `dns_failover.py` (dr) | DR | Monitors primary health, takes over DNS if primary dies |
| `otel_watcher.py` | DR only | Watches DNS, starts OTEL when DR is active |
| OTEL Collector | Primary (always on), DR (controlled) | Scrapes BIG-IP telemetry |
| VictoriaMetrics | Both | Stores metrics, replicates between sites |
| ClickHouse | Both | Stores telemetry, distributed across sites |

---

## Normal Operation (Primary Active)

```
DNS Record: syslog.example.com → 10.10.10.10 (PRIMARY)

PRIMARY:
  - dns_failover.py: Renewing lease... ✓
  - OTEL Collector: Running, scraping BIG-IPs ✓

DR:
  - dns_failover.py: Primary healthy ✓
  - otel_watcher.py: DNS points to 10.10.10.10, not me (10.20.20.20) - IDLE
  - OTEL Collector: STOPPED
```

---

## Failover (Primary Dies)

```
1. Primary server crashes
   
2. DR dns_failover.py detects:
   "Primary health check failed (1/3)"
   "Primary health check failed (2/3)"
   "Primary health check failed (3/3)"
   "Primary lease expired - initiating failover!"
   
3. DNS updates:
   syslog.example.com → 10.20.20.20 (DR)
   
4. DR otel_watcher.py detects:
   "DNS points to us (10.20.20.20) - ACTIVATING"
   "Starting OTEL collector..."
   
5. New state:
   PRIMARY: Dead
   DR OTEL: Running, scraping BIG-IPs ✓
```

---

## Failback (Primary Recovers)

```
1. Primary server comes back online
   
2. Admin runs on Primary:
   docker exec dns-failover python3 /app/dns_failover.py failback
   
3. DNS updates:
   syslog.example.com → 10.10.10.10 (PRIMARY)
   
4. DR otel_watcher.py detects:
   "DNS points elsewhere (10.10.10.10) - DEACTIVATING"
   "Stopping OTEL collector..."
   
5. New state:
   PRIMARY: Running, scraping BIG-IPs ✓
   DR OTEL: STOPPED
```

---

## Setup Steps

### Step 1: Run Setup Wizard (on any machine)

```bash
tar -xvf dns-failover.tar
pip install requests
python setup.py
```

This creates `.env.primary` and `.env.dr`

---

### Step 2: Deploy to PRIMARY Site

Copy these files to primary server:
```
dns_failover.py
Dockerfile
requirements.txt
.env.primary
```

Run:
```bash
# Build and start DNS failover
docker build -t dns-failover .
docker run -d --name dns-failover \
  --restart unless-stopped \
  --env-file .env.primary \
  dns-failover

# Initialize DNS (one time only)
docker exec dns-failover python3 /app/dns_failover.py init

# Your OTEL collector runs normally (always on)
# No changes needed to OTEL on primary
```

---

### Step 3: Deploy to DR Site

Copy these files to DR server:
```
dns_failover.py
otel_watcher.py
Dockerfile
requirements.txt
.env.dr
```

Create `.env.otel-watcher`:
```bash
# DNS record to watch (same as failover)
DNS_RECORD=syslog.example.com

# This site's IP (DR)
MY_IP=10.20.20.20

# Optional: specific DNS server
DNS_SERVER=10.10.1.53

# Check interval
OTEL_CHECK_INTERVAL=15

# Command to start OTEL collector
OTEL_COMMAND=otelcol-contrib --config /etc/otel/config.yaml
```

Run:
```bash
# Build and start DNS failover
docker build -t dns-failover .
docker run -d --name dns-failover \
  --restart unless-stopped \
  --env-file .env.dr \
  dns-failover

# Start OTEL watcher (controls your OTEL collector)
docker run -d --name otel-watcher \
  --restart unless-stopped \
  --env-file .env.otel-watcher \
  -v /etc/otel:/etc/otel:ro \
  -v /var/run/docker.sock:/var/run/docker.sock \
  python:3.11-slim \
  python3 /app/otel_watcher.py
```

---

## Alternative: Docker Compose for DR Site

```yaml
version: '3.8'

services:
  dns-failover:
    build: .
    container_name: dns-failover
    restart: unless-stopped
    env_file: .env.dr

  otel-watcher:
    image: python:3.11-slim
    container_name: otel-watcher
    restart: unless-stopped
    volumes:
      - ./otel_watcher.py:/app/otel_watcher.py:ro
      - /etc/otel:/etc/otel:ro
    environment:
      - DNS_RECORD=syslog.example.com
      - MY_IP=10.20.20.20
      - DNS_SERVER=10.10.1.53
      - OTEL_CHECK_INTERVAL=15
      - OTEL_COMMAND=otelcol-contrib --config /etc/otel/config.yaml
    command: python3 /app/otel_watcher.py
    depends_on:
      - dns-failover
```

---

## Verification

### Check DNS state:
```bash
# From anywhere
dig syslog.example.com A +short
# Returns: 10.10.10.10 (primary) or 10.20.20.20 (DR)
```

### Check failover status:
```bash
# On either site
docker exec dns-failover python3 /app/dns_failover.py show
```

### Check OTEL watcher logs (DR):
```bash
docker logs -f otel-watcher
```

---

## Summary

| Site | Runs | Purpose |
|------|------|---------|
| Primary | `dns_failover.py` (ROLE=primary) | Keep DNS pointed here |
| Primary | OTEL Collector (always) | Collect telemetry |
| DR | `dns_failover.py` (ROLE=dr) | Take over DNS if primary dies |
| DR | `otel_watcher.py` | Start OTEL only when DR is active |
| DR | OTEL Collector (controlled) | Collect telemetry only during failover |
