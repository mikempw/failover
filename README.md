# AST active/passive failover

This repository runs the Application Study Tool (AST) at a primary and a disaster-recovery (DR) site. A DNS A record selects the active site, while a companion TXT lease records the owner and expiry. A watcher at each site starts its local OpenTelemetry collector only when the single A answer equals that site's IPv4 address and matches a current controller-published lease proof.

The stack includes VictoriaMetrics, ClickHouse, Grafana, the custom AST OpenTelemetry collector, DNS lease controllers, collector watchers, and recovery synchronizers.

## How it works

```text
                       managed A + TXT lease
                    +-------------------------+
                    |                         |
             +------v------+           +------v------+
             |   Primary   |           |     DR      |
             | DNS agent   |           | DNS agent   |
             | OTEL watcher|           | OTEL watcher|
             +------+------+
                    |                         |
             active IP runs OTEL; the other site's OTEL is stopped

             OTEL writes metrics to both VictoriaMetrics sites
             vm-sync fills VictoriaMetrics gaps in both directions
             ch-sync copies ClickHouse partitions primary -> DR
```

The DNS controller is fail-closed. DR promotion requires repeated primary-health failures, an expired coherent lease (including a clock-skew allowance), and optional DR-local health. Unknown, partial, malformed, or unreadable lease state blocks promotion. DNS writes are read back and verified.

Primary reclamation requires a configurable healthy streak. The default
`AUTO_FAILBACK=false` keeps a live DR site authoritative until an operator
completes recovery and switches it back; an expired lease can still be
reclaimed if the DR controller or its local backing service is gone.
Reclamation while DR owns the lease uses the primary site's local
`FAILBACK_HEALTH_URL`, never the intentionally stopped primary collector.
With `AUTO_FAILBACK=true`, DR uses its separate, DR-visible
`FAILBACK_HEALTH_URL` to decide when to stop renewing; primary still waits for
lease expiry and clock skew before reclaiming.
See [USER_GUIDE.md](USER_GUIDE.md) before enabling it: ClickHouse data created
while DR is active must first be copied back to primary.

## Requirements

- Two Linux/amd64 Docker hosts, normally one per site; the pinned upstream AST
  collector image does not provide an arm64 variant
- Docker Engine with Docker Compose v2.24 or newer (the manifests use
  `env_file.required`)
- A DNS provider supported by `failover/dns_failover.py`, or two custom provider scripts
- Routed connectivity between sites for the endpoints you configure
- Reliable time synchronization on both hosts; lease and proof expiry use
  absolute Unix time
- Python 3.11+ for `services/preflight.sh`, the setup wizard, or tests on the host

The watcher uses Linux host networking and the Docker socket. This deployment is not suitable for Docker Desktop without adaptation.

## Secure quick start

Run the following in a clone of this repository on each site. Files without `.example` suffixes are ignored by Git.

### 1. Create local configuration

```sh
cp .env.example .env
```

On primary (the root location is preferred; Compose also accepts the legacy
`failover/` location):

```sh
cp failover/.env.primary.example .env.primary
```

On DR:

```sh
cp failover/.env.dr.example .env.dr
```

Replace every TEST-NET address and `example.test` value. In `.env`, set the peer VictoriaMetrics URLs, the primary ClickHouse address, and `BIND_ADDRESS`. The default `127.0.0.1` binding is safe for local evaluation but is not reachable from the peer site. Set `CLICKHOUSE_REMOTE_CIDRS` on primary to the narrow DR/recovery source ranges that need the main ClickHouse account. Expose only the required interfaces through a private network and firewall.

Before first startup, set optional `VM_SYNC_START_TIMESTAMP` to the Unix-second
boundary from which each site should repair VictoriaMetrics history. If it is
unset, first startup begins only `VM_SYNC_LOOKBACK` minutes in the past (30 by
default). Preserve the site's `*_vm_sync_state` named volume thereafter.

The committed receiver file is a configuration-valid placeholder and does not
collect production data. Create a real receiver configuration, point
`OTEL_RECEIVERS_FILE` at it, and use file substitutions under
`/run/runtime-secrets` for credentials. Set `OTEL_SECRETS_DIR` to the dedicated
host directory that supplies those files; the collector no longer accepts a
shared plaintext device-secret environment file.

### 2. Create runtime secrets

```sh
umask 077
mkdir -p secrets/failover secrets/otel
chmod 700 secrets secrets/failover secrets/otel
openssl rand -base64 32 | tr -d '\n' > secrets/clickhouse_password.txt
openssl rand -base64 32 | tr -d '\n' > secrets/grafana_clickhouse_password.txt
openssl rand -base64 32 | tr -d '\n' > secrets/grafana_admin_password.txt
```

Keep the directory and files operator-owned (`0700` and `0600`). The custom
containers start through a narrow bootstrap wrapper that stages only each
service's granted secrets into a private in-container directory, then
permanently drops groups and user ID before launching the service. Do not make
the host files world-readable to accommodate container UIDs.

Set `OTEL_SECRETS_DIR=./secrets/otel` in `.env`. At minimum, install the files
referenced by the supplied collector configuration:

```sh
install -m 600 /secure-transfer/bigip-password \
  secrets/otel/bigip_password
install -m 600 /secure-transfer/datafabric-authorization \
  secrets/otel/datafabric_authorization
```

`datafabric_authorization` contains the complete header value
`kovacs <sensor-id> <token>`, not only the token. Keep unrelated application
and DNS-provider credentials out of this collector-only directory.

On DR, securely copy the primary site's ClickHouse password into the separate source credential:

```sh
install -m 600 /secure-transfer/primary-clickhouse-password \
  secrets/primary_clickhouse_password.txt
```

`grafana_clickhouse_password.txt` belongs to a separate, internal-only,
read-only ClickHouse user. Do not reuse the DR-local ClickHouse password as the
primary source password unless the sites intentionally share that credential.
Details are in [secrets/README.md](secrets/README.md).

### 3. Configure DNS failover

For a manual configuration, edit root `.env.primary` or `.env.dr` and replace
`DNS_PROVIDER=dry-run` with the production provider and its settings. Secret
settings support `NAME_FILE=/secrets/name`; do not put API tokens or passwords
directly in the environment file.

The optional interactive wizard can create both failover files and private provider-secret files:

```sh
python -m pip install -r failover/requirements.txt
python failover/setup.py
```

Accept `.` as the output directory. The wizard writes root `.env.primary`,
`.env.dr`, and provider credentials under `secrets/failover/`, which matches
the `FAILOVER_SECRETS_DIR` value in `.env.example`. Use one failover environment
location per host; a file under `failover/` has precedence when both exist.
When Google Cloud file credentials are selected, the wizard validates that the
source is a JSON object, copies it to
`secrets/failover/gcp_credentials.json` with mode `0600`, and writes the
container path `/secrets/gcp_credentials.json`. Choose Application Default
Credentials to leave that setting unset.

### 4. Preflight and validate

On primary:

```sh
sh services/preflight.sh docker-compose.primary.yaml
docker compose -f docker-compose.primary.yaml pull victoriametrics clickhouse
docker compose -f docker-compose.primary.yaml build --pull
docker compose -f docker-compose.primary.yaml run --rm --no-deps dns-failover validate
```

On DR:

```sh
sh services/preflight.sh docker-compose.dr.yaml
docker compose -f docker-compose.dr.yaml pull victoriametrics clickhouse
docker compose -f docker-compose.dr.yaml build --pull
docker compose -f docker-compose.dr.yaml run --rm --no-deps dns-failover validate
```

Preflight enforces Compose 2.24+, an amd64 Docker server, required secret files,
the dedicated collector and DNS-provider secret directories, the receiver
file, and the rendered Compose model. DNS validation checks provider-specific
settings, addresses, timing constraints, and health configuration. It does not
prove production connectivity to every external service.

### 5. Initialize the lease, then start primary

```sh
docker compose -f docker-compose.primary.yaml up -d --no-deps --wait \
  --wait-timeout 300 victoriametrics clickhouse
docker compose -f docker-compose.primary.yaml run --rm --no-deps dns-failover init
docker compose -f docker-compose.primary.yaml up -d
docker compose -f docker-compose.primary.yaml ps
```

Pull and build before creating the lease, then start only VictoriaMetrics and
ClickHouse and wait for both health checks. The collector, watcher, and DNS
controller remain absent during this preparation. Run `init` once, only after
confirming the record and IPs, and start the prebuilt, prepulled stack
immediately. A pull, build, or first-time storage initialization after `init`
can outlive the short lease. Neither role automatically renews its own lease
after it has expired past the skew allowance, because the peer may already be
promoting or reclaiming. Initialization must precede the collector because its
bootstrap gate refuses to ingest until DNS stably selects that site.

### 6. Start DR

After primary is healthy, start DR normally:

```sh
docker compose -f docker-compose.dr.yaml up -d --no-deps --wait \
  --wait-timeout 300 victoriametrics clickhouse
docker compose -f docker-compose.dr.yaml up -d
docker compose -f docker-compose.dr.yaml ps
```

The collector entrypoint itself requires both one matching A record and a
matching unexpired controller-published lease proof for several stable checks
before launching the collector binary. On standby it remains inert until the
watcher stops the container, preventing the first-start duplicate-scrape
window. The watcher starts independently of the DNS controller's Compose
health so it can fence the collector even when that controller is not ready.
Verify DR is stopped while DNS points to primary:

```sh
docker inspect -f '{{.State.Running}}' application-study-tool-otel-collector-1
docker logs --tail 50 otel-watcher
```

## Day-two commands

```sh
# Render status for the coherent A/TXT lease
docker compose -f docker-compose.primary.yaml exec dns-failover \
  python3 /app/dns_failover.py show

# Follow the control loops
docker compose -f docker-compose.primary.yaml logs -f dns-failover otel-watcher vm-sync
docker compose -f docker-compose.dr.yaml logs -f dns-failover otel-watcher vm-sync ch-sync

# Show container health
docker compose -f docker-compose.primary.yaml ps
docker compose -f docker-compose.dr.yaml ps

# Run the repository test suite
python -m unittest discover -s tests -v
```

The custom services expose `/healthz`, `/readyz`, and `/metrics` inside their containers. DNS failover and the watcher use `STATUS_PORT` (8080 by default); `vm-sync` and `ch-sync` use `HEALTH_PORT` (8080 by default). Compose health checks call `/readyz`.

## Important operational limits

- The Compose stack does not provision TLS for Grafana, VictoriaMetrics, ClickHouse, or cross-site sync. Use a private encrypted network or operator-managed TLS endpoints and CA files.
- Mounting `/var/run/docker.sock` gives the watcher host-level control. Restrict access to the host and the watcher image.
- A lookup failure, ambiguous A answer, or any single non-local answer immediately makes the watcher stop its collector; a complete stable sequence of local answers is required before a stopped collector can start.
- Each controller atomically publishes its last coherent provider A/TXT observation into the site failover-state volume. The watcher requires the recursive A answer to match that unexpired local proof, so a stale cached local A cannot remain trusted past the lease. Missing, malformed, mismatched, future-dated, or near-expiry proof causes an immediate stop; a Docker inspection/stop failure can still defeat fencing and must be alerted on.
- DNS read-after-write verification is not provider-side compare-and-set. Run exactly one controller for each role and use a DNS backend with atomic record-set updates where possible.
- A controller refuses to renew its own lease after that lease is expired past
  `MAX_CLOCK_SKEW`. Inspect authoritative DNS and both sites before a controlled
  `promote` or `failback`; never overwrite a peer that is active or changing
  ownership.
- `ch-sync` replaces differing MergeTree partitions. While a coherent DNS and
  lease proof selects DR, its active-destination guard stops normal
  primary-to-DR replacement. Reverse DR-to-primary recovery is mandatory
  before failback.
- The two Compose projects use fixed container names and host ports; they are intended for separate hosts.

For the recovery runbook, observability commands, and troubleshooting, see [USER_GUIDE.md](USER_GUIDE.md). For algorithms and configuration reference, see [TECHNICAL_DOCUMENTATION.md](TECHNICAL_DOCUMENTATION.md). Custom DNS integrations are covered in [SCRIPT_PROVIDER_GUIDE.md](SCRIPT_PROVIDER_GUIDE.md).
