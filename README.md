# AST Full Stack with DR Failover

Complete Application Study Tool deployment with active/passive failover.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         PRIMARY (192.168.100.144)                           │
│                                                                             │
│  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐     │
│  │VictoriaMetrics│ │  ClickHouse  │   │   Grafana   │   │dns-failover │     │
│  │   :8428     │   │ :8123/:9000 │   │   :3000     │   │             │     │
│  └──────▲──────┘   └──────▲──────┘   └─────────────┘   └─────────────┘     │
│         │                 │                                                 │
│         │     ┌───────────┴───────────┐        ┌─────────────┐             │
│         └─────┤   OTEL Collector      │◄───────│otel-watcher │             │
│               │       :8888           │        └─────────────┘             │
│               └───────────┬───────────┘                                     │
│                           │                    ┌─────────────┐             │
│                           │                    │   vm-sync   │◄─── Pulls   │
│                           │                    │ (from DR)   │     from DR │
│                           │                    └─────────────┘             │
└───────────────────────────┼─────────────────────────────────────────────────┘
                            │ Scrapes BIG-IPs
                            │ Pushes metrics to BOTH sites
                            ▼
┌───────────────────────────┼─────────────────────────────────────────────────┐
│                           │                                                 │
│                         DR (192.168.100.143)                                │
│                                                                             │
│  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐     │
│  │VictoriaMetrics│ │  ClickHouse  │   │   Grafana   │   │dns-failover │     │
│  │   :8428     │   │ :8123/:9000 │   │   :3000     │   │             │     │
│  └──────▲──────┘   └──────▲──────┘   └─────────────┘   └─────────────┘     │
│         │                 │                                                 │
│         │     ┌───────────┴───────────┐        ┌─────────────┐             │
│         └─────┤   OTEL Collector      │◄───────│otel-watcher │             │
│               │   (STANDBY)           │        └─────────────┘             │
│               └───────────────────────┘                                     │
│                                                                             │
│  ┌─────────────┐   ┌─────────────┐                                         │
│  │   ch-sync   │   │   vm-sync   │◄─── Pulls from Primary                  │
│  │ (from Pri)  │   │ (from Pri)  │     (Primary runs the same, from DR)    │
│  └─────────────┘   └─────────────┘                                         │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Data Replication

| Data | Method | Direction |
|------|--------|-----------|
| VictoriaMetrics | OTEL dual-write | Real-time from the active site to both (`PEER_VM_URL`) |
| VictoriaMetrics | vm-sync | Bidirectional (fills gaps, last 30 min) |
| ClickHouse | ch-sync | Bidirectional, append-only gap fill (every 5 min, last 72 h) |

ch-sync never deletes. Each run compares per-hour row count + content hash
on both sides and inserts only rows missing from the local site, so after a
failback both sites converge on the union of what each collected.

## Directory Structure

```
ast-full-stack/
├── docker-compose.primary.yaml
├── docker-compose.dr.yaml
├── failover/
│   ├── .env.primary.example   # copy to .env.primary (gitignored)
│   ├── .env.dr.example        # copy to .env.dr (gitignored)
│   ├── secrets/               # tsig.key goes here (gitignored, mounted at /secrets)
│   ├── dns_failover.py
│   ├── otel_watcher_docker.py
│   ├── Dockerfile
│   └── Dockerfile.otel-watcher-docker
├── ch-sync/
│   ├── ch_sync.py
│   └── Dockerfile
├── vm-sync/
│   ├── vm_sync.py
│   └── Dockerfile
└── services/
    ├── clickhouse/
    ├── grafana/
    └── otel_collector/
```

## Deployment

### 1. Extract on both sites

```bash
mkdir -p ~/fullstack && cd ~/fullstack
tar -xvf ast-full-stack.tar
```

### 2. Copy your existing AST configs

```bash
cp ~/application-study-tool/.env .
cp ~/application-study-tool/.env.device-secrets .
cp ~/application-study-tool/services/otel_collector/receivers.yaml services/otel_collector/
```

### 3. Create failover .env files

```bash
cp failover/.env.primary.example failover/.env.primary
cp failover/.env.dr.example failover/.env.dr
```

Edit both with your DNS provider settings, `DNS_RECORD`, `PRIMARY_IP` and
`DR_IP`. otel-watcher reads the same file, so these must be the record and
site IPs the watcher compares DNS against.

For `bind-tsig`, put the key at `failover/secrets/tsig.key` (readable by
UID 1000, the container's `appuser`).

### 4. Start Primary

```bash
docker compose -f docker-compose.primary.yaml build
docker compose -f docker-compose.primary.yaml up -d
docker exec dns-failover python3 /app/dns_failover.py init
```

### 5. Start DR

```bash
docker compose -f docker-compose.dr.yaml build
docker compose -f docker-compose.dr.yaml up -d
```

## Operations

### Check Status
```bash
docker exec dns-failover python3 /app/dns_failover.py show
```

### Manual Failback
```bash
docker exec dns-failover python3 /app/dns_failover.py failback
```

DR keeps the lease after a failover until this is run. If DR goes down while
holding it, primary reclaims the lease once it expires.

### Manual ClickHouse catch-up
```bash
docker exec ch-sync python3 /app/ch_sync.py --once
# wider window for a long outage:
docker exec -e SYNC_LOOKBACK_HOURS=168 ch-sync python3 /app/ch_sync.py --once
```

### View Logs
```bash
docker logs -f dns-failover
docker logs -f otel-watcher
docker logs -f vm-sync
docker logs -f ch-sync
```

## Ports

| Service | Port | Purpose |
|---------|------|---------|
| VictoriaMetrics | 8428 | Metrics storage/query |
| ClickHouse | 8123 | HTTP interface |
| ClickHouse | 9000 | Native protocol |
| Grafana | 3000 | Dashboards |
| OTEL Collector | 8888 | Health metrics |

## Sync Configuration

### vm-sync (both sites)
| Env Var | Primary | DR |
|---------|---------|-----|
| SOURCE_URL | http://192.168.100.143:8428 | http://192.168.100.144:8428 |
| DEST_URL | http://victoriametrics:8428 | http://victoriametrics:8428 |
| SYNC_INTERVAL | 300 | 300 |
| SYNC_LOOKBACK | 30 | 30 |

### ch-sync (both sites)
| Env Var | Primary | DR |
|---------|---------|-----|
| SOURCE_HOST | 192.168.100.143 | 192.168.100.144 |
| SOURCE_PORT | 8123 | 8123 |
| SOURCE_NATIVE_PORT | 9000 | 9000 |
| DEST_HOST | clickhouse | clickhouse |
| SYNC_INTERVAL | 300 | 300 |
| SYNC_LOOKBACK_HOURS | 72 | 72 |
| SYNC_DATABASES | ast | ast |

Performance: every run hashes all rows in the lookback window on both sides.
Measured ~0.8 s for 10M rows / 72 h on 2 vCPUs (ClickHouse 24.8). Lower
`SYNC_LOOKBACK_HOURS` or raise `SYNC_INTERVAL` if your volume is much larger.

### OTEL collector
| Env Var | Primary | DR |
|---------|---------|-----|
| PEER_VM_URL | http://192.168.100.143:8428/api/v1/write | http://192.168.100.144:8428/api/v1/write |

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The ch-sync integration tests start two real ClickHouse servers and are
skipped unless a `clickhouse` binary is on `PATH` or `CLICKHOUSE_BIN` is set.
CI downloads one automatically.

## Grafana Access

- Primary: http://192.168.100.144:3000
- DR: http://192.168.100.143:3000
- Default: admin/admin
