# CH-Sync: ClickHouse Data Synchronization for DR

Automated data synchronization between primary and DR ClickHouse instances, designed to work with the AST DNS Failover system.

## Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              PRIMARY SITE                                    │
│                                                                              │
│  ┌──────────────────────┐      ┌──────────────────────┐                     │
│  │     ClickHouse       │◄─────│   OTEL Collector     │◄──── BIG-IPs        │
│  │   :8123 / :9000      │      │   (dual-write)       │                     │
│  └──────────┬───────────┘      └──────────────────────┘                     │
│             │                                                                │
│             │ ch-sync (monitors + repairs gaps)                             │
│             │                                                                │
└─────────────┼───────────────────────────────────────────────────────────────┘
              │
              │ remote() function over native port 9000
              │
┌─────────────┼───────────────────────────────────────────────────────────────┐
│             │                         DR SITE                                │
│             ▼                                                                │
│  ┌──────────────────────┐      ┌──────────────────────┐                     │
│  │     ClickHouse       │◄─────│   OTEL Collector     │◄──── BIG-IPs        │
│  │   :8123 / :9000      │      │   (dual-write)       │   (when active)     │
│  └──────────────────────┘      └──────────────────────┘                     │
│                                                                              │
│             ch-sync (monitors + repairs gaps)                               │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

## How It Works

1. **Table Discovery**: Scans `system.tables` on both nodes, excludes system/temp tables
2. **New Table Detection**: Alerts if active site has tables that don't exist locally
3. **Partition Comparison**: Compares row counts per partition via `system.parts`
4. **Gap Sync**: Uses `INSERT...SELECT...remote()` to copy missing partitions
5. **Failback Signal**: After 3 consecutive clean checks, emits "FAILBACK READY"

## Prerequisites

**All tables must be partitioned by day:**
```sql
CREATE TABLE telemetry (
    timestamp DateTime,
    ...
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (timestamp, ...);
```

## Quick Start

### Primary Site

```bash
# 1. Extract and enter directory
tar -xzvf ch-sync.tar.gz
cd ch-sync

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
| `LOCAL_CH_URL` | Local ClickHouse HTTP | `http://localhost:8123` |
| `REMOTE_CH_URL` | Remote ClickHouse HTTP | `http://192.168.100.143:8123` |
| `REMOTE_CH_HOST` | Remote native host | `192.168.100.143` |
| `REMOTE_CH_PORT` | Remote native port | `9000` |
| `LOCAL_CH_USER` | Local CH username | `default` |
| `LOCAL_CH_PASSWORD` | Local CH password | (empty or password) |
| `REMOTE_CH_USER` | Remote CH username | `default` |
| `REMOTE_CH_PASSWORD` | Remote CH password | (empty or password) |
| `DNS_RECORD` | Failover DNS record | `failover.example.com` |
| `PRIMARY_IP` | Primary site IP | `192.168.100.144` |
| `DR_IP` | DR site IP | `192.168.100.143` |

### Optional Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `CHECK_INTERVAL` | `300` | Seconds between sync checks |
| `CH_EXCLUDE_PATTERNS` | `system.*,...` | Tables to exclude (comma-separated globs) |
| `CONNECT_TIMEOUT_MS` | `2000` | Timeout for remote() connections |
| `MAX_INSERT_THREADS` | `4` | Parallel insert threads |
| `FAILBACK_CLEAN_CHECKS` | `3` | Clean checks before failback ready |
| `NOTIFY_WEBHOOK` | (none) | Slack/Teams webhook URL |

## File Structure

```
/opt/ch-sync/
├── .env                    # Active configuration
├── .env.primary            # Primary site template
├── .env.dr                 # DR site template
├── docker-compose.yml      # Stack definition
├── Dockerfile.ch-sync      # Sync daemon container
├── ch_sync.py              # Sync daemon code
├── setup.sh                # Setup script
└── data/
    └── ch-sync-state.json  # Sync state file
```

## Operations

### View Logs

```bash
# Follow logs
docker compose logs -f ch-sync

# You should see:
# [INFO] CH-Sync - ClickHouse Data Synchronization
# [INFO] Active site: primary (we are: dr)
# [INFO] Checking table: default.http_telemetry
# [INFO] All tables in sync (clean check #1)
```

### Check Sync State

```bash
cat /opt/ch-sync/data/ch-sync-state.json | jq .

# Example output:
{
  "last_check": "2024-01-15T10:00:00Z",
  "last_sync": "2024-01-15T09:30:00Z",
  "consecutive_clean": 3,
  "failback_ready": true,
  "active_site": "dr",
  "tables_checked": 5,
  "tables_with_gaps": 0,
  "partitions_synced": 12,
  "rows_synced": 1500000,
  "new_tables_detected": [],
  "last_error": null
}
```

### Manual Sync Query

If you need to manually sync a partition:

```sql
-- On the passive node, pull from active
INSERT INTO database.table
SELECT * FROM remote(
    'active_host:9000',
    'database.table',
    'user',
    'password'
)
WHERE _partition_id = '20240115'
SETTINGS
    connect_timeout_with_failover_ms = 2000,
    max_insert_threads = 4;
```

### Check Partition Counts

```sql
-- Compare partitions between nodes
SELECT 
    partition,
    sum(rows) as row_count
FROM system.parts 
WHERE database = 'default' 
  AND table = 'telemetry'
  AND active = 1
GROUP BY partition
ORDER BY partition DESC
LIMIT 10;
```

## Failover Scenarios

### Scenario 1: Normal Operation (Dual-Write)

- Both OTEL collectors write to both ClickHouse instances
- ch-sync finds no gaps
- Both nodes have identical data

### Scenario 2: DR Site Down, Comes Back Up

1. DR ClickHouse was offline for 2 hours
2. DR comes back online
3. ch-sync detects gaps (missing partitions)
4. Syncs partitions from primary → DR
5. Emits "FAILBACK READY" when complete

### Scenario 3: Primary Site Down, Failover to DR

1. Primary goes down
2. DNS failover points to DR
3. DR OTEL becomes active, writes to DR ClickHouse only
4. Primary comes back
5. ch-sync on primary detects it's passive with gaps
6. Syncs partitions from DR → Primary
7. Emits "FAILBACK READY" when safe to switch back

### Scenario 4: New Table Created During Failover

1. Active site creates new table
2. ch-sync detects table exists on active but not passive
3. Logs WARNING and sends notification
4. **Manual action required**: CREATE TABLE on passive site
5. After table exists, ch-sync will sync data

## Notifications

Configure `NOTIFY_WEBHOOK` for Slack/Teams alerts:

| Event | Message |
|-------|---------|
| Gap detected | ⚠️ CH-Sync: X tables have data gaps |
| Sync complete | ✅ CH-Sync: Sync complete. X partitions synced |
| Failback ready | 🟢 CH-Sync: FAILBACK READY. All tables synced |
| New table | 🆕 CH-Sync: New tables detected. Manual creation required |

## Troubleshooting

### "Remote ClickHouse unhealthy"

```
[WARN] Remote ClickHouse unhealthy: http://192.168.100.143:8123
```

- Check network connectivity between sites
- Verify ClickHouse is running on remote
- Check firewall allows ports 8123 and 9000

### "Could not determine active site from DNS"

```
[WARN] DNS returned unexpected IP: None
```

- DNS record not resolving
- Check DNS_RECORD and DNS_SERVER settings
- Ensure dns-failover has initialized the record

### Sync taking too long

For large partitions (billions of rows):
- Increase `CHECK_INTERVAL` to avoid overlapping syncs
- Increase `MAX_INSERT_THREADS` for faster writes
- Consider syncing during off-peak hours

### Connection timeout on remote()

```
[ERROR] ClickHouse execute error: Timeout: connect timed out
```

- Increase `CONNECT_TIMEOUT_MS` (default 2000ms)
- Check network latency between sites
- For >300ms latency, use 5000ms or higher

## Integration with VM-Sync

Run both ch-sync and vm-sync on each site:

```
/opt/vm-sync/     # VictoriaMetrics sync
/opt/ch-sync/     # ClickHouse sync
```

Both use the same DNS record to determine active site. When both report "FAILBACK READY", it's safe to failback.

## Partition Best Practices

```sql
-- Recommended: Daily partitions for telemetry
PARTITION BY toYYYYMMDD(timestamp)

-- Alternative: Monthly for slower-moving data
PARTITION BY toYYYYMM(timestamp)

-- Avoid: No partition key (makes sync very slow)
-- Avoid: Too granular (hourly creates too many partitions)
```
