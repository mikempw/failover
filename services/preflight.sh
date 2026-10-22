#!/bin/sh
set -eu

if [ "$#" -eq 0 ]; then
    set -- -f docker-compose.primary.yaml
elif [ "$1" != "-f" ]; then
    compose_file=$1
    shift
    set -- -f "$compose_file" "$@"
fi
compose_description="$*"
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"

compose_version=$(docker compose version --short)
python3 - "$compose_version" <<'PY'
import re
import sys

match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", sys.argv[1])
if not match or tuple(map(int, match.groups(default="0"))) < (2, 24, 0):
    raise SystemExit(
        f"Docker Compose 2.24.0 or newer is required; found {sys.argv[1]!r}"
    )
PY

docker compose "$@" config --quiet
rendered_config=$(mktemp)
trap 'rm -f "$rendered_config"' EXIT HUP INT TERM
docker compose "$@" config --format json > "$rendered_config"

# The repository's pinned collector is amd64-only.  A DNS-only deployment
# does not contain that image and can run on any architecture supported by
# the Python base image.
if python3 - "$rendered_config" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    model = json.load(stream)
raise SystemExit(0 if "otel-collector" in model.get("services", {}) else 1)
PY
then
    docker_arch=$(docker version --format '{{.Server.Arch}}')
    if [ "$docker_arch" != "amd64" ]; then
        echo "The pinned custom OTel collector supports amd64 only; Docker server is $docker_arch" >&2
        exit 1
    fi
fi

# Validate the paths Compose actually rendered after applying .env and shell
# overrides.  Parsing the JSON model avoids sourcing an arbitrary dotenv file
# as shell code and correctly handles absolute paths and spaces.
python3 - "$rendered_config" <<'PY'
import json
import pathlib
import sys

model_path = pathlib.Path(sys.argv[1])
model = json.loads(model_path.read_text(encoding="utf-8"))
errors = []
services = model.get("services", {})

for secret_name, secret in sorted(model.get("secrets", {}).items()):
    filename = secret.get("file")
    if not filename:
        errors.append(f"Compose secret {secret_name} has no file source")
        continue
    path = pathlib.Path(filename)
    try:
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except OSError:
        errors.append(f"Missing readable secret file: {path}")
        continue
    if len(value) < 16:
        errors.append(f"Secret must contain at least 16 characters: {path}")

failover = services.get("dns-failover", {})
failover_environment = failover.get("environment", {})
deployment_mode = failover_environment.get("DEPLOYMENT_MODE", "full")
if deployment_mode not in {"full", "dns-only"}:
    errors.append("DEPLOYMENT_MODE must be either 'full' or 'dns-only'")

if deployment_mode == "dns-only":
    unexpected_services = sorted(set(services) - {"dns-failover"})
    if unexpected_services or set(services) != {"dns-failover"}:
        errors.append(
            "DNS-only mode must render exactly the dns-failover service; found: "
            + (", ".join(sorted(services)) or "none")
        )
    if model.get("secrets"):
        errors.append("DNS-only mode must not render application Docker secrets")
    if failover_environment.get("ROLE") not in {"primary", "dr"}:
        errors.append("DNS-only mode must force ROLE to primary or dr")

    failover_volumes = [
        volume
        for volume in failover.get("volumes", [])
        if isinstance(volume, dict)
    ]
    volumes_by_target = {
        volume.get("target"): volume
        for volume in failover_volumes
    }
    allowed_targets = {"/state", "/run/source-secrets", "/run/source-scripts"}
    if len(failover_volumes) != 3 or set(volumes_by_target) != allowed_targets:
        errors.append(
            "DNS-only dns-failover mounts must be exactly /state, "
            "/run/source-secrets, and /run/source-scripts"
        )
    else:
        state_mount = volumes_by_target["/state"]
        state_source = state_mount.get("source")
        declared_volumes = model.get("volumes", {})
        declared_volume_names = {
            definition.get("name", key)
            for key, definition in declared_volumes.items()
            if isinstance(definition, dict)
        }
        if (
            state_mount.get("type") != "volume"
            or state_mount.get("read_only")
            or not state_source
            or len(declared_volumes) != 1
            or (
                state_source not in declared_volumes
                and state_source not in declared_volume_names
            )
        ):
            errors.append("DNS-only /state must be one writable named volume")
        for target in ("/run/source-secrets", "/run/source-scripts"):
            bind = volumes_by_target[target]
            if bind.get("type") != "bind" or not bind.get("read_only"):
                errors.append(f"DNS-only {target} must be one read-only bind")
    if failover.get("ports") or failover.get("depends_on"):
        errors.append("DNS-only dns-failover must not publish ports or depend on services")
    for volume in failover_volumes:
        if not isinstance(volume, dict):
            continue
        target = volume.get("target")
        if volume.get("source") == "/var/run/docker.sock" or target == "/var/run/docker.sock":
            errors.append("DNS-only mode must not mount the Docker socket")

collector = services.get("otel-collector", {})
collector_volumes = collector.get("volumes", [])
if deployment_mode == "full" and not collector:
    errors.append("Full deployment mode must include otel-collector")
elif collector:
    replication_mode = collector.get("environment", {}).get(
        "DATA_REPLICATION_MODE", "builtin"
    )
    if replication_mode not in {"builtin", "external"}:
        errors.append(
            "DATA_REPLICATION_MODE must be either 'builtin' or 'external'"
        )
    elif replication_mode == "external":
        unexpected_sync = sorted({"vm-sync", "ch-sync"}.intersection(services))
        if unexpected_sync:
            errors.append(
                "External-replication mode must disable built-in sync services: "
                + ", ".join(unexpected_sync)
            )

        pipeline_sources = [
            pathlib.Path(volume["source"])
            for volume in collector_volumes
            if isinstance(volume, dict)
            and volume.get("target") == "/etc/otel-collector-config/pipelines.yaml"
            and volume.get("source")
        ]
        if len(pipeline_sources) != 1:
            errors.append(
                "External-replication mode must bind exactly one local-only "
                "collector pipeline"
            )
        else:
            try:
                pipeline_text = pipeline_sources[0].read_text(encoding="utf-8")
            except OSError:
                errors.append(
                    f"Missing local-only collector pipeline: {pipeline_sources[0]}"
                )
            else:
                if "prometheusremotewrite/peer" in pipeline_text:
                    errors.append(
                        "External-replication collector pipeline still enables "
                        "peer remote write"
                    )
    receiver_sources = [
        pathlib.Path(volume["source"])
        for volume in collector_volumes
        if isinstance(volume, dict)
        and volume.get("target") == "/run/source-otel-config/receivers.yaml"
        and volume.get("source")
    ]
    if len(receiver_sources) != 1:
        errors.append("OTEL receiver configuration must have exactly one bind source")
    elif not receiver_sources[0].is_file():
        errors.append(f"Missing OTEL receiver configuration: {receiver_sources[0]}")

    otel_secret_sources = [
        pathlib.Path(volume["source"])
        for volume in collector_volumes
        if isinstance(volume, dict)
        and volume.get("target") == "/run/source-otel-secrets"
        and volume.get("source")
    ]
    if len(otel_secret_sources) != 1:
        errors.append("OTel must have exactly one dedicated secret-directory bind source")
    elif not otel_secret_sources[0].is_dir():
        errors.append(f"Missing OTel secret directory: {otel_secret_sources[0]}")

failover_secret_sources = [
    pathlib.Path(volume["source"])
    for volume in failover.get("volumes", [])
    if isinstance(volume, dict)
    and volume.get("target") == "/run/source-secrets"
    and volume.get("source")
]
if len(failover_secret_sources) != 1:
    errors.append("dns-failover must have exactly one dedicated provider-secret bind source")
elif not failover_secret_sources[0].is_dir():
    errors.append(f"Missing failover provider secret directory: {failover_secret_sources[0]}")

if errors:
    for error in errors:
        print(error, file=sys.stderr)
    raise SystemExit(1)
PY

echo "Preflight passed for $compose_description"
