#!/bin/sh
set -eu

compose_file="${1:-docker-compose.primary.yaml}"
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

docker_arch=$(docker version --format '{{.Server.Arch}}')
if [ "$docker_arch" != "amd64" ]; then
    echo "The pinned custom OTel collector supports amd64 only; Docker server is $docker_arch" >&2
    exit 1
fi

docker compose -f "$compose_file" config --quiet
rendered_config=$(mktemp)
trap 'rm -f "$rendered_config"' EXIT HUP INT TERM
docker compose -f "$compose_file" config --format json > "$rendered_config"

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

collector = model.get("services", {}).get("otel-collector", {})
collector_volumes = collector.get("volumes", [])
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

failover = model.get("services", {}).get("dns-failover", {})
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

echo "Preflight passed for $compose_file"
