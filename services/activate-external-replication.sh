#!/bin/sh
set -eu

site=${1:-}
if [ "$#" -gt 0 ]; then
    shift
fi
case "$site" in
    primary)
        base=docker-compose.primary.yaml
        overlay=docker-compose.external.primary.yaml
        ;;
    dr)
        base=docker-compose.dr.yaml
        overlay=docker-compose.external.dr.yaml
        ;;
    *)
        echo "Usage: $0 primary|dr [additional Docker Compose options]" >&2
        exit 2
        ;;
esac

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"

# Preserve operator-supplied -f/--env-file/-p options so activation does not
# discard a site's local Compose overrides or change its project identity.
# Apply the safety overlay last so a local override cannot re-enable sync.
set -- -f "$base" "$@" -f "$overlay"

assert_container_absent() {
    if docker container inspect "$1" >/dev/null 2>&1; then
        echo "Refusing external mode: built-in sync container still exists: $1" >&2
        exit 1
    fi
}

assert_container_project() {
    container=$1
    if docker container inspect "$container" >/dev/null 2>&1; then
        actual_project=$(docker container inspect --format \
            '{{ index .Config.Labels "com.docker.compose.project" }}' "$container")
        if [ "$actual_project" != "$project" ]; then
            echo "Refusing external mode: $container belongs to Compose project " \
                "$actual_project, not rendered project $project" >&2
            exit 1
        fi
    fi
}

remove_sync_service_containers() {
    service=$1
    container_ids=$(docker ps -aq \
        --filter "label=com.docker.compose.project=$project" \
        --filter "label=com.docker.compose.service=$service")
    for container_id in $container_ids; do
        docker stop "$container_id"
        docker rm "$container_id"
    done
}

assert_sync_service_absent() {
    service=$1
    remaining=$(docker ps -aq \
        --filter "label=com.docker.compose.project=$project" \
        --filter "label=com.docker.compose.service=$service")
    if [ -n "$remaining" ]; then
        echo "Refusing external mode: $project still has $service containers" >&2
        exit 1
    fi
}

assert_no_collector_oneoff() {
    oneoffs=$(docker ps -aq \
        --filter "label=com.docker.compose.project=$project" \
        --filter "label=com.docker.compose.service=otel-collector" \
        --filter "label=com.docker.compose.oneoff=True")
    if [ -n "$oneoffs" ]; then
        echo "Refusing external mode: an otel-collector one-off still exists" >&2
        echo "Remove container(s): $oneoffs" >&2
        exit 1
    fi
}

# Validate before stopping a running synchronization path. The removal below
# is container-only: named watermark/state volumes are deliberately retained.
sh services/preflight.sh "$@"

project=$(
    docker compose "$@" config --format json |
        python3 -c 'import json, sys; print(json.load(sys.stdin)["name"])'
)
[ -n "$project" ] || {
    echo "Could not determine the rendered Compose project name" >&2
    exit 1
}

assert_no_collector_oneoff
for container in \
    victoriametrics clickhouse grafana dns-failover otel-watcher \
    application-study-tool-otel-collector-1 vm-sync ch-sync
do
    assert_container_project "$container"
done

remove_sync_service_containers vm-sync
remove_sync_service_containers ch-sync

assert_sync_service_absent vm-sync
assert_container_absent vm-sync
assert_sync_service_absent ch-sync
assert_container_absent ch-sync

docker compose "$@" up -d

assert_no_collector_oneoff
assert_sync_service_absent vm-sync
assert_container_absent vm-sync
assert_sync_service_absent ch-sync
assert_container_absent ch-sync

echo "External-replication mode is active for $site"
