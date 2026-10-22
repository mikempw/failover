# Operator guide

This guide covers secure deployment, normal operations, failover, and recovery.
Run the primary and DR roles on separate hosts with Docker Compose v2.24 or
newer; the manifests use `env_file.required`. The full and
external-replication stacks require Linux/amd64 because the pinned upstream
AST collector is not available for arm64. The standalone DNS-only controller
does not include that collector.

## Operating model

Only the site whose IPv4 address is the single A answer for `DNS_RECORD` should run the OpenTelemetry collector. Both sites run the same controls:

- `dns-failover` maintains or observes an A + TXT lease.
- `otel-watcher` resolves the A record and reconciles the local collector through the Docker socket.
- The active collector writes metrics to local and peer VictoriaMetrics.
- `vm-sync` pulls from the peer into local VictoriaMetrics to repair missed windows.
- DR `ch-sync` copies differing MergeTree partitions from primary to DR while primary is active.

The TXT value has the form `owner=primary exp=UNIX_TIME` or `owner=dr exp=UNIX_TIME`. The A address, owner, and configured site IP must agree. Controllers fail closed when that state is absent or incoherent.

An opt-in external-replication overlay replaces the three cross-site data
paths with local-only ingestion. It does not change DNS ownership or collector
fencing, and it does not add a replication-completeness check.

An independent DNS-only model runs just the lease controller. It does not run
or fence any application or data component. Use the following runbook for that
model; the later storage, collector, synchronization, and recovery procedures
apply only to the full or external-replication stacks.

## DNS-only control plane runbook

### Scope and external contract

`docker-compose.dns-only.primary.yaml` and
`docker-compose.dns-only.dr.yaml` each render exactly one `dns-failover`
service. They force their own `ROLE` and `DEPLOYMENT_MODE=dns-only`, retain the
site's existing failover-state volume, and mount only provider secrets and
custom DNS scripts. They contain no watcher, Docker socket, collector,
VictoriaMetrics, ClickHouse, Grafana, synchronization service, application
secret, or published port.

The controller still uses the same coherent A + TXT lease algorithm. DR can
promote only after repeated primary failures and lease expiry plus clock skew;
it must also receive HTTP 2xx from its external DR-readiness endpoint. What
changes is the ownership boundary:

- The external platform owns WireGuard, data copy, lag/RPO monitoring,
  reverse recovery, application lifecycle, and single-writer fencing.
- This repository owns only the managed DNS A/TXT records.
- The controller trusts configured HTTP 2xx responses. It does not examine
  data, WireGuard state, response bodies, or replication epochs.
- DNS TTL is not a connection drain. Cached clients can continue reaching the
  old address, so external fencing must tolerate that overlap.

The fixed `dns-failover` container name means primary and DR are intended for
separate hosts. Run exactly one controller per role and keep reliable time
synchronization enabled on both.

### Configure each host

Create a small root Compose environment and one role-specific controller file:

```sh
cp .env.dns-only.example .env

# Primary host
cp failover/.env.primary.example .env.primary

# DR host instead
# cp failover/.env.dr.example .env.dr

mkdir -p secrets/failover
chmod 700 secrets secrets/failover
```

Replace every TEST-NET address and `example.test` value. In `.env.primary` or
`.env.dr`, configure `DNS_PROVIDER`, the zone/record, primary and DR IPs,
lease timing, and provider-specific settings. Store secret values in private
files under `secrets/failover` and reference them with `NAME_FILE=/secrets/name`
where supported.

Root `.env` supplies host-side Compose interpolation, not controller
`env_file` values. Configure:

- `DNS_ONLY_PRIMARY_HEALTH_URL`: direct primary availability URL. Both roles
  use it; it must not resolve through the managed `DNS_RECORD`.
- `DNS_ONLY_DR_HEALTH_URL`: direct DR traffic-readiness URL. DR must return 2xx
  before promotion and while it renews ownership.
- `DNS_ONLY_PRIMARY_FAILBACK_HEALTH_URL`: separate direct primary recovery
  gate. The primary uses it before reclaiming from DR. If automatic failback
  is enabled, DR also uses it before yielding.
- `FAILOVER_SECRETS_DIR`: host provider-secret directory.

The recovery URL is intentionally distinct from normal primary health. It may
be backed by an external adapter that verifies restored data and fencing, but
the repository cannot tell whether it does. Keep `AUTO_FAILBACK=false` unless
that contract is trustworthy.

### Validate and start a fresh deployment

On primary, validate configuration and all external gates used by that role,
then create
the initial lease once:

```sh
sh services/preflight.sh docker-compose.dns-only.primary.yaml
docker compose -f docker-compose.dns-only.primary.yaml build --pull
docker compose -f docker-compose.dns-only.primary.yaml run --rm --no-deps \
  dns-failover validate
docker compose -f docker-compose.dns-only.primary.yaml run --rm --no-deps \
  dns-failover probe
docker compose -f docker-compose.dns-only.primary.yaml run --rm --no-deps \
  dns-failover init
sh services/activate-dns-only.sh primary
```

`init` writes immediately, so run it only after verifying the target record,
both addresses, and externally managed primary readiness. On DR, do not run
`init`:

```sh
sh services/preflight.sh docker-compose.dns-only.dr.yaml
docker compose -f docker-compose.dns-only.dr.yaml build --pull
docker compose -f docker-compose.dns-only.dr.yaml run --rm --no-deps \
  dns-failover validate
docker compose -f docker-compose.dns-only.dr.yaml run --rm --no-deps \
  dns-failover probe
sh services/activate-dns-only.sh dr
```

For a fresh deployment or full-stack transition, activation repeats the
non-writing `probe` inside a one-off controller container, starts the
controller, and waits for Compose health. A controller's
`/readyz` reflects a recent successful loop iteration, not ownership or data
completeness. On DR while primary is healthy, that loop does not exercise the
DR-local or failback URL; the explicit `probe` covers those endpoints once at
activation, and the loop checks each gate again when its ownership phase needs
it. An idempotent restart of an existing DNS-only project skips the all-gates
probe because primary availability and recovery are expected to fail during a
real outage; the controller still enforces the gate appropriate to its current
lease phase. If a DR controller must be created from scratch during an active
primary outage, the explicit
`DNS_ONLY_SKIP_PROBE=START_DURING_OUTAGE` override skips only the one-shot
all-gates probe and instead runs `probe-dr`, which still requires the direct DR
readiness URL to return 2xx. The override is rejected for the primary role.
DNS-only validation and normal failover gates still apply.

### Migrate a running repository stack

A standalone `docker compose -f docker-compose.dns-only.*.yaml up -d` does not
remove containers created by the old full manifest. In particular, a leftover
`otel-watcher` can continue manipulating the collector. Use the activation
helper.

First move all data-plane responsibility outside this Compose project and
confirm the external endpoints are ready. Preserve the original Compose
project identity. If the old deployment used `-p` or `COMPOSE_PROJECT_NAME`,
pass the same `-p` option; replacing root `.env` may otherwise drop that value:

```sh
# Prepare the DNS controller image before changing the running lease or stack.
docker compose -f docker-compose.dns-only.primary.yaml build --pull

# Discover the current project name if needed.
docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' \
  dns-failover

# Example primary transition with the default project name.
DNS_ONLY_CONFIRM=RETIRE_REPOSITORY_STACK \
  sh services/activate-dns-only.sh primary

# Example when the existing project used a custom name.
DNS_ONLY_CONFIRM=RETIRE_REPOSITORY_STACK \
  sh services/activate-dns-only.sh primary -p ast-primary-custom
```

Use `dr` on the DR host. The helper validates the exact one-service model,
refuses a fixed-name container owned by a different project, probes all
configured external gates, and establishes a healthy DNS-only controller
before touching the old stack. With the confirmation present, it stops the
Docker-socket watcher first, retires writers and storage, then removes every
other same-project non-DNS or one-off container—including services added by a
local override—and preserves named volumes. It then force-recreates the
controller from a fresh readiness state and requires exactly one healthy,
running, non-one-off `dns-failover` container in the project.

Without `DNS_ONLY_CONFIRM=RETIRE_REPOSITORY_STACK`, any old non-DNS or one-off
container makes activation fail without changing it. The confirmation removes
containers and interrupts the old repository data plane; it does not delete
named volumes. Do not give that confirmation until the external owner has
taken over.

### Operate and switch

Inspect the authoritative lease with a disposable read-only command:

```sh
docker compose -f docker-compose.dns-only.primary.yaml run --rm --no-deps \
  dns-failover show
docker compose -f docker-compose.dns-only.primary.yaml ps
docker compose -f docker-compose.dns-only.primary.yaml logs dns-failover
```

Use the matching DR manifest on that host. Also monitor the authoritative A
and TXT answers, lease-owner/expiry metrics, the external replication system,
and both sites' fencing state. Do not interpret `/readyz` as proof that this
site owns DNS or that its data is current.

Normal automatic failover should be preferred. Manual `promote` and `failback`
write the lease immediately; `init` overwrites it with primary ownership. All
three commands bypass failure thresholds, lease-expiry waits, and health
checks. Treat them as break-glass DNS changes and independently verify the
external data/fencing contract first.

## Prepare each site

### Local files

Create ignored working copies:

```sh
cp .env.example .env
```

On primary:

```sh
cp failover/.env.primary.example .env.primary
```

On DR:

```sh
cp failover/.env.dr.example .env.dr
```

Compose also accepts the legacy files under `failover/`, but use only one
location per host. A `failover/.env.*` file is loaded last and overrides a root
file when both exist.

The committed examples use TEST-NET addresses, `example.test`, a placeholder BIG-IP receiver, and `DNS_PROVIDER=dry-run`. They are safe examples, not production settings.

Enable reliable time synchronization on both hosts before deployment. Lease
expiry and local ownership-proof validation use absolute Unix timestamps;
`MAX_CLOCK_SKEW` is a bounded guard, not a substitute for synchronized clocks.

### Root `.env`

Set at least:

- `BIND_ADDRESS`: interface used for published ports. `127.0.0.1` is local-only.
- `AST_NETWORK_SUBNET`: non-overlapping private subnet used for the local
  Compose network and internal ClickHouse allowlist.
- `CLICKHOUSE_REMOTE_CIDRS`: narrow peer/recovery CIDRs allowed to use the
  main ClickHouse account. Primary must allow the DR synchronization source.
- `PRIMARY_VM_URL`, `DR_VM_URL`, and their write URLs.
- `VM_SYNC_START_TIMESTAMP`: optional Unix seconds for the oldest
  VictoriaMetrics history to copy when the watermark does not yet exist.
- `PRIMARY_CLICKHOUSE_HOST` and related scheme/port/user settings on DR.
- `OTEL_RECEIVERS_FILE`: path to a real receiver YAML file.
- `OTEL_SECRETS_DIR`: dedicated host directory containing only collector
  secret files referenced through `/run/runtime-secrets`.
- Password-file paths if you do not use the defaults under `./secrets`.

Cross-site VictoriaMetrics and ClickHouse endpoints must be reachable through a protected network. The supplied Compose files do not configure TLS. If you use TLS endpoints, set the synchronizers' `https` scheme and CA-file variables and mount the CA files with a local Compose override.

Choose `VM_SYNC_START_TIMESTAMP` before the first startup if more than the
default 30-minute `VM_SYNC_LOOKBACK` must be recovered. The value is also the
deterministic restart anchor if a watermark volume must be recreated. Preserve
the `primary_vm_sync_state` and `dr_vm_sync_state` named volumes during routine
upgrades; deleting either discards that site's progress watermark and causes a
replay from the configured start timestamp, or only from the current lookback
when the timestamp is unset.

The peer VictoriaMetrics values, `VM_SYNC_START_TIMESTAMP`, primary ClickHouse
source settings, and `primary_clickhouse_password.txt` are needed by the
built-in replication mode. They are not consumed by this repository when the
external-replication overlay is active. The external replicator may still need
its own network allowlists and credentials.

### External replication mode

Use this mode only if a system other than WireGuard copies both VictoriaMetrics
and ClickHouse data. WireGuard authenticates and encrypts peers, but does not
replicate application or storage data.

The external mode makes these changes:

- The collector writes only to local VictoriaMetrics and local ClickHouse.
- `vm-sync` is disabled at both sites.
- `ch-sync` is disabled at DR.
- DNS failover does not check external replica lag or completeness.

Activate each site with the transition helper:

```sh
sh services/activate-external-replication.sh primary  # on primary
sh services/activate-external-replication.sh dr       # on DR
```

If the site normally uses additional Compose options, append them to the
helper command, for example:

```sh
sh services/activate-external-replication.sh primary \
  -f docker-compose.local.yaml -p ast-primary-custom
```

The same options are used for validation, project-label cleanup, and startup,
so local mounts, environment, and services are not discarded. The external
safety overlay is applied last so a local override cannot re-enable a sync
path. The helper refuses to proceed if any `otel-collector` one-off still
exists, including a stopped one; remove that one-off first.

The helper validates first, then stops and removes any existing `vm-sync` and
`ch-sync` containers without deleting their named state volumes. It starts
the merged external model without removing unrelated services and verifies
those sync containers—including detached Compose one-offs—are absent both
before and after startup. Do not migrate a
running site with a plain overlay `up -d`: a sync container created by the base
model can otherwise retain its old environment and continue copying.

Use the same two-file form for every later `pull`, `build`, `run`, `up`,
`stop`, and `down` command. A base manifest used alone restores built-in sync.
Do not explicitly enable the `builtin-replication-disabled` profile with an
external overlay; the sync executables also refuse to start when
`DATA_REPLICATION_MODE=external`.

Before production use, define the acceptable recovery point and independently
alert on external replication lag and errors. If the external system performs
block or volume replication, it must also guarantee that only one copy is
writable and mounted. This stack fences the collector, not the storage engine.
Promotion remains availability-first and may expose stale data. Keep
`AUTO_FAILBACK=false`; do not fail back until the external system has copied
DR writes to primary and an operator has validated both data stores.

### BIG-IP receiver secrets

Set `OTEL_SECRETS_DIR=./secrets/otel` and place only collector credentials in
that ignored, operator-owned directory. The bootstrap gate copies its regular
files into `/run/runtime-secrets`, rejects symbolic links, and sets files to
mode `0400`. It likewise validates and privately stages `OTEL_RECEIVERS_FILE`
as `/run/runtime-config/receivers.yaml`, then permanently drops to collector
UID 10001. Reference secrets from receiver YAML with the collector's file substitution, for example
`${file:/run/runtime-secrets/bigip_password}`. Do not put device credentials in
environment variables.

The supplied configuration also reads
`/run/runtime-secrets/datafabric_authorization`. Its production content is the
complete `kovacs <sensor-id> <token>` authorization header, not just the token.
Install both files from a protected source:

```sh
umask 077
mkdir -p secrets/otel
chmod 700 secrets secrets/otel
install -m 600 /secure-transfer/bigip-password \
  secrets/otel/bigip_password
install -m 600 /secure-transfer/datafabric-authorization \
  secrets/otel/datafabric_authorization
```

The placeholder `services/otel_collector/receivers.example.yaml` points at a TEST-NET address, uses a placeholder username, and disables the sample data types. Replace it before expecting telemetry.

### Application passwords

Create independent secrets with no trailing newline:

```sh
umask 077
mkdir -p secrets/failover secrets/otel
chmod 700 secrets secrets/failover secrets/otel
openssl rand -base64 32 | tr -d '\n' > secrets/clickhouse_password.txt
openssl rand -base64 32 | tr -d '\n' > secrets/grafana_clickhouse_password.txt
openssl rand -base64 32 | tr -d '\n' > secrets/grafana_admin_password.txt
```

Keep `secrets/` mode `0700` and its files mode `0600`, owned by the deployment
operator. The custom images briefly bootstrap as root to stage only their
granted bind-mounted secrets into private runtime paths, then permanently drop
supplementary groups, group ID, and user ID before executing the service. Do
not loosen the host files to make the image's runtime UID read them directly.

DR also needs a credential that can read primary ClickHouse:

```sh
install -m 600 /secure-transfer/primary-clickhouse-password \
  secrets/primary_clickhouse_password.txt
```

The DR-local password belongs in `clickhouse_password.txt`; the copied primary
password belongs in `primary_clickhouse_password.txt`.
`grafana_clickhouse_password.txt` is intentionally separate: the ClickHouse
entrypoint creates `GRAFANA_CLICKHOUSE_USER` with an internal-only, read-only
profile, and Grafana uses that secret for its datasource.

### DNS provider secrets

Edit `.env.primary` and `.env.dr` with the same zone, record, site IPs, lease
timing, and provider account. Set each site's `ROLE` correctly.

Use `NAME_FILE=/secrets/name` for provider secrets. The copied root `.env` sets
`FAILOVER_SECRETS_DIR=./secrets/failover`; Compose mounts only that directory at
a read-only bootstrap path, and the secure entrypoint copies its files into the
private runtime `/secrets` directory before dropping privileges. Never set both
`NAME` and `NAME_FILE`. Keep application passwords outside this provider-only
directory.

The setup wizard can generate both files:

```sh
python -m pip install -r failover/requirements.txt
python failover/setup.py
```

Accept `.` as its output directory. This creates root `.env.primary`,
`.env.dr`, and provider files under `secrets/failover/`, matching the paths
loaded and mounted by Compose. `FAILOVER_SECRETS_DIR` remains available when an
operator deliberately keeps provider secrets elsewhere. For Google Cloud file
credentials, the wizard validates a JSON object, copies it to
`secrets/failover/gcp_credentials.json` with mode `0600`, and stores
`GCP_CREDENTIALS_FILE=/secrets/gcp_credentials.json`; selecting Application
Default Credentials leaves the setting unset.

For BIND, put the TSIG key in the mounted secret directory and use `TSIG_KEYFILE=/secrets/tsig.key`. For a custom backend, follow [SCRIPT_PROVIDER_GUIDE.md](SCRIPT_PROVIDER_GUIDE.md).

## Health configuration

`HEALTH_MODE` controls the service check used by the DNS controller:

| Mode | Settings | Meaning |
|---|---|---|
| `tcp` | `HEALTH_HOST`, `HEALTH_PORT` | A TCP connection succeeds. |
| `http` | `HEALTH_URL` | A 2xx response succeeds. |
| `metrics` | `HEALTH_URL`, `HEALTH_METRIC`, `HEALTH_STALE_COUNT` | The exact Prometheus counter advances; a counter reset is healthy. Repeated flat values become unhealthy. |

`HEALTH_*` represents the active primary service: DR uses it to detect failure
while primary owns the lease, and primary uses it while renewing primary
ownership. When DR owns the lease, the primary controller instead requires a
2xx response from its local `FAILBACK_HEALTH_URL` for `FAILBACK_THRESHOLD`
consecutive checks. If that URL is unset or unhealthy, automatic reclamation
fails closed. The committed primary example uses local VictoriaMetrics health
so recovery can be observed while the primary collector is intentionally
stopped.

While primary owns a lease that has not expired past the skew allowance, it
continues using the normal `HEALTH_*` probe. Once its own lease is already
expired beyond that guard, it reports `expired-self-blocked` and makes no
automatic write because DR may be promoting.

The same setting has a distinct per-site value. DR consults its DR-visible
value only while DR owns the lease and `AUTO_FAILBACK=true`; a successful check
makes DR yield rather than renew. Use an endpoint on the primary recovery path
that is actually reachable from DR.

```dotenv
# .env.primary: local to the primary controller
FAILBACK_HEALTH_URL=http://victoriametrics:8428/health

# .env.dr: primary recovery as seen from DR
FAILBACK_HEALTH_URL=http://192.0.2.10:8428/health
AUTO_FAILBACK=false
```

On DR, set `LOCAL_HEALTH_URL` to a DR-local backing service such as `http://victoriametrics:8428/health`. When configured, DR refuses promotion if that URL is not ready.

Recommended lease relationship:

```text
LEASE_TTL > UPDATE_INTERVAL + (2 * MAX_CLOCK_SKEW)
```

Configuration validation enforces this condition.

## Validate before deployment

Primary:

```sh
sh services/preflight.sh docker-compose.primary.yaml
docker compose -f docker-compose.primary.yaml pull victoriametrics clickhouse
docker compose -f docker-compose.primary.yaml build --pull
docker compose -f docker-compose.primary.yaml run --rm --no-deps dns-failover validate
```

DR:

```sh
sh services/preflight.sh docker-compose.dr.yaml
docker compose -f docker-compose.dr.yaml pull victoriametrics clickhouse
docker compose -f docker-compose.dr.yaml build --pull
docker compose -f docker-compose.dr.yaml run --rm --no-deps dns-failover validate
```

If secret paths are supplied to the shell rather than using the defaults, export those path variables before calling `services/preflight.sh`. Compose itself also reads root `.env`.

Preflight rejects Compose older than 2.24, a non-amd64 Docker server, missing
application secrets, missing dedicated collector/provider secret directories,
an absent receiver file, or an invalid rendered Compose model.

## Bootstrap primary

Initialize the lease exactly once, before starting the gated collector, and
then start primary:

```sh
docker compose -f docker-compose.primary.yaml up -d --no-deps --wait \
  --wait-timeout 300 victoriametrics clickhouse
docker compose -f docker-compose.primary.yaml run --rm --no-deps dns-failover init
docker compose -f docker-compose.primary.yaml up -d
docker compose -f docker-compose.primary.yaml exec dns-failover \
  python3 /app/dns_failover.py show
```

The first command starts only storage and waits for both backends to be
healthy; it does not start ingestion or either control loop. Confirm that the
final result contains primary's address, `owner: primary`, and a future expiry.
Images and upstream layers must already be built and pulled by the validation
step. Start the full stack promptly after `init`; do not add a pull or build.
If the initial lease expires during image work or first-time storage
initialization, primary intentionally refuses automatic renewal because DR
could already be promoting.

## Bootstrap DR

After primary is healthy, start the complete DR stack:

```sh
docker compose -f docker-compose.dr.yaml up -d --no-deps --wait \
  --wait-timeout 300 victoriametrics clickhouse
docker compose -f docker-compose.dr.yaml up -d
```

The collector bootstrap gate independently requires one A record equal to the
role-derived local address plus a matching unexpired
`/state/observed-lease.json` proof for `OTEL_BOOTSTRAP_STABLE_CHECKS`
observations. On standby it never starts the collector binary, and the watcher
then stops the container. This removes the first-start duplicate-scrape
window and refuses a stale cached A whose provider-observed lease has expired.
The watcher has no controller-health dependency in Compose, so it begins
fencing independently even when `dns-failover` is not ready.

Verify the collector is stopped while DNS points to primary:

```sh
docker inspect -f '{{.State.Running}}' application-study-tool-otel-collector-1
docker logs --tail 100 otel-watcher
```

The expected inspect result is `false`.

## Observe the system

### Container and lease status

```sh
docker compose -f docker-compose.primary.yaml ps
docker compose -f docker-compose.dr.yaml ps

docker compose -f docker-compose.primary.yaml exec dns-failover \
  python3 /app/dns_failover.py show
```

`show` is intentionally strict. Failure means the controller could not obtain one coherent lease; investigate instead of forcing promotion.

### Logs

```sh
docker compose -f docker-compose.primary.yaml logs -f \
  dns-failover otel-watcher vm-sync

docker compose -f docker-compose.dr.yaml logs -f \
  dns-failover otel-watcher vm-sync ch-sync
```

### Service endpoints

The four Python control services expose:

- `/healthz`: process loop liveness
- `/readyz`: a successful operation occurred recently
- `/metrics`: Prometheus text metrics

`dns-failover` and `otel-watcher` use `STATUS_PORT`; `vm-sync` and `ch-sync` use `HEALTH_PORT`. All default to 8080. These ports are used inside the containers and are not generally published by Compose. Query one with its bundled Python runtime:

```sh
docker exec dns-failover python3 -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/readyz').read().decode())"

docker exec vm-sync python3 -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/metrics').read().decode())"
```

Platform checks used by Compose are:

```sh
curl -fsS http://127.0.0.1:8428/health
curl -fsS http://127.0.0.1:8123/ping
curl -fsS http://127.0.0.1:3000/api/health
curl -fsS http://127.0.0.1:8888/metrics >/dev/null
```

Adjust the host address if `BIND_ADDRESS` is not loopback.

## What happens during failover

1. DR reads and strictly validates the provider's A and TXT lease.
2. While primary owns it, DR records consecutive primary health failures.
3. After `FAIL_THRESHOLD`, DR waits until the primary lease has expired beyond
   `MAX_CLOCK_SKEW`.
4. If `LOCAL_HEALTH_URL` is configured, DR checks it before promotion.
5. DR writes its A address plus a DR TXT lease and verifies the readback.
6. A stopped watcher requires one valid local A address, a matching unexpired
   controller-published proof, and `DNS_CONSISTENT_ANSWERS` identical
   observations before starting its collector.
7. The DR watcher starts its collector; the primary watcher stops its collector.
8. A locally healthy DR controller renews a DR-owned lease. With the default
   `AUTO_FAILBACK=false`, it continues renewing even if primary later appears
   healthy; `AUTO_FAILBACK=true` allows it to yield for automatic return. If
   its own lease has already expired past the skew allowance, it refuses to
   renew because primary may be reclaiming.

DNS lookup failures, zero or multiple A answers, and malformed addresses cause
the watcher to stop the collector immediately. Any observed single non-local A
also stops it immediately. A local A observation remains pending until
`DNS_CONSISTENT_ANSWERS` identical results have accumulated, and cannot start a
stopped collector before then. Docker inspection errors or a missing container
can prevent a requested fence; they are reported and retried with bounded
exponential backoff.

The local DNS controller also atomically publishes every coherent provider
A/TXT observation to `/state/observed-lease.json`. The watcher accepts a
recursive A answer only when that proof names the same record and address, has
a coherent owner/site mapping, is not future-dated, and remains valid beyond
the `MAX_CLOCK_SKEW` guard. A missing, malformed, mismatched, expired, or
near-expiry proof causes an immediate stop. This prevents a stale single local
A cache from being trusted after its lease, provided the watcher can inspect
and stop Docker; alert on any fencing failure.

`promote` is an operator override that writes DR ownership without the health and expiry gates. Use it only in a controlled recovery:

```sh
docker compose -f docker-compose.dr.yaml exec dns-failover \
  python3 /app/dns_failover.py promote
```

## Recover primary and fail back

Do not switch DNS back merely because the primary host boots. VictoriaMetrics must catch up and ClickHouse data created on DR must be copied back first.

With the default `AUTO_FAILBACK=false`, a healthy DR site continuously renews
its still-valid lease even after primary recovery, so it remains authoritative
until this runbook's operator failback. If the DR controller or its local
backing service fails and its lease expires, primary may reclaim after
`FAILBACK_HEALTH_URL` meets the healthy-streak threshold. Keep primary
`dns-failover` stopped during recovery unless that readiness endpoint is itself
a deliberate data-recovery gate.

### 1. Bulk-recover while DR remains active

Leave DR `dns-failover`, its collector, and its storage running. On primary,
stop every component that could reclaim DNS or start ingestion, then start only
storage and the VictoriaMetrics repair job:

```sh
docker compose -f docker-compose.primary.yaml stop \
  dns-failover otel-watcher otel-collector
docker compose -f docker-compose.primary.yaml up -d --build \
  victoriametrics clickhouse grafana vm-sync
```

Primary `vm-sync` pulls from DR using its durable watermark. Wait for a successful run and inspect lag:

```sh
docker logs --tail 100 vm-sync
docker exec vm-sync python3 -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/metrics').read().decode())"
```

Do not continue until `vm_sync_ready` is 1 and `vm_sync_lag_seconds` is acceptable for your recovery point objective.

### 2. Enter a final ingestion freeze

A reverse copy performed while DR continues writing has a post-copy loss
window. Before the final synchronization, stop the DR watcher first so it
cannot restart the collector, then stop the collector gracefully:

```sh
docker compose -f docker-compose.dr.yaml stop otel-watcher
docker compose -f docker-compose.dr.yaml stop otel-collector
docker inspect -f '{{.State.Running}}' application-study-tool-otel-collector-1
```

The inspect result must be `false`. DR `dns-failover` must remain running so
it continues to own and renew the lease. New telemetry is not collected during
this cutover window.

Stop the scheduled primary repair loop and run one final VictoriaMetrics pass
against the now-quiescent DR site:

```sh
docker compose -f docker-compose.primary.yaml stop vm-sync
docker compose -f docker-compose.primary.yaml run --rm --no-deps \
  vm-sync python3 -u /app/vm_sync.py --once
```

Do not proceed if the one-shot command exits nonzero.

### 3. Run the final reverse ClickHouse sync

Normal DR `ch-sync` is primary-to-DR. Its DNS-and-lease-proof guard skips
replacement while the DR address is active, which protects newly ingested DR
partitions from an older primary. Stop the long-running job, then run one
reverse pass from the DR host. Set the guard destination to primary so the
same guard protects the reverse-copy destination:

```sh
docker compose -f docker-compose.dr.yaml stop ch-sync

docker compose -f docker-compose.dr.yaml run --rm --no-deps \
  -e SOURCE_SCHEME=http \
  -e SOURCE_HOST=clickhouse \
  -e SOURCE_PORT=8123 \
  -e SOURCE_USER=ast \
  -e SOURCE_PASSWORD_FILE=/run/secrets/clickhouse_password \
  -e DEST_SCHEME=http \
  -e DEST_HOST=PRIMARY_CLICKHOUSE_ADDRESS \
  -e DEST_PORT=8123 \
  -e DEST_USER=ast \
  -e DEST_PASSWORD_FILE=/run/secrets/primary_clickhouse_password \
  -e SYNC_GUARD_DNS_RECORD=FAILOVER_FQDN \
  -e SYNC_GUARD_DEST_IP=PRIMARY_SITE_IP \
  -e SYNC_GUARD_LEASE_PROOF_FILE=/failover-state/observed-lease.json \
  ch-sync python3 /app/ch_sync.py --once
```

Replace `PRIMARY_CLICKHOUSE_ADDRESS`, `FAILOVER_FQDN`, and `PRIMARY_SITE_IP`
with the primary ClickHouse address, `DNS_RECORD`, and exact `PRIMARY_IP` from
the DR configuration, plus any non-default user, scheme, port, or CA settings.
The service's normal DR failover-state mount supplies the proof file. The guard
remains enabled: it refuses an incoherent/expired DNS proof and rechecks before
each live partition replacement, aborting if the primary destination becomes
active during the copy. `SYNC_ALLOW_ACTIVE_DESTINATION` has been removed and
any non-empty value is rejected.

Run another one-shot pass and validate important row counts/queries at both sites. Because DR ingestion is stopped, a successful final pass closes the normal post-sync write window. The synchronizer compares partition row counts; it cannot detect different contents with identical counts, schema drift in an existing table, or destination-only partitions. Resolve those cases manually before failback.

The source-tree helper `ch-sync/ch_sync_recovery.sh` requires
`CH_SYNC_RECOVERY_CONFIRM=REPLACE_DESTINATION_PARTITIONS` for direct,
non-Compose execution, derives the protected destination from `PRIMARY_IP`,
and preserves the same DNS/proof checks. Its human confirmation does not bypass
fencing.

If either final sync or validation fails, abort the cutover and restore DR
ingestion while DNS still points to DR:

```sh
docker compose -f docker-compose.dr.yaml up -d otel-collector otel-watcher
```

### 4. Switch the lease only after validation

Start the primary controls and gated collector only after both final syncs pass,
then issue the operator failback:

```sh
docker compose -f docker-compose.primary.yaml up -d --no-deps \
  dns-failover otel-collector otel-watcher
docker compose -f docker-compose.primary.yaml exec dns-failover \
  python3 /app/dns_failover.py failback
```

Before the DNS write, the primary bootstrap gate keeps its collector inert.
`failback` is an override and writes primary ownership immediately. The gate
and watcher then start primary only after stable primary ownership. Watch both
sites until primary is healthy and DR remains stopped:

```sh
docker logs -f otel-watcher
docker inspect -f '{{.State.Running}}' application-study-tool-otel-collector-1
```

Those commands must be run on each respective host because the fixed container name is the same.

### 5. Resume controls and normal replication

Once DNS consistently resolves only to primary:

```sh
docker compose -f docker-compose.primary.yaml up -d vm-sync
docker compose -f docker-compose.dr.yaml up -d otel-watcher ch-sync
```

The restarted DR watcher confirms the already-stopped DR collector remains
inactive. DR `ch-sync` now sees an inactive DR destination and resumes
primary-to-DR synchronization.

## Automatic failback

When primary reads a DR-owned lease, it ignores the normal collector-oriented
`HEALTH_*` probe and requires `FAILBACK_HEALTH_URL` for
`FAILBACK_THRESHOLD` consecutive checks. A missing or unhealthy URL blocks
automatic reclaim. Manual `failback` remains an explicit override.

- `AUTO_FAILBACK=false` (default): a locally healthy DR controller retains and
  renews its still-valid lease even when primary health returns. Primary can
  reclaim after that lease expires, such as after DR controller or
  DR-local-health loss; a restarted DR will not renew an already expired
  self-owned lease. Alternatively, an operator can complete the recovery
  runbook and issue `failback`.
- `AUTO_FAILBACK=true`: while DR owns the lease, it uses its DR-visible
  `FAILBACK_HEALTH_URL` instead of the normal failure-detection probe and yields
  rather than renewing once that recovery check succeeds. Primary separately
  requires its local failback-readiness streak and waits for the DR lease to
  expire beyond `MAX_CLOCK_SKEW`; it never automatically overwrites an
  unexpired DR lease.

Only the operator `failback` command can deliberately replace an unexpired DR
lease, which is why it belongs at the end of the validated recovery runbook.

The example `FAILBACK_HEALTH_URL` values prove only that primary
VictoriaMetrics is up from each controller's viewpoint; they do not prove
VictoriaMetrics catch-up or reverse ClickHouse recovery. Do not enable
`AUTO_FAILBACK` unless the DR-visible recovery endpoint is gated on those
recovery steps, and use an equivalent composite readiness gate for the
primary-local value when automatic reclamation must wait for them. The DNS
loop has no knowledge of replication completeness.

## Troubleshooting

### Preflight reports a missing secret

Create the file named in the message, ensure it has at least 16 characters, and
make it readable only by the deployment operator. Primary requires the local
ClickHouse, Grafana ClickHouse, and Grafana admin files. DR additionally
requires the primary ClickHouse source credential.

If preflight reports a missing dedicated directory, set `OTEL_SECRETS_DIR` and
`FAILOVER_SECRETS_DIR` to existing operator-owned `0700` directories. Preflight
checks that each directory exists; the collector itself will reject startup if
a file referenced by its YAML, such as `bigip_password` or
`datafabric_authorization`, is absent.

### DNS controller is not ready

Run `validate`, then `show`. Check that both A and TXT exist, the TXT owner is `primary` or `dr`, the expiry is an integer, and the A address matches that owner. Check provider permissions and mounted `_FILE` paths.

### A controller reports `expired-self-blocked`

This is deliberate split-brain protection. Neither primary nor DR extends its
own lease after it is already expired beyond `MAX_CLOCK_SKEW`: DR may be
promoting an expired primary lease, or primary may be reclaiming an expired DR
lease. Do not immediately run `init`, `promote`, or `failback`.

First inspect authoritative A/TXT state through the DNS provider API (or the
strict `show` command) and inspect both sites' controller, watcher, and
collector state:

```sh
# Run the matching commands on each site/host.
docker compose -f docker-compose.primary.yaml run --rm --no-deps dns-failover show
docker compose -f docker-compose.primary.yaml ps \
  dns-failover otel-watcher otel-collector
docker compose -f docker-compose.primary.yaml logs --tail 100 \
  dns-failover otel-watcher
```

Use `docker-compose.dr.yaml` on DR. If authoritative state shows peer ownership,
or the peer may be promoting or reclaiming, the blocked controller must not
overwrite it. For a brand-new primary bootstrap only, when DR has never been
started and is confirmed unable to promote, rerun `init` and immediately start
the already-built primary stack with `up -d`.

In any existing deployment, coordinate both sites and fence the site that will
remain passive. To select primary, first verify DR is not active or reclaiming,
resolve whether DR accepted writes, complete the recovery runbook, and then use
the explicit operator `failback`. To select DR, verify primary is unavailable,
not active, and not reclaiming; verify DR-local readiness and data state; then
use the controlled `promote` override. These commands bypass the continuous
loop's health and expiry gates, so never use either while peer ownership is
uncertain.

### Watcher is not ready

Check its logs for resolver failures, DNSSEC AD-flag failures, multiple A
answers, unstable answers, Docker socket errors, or a missing
`OTEL_CONTAINER`. The watcher tries configured `DNS_SERVERS` in order, then the
system resolver only when allowed. An uncertain answer cannot start a
collector. An unresolved or ambiguous answer, or any single non-local answer,
causes an immediate stop; a pending local answer waits for the consistency
threshold. Investigate any log saying that fencing failed.

### `ch-sync` says it is skipping

That is expected while a coherent A record and local proof show DR ownership.
If guard DNS cannot be resolved to exactly one address, or the mounted lease
proof is absent, stale, or inconsistent, synchronization fails closed. Never
disable the guard. The controlled reverse pass changes the protected
destination to `PRIMARY_IP` while retaining DNS/proof fencing; the helper's
exact recovery confirmation authorizes the direction change, not a bypass.

### A synchronizer is unhealthy

Inspect `/metrics` and logs. `vm-sync` does not advance its watermark on a partial or failed catch-up. `ch-sync` copies into a staging table, verifies row count, and only then issues `REPLACE PARTITION`; a failed copy leaves the live partition unchanged.

### Peer connections fail

Confirm `BIND_ADDRESS`, routing, firewall rules, service credentials,
`CLICKHOUSE_REMOTE_CIDRS`, and URL scheme. The defaults are HTTP and are
appropriate only on a trusted encrypted network. When using HTTPS, mount the
correct CA and set `SOURCE_CA_FILE`/`DEST_CA_FILE`.
