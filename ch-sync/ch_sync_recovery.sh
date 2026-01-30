#!/bin/bash
#
# ClickHouse Recovery Sync
# Pulls missing data from DR to Primary (append-only, no deletes)
#
# Usage: ./ch_sync_recovery.sh

SOURCE="192.168.100.143"  # DR
DEST="192.168.100.144"    # Primary
DB="ast"

echo "Syncing from DR ($SOURCE) to Primary ($DEST)"

# Get tables
TABLES=$(curl -s "http://${SOURCE}:8123/?query=SHOW+TABLES+FROM+${DB}" | tr '\n' ' ')

for TABLE in $TABLES; do
    [ -z "$TABLE" ] && continue
    echo "Syncing $DB.$TABLE..."
    
    # Get max timestamp from destination (what we already have)
    MAX_TS=$(curl -s "http://${DEST}:8123/" \
        --data-urlencode "query=SELECT max(TimeUnix) FROM ${DB}.${TABLE}" 2>/dev/null | tr -d '\n')
    
    if [ -z "$MAX_TS" ] || [ "$MAX_TS" = "0" ]; then
        echo "  No existing data, pulling all..."
        curl -s "http://${DEST}:8123/" \
            --data-urlencode "query=INSERT INTO ${DB}.${TABLE} SELECT * FROM remote('${SOURCE}:9000', '${DB}.${TABLE}', 'default', '')"
    else
        echo "  Pulling data after $MAX_TS..."
        curl -s "http://${DEST}:8123/" \
            --data-urlencode "query=INSERT INTO ${DB}.${TABLE} SELECT * FROM remote('${SOURCE}:9000', '${DB}.${TABLE}', 'default', '') WHERE TimeUnix > ${MAX_TS}"
    fi
done

echo "Done"