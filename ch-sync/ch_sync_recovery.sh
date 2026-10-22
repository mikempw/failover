#!/bin/sh
# Run an explicitly configured, one-shot reverse synchronization for recovery.
set -eu

RECOVERY_CONFIRMATION=REPLACE_DESTINATION_PARTITIONS

if [ "${CH_SYNC_RECOVERY_CONFIRM:-}" != "$RECOVERY_CONFIRMATION" ]; then
    echo "Refusing recovery sync." >&2
    echo "Set SOURCE_HOST/DEST_HOST and CH_SYNC_RECOVERY_CONFIRM=REPLACE_DESTINATION_PARTITIONS." >&2
    exit 2
fi

: "${SOURCE_HOST:?SOURCE_HOST must identify the recovery source}"
: "${DEST_HOST:?DEST_HOST must identify the recovery destination}"
: "${DNS_RECORD:?DNS_RECORD must identify the failover DNS record}"
: "${PRIMARY_IP:?PRIMARY_IP must identify the recovery destination address}"
: "${SYNC_GUARD_LEASE_PROOF_FILE:?SYNC_GUARD_LEASE_PROOF_FILE must identify the observed lease proof}"

echo "WARNING: reverse sync can replace destination partitions." >&2
echo "Run it only while DNS still points to DR and primary OTEL ingestion is stopped." >&2

# Recovery reverses the source and destination, so fence the primary address.
# The human confirmation does not bypass DNS/lease-proof verification.
export SYNC_GUARD_DNS_RECORD="${SYNC_GUARD_DNS_RECORD:-$DNS_RECORD}"
export SYNC_GUARD_DEST_IP="${SYNC_GUARD_DEST_IP:-$PRIMARY_IP}"
unset SYNC_ALLOW_ACTIVE_DESTINATION

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$SCRIPT_DIR/ch_sync.py" --once
