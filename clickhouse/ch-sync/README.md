# CH-Sync: ClickHouse Data Synchronization for DR

Automated data synchronization between primary and DR ClickHouse instances.

## Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         PRIMARY SITE                             │
│  ┌──────────────────┐                                           │
│  │   ClickHouse     │◄──── Data writes                          │
│  │  :8123 / :9000   │                                           │
│  └────────┬─────────┘                                           │
│           │ ch-sync monitors                                    │
└───────────┼─────────────────────────────────────────────────────┘
            │ remote() sync via port 9000
┌───────────┼─────────────────────────────────────────────────────┐
│           ▼                       DR SITE                        │
│  ┌──────────────────┐                                           │
│  │   ClickHouse     │                                           │
│  │  :8123 / :9000   │                                           │
│  └──────────────────┘                                           │
│           ch-sync monitors + syncs gaps                         │
└─────────────────────────────────────────────────────────────────┘
```

## How It Works

1. **DNS Check**: Reads DNS record to determine active site
2. **Table Discovery**: Scans both ClickHouse instances for MergeTree tables
3. **Auto-Create Tables**: Creates missing tables on passive site (if enabled)
4. **Partition Comparison**: Compares row counts per partition via `system.parts`
5. **Gap Sync**: Uses DROP + INSERT to copy missing/mismatched partitions
6. **Failback Signal**: After 3 clean checks, emits "FAILBACK READY"

### Sync Method: DROP + Re-copy

When a partition mismatch is detected, ch-sync:
1. **DROPs** the partition locally (avoids duplicates)
2. **INSERTs** from remote using `remote()` function

This ensures exact data parity - no duplicate rows, no partial syncs.

## Prerequisites

- ClickHouse running on both sites (ports 8123 and 9000 accessible)
- Tables partitioned by day: `PARTITION BY toYYYYMMDD(timestamp)`
- DNS record pointing to active site IP

## Quick Start

### Primary Site

```bash
chmod +x setup.sh
./setup.sh primary
vim .env  # Update IPs and DNS record
docker compose build --no-cache
docker compose up -d
docker compose logs -f ch-sync
```

### DR Site

```bash
chmod +x setup.sh
./setup.sh dr
vim .env  # Update IPs and DNS record
docker compose build --no-cache
docker compose up -d
docker compose logs -f ch-sync
```

## Files

| File | Purpose |
|------|---------|
| `docker-compose.yml` | ch-sync container (uses host network) |
| `Dockerfile.ch-sync` | Container build |
| `ch_sync.py` | Sync daemon (~500 lines) |
| `.env.primary` | Primary site config template |
| `.env.dr` | DR site config template |
| `setup.sh` | Creates directories, copies env |

## Configuration (.env)

| Variable | Description |
|----------|-------------|
| `ROLE` | `primary` or `dr` |
| `LOCAL_CH_URL` | Local ClickHouse HTTP URL |
| `REMOTE_CH_URL` | Remote ClickHouse HTTP URL |
| `REMOTE_CH_HOST` | Remote ClickHouse IP for remote() |
| `REMOTE_CH_PORT` | Remote native port (usually 9000) |
| `DNS_RECORD` | Failover DNS record |
| `PRIMARY_IP` | Primary site IP |
| `DR_IP` | DR site IP |
| `CHECK_INTERVAL` | Seconds between checks (default 60) |
| `AUTO_CREATE_TABLES` | Auto-create missing tables (default false) |

## Testing DNS Without Real DNS

Add to `/etc/hosts` on both sites:

```bash
# Point to primary (primary is active)
echo "192.168.100.144 failover.mpwlabs.com" >> /etc/hosts
```

## Logs

```bash
# Follow logs
docker compose logs -f ch-sync

# Expected output (passive site):
# [INFO] Active site: primary (we are: dr)
# [INFO] Discovering tables...
# [INFO] Found 1 local tables, 1 remote tables
# [INFO] All tables in sync (clean check #1)
```

## Auto-Create Tables

When `AUTO_CREATE_TABLES=true`, ch-sync will automatically create tables that exist on the active site but not on the passive site.

```bash
# With auto-create enabled, you'll see:
# [INFO] Auto-creating table: default.new_telemetry
# [INFO] Successfully created table: default.new_telemetry
# [INFO] Auto-created 1 tables: default.new_telemetry
```

This copies the exact DDL from the active site, including:
- Engine type and settings
- Partition key
- Order by / Primary key
- TTL settings
- Any other table options

**Note**: The database will also be created if it doesn't exist.

## Test Sync

```bash
# On DR - delete a partition
curl "http://localhost:8123" -d "ALTER TABLE test_telemetry DROP PARTITION 20240117"

# Restart ch-sync to trigger check
docker compose restart ch-sync
docker compose logs -f ch-sync

# Should see:
# [INFO] Table default.test_telemetry: 1 partitions need sync
# [INFO] Partition 20240117: synced successfully (1440 rows total)
```

## State File

```bash
cat /opt/ch-sync/data/ch-sync-state.json
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Local ClickHouse unhealthy" | Check ClickHouse is running on port 8123 |
| "Could not determine active site" | Check DNS record or /etc/hosts |
| "Permission denied: /state/" | Run `chmod 777 /opt/ch-sync/data` |
| Tables not found | Ensure tables use MergeTree engine |
