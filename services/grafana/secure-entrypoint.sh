#!/bin/sh
set -eu

secret_file="${CLICKHOUSE_PASSWORD_FILE:-/run/secrets/grafana_clickhouse_password}"
admin_secret_file="${GF_SECURITY_ADMIN_PASSWORD__FILE:-/run/secrets/grafana_admin_password}"
runtime_dir=/run/grafana-secrets

if [ "$(id -u)" != "0" ]; then
  echo "Grafana secret staging must start as root" >&2
  exit 1
fi

if [ ! -r "$secret_file" ]; then
  echo "Grafana cannot read the ClickHouse password secret: $secret_file" >&2
  exit 1
fi
if [ ! -r "$admin_secret_file" ]; then
  echo "Grafana cannot read the admin password secret: $admin_secret_file" >&2
  exit 1
fi

CLICKHOUSE_PASSWORD="$(cat "$secret_file")"
admin_password="$(cat "$admin_secret_file")"
if [ "${#CLICKHOUSE_PASSWORD}" -lt 16 ]; then
  echo "ClickHouse datasource password must contain at least 16 characters" >&2
  exit 1
fi
if [ "${#admin_password}" -lt 16 ]; then
  echo "Grafana admin password must contain at least 16 characters" >&2
  exit 1
fi

rm -rf "$runtime_dir"
mkdir -p "$runtime_dir"
printf '%s' "$admin_password" > "$runtime_dir/admin_password"

grafana_uid="$(id -u grafana)"
grafana_gid="$(id -g grafana)"
chown -R "$grafana_uid:$grafana_gid" "$runtime_dir"
chmod 0700 "$runtime_dir"
chmod 0400 "$runtime_dir/admin_password"

export CLICKHOUSE_PASSWORD
export GF_SECURITY_ADMIN_PASSWORD__FILE="$runtime_dir/admin_password"
unset admin_password secret_file admin_secret_file
exec /usr/local/bin/drop-privileges "$grafana_uid" "$grafana_gid" /run.sh "$@"
