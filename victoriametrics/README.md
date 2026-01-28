# VM-Sync: VictoriaMetrics Replication for DR

Automated data synchronization between primary and DR VictoriaMetrics instances, designed to work with the AST DNS Failover system.

## Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              PRIMARY SITE                                    │
│                                                                              │
│  ┌──────────────────────┐      ┌──────────────────────┐                     │
│  │   VictoriaMetrics    │◄─────│   OTEL Collector     │◄──── BIG-IPs        │
│  │   :8428              │      │   (dual-write)       │                     │
│  └──────────┬───────────┘      └──────────────────────┘                     │
│             │                                                                │
│             │ vm-sync (monitors + repairs gaps)                             │
│             │                                                                │
└─────────────┼───────────────────────────────────────────────────────────────┘
              │
              │ Export/Import API (port 8428)
              │
┌─────────────┼───────────────────────────────────────────────────────────────┐
│             │                         DR SITE                                │
│             ▼                                                                │
│  ┌──────────────────────┐      ┌──────────────────────┐                     │
│  │   VictoriaMetrics    │◄─────│   OTEL Collector     │◄──── BIG-IPs        │
│  │   :8428              │      │   (dual-write)       │   (when active)     │
│  └──────────────────────┘      └──────────────────────┘                     │
│                                                                              │
│             vm-sync (monitors + repairs gaps)                               │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

## How It Works

1. **Dual-Write**: Active OTEL collector writes to BOTH VictoriaMetrics instances
2. **Gap Detection**: vm-sync queries both VMs every 2 minutes, compares sample counts
3. **Auto-Repair**: If gaps are found, data is exported from source and imported to destination
4. **Failback Signal**: After 3 consecutive clean checks, vm-sync emits "FAILBACK READY"

## Quick Start

### Primary Site

```bash
# 1. Extract and enter directory
tar -xzvf vm-sync.tar.gz
cd vm-sync

# 2. Run setup script
chmod +x setup.sh
./setup.sh primary

# 3. Edit .env with your values
vim .env

# 4. Start the stack
docker compose build
docker compose up -d
```

### DR Site

```bash
# Same steps, but use 'dr' role
./setup.sh dr
vim .env
docker compose build
docker compose up -d
```

## Configuration

### Required Settings (.env)

| Variable | Description | Example |
|----------|-------------|---------|
| `ROLE` | Site role | `primary` or `dr` |
| `LOCAL_VM_URL` | Local VictoriaMetrics | `http://victoriametrics:8428` |
| `REMOTE_VM_URL` | Remote site's VM | `http://192.168.100.143:8428` |
| `DNS_RECORD` | Failover DNS record | `failover.example.com` |
| `PRIMARY_IP` | Primary site IP | `192.168.100.144` |
| `DR_IP` | DR site IP | `192.168.100.143` |

### Optional Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `CHECK_INTERVAL` | `120` | Seconds between sync checks |
| `QUERY_WINDOW` | `3600` | Seconds to look back (1 hour) |
| `GAP_THRESHOLD` | `0.9` | Sync if dest has <90% of source |
| `FAILBACK_CLEAN_CHECKS` | `3` | Clean checks before failback ready |
| `NOTIFY_WEBHOOK` | (none) | Slack/Teams webhook URL |

### VictoriaMetrics Tuning

Key settings in `docker-compose.yml`:

| Flag | Default | Description |
|------|---------|-------------|
| `--retentionPeriod` | `90d` | How long to keep data |
| `--dedup.minScrapeInterval` | `60s` | Dedupe window for dual-write |
| `--memory.allowedPercent` | `60` | Max RAM percentage |
| `--search.maxQueryDuration` | `120s` | Query timeout |

## File Structure

```
/opt/vm-sync/
├── .env                    # Active configuration
├── .env.primary            # Primary site template
├── .env.dr                 # DR site template
├── docker-compose.yml      # Stack definition
├── Dockerfile.vm-sync      # Sync daemon container
├── vm_sync.py              # Sync daemon code
├── setup.sh                # Setup script
└── data/
    ├── victoriametrics/    # VM time-series storage
    └── vm-sync/            # Sync state file
```

## Operations

### View Logs

```bash
# All services
docker compose logs -f

# Just vm-sync
docker compose logs -f vm-sync

# Just VictoriaMetrics
docker compose logs -f victoriametrics
```

### Check Sync State

```bash
# View state file
cat /opt/vm-sync/data/vm-sync/vm-sync-state.json | jq .

# Example output:
{
  "last_check": "2024-01-15T10:00:00Z",
  "last_sync": null,
  "consecutive_clean": 5,
  "failback_ready": true,
  "active_site": "primary",
  "gaps_detected": 0,
  "samples_synced": 0,
  "last_error": null
}
```

### VictoriaMetrics UI

Access at `http://<host>:8428/vmui`

### Manual Queries

```bash
# Check sample count for last hour
curl -s 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=count(count_over_time(up[1h]))'

# Check VM health
curl -s 'http://localhost:8428/health'

# Check VM stats
curl -s 'http://localhost:8428/api/v1/status/tsdb'
```

### Force Sync Check

```bash
# Restart vm-sync to trigger immediate check
docker compose restart vm-sync
```

## Failover Scenarios

### Scenario 1: Primary OTEL fails, VMs stay up

- DR OTEL takes over (via dns-failover)
- Dual-write continues to both VMs
- **No gap** - both VMs have complete data
- vm-sync reports clean

### Scenario 2: Primary site fully down

- DR OTEL takes over
- Writes go to DR VM only (primary unreachable)
- **Gap created** on primary VM
- When primary recovers:
  - vm-sync detects gap
  - Syncs data from DR → Primary
  - Emits "FAILBACK READY" when complete

### Scenario 3: Network partition during failover

- Both OTELs might briefly write (split-brain)
- VictoriaMetrics dedupes overlapping samples
- vm-sync reconciles any differences
- **No data loss**

## Notifications

Configure `NOTIFY_WEBHOOK` for Slack/Teams alerts:

| Event | Message |
|-------|---------|
| Gap detected | ⚠️ VM-Sync: X data gaps detected |
| Sync complete | ✅ VM-Sync: Sync complete |
| Failback ready | 🟢 VM-Sync: FAILBACK READY |

## Troubleshooting

### vm-sync can't reach remote VM

```
[WARN] Remote VM unhealthy: http://192.168.100.143:8428
```

- Check network connectivity between sites
- Verify firewall allows port 8428
- Ensure remote VM is running

### Gaps not syncing

```
[ERROR] VM export error: ...
```

- Check disk space on source VM
- Verify export API is accessible
- Check for query timeouts (large time ranges)

### High memory usage on VM

Adjust in `docker-compose.yml`:
```yaml
command:
  - "--memory.allowedPercent=40"  # Lower from 60
```

## Integration with DNS Failover

vm-sync reads the same DNS record as your existing dns-failover setup:

```
DNS_RECORD=failover.example.com
```

When DNS points to primary IP → primary is source of truth
When DNS points to DR IP → DR is source of truth

This ensures sync direction is always correct after failover.
