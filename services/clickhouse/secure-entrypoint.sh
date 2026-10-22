#!/bin/bash
set -euo pipefail

read_secret() {
  local secret_file=$1
  local label=$2
  local value

  if [[ ! -r "$secret_file" ]]; then
    echo "$label secret is missing or unreadable: $secret_file" >&2
    return 1
  fi

  value="$(<"$secret_file")"
  if [[ ${#value} -lt 16 ]]; then
    echo "$label secret must contain at least 16 characters" >&2
    return 1
  fi

  case "${value,,}" in
    admin|password|changeme|change-me|clickhouse)
      echo "Refusing an unsafe default $label secret" >&2
      return 1
      ;;
  esac

  printf '%s' "$value"
}

validate_user_name() {
  local name=$1
  local label=$2
  if [[ ! "$name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || [[ "$name" == "default" ]]; then
    echo "$label must be a safe XML element name and cannot be 'default'" >&2
    exit 1
  fi
}

validate_ipv4_cidr() {
  local cidr=$1
  local address prefix octet
  local -a octets

  if [[ "$cidr" != */* ]]; then
    echo "ClickHouse network allowlist entry must be an IPv4 CIDR: $cidr" >&2
    exit 1
  fi
  address=${cidr%/*}
  prefix=${cidr##*/}
  if [[ ! "$prefix" =~ ^[0-9]+$ ]] || (( 10#$prefix > 32 )); then
    echo "Invalid IPv4 CIDR prefix: $cidr" >&2
    exit 1
  fi

  IFS='.' read -r -a octets <<< "$address"
  if (( ${#octets[@]} != 4 )); then
    echo "Invalid IPv4 CIDR address: $cidr" >&2
    exit 1
  fi
  for octet in "${octets[@]}"; do
    if [[ ! "$octet" =~ ^[0-9]+$ ]] || (( 10#$octet > 255 )); then
      echo "Invalid IPv4 CIDR address: $cidr" >&2
      exit 1
    fi
  done
}

trim_space() {
  local value=$1
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

clickhouse_user=${CLICKHOUSE_USER:-ast}
grafana_user=${GRAFANA_CLICKHOUSE_USER-grafana}
validate_user_name "$clickhouse_user" "CLICKHOUSE_USER"
if [[ -n "$grafana_user" ]]; then
  validate_user_name "$grafana_user" "GRAFANA_CLICKHOUSE_USER"
  if [[ "$grafana_user" == "$clickhouse_user" ]]; then
    echo "GRAFANA_CLICKHOUSE_USER must differ from CLICKHOUSE_USER" >&2
    exit 1
  fi
fi

clickhouse_password="$(
  read_secret \
    "${CLICKHOUSE_PASSWORD_FILE:-/run/secrets/clickhouse_password}" \
    "ClickHouse password"
)"
clickhouse_password_hash="$(printf '%s' "$clickhouse_password" | sha256sum | awk '{print $1}')"

grafana_password_hash=''
if [[ -n "$grafana_user" ]]; then
  grafana_password="$(
    read_secret \
      "${GRAFANA_CLICKHOUSE_PASSWORD_FILE:-/run/secrets/grafana_clickhouse_password}" \
      "Grafana ClickHouse password"
  )"
  grafana_password_hash="$(printf '%s' "$grafana_password" | sha256sum | awk '{print $1}')"
fi

internal_cidr=${CLICKHOUSE_INTERNAL_CIDR:-172.30.0.0/24}
validate_ipv4_cidr "$internal_cidr"
allowed_cidrs=("$internal_cidr")

IFS=',' read -r -a remote_cidrs <<< "${CLICKHOUSE_REMOTE_CIDRS:-}"
for remote_cidr in "${remote_cidrs[@]}"; do
  remote_cidr="$(trim_space "$remote_cidr")"
  [[ -z "$remote_cidr" ]] && continue
  validate_ipv4_cidr "$remote_cidr"
  allowed_cidrs+=("$remote_cidr")
done

users_dir=/etc/clickhouse-server/users.d
users_config=$users_dir/zz-runtime-users.xml
mkdir -p "$users_dir"
umask 077
users_config_tmp="$(mktemp "$users_dir/.zz-runtime-users.xml.XXXXXX")"
trap 'rm -f "$users_config_tmp"' EXIT

{
  printf '%s\n' '<clickhouse>' '  <users>'
  printf '%s\n' '    <default remove="remove" />'
  printf '    <%s>\n' "$clickhouse_user"
  printf '      <password_sha256_hex>%s</password_sha256_hex>\n' "$clickhouse_password_hash"
  printf '%s\n' '      <networks>'
  # The official image initializes CLICKHOUSE_DB through a loopback-only
  # temporary server before starting the final daemon.
  printf '%s\n' '        <ip>127.0.0.1/32</ip>' '        <ip>::1/128</ip>'
  for cidr in "${allowed_cidrs[@]}"; do
    printf '        <ip>%s</ip>\n' "$cidr"
  done
  printf '%s\n' '      </networks>'
  printf '%s\n' '      <profile>default</profile>' '      <quota>default</quota>'
  printf '%s\n' '      <access_management>1</access_management>'
  printf '    </%s>\n' "$clickhouse_user"

  if [[ -n "$grafana_user" ]]; then
    printf '    <%s>\n' "$grafana_user"
    printf '      <password_sha256_hex>%s</password_sha256_hex>\n' "$grafana_password_hash"
    printf '%s\n' '      <networks>'
    printf '        <ip>%s</ip>\n' "$internal_cidr"
    printf '%s\n' '      </networks>'
    printf '%s\n' '      <profile>grafana_readonly</profile>' '      <quota>default</quota>'
    printf '%s\n' '      <access_management>0</access_management>'
    printf '    </%s>\n' "$grafana_user"
  fi

  printf '%s\n' '  </users>' '</clickhouse>'
} > "$users_config_tmp"

chown clickhouse:clickhouse "$users_config_tmp"
chmod 0640 "$users_config_tmp"
mv -f "$users_config_tmp" "$users_config"
trap - EXIT

# The official entrypoint uses these only for initialization clients. User
# creation itself is disabled so it cannot replace the restricted config above.
export CLICKHOUSE_PASSWORD="$clickhouse_password"
export CLICKHOUSE_SKIP_USER_SETUP=1
exec /entrypoint.sh "$@"
