# ClickHouse Test Environment

Standalone ClickHouse server for testing ch-sync replication.

## Quick Start

```bash
# Make scripts executable
chmod +x setup.sh test.sh

# Run setup
./setup.sh

# Create test data
./test.sh
```

## Files

| File | Purpose |
|------|---------|
| `docker-compose.yml` | ClickHouse container definition |
| `clickhouse-config/cluster.xml` | Cluster/replica config (update IPs!) |
| `clickhouse-config/network.xml` | Network settings |
| `clickhouse-users/users.xml` | User permissions and timeouts |
| `setup.sh` | Creates directories, starts ClickHouse |
| `test.sh` | Creates test table and sample data |

## Configuration

**Update `clickhouse-config/cluster.xml` with your IPs:**

```xml
<replica>
    <host>192.168.100.144</host>  <!-- Primary IP -->
    <port>9000</port>
</replica>
<replica>
    <host>192.168.100.143</host>  <!-- DR IP -->
    <port>9000</port>
</replica>
```

## Verify

```bash
# Health check
curl http://localhost:8123/ping

# Query
curl "http://localhost:8123" -d "SELECT count() FROM test_telemetry"

# Test remote connection (from primary to DR)
curl "http://localhost:8123" -d "SELECT * FROM remote('192.168.100.143:9000', 'system', 'one')"
```

## Ports

| Port | Purpose |
|------|---------|
| 8123 | HTTP interface (queries) |
| 9000 | Native protocol (remote() function) |
| 9009 | Interserver communication |
