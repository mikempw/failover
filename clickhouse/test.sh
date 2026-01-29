#!/bin/bash
# =============================================================================
# ClickHouse Test Script
# =============================================================================
# Creates test table and sample data for ch-sync testing
#
# Usage: ./test.sh
# =============================================================================

set -e

CH="docker exec -i clickhouse clickhouse-client"

echo "============================================"
echo "ClickHouse Test Script"
echo "============================================"

# -----------------------------------------------------------------------------
# Create test table with daily partitioning
# -----------------------------------------------------------------------------
echo ""
echo "[1/3] Creating test table..."

$CH << 'EOF'
CREATE TABLE IF NOT EXISTS default.test_telemetry (
    timestamp DateTime,
    device String,
    metric String,
    value Float64
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (device, timestamp)
EOF

echo "  ✓ Table created: default.test_telemetry"

# -----------------------------------------------------------------------------
# Insert test data for multiple days
# -----------------------------------------------------------------------------
echo ""
echo "[2/3] Inserting test data..."

$CH << 'EOF'
INSERT INTO default.test_telemetry
SELECT
    toDateTime('2024-01-15 00:00:00') + number * 60 AS timestamp,
    concat('device_', toString(number % 10)) AS device,
    arrayElement(['cpu', 'memory', 'disk', 'network'], (number % 4) + 1) AS metric,
    rand() / 1000000000.0 AS value
FROM numbers(10000)
EOF

echo "  ✓ Inserted 10,000 rows"

# -----------------------------------------------------------------------------
# Show partition info
# -----------------------------------------------------------------------------
echo ""
echo "[3/3] Partition summary:"

$CH << 'EOF'
SELECT 
    partition,
    sum(rows) as row_count,
    formatReadableSize(sum(bytes_on_disk)) as size
FROM system.parts 
WHERE database = 'default' 
  AND table = 'test_telemetry'
  AND active = 1
GROUP BY partition
ORDER BY partition
FORMAT Pretty
EOF

echo ""
echo "============================================"
echo "Test setup complete!"
echo ""
echo "Verify with:"
echo "  curl http://localhost:8123/ping"
echo "  curl 'http://localhost:8123' -d 'SELECT count() FROM test_telemetry'"
echo "============================================"
