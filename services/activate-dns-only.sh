#!/bin/sh
set -eu

site=${1:-}
if [ "$#" -gt 0 ]; then
    shift
fi
case "$site" in
    primary)
        manifest=docker-compose.dns-only.primary.yaml
        ;;
    dr)
        manifest=docker-compose.dns-only.dr.yaml
        ;;
    *)
        echo "Usage: $0 primary|dr [additional Docker Compose options]" >&2
        exit 2
        ;;
esac

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"
set -- -f "$manifest" "$@"

# Validate the one-service model before touching a running deployment.
sh services/preflight.sh "$@"

rendered_config=$(mktemp)
retire_list=$(mktemp)
trap 'rm -f "$rendered_config" "$retire_list"' EXIT HUP INT TERM
docker compose "$@" config --format json > "$rendered_config"

project=$(
    python3 - "$rendered_config" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    print(json.load(stream)["name"])
PY
)
rendered_role=$(
    python3 - "$rendered_config" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    model = json.load(stream)
print(model["services"]["dns-failover"]["environment"]["ROLE"])
PY
)
rendered_mode=$(
    python3 - "$rendered_config" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    model = json.load(stream)
print(model["services"]["dns-failover"]["environment"].get("DEPLOYMENT_MODE", "full"))
PY
)

if [ "$rendered_role" != "$site" ]; then
    echo "Refusing DNS-only activation: rendered ROLE=$rendered_role, expected $site" >&2
    exit 1
fi
if [ "$rendered_mode" != "dns-only" ]; then
    echo "Refusing DNS-only activation: rendered DEPLOYMENT_MODE=$rendered_mode" >&2
    exit 1
fi

# A fixed container name is retained for compatibility with existing
# installations. Refuse to take over a container owned by another project.
if docker container inspect dns-failover >/dev/null 2>&1; then
    actual_project=$(docker container inspect --format \
        '{{ index .Config.Labels "com.docker.compose.project" }}' dns-failover)
    if [ "$actual_project" != "$project" ]; then
        echo "Refusing DNS-only activation: dns-failover belongs to Compose project " \
            "$actual_project, not rendered project $project" >&2
        exit 1
    fi
fi

find_retired_containers() {
    : > "$retire_list"
    container_ids=$(docker ps -aq \
        --filter "label=com.docker.compose.project=$project")
    for container_id in $container_ids; do
        service=$(docker container inspect --format \
            '{{ index .Config.Labels "com.docker.compose.service" }}' "$container_id")
        oneoff=$(docker container inspect --format \
            '{{ index .Config.Labels "com.docker.compose.oneoff" }}' "$container_id")
        if [ "$service" != "dns-failover" ] || [ "$oneoff" = "True" ]; then
            printf '%s %s\n' "$container_id" "$service" >> "$retire_list"
        fi
    done
}

remove_service_containers() {
    service=$1
    container_ids=$(docker ps -aq \
        --filter "label=com.docker.compose.project=$project" \
        --filter "label=com.docker.compose.service=$service")
    for container_id in $container_ids; do
        docker stop "$container_id" >/dev/null
        docker rm "$container_id" >/dev/null
    done
}

find_retired_containers
if [ -s "$retire_list" ]; then
    if [ "${DNS_ONLY_CONFIRM:-}" != "RETIRE_REPOSITORY_STACK" ]; then
        echo "Refusing DNS-only activation while non-DNS containers remain in $project:" >&2
        while read -r container_id service; do
            echo "  $container_id ($service)" >&2
        done < "$retire_list"
        echo "Move data-plane ownership outside this Compose project, then rerun with:" >&2
        echo "  DNS_ONLY_CONFIRM=RETIRE_REPOSITORY_STACK $0 $site" >&2
        echo "The confirmed transition removes containers only; named volumes are preserved." >&2
        exit 1
    fi
fi

canonical_before=$(docker ps -aq \
    --filter "label=com.docker.compose.project=$project" \
    --filter "label=com.docker.compose.service=dns-failover" \
    --filter "label=com.docker.compose.oneoff=False")
if [ -s "$retire_list" ] || [ -z "$canonical_before" ]; then
    # A fresh deployment or full-stack transition probes every gate used by
    # this role from the controller image/network context. A stable DNS-only
    # restart skips this all-gates check: during a real primary outage, primary
    # availability and recovery are expected to be non-2xx.
    if [ -z "$canonical_before" ] && [ ! -s "$retire_list" ] && \
       [ "${DNS_ONLY_SKIP_PROBE:-}" = "START_DURING_OUTAGE" ]; then
        if [ "$rendered_role" != "dr" ]; then
            echo "DNS_ONLY_SKIP_PROBE=START_DURING_OUTAGE is valid only for DR" >&2
            exit 1
        fi
        echo "Primary is allowed to be unavailable; probing DR readiness only" >&2
        docker compose "$@" run --rm --no-deps dns-failover probe-dr
    else
        docker compose "$@" run --rm --no-deps dns-failover probe
    fi
fi

# Recreate the controller with its external health endpoints first and wait
# for a successful control iteration. This avoids starting the lease-expiry
# clock merely because the old repository-managed data plane was stopped.
docker compose "$@" up -d --wait --wait-timeout "${DNS_ONLY_WAIT_TIMEOUT:-120}"

if [ -s "$retire_list" ]; then
    # Disable the Docker-socket watcher first. Then retire every remaining
    # repository-managed writer before storage. Named volumes are retained.
    remove_service_containers otel-watcher
    for service in otel-collector vm-sync ch-sync grafana victoriametrics clickhouse; do
        remove_service_containers "$service"
    done
    find_retired_containers
    while read -r container_id _service; do
        [ -n "$container_id" ] || continue
        docker stop "$container_id" >/dev/null
        docker rm "$container_id" >/dev/null
    done < "$retire_list"

    # Start from a fresh readiness state after the former data plane is gone.
    # This catches health URLs that accidentally depended on those containers.
    docker compose "$@" up -d --force-recreate --wait \
        --wait-timeout "${DNS_ONLY_WAIT_TIMEOUT:-120}"
fi

find_retired_containers
if [ -s "$retire_list" ]; then
    echo "DNS-only activation failed: non-DNS project containers remain" >&2
    cat "$retire_list" >&2
    exit 1
fi

project_containers=$(docker ps -aq \
    --filter "label=com.docker.compose.project=$project")
set -- $project_containers
if [ "$#" -ne 1 ]; then
    echo "DNS-only activation failed: expected exactly one project container" >&2
    exit 1
fi
dns_container=$1
dns_service=$(docker container inspect --format \
    '{{ index .Config.Labels "com.docker.compose.service" }}' "$dns_container")
dns_oneoff=$(docker container inspect --format \
    '{{ index .Config.Labels "com.docker.compose.oneoff" }}' "$dns_container")
dns_name=$(docker container inspect --format '{{ .Name }}' "$dns_container")
dns_running=$(docker container inspect --format '{{ .State.Running }}' "$dns_container")
dns_health=$(docker container inspect --format '{{ .State.Health.Status }}' "$dns_container")
if [ "$dns_service" != "dns-failover" ] || \
   [ "$dns_oneoff" != "False" ] || \
   [ "$dns_name" != "/dns-failover" ] || \
   [ "$dns_running" != "true" ] || \
   [ "$dns_health" != "healthy" ]; then
    echo "DNS-only activation failed: canonical dns-failover is not the sole healthy running container" >&2
    exit 1
fi

echo "DNS-only control plane is active for $site"
