#!/bin/sh
set -eu

copy_tree() {
  source_dir=$1
  destination_dir=$2
  label=$3
  file_mode=$4

  rm -rf "$destination_dir"
  mkdir -p "$destination_dir"
  if [ -d "$source_dir" ]; then
    cp -R "$source_dir"/. "$destination_dir"/
  fi
  chown -R appuser:appuser "$destination_dir"
  find "$destination_dir" -type d -exec chmod 0700 {} +
  find "$destination_dir" -type f -exec chmod "$file_mode" {} +
  if [ ! -x "$destination_dir" ]; then
    echo "Unable to prepare $label directory" >&2
    exit 1
  fi
}

if [ "$(id -u)" != "0" ]; then
  echo "DNS failover secret staging must start as root" >&2
  exit 1
fi

copy_tree /run/source-secrets /secrets "secrets" 0600
copy_tree /run/source-scripts /scripts "scripts" 0700

app_uid=$(id -u appuser)
app_gid=$(id -g appuser)
exec /usr/local/bin/drop-privileges "$app_uid" "$app_gid" \
  python3 /app/dns_failover.py "$@"
