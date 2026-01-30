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
│  │ (from Pri)  │   │ (from Pri)  │                                         │
│  └─────────────┘   └─────────────┘                                         │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Data Replication

| Data | Method | Direction |
|------|--------|-----------|
| VictoriaMetrics | OTEL dual-write | Real-time to both |
| VictoriaMetrics | vm-sync | Bidirectional (fills gaps) |
| ClickHouse | ch-sync | Primary → DR (every 5 min) |

## Directory Structure

```
ast-full-stack/
├── docker-compose.primary.yaml
├── docker-compose.dr.yaml
├── failover/
│   ├── .env.primary
│   ├── .env.dr
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

### 3. Update failover .env files

Edit `failover/.env.primary` and `failover/.env.dr` with your Route53 credentials.

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

### View Logs
```bash
docker logs -f dns-failover
docker logs -f otel-watcher
docker logs -f vm-sync
docker logs -f ch-sync  # DR only
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

### ch-sync (DR only)
| Env Var | Value |
|---------|-------|
| SOURCE_HOST | 192.168.100.144 |
| SOURCE_PORT | 8123 |
| SOURCE_NATIVE_PORT | 9000 |
| SYNC_INTERVAL | 300 |
| SYNC_DATABASES | ast |

## Grafana Access

- Primary: http://192.168.100.144:3000
- DR: http://192.168.100.143:3000
- Default: admin/admin
