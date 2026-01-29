#!/bin/bash
# =============================================================================
# CH-Sync Setup Script
# =============================================================================
# Run this ONCE on each site before starting docker compose
#
# Usage:
#   Primary site: ./setup.sh primary
#   DR site:      ./setup.sh dr
# =============================================================================

set -e

ROLE=${1:-}
INSTALL_DIR="/opt/ch-sync"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# -----------------------------------------------------------------------------
# Validate input
# -----------------------------------------------------------------------------
if [[ "$ROLE" != "primary" && "$ROLE" != "dr" ]]; then
    echo "Usage: $0 <primary|dr>"
    echo ""
    echo "Examples:"
    echo "  $0 primary    # Setup for primary site"
    echo "  $0 dr         # Setup for DR site"
    exit 1
fi

log "Setting up CH-Sync for role: $ROLE"

# -----------------------------------------------------------------------------
# Create data directories
# -----------------------------------------------------------------------------
log "Creating data directories..."

mkdir -p ${INSTALL_DIR}/data
chmod 755 ${INSTALL_DIR}/data

log "Data directory created: ${INSTALL_DIR}/data"

# -----------------------------------------------------------------------------
# Setup .env file
# -----------------------------------------------------------------------------
if [[ -f ".env" ]]; then
    warn ".env already exists - backing up to .env.backup"
    cp .env .env.backup
fi

log "Copying .env.${ROLE} to .env..."
cp .env.${ROLE} .env

# -----------------------------------------------------------------------------
# Remind user to configure
# -----------------------------------------------------------------------------
echo ""
echo "============================================================================="
echo -e "${GREEN}Setup complete!${NC}"
echo "============================================================================="
echo ""
echo "Next steps:"
echo ""
echo "  1. Edit .env and update these values for your environment:"
echo "     - LOCAL_CH_URL        (local ClickHouse HTTP URL)"
echo "     - REMOTE_CH_URL       (remote ClickHouse HTTP URL)"
echo "     - REMOTE_CH_HOST/PORT (remote ClickHouse native port for remote())"
echo "     - LOCAL_CH_USER/PASSWORD"
echo "     - REMOTE_CH_USER/PASSWORD"
echo "     - DNS_RECORD          (your failover DNS record)"
echo "     - PRIMARY_IP / DR_IP  (site IPs)"
echo "     - NOTIFY_WEBHOOK      (optional: Slack/Teams webhook)"
echo ""
echo "  2. Ensure ClickHouse tables have consistent partitioning:"
echo "     ALTER TABLE <table> MODIFY SETTING partition_by = 'toYYYYMMDD(timestamp)'"
echo "     (or recreate tables with PARTITION BY toYYYYMMDD(timestamp))"
echo ""
echo "  3. Build and start the stack:"
echo "     docker compose build"
echo "     docker compose up -d"
echo ""
echo "  4. Verify it's running:"
echo "     docker compose logs -f"
echo ""
echo "============================================================================="
