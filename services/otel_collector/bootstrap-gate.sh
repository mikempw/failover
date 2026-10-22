#!/bin/busybox sh
# Keep the collector inert until DNS and the local controller prove ownership.

set -eu

log() {
    printf '%s\n' "otel-bootstrap-gate: $*" >&2
}

case "${ROLE:-}" in
    primary)
        expected_ip="${PRIMARY_IP:-}"
        ;;
    dr)
        expected_ip="${DR_IP:-}"
        ;;
    *)
        log "ROLE must be primary or dr; refusing to start the collector"
        exit 64
        ;;
esac

if [ -z "${DNS_RECORD:-}" ] || [ -z "$expected_ip" ]; then
    log "DNS_RECORD and the ROLE-derived local IP are required"
    exit 64
fi
dns_record="$(
    /bin/busybox printf '%s' "${DNS_RECORD%.}" |
        /bin/busybox tr '[:upper:]' '[:lower:]'
)"

case "${MAX_CLOCK_SKEW:-5}" in
    ''|*[!0-9]*)
        log "MAX_CLOCK_SKEW must be a non-negative integer"
        exit 64
        ;;
esac
max_clock_skew="${MAX_CLOCK_SKEW:-5}"
lease_proof_file="${LEASE_PROOF_FILE:-/state/observed-lease.json}"
if [ "${OTEL_SECRETS_STAGED:-}" != "1" ]; then
    if [ "$(/bin/busybox id -u)" != "0" ]; then
        log "secret staging must start as root"
        exit 1
    fi
    clickhouse_secret="${CLICKHOUSE_PASSWORD_FILE:-/run/secrets/clickhouse_password}"
    otel_secret_source="${OTEL_SECRETS_SOURCE_DIR:-/run/source-otel-secrets}"
    receivers_source="${OTEL_RECEIVERS_SOURCE_FILE:-/run/source-otel-config/receivers.yaml}"
    if [ ! -r "$clickhouse_secret" ]; then
        log "ClickHouse password secret is unavailable"
        exit 1
    fi
    clickhouse_password="$(/bin/busybox cat "$clickhouse_secret")"
    if [ "${#clickhouse_password}" -lt 16 ]; then
        log "ClickHouse password secret must contain at least 16 characters"
        exit 1
    fi
    if [ ! -f "$receivers_source" ] || [ ! -r "$receivers_source" ] || [ -L "$receivers_source" ]; then
        log "OTel receiver configuration must be a readable regular file, not a symbolic link"
        exit 1
    fi
    /bin/busybox rm -rf /run/runtime-secrets
    /bin/busybox rm -rf /run/runtime-config
    /bin/busybox mkdir -p /run/runtime-secrets
    /bin/busybox mkdir -p /run/runtime-config
    if [ -d "$otel_secret_source" ]; then
        /bin/busybox cp -R "$otel_secret_source"/. /run/runtime-secrets/
    fi
    if [ -n "$(
        /bin/busybox find /run/runtime-secrets -type l -print -quit 2>/dev/null
    )" ]; then
        log "OTel secret source must not contain symbolic links"
        exit 1
    fi
    /bin/busybox printf '%s' "$clickhouse_password" > /run/runtime-secrets/clickhouse_password
    /bin/busybox cp "$receivers_source" /run/runtime-config/receivers.yaml
    /bin/busybox chown -R 10001:0 /run/runtime-secrets
    /bin/busybox chown -R 10001:0 /run/runtime-config
    /bin/busybox find /run/runtime-secrets -type d -exec /bin/busybox chmod 0700 {} +
    /bin/busybox find /run/runtime-secrets -type f -exec /bin/busybox chmod 0400 {} +
    /bin/busybox chmod 0500 /run/runtime-config
    /bin/busybox chmod 0400 /run/runtime-config/receivers.yaml
    unset clickhouse_password clickhouse_secret otel_secret_source receivers_source
    export OTEL_SECRETS_STAGED=1
    exec /usr/local/bin/drop-privileges 10001 0 \
        /bin/busybox sh /otel-bootstrap-gate.sh "$@"
fi

if [ "$(/bin/busybox id -u)" != "10001" ]; then
    log "bootstrap gate must run as UID 10001 after secret staging"
    exit 1
fi

if [ "${OTEL_CONFIG_VALIDATE_ONLY:-}" = "1" ]; then
    log "validating the rendered collector configuration as UID 10001"
    exec /otelcol-custom validate "$@"
fi

stable_required="${OTEL_BOOTSTRAP_STABLE_CHECKS:-3}"
check_interval="${OTEL_BOOTSTRAP_CHECK_INTERVAL:-2}"
case "$stable_required" in
    ''|*[!0-9]*|0)
        log "OTEL_BOOTSTRAP_STABLE_CHECKS must be a positive integer"
        exit 64
        ;;
esac
case "$check_interval" in
    ''|*[!0-9]*|0)
        log "OTEL_BOOTSTRAP_CHECK_INTERVAL must be a positive integer"
        exit 64
        ;;
esac

resolve_a_records() {
    if [ -n "${OTEL_BOOTSTRAP_DNS_SERVER:-}" ]; then
        lookup_output="$(
            /bin/busybox nslookup -type=A "$dns_record" \
                "$OTEL_BOOTSTRAP_DNS_SERVER" 2>/dev/null
        )" || return 1
    else
        lookup_output="$(
            /bin/busybox nslookup -type=A "$dns_record" 2>/dev/null
        )" || return 1
    fi

    # Ignore the resolver's own address (it appears before the Name line),
    # accept both BusyBox output variants, and return unique IPv4 answers.
    printf '%s\n' "$lookup_output" |
        /bin/busybox awk '
            $1 == "Name:" { in_answer = 1; next }
            in_answer && $1 ~ /^Address/ {
                for (i = 2; i <= NF; i++) {
                    value = $i
                    sub(/#[0-9]+$/, "", value)
                    if (value ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/) {
                        print value
                        break
                    }
                }
            }
        ' |
        /bin/busybox sort -u
}

lease_proof_matches() {
    [ -r "$lease_proof_file" ] || return 1
    proof_line="$(/bin/busybox cat "$lease_proof_file" 2>/dev/null)" || return 1
    case "$proof_line" in
        *'
'*) return 1 ;;
    esac

    expires_at="$(
        /bin/busybox printf '%s\n' "$proof_line" |
            /bin/busybox sed -n 's/^{"expires_at":\([0-9][0-9]*\),.*/\1/p'
    )"
    proof_ip="$(
        /bin/busybox printf '%s\n' "$proof_line" |
            /bin/busybox sed -n 's/.*,"ip":"\([^"]*\)","observed_at".*/\1/p'
    )"
    observed_at="$(
        /bin/busybox printf '%s\n' "$proof_line" |
            /bin/busybox sed -n 's/.*,"observed_at":\([0-9][0-9]*\),"owner".*/\1/p'
    )"
    proof_owner="$(
        /bin/busybox printf '%s\n' "$proof_line" |
            /bin/busybox sed -n 's/.*,"owner":"\([^"]*\)","record".*/\1/p'
    )"
    proof_record="$(
        /bin/busybox printf '%s\n' "$proof_line" |
            /bin/busybox sed -n 's/.*,"record":"\([^"]*\)","version".*/\1/p'
    )"
    proof_version="$(
        /bin/busybox printf '%s\n' "$proof_line" |
            /bin/busybox sed -n 's/.*,"version":\([0-9][0-9]*\)}$/\1/p'
    )"

    [ -n "$expires_at" ] && [ -n "$observed_at" ] || return 1
    expected_json="{\"expires_at\":$expires_at,\"ip\":\"$proof_ip\",\"observed_at\":$observed_at,\"owner\":\"$proof_owner\",\"record\":\"$proof_record\",\"version\":$proof_version}"
    [ "$proof_line" = "$expected_json" ] || return 1
    [ "$proof_version" = "1" ] || return 1
    [ "$proof_record" = "$dns_record" ] || return 1
    [ "$proof_ip" = "$expected_ip" ] || return 1
    [ "$proof_owner" = "$ROLE" ] || return 1

    now="$(/bin/busybox date +%s)" || return 1
    [ "$observed_at" -le $((now + max_clock_skew)) ] || return 1
    [ "$observed_at" -le "$expires_at" ] || return 1
    [ $((now + max_clock_skew)) -lt "$expires_at" ] || return 1
}

stable_count=0
last_observation=''
log "waiting for stable DNS plus an unexpired local lease proof for $dns_record"

while :; do
    addresses="$(resolve_a_records || true)"
    address_count="$(
        printf '%s\n' "$addresses" |
            /bin/busybox awk 'NF { count++ } END { print count + 0 }'
    )"

    proof_state=invalid
    if lease_proof_matches; then
        proof_state=valid
    fi

    if [ "$address_count" -eq 1 ] \
        && [ "$addresses" = "$expected_ip" ] \
        && [ "$proof_state" = "valid" ]; then
        stable_count=$((stable_count + 1))
    else
        stable_count=0
    fi

    observation="count=$address_count addresses=${addresses:-none} proof=$proof_state stable=$stable_count/$stable_required"
    if [ "$observation" != "$last_observation" ]; then
        log "$observation"
        last_observation="$observation"
    fi

    if [ "$stable_count" -ge "$stable_required" ]; then
        log "DNS and lease ownership are stable; starting the collector as UID 10001"
        exec /otelcol-custom "$@"
    fi

    /bin/busybox sleep "$check_interval"
done
