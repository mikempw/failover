#!/bin/bash
# =============================================================================
# CH-Sync Setup Script
# =============================================================================
# Run this on each site before starting docker compose
#
# Usage:
#   Primary site: ./setup.sh primary
#   DR site:      ./setup.sh dr
# =============================================================================

set -e

ROLE=${1:-}

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# Validate input
if [[ "$ROLE" != "primary" && "$ROLE" != "dr" ]]; then
    echo "Usage: $0 <primary|dr>"
    echo ""
    echo "Examples:"
    echo "  $0 primary    # Setup for primary site"
    echo "  $0 dr         # Setup for DR site"
    exit 1
fi

log "Setting up CH-Sync for role: $ROLE"

# Create state directory
log "Creating state directory..."
mkdir -p /opt/ch-sync/data
chmod 777 /opt/ch-sync/data
log "  ✓ /opt/ch-sync/data created"

# Copy env file
if [[ -f ".env" ]]; then
    warn ".env already exists - backing up to .env.backup"
    cp .env .env.backup
fi

log "Copying .env.${ROLE} to .env..."
cp .env.${ROLE} .env
log "  ✓ .env configured for ${ROLE}"

echo ""
echo "============================================"
echo -e "${GREEN}Setup complete!${NC}"
echo "============================================"
echo ""
echo "Next steps:"
echo ""
echo "  1. Edit .env and update these values:"
echo "     - REMOTE_CH_URL, REMOTE_CH_HOST"
echo "     - DNS_RECORD, PRIMARY_IP, DR_IP"
echo ""
echo "  2. Add DNS hosts entry (for testing):"
echo "     echo '192.168.100.144 failover.mpwlabs.com' >> /etc/hosts"
echo ""
echo "  3. Build and start:"
echo "     docker compose build --no-cache"
echo "     docker compose up -d"
echo ""
echo "  4. Check logs:"
echo "     docker compose logs -f ch-sync"
echo ""
echo "============================================"
