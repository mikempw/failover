# Runtime secrets

The compose stacks require password files and mount them as Docker secrets. Do
not commit the generated files.

Create independent, newline-free values with restrictive permissions:

```sh
umask 077
openssl rand -base64 32 | tr -d '\n' > secrets/clickhouse_password.txt
openssl rand -base64 32 | tr -d '\n' > secrets/grafana_clickhouse_password.txt
openssl rand -base64 32 | tr -d '\n' > secrets/primary_clickhouse_password.txt
openssl rand -base64 32 | tr -d '\n' > secrets/grafana_admin_password.txt
```

On the primary site, `clickhouse_password.txt`,
`grafana_clickhouse_password.txt`, and `grafana_admin_password.txt` are used.
The separate Grafana credential is assigned a read-only ClickHouse profile. On the DR site,
`primary_clickhouse_password.txt` authenticates replication to the primary and
may be a copy of the primary site's ClickHouse password. Override any host path
with `CLICKHOUSE_PASSWORD_FILE`, `GRAFANA_CLICKHOUSE_PASSWORD_FILE`,
`PRIMARY_CLICKHOUSE_PASSWORD_FILE`, or `GRAFANA_ADMIN_PASSWORD_FILE` in the
root `.env` file.

Run `sh services/preflight.sh docker-compose.primary.yaml` (or the DR compose
file) before starting a stack.
