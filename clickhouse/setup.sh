#!/bin/bash
# =============================================================================
# ClickHouse Test Setup Script
# =============================================================================
# Creates required directories and starts ClickHouse
#
# Usage: ./setup.sh
# =============================================================================

set -e

echo "============================================"
echo "ClickHouse Test Setup"
echo "============================================"

# Create data directories
echo "[1/3] Creating data directories..."
mkdir -p clickhouse-data clickhouse-logs
echo "  ✓ Directories created"

# Start ClickHouse
echo ""
echo "[2/3] Starting ClickHouse..."
docker compose up -d
echo "  ✓ ClickHouse starting"

# Wait for it to be ready
echo ""
echo "[3/3] Waiting for ClickHouse to be ready..."
for i in {1..30}; do
    if curl -s http://localhost:8123/ping > /dev/null 2>&1; then
        echo "  ✓ ClickHouse is ready"
        break
    fi
    if [ $i -eq 30 ]; then
        echo "  ✗ Timeout waiting for ClickHouse"
        echo "  Check logs: docker compose logs clickhouse"
        exit 1
    fi
    sleep 1
done

echo ""
echo "============================================"
echo "Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Update clickhouse-config/cluster.xml with your IPs"
echo "  2. Run ./test.sh to create test data"
echo "  3. Repeat on DR site"
echo "============================================"
