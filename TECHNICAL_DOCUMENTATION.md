# Technical documentation

## Scope

This repository implements active/passive AST collection across a primary and DR site. DNS is the control plane: a single A record selects the active collector, and a TXT record carries an expiring ownership lease. The design favors fail-closed behavior over availability when control-plane state is ambiguous.

The supplied deployment targets Linux/amd64. Its pinned upstream AST collector
image does not provide an arm64 variant.

This is not an active/active database system, a distributed consensus implementation, or a backup product. It coordinates collector activity and provides best-effort cross-site repair for VictoriaMetrics and ClickHouse.

## Components

| Component | Sites | Responsibility |
|---|---|---|
| VictoriaMetrics | Both | Metrics storage and remote-write target. |
| ClickHouse | Both | AST analytics plus OTLP log/trace storage. |
| Grafana | Both | Dashboards with provisioned VictoriaMetrics and ClickHouse datasources. |
| `otel-collector` | Both | BIG-IP metrics collection, OTLP ingestion, local ClickHouse export, and dual VictoriaMetrics remote write. |
| `dns-failover` | Both | A/TXT lease management and primary-health decisions. |
| `otel-watcher` | Both | DNS-driven start/stop reconciliation for the local collector. |
| `vm-sync` | Both | Watermark-based VictoriaMetrics gap repair from peer to local. |
| `ch-sync` | DR | Partition repair from primary to DR, with an active-DR guard. |

The Compose projects require Docker Compose v2.24 or newer because they use
`env_file.required`. They use fixed container names and are intended for
separate hosts.
Each stack loads its committed site example first, then an optional root
`.env.primary`/`.env.dr`, then the legacy optional file under `failover/`.
Operators should keep only one writable site file; the legacy location wins if
both are present.

The optional `docker-compose.external.primary.yaml` and
`docker-compose.external.dr.yaml` overlays remove the repository-owned
cross-site data paths while preserving the control plane and local ingestion.

## Control-plane flow

```text
             provider A record              provider TXT record
             one site IPv4                  owner=<role> exp=<unix>
                    |                                |
                    +---------------+----------------+
                                    |
                         strict coherent lease
                                    |
                 +------------------+------------------+
                 |                                     |
       primary dns-failover                    DR dns-failover
       renew/reclaim lease                     monitor/promote/renew
                 |                                     |
                 +------------------+------------------+
                                    |
                         public A-record answer
                                    |
                      watcher at each site
                                    |
                    reconcile local Docker state
```

### Lease representation

The managed records are:

```text
failover.example.test.  A    192.0.2.10
failover.example.test.  TXT  "owner=primary exp=1792688400"
```

`parse_lease` accepts a lease only when all of these are true:

- Both A and TXT values exist.
- A is one valid IPv4 address.
- `owner` is exactly `primary` or `dr`.
- `exp` is a positive integer.
- The address equals `PRIMARY_IP` for primary ownership or `DR_IP` for DR ownership.

An incomplete, malformed, out-of-band, or mismatched result raises `LeaseReadError`. DR never promotes from unknown state.

Lease and local-proof expiry use absolute Unix time. Both hosts require
reliable time synchronization; `MAX_CLOCK_SKEW` provides a bounded guard but
does not replace it.

### Write safety

Continuous loops pass their observed lease to `set_lease_verified`, which uses
an optimistic sequence:

1. Re-read the lease and compare it with the caller's expected value.
2. Ask the provider to update A and TXT.
3. Re-read up to `WRITE_VERIFY_ATTEMPTS` times.
4. Accept only the exact intended owner, address, and expiry.

Operator overrides omit the expected-value precondition but still require an
exact readback. The continuous-loop sequence detects stale callers and partial
writes, but it is not provider-side compare-and-set. Another writer can race
between the pre-write read and update. Deploy exactly one controller for each
role. Prefer DNS providers that can atomically update the A/TXT record set.

Provider errors use capped exponential backoff with jitter up to `DNS_RETRY_MAX`.

### Primary loop

Each iteration:

1. Read and strictly validate the lease before choosing a health signal.
2. If primary owns a lease that is not expired past the skew allowance,
   require the configured `HEALTH_*` check and renew only while healthy.
3. If primary's own lease is already expired past the skew allowance, report
   `expired-self-blocked` and do not check health or write; DR may be promoting.
4. If DR owns it, require an absolute HTTP(S) `FAILBACK_HEALTH_URL`; missing or
   non-2xx readiness fails closed, and require `FAILBACK_THRESHOLD` consecutive
   recovery results.
5. Always require the DR lease to expire beyond `MAX_CLOCK_SKEW`; automatic
   primary logic never overwrites an unexpired DR lease.
6. Optimistically write and verify a new primary lease.

The committed primary example uses local VictoriaMetrics health for
`FAILBACK_HEALTH_URL`, because the watcher intentionally stops the primary
collector during DR ownership. This proves backing-service readiness only; it
does not prove VictoriaMetrics catch-up or reverse ClickHouse recovery. Planned
automatic failback needs an external composite data-recovery gate.

### DR loop

Each iteration:

1. Read and validate the lease before choosing a health signal or deciding
   whether to monitor, promote, retain, or yield.
2. If DR owns a lease already expired past the skew allowance, report
   `expired-self-blocked` and do not check health or write; primary may be
   reclaiming.
3. If DR owns the lease and `AUTO_FAILBACK=true`, check the DR-specific,
   DR-visible `FAILBACK_HEALTH_URL`. Yield without renewing when it succeeds.
4. If DR owns the lease and has not yielded, require DR `LOCAL_HEALTH_URL` when
   configured, then renew. With `AUTO_FAILBACK=false`, the recovery probe is
   not consulted.
5. If primary owns the lease, use the normal `HEALTH_*` check. Continue
   monitoring when it is healthy.
6. If primary owns the lease and is unhealthy, require `FAIL_THRESHOLD`
   consecutive failures and DR-local readiness.
7. Wait for the primary lease to expire beyond `MAX_CLOCK_SKEW`, then write and
   verify a DR lease.

When DR already owns the lease, `AUTO_FAILBACK=false` makes a locally healthy
DR controller retain and renew it without consulting primary recovery, as long
as the lease has not already expired past the skew allowance. This prevents an
automatic return before recovery. With `AUTO_FAILBACK=true`, DR yields when its
role-specific recovery probe succeeds rather than deliberately retaining its
lease. DR never writes primary ownership. If DR stops or loses local readiness,
its lease can expire and a failback-ready primary can reclaim it; a restarted
DR controller will not renew that expired self-owned lease automatically.

### Health modes

`HEALTH_MODE` can be:

- `tcp`: successful connection to `HEALTH_HOST:HEALTH_PORT`.
- `http`: a 2xx response from `HEALTH_URL`.
- `metrics`: successful Prometheus fetch plus progress of the exact `HEALTH_METRIC` sample name.

Metrics mode sums all label variants. A rising value is healthy, a lower value is treated as a counter reset and healthy, and repeated unchanged or missing readings become unhealthy after `HEALTH_STALE_COUNT`. Use HTTP mode when an idle-but-ready collector should remain healthy.

### DNS CLI

The container entrypoint is `dns_failover.py` and supports:

| Command | Effect |
|---|---|
| `run` | Start the role loop and status server. |
| `run --once` | Execute one role iteration. |
| `validate` | Validate configuration without building a provider client. |
| `show` | Strictly read and print the coherent lease. |
| `init` | Immediately write primary ownership. |
| `promote` | Immediately write DR ownership. |
| `failback` | Immediately write primary ownership. |

`init`, `promote`, and `failback` are operator overrides. They do not apply the continuous loop's health, failure-threshold, expiry, or data-recovery gates. In particular, only an explicit operator `failback` may deliberately replace an unexpired DR lease.

On a fresh deployment, run
`docker compose ... run --rm --no-deps dns-failover init`
only after pulling upstream images, building custom images, and starting and
waiting for VictoriaMetrics and ClickHouse health with `--no-deps`. Ingestion
and both control services remain stopped during that preparation. Initialize
immediately before the full `up`, without another pull or build. The continuous
controller deliberately does not invent an absent lease, while the collector
gate requires both current DNS ownership and the controller-published proof.
This order avoids a bootstrap deadlock without spending the short initial
lease on image work or first-time storage initialization.

## Collector watcher

`failover/otel_watcher_docker.py` treats DNS as desired state and Docker as actual state. It reconciles every stable iteration, so a failed start or stop is retried even if DNS does not change. Compose starts the watcher without a `dns-failover` health dependency, allowing it to fence independently while the controller is unready.

### Site identity

The watcher selects its site address as follows:

1. Use explicit `MY_IP` when set.
2. Otherwise, `ROLE=primary` selects `PRIMARY_IP`.
3. Otherwise, `ROLE=dr` selects `DR_IP`.

All paths require a valid IPv4 address.

### DNS resolution

Resolver order is:

1. Each comma-separated `DNS_SERVERS` entry, or legacy `DNS_SERVER` when `DNS_SERVERS` is absent.
2. The system resolver when `DNS_USE_SYSTEM_RESOLVER=true`.

Configured servers are queried with `dig`, `+dnssec`, and `+adflag`. When `DNSSEC_REQUIRED=true`, the watcher accepts only a configured-server response containing the AD flag and does not use the system fallback. The watcher trusts the recursive server's AD assertion; it is not a local validating resolver.

Before a stopped collector may start, the answer must:

- Contain exactly one A address.
- Contain a valid IPv4 value.
- Remain identical for `DNS_CONSISTENT_ANSWERS` observations.

An unresolved lookup or zero/multiple A answers immediately reconciles the
collector to stopped. Any valid single non-local A answer also stops it
immediately, without waiting for the consistency threshold. A single local
answer is accumulated as pending, but cannot start a stopped collector until
the same local answer satisfies the complete consistency threshold.

Every successful strict provider read atomically writes a non-secret local
proof to `LEASE_PROOF_FILE` (default `/state/observed-lease.json`); an
untrusted read removes it. The watcher accepts the recursive A only when the
proof names the same normalized record and address, maps its owner to the
configured primary/DR address, is not observed implausibly in the future, and
expires later than `now + MAX_CLOCK_SKEW`. Missing, malformed, mismatched,
expired, or inside-skew proof immediately fences the collector. Because the
proof carries the provider-observed TXT expiry, a stale single local A cannot
remain trusted beyond its lease. The controller, watcher, and collector gate
share the per-site failover-state volume; preserve it during normal upgrades.

### Docker reconciliation

The watcher distinguishes `running`, `stopped`, `missing`, and `error`.
Missing containers and inspection errors prevent reconciliation, including a
requested fence, and are reported and retried. Successful actions are
inspected again to verify final state.

DNS failures and Docker-action failures have independent bounded exponential backoffs. On a later successful or pending iteration, the relevant backoff resets.

The container uses host networking and mounts `/var/run/docker.sock`. Its status server binds `STATUS_HOST=127.0.0.1` by default. `--once` performs enough immediate observations to meet the consistency threshold and exits without starting the HTTP status server.

## Telemetry data plane

### OpenTelemetry collector

The custom image wraps the collector with `bootstrap-gate.sh`. Before it execs
the real collector binary, the gate resolves `DNS_RECORD` and requires exactly
one A answer equal to the `ROLE`-derived local address plus a matching owner,
address, record, version, and unexpired `LEASE_PROOF_FILE` outside the
`MAX_CLOCK_SKEW` guard. Both must remain valid for
`OTEL_BOOTSTRAP_STABLE_CHECKS` observations. An absent, multiple, unstable, or
non-local answer, or a missing/malformed/mismatched/expired proof, leaves the
container inert. This prevents a standby or stale-cache collector from
scraping during the interval before `otel-watcher` performs its first
reconciliation. `OTEL_BOOTSTRAP_DNS_SERVER` can select a container-reachable
resolver; otherwise the container resolver is used.

Before ownership checks, the root bootstrap stage copies the dedicated
`OTEL_SECRETS_DIR` bind mount plus the collector's ClickHouse Docker secret
into `/run/runtime-secrets`, rejects symbolic links, and applies private
ownership and modes. It also validates the selected receiver source as a
readable regular, non-symlink file and stages it at mode `0400` as
`/run/runtime-config/receivers.yaml`. The bootstrap then permanently drops to
UID 10001. Collector configuration uses file substitutions from the private
runtime secret directory; device credentials are not injected through a shared
environment file.

The pinned AST collector receives:

- BIG-IP metrics through the `bigip` receiver loaded from `OTEL_RECEIVERS_FILE`.
- OTLP logs and traces on 4317/4318 through the included `otlp` receiver.

Metrics are exported to:

- Local VictoriaMetrics through `prometheusremotewrite/local`.
- Peer VictoriaMetrics through `prometheusremotewrite/peer` with retries and an in-memory sending queue.
- Local ClickHouse.

OTLP logs and traces are exported to local ClickHouse. The BIG-IP receiver is metrics-only.

The base collector image is scratch-based and does not provide a probe utility or the configured health-check extension. `services/otel_collector/Dockerfile` adds a pinned BusyBox binary, and Compose probes the native Prometheus telemetry endpoint at `:8888/metrics`.

### VictoriaMetrics synchronization

Each site pulls from the peer into its local VictoriaMetrics. The algorithm is:

1. Load `/state/vm-sync.json` from a persistent named volume.
2. On first use, atomically persist `SYNC_START_TIMESTAMP` when configured;
   otherwise persist `now - SYNC_LOOKBACK` as the catch-up anchor before
   network I/O.
3. Choose a target of `now - SYNC_SETTLE_DELAY_SECONDS`.
4. Export bounded `SYNC_CHUNK_SECONDS` windows from `/api/v1/export`.
5. Replay `SYNC_OVERLAP_SECONDS` before each boundary.
6. Import non-empty results into `/api/v1/import`.
7. Atomically advance the watermark only after every window succeeds.

A failed or partial run never advances the watermark, so a long outage is not silently truncated to the current lookback. The default initial lookback is only 30 minutes; operators who need older history must set the Compose-level `VM_SYNC_START_TIMESTAMP` before first startup. Retaining it makes a missing/recreated watermark deterministic, but the persistent `primary_vm_sync_state` and `dr_vm_sync_state` volumes should normally be preserved. Transient HTTP and network failures are retried with bounded exponential backoff and jitter. Basic credentials are sent in headers, not URLs. `SOURCE_PASSWORD_FILE` and `DEST_PASSWORD_FILE` are supported.

The Compose stack configures VictoriaMetrics with `--dedup.minScrapeInterval=30s`, matching the default overlap. Operators who change either value should review duplicate handling.

### ClickHouse synchronization

DR pulls from primary over the ClickHouse HTTP API. For each configured database, `ch-sync`:

1. Finds source tables whose engine matches `MergeTree`.
2. Creates destination databases and missing tables from source DDL.
3. Compares active partitions by `partition_id` and row count.
4. Exports a differing source partition in Native format.
5. Imports it into a deterministic staging table.
6. Verifies staging row count.
7. Atomically applies `ALTER TABLE ... REPLACE PARTITION ID ... FROM staging`.
8. Verifies the live destination row count and drops staging.

Live data is not dropped before staging verification. An import request is deliberately not retried because a timeout may have committed; the next scheduled run recreates staging and tries again.

Credentials use `X-ClickHouse-User` and `X-ClickHouse-Key` headers. They are not placed in the URL or SQL.

#### Active-DR guard

DR `ch-sync` receives the failover environment through Compose. `DNS_RECORD` and `DR_IP` become the guard defaults; explicit `SYNC_GUARD_DNS_RECORD` and `SYNC_GUARD_DEST_IP` can override them.

Before any database operation, it resolves the guard record and reads
`SYNC_GUARD_LEASE_PROOF_FILE` from the shared DR failover-state volume:

- DNS must return exactly one valid A address.
- The proof must use the exact schema published by `dns-failover`, name the
  same normalized record and resolved address, and remain valid beyond
  `MAX_CLOCK_SKEW`.
- A coherent proof whose address equals `SYNC_GUARD_DEST_IP` marks the
  configured destination active and skips the run-level sync.
- A coherent proof selecting a different address permits synchronization.
- Resolution failure, an ambiguous answer, or a missing, stale, future-dated,
  malformed, or inconsistent proof fails closed.

The guard is evaluated at run start and again immediately before each live
`REPLACE PARTITION`, so an ownership transition during a long copy cannot
overwrite a site that has become active. This prevents an older recovered
primary from replacing partitions that received new data while DR was active.

Normal primary-to-DR replication protects `DR_IP`. Reverse recovery changes
`SYNC_GUARD_DEST_IP` to `PRIMARY_IP` while retaining the same DNS record and
mounted lease proof. Therefore a failback that makes primary active during a
long reverse copy is detected at the last-moment recheck and the live
replacement is refused. The removed `SYNC_ALLOW_ACTIVE_DESTINATION` bypass is
rejected when non-empty. `ch-sync/ch_sync_recovery.sh` retains a human
`CH_SYNC_RECOVERY_CONFIRM=REPLACE_DESTINATION_PARTITIONS` check for direct
execution, but that confirmation does not disable fencing. The Compose
recovery runbook is in [USER_GUIDE.md](USER_GUIDE.md).

The final recovery pass must run after stopping the DR watcher and collector.
Otherwise DR can write new ClickHouse or VictoriaMetrics data after it was
copied but before DNS moves, creating a loss window. DR lease renewal stays up
during this short ingestion freeze; if validation fails, the operator restarts
DR ingestion without changing DNS.

The synchronizer does not detect same-row-count content differences, alter existing schemas, remove destination-only partitions, or replicate non-MergeTree tables. Validate those cases separately.

## External replication mode

External mode is selected only by explicitly merging the appropriate external
overlay with a base manifest. The default remains `builtin`. In the rendered
external model:

- profiled `vm-sync` and `ch-sync` services are inactive;
- `pipelines.local-only.yaml` removes `prometheusremotewrite/peer` from the
  collector metrics pipeline; and
- `DATA_REPLICATION_MODE=external` makes either sync executable fail
  configuration if it is explicitly targeted despite its inactive profile.

Preflight validates the merged Compose model and the local-only pipeline. It
does not test an external replicator. The external system owns copy direction,
credentials, transport, lag monitoring, recovery-point guarantees, reverse
recovery, and any storage-level single-writer fencing. WireGuard can protect
the transport but supplies none of those replication semantics.

Compose profiles do not retroactively stop a container created from the base
model. `services/activate-external-replication.sh` therefore validates the
merged model, explicitly stops and removes the built-in sync containers while
retaining their named volumes, and starts the external model without removing
unrelated services. It derives the effective project name from Compose,
includes any operator-supplied Compose options consistently, applies the
safety overlay last, and refuses to transition while any collector one-off
exists. A plain overlay `up` is not a valid migration procedure.

The DNS controller consumes no `vm-sync`, `ch-sync`, or external-replication
status before promotion. Existing `LOCAL_HEALTH_URL` checks DR-local backing
service availability, not replica equality. `FAILBACK_HEALTH_URL` can point to
an operator-managed composite recovery gate, but manual `promote` and
`failback` remain explicit overrides. External mode is therefore an
availability-first configuration that can expose data older than the intended
recovery point. `AUTO_FAILBACK=false` is the safe default.

## Observability

`common/health_server.py` supplies a dependency-free status server to all Python control services.

| Endpoint | Meaning |
|---|---|
| `/healthz` | The main loop has touched status within the liveness window. |
| `/readyz` | The service is live and has a recent successful operation. |
| `/metrics` | Prometheus text metrics. |

Readiness is age-based. A single failed iteration increments failure metrics but may not immediately make `/readyz` fail while a prior success is still fresh.

Metric prefixes are `dns_failover_`, `otel_watcher_`, `vm_sync_`, and `ch_sync_`. Common series include:

- `up`, `ready`, `runs_total`, `failures_total`, and `consecutive_failures`
- `last_success_timestamp_seconds`, `seconds_since_last_success`, and `last_duration_seconds`
- `items_total` and `bytes_total`

Service-specific gauges include lease owner/expiry and local health, DNS resolution and desired collector state, VictoriaMetrics watermark/lag/retries, and ClickHouse pending partitions/row lag/guard state.

Default status settings:

| Service | Port setting | Default readiness age |
|---|---|---|
| `dns-failover` | `STATUS_PORT=8080` | max(`UPDATE_INTERVAL * 3`, `LEASE_TTL + MAX_CLOCK_SKEW`) |
| `otel-watcher` | `STATUS_PORT=8080` | max(`OTEL_CHECK_INTERVAL * 3`, 30), overridable by `READINESS_MAX_AGE` |
| `vm-sync` | `HEALTH_PORT=8080` | max(`SYNC_INTERVAL * 3`, 900), overridable by `READINESS_MAX_AGE` |
| `ch-sync` | `HEALTH_PORT=8080` | max(`SYNC_INTERVAL * 3`, 900), overridable by `READINESS_MAX_AGE` |

Compose does not publish most status ports. Its health checks query `/readyz` from inside each container.

Platform probes are VictoriaMetrics `/health`, ClickHouse `/ping`, Grafana `/api/health`, and collector `/metrics` on port 8888.

## Configuration reference

### Root deployment settings

These settings are read by Compose from root `.env`:

| Setting | Purpose |
|---|---|
| `BIND_ADDRESS` | Host address for published platform ports; example default is `127.0.0.1`. |
| `AST_NETWORK_SUBNET` | Local Compose subnet and internal ClickHouse allowlist. |
| `CLICKHOUSE_DATABASE`, `CLICKHOUSE_USER` | Local database and main ingestion/sync identity. |
| `GRAFANA_CLICKHOUSE_USER` | Separate internal read-only datasource identity. |
| `GRAFANA_ADMIN_USER` | Grafana bootstrap administrator name. |
| `CLICKHOUSE_PASSWORD_FILE` | Host path for local ClickHouse Docker secret. |
| `GRAFANA_CLICKHOUSE_PASSWORD_FILE` | Host path for the read-only datasource credential. |
| `PRIMARY_CLICKHOUSE_PASSWORD_FILE` | DR host path for the primary read credential. |
| `GRAFANA_ADMIN_PASSWORD_FILE` | Host path for Grafana's Docker secret. |
| `CLICKHOUSE_REMOTE_CIDRS` | Comma-separated peer/recovery CIDRs allowed to use the main ClickHouse account. |
| `PRIMARY_VM_URL`, `DR_VM_URL` | Peer read endpoints used by `vm-sync`. |
| `PRIMARY_VM_WRITE_URL`, `DR_VM_WRITE_URL` | Peer remote-write endpoints used by the active collector. |
| `PRIMARY_CLICKHOUSE_SCHEME/HOST/HTTP_PORT/USER` | Primary source endpoint used by DR `ch-sync`. |
| `VM_SYNC_INTERVAL`, `VM_SYNC_LOOKBACK` | Compose-level VictoriaMetrics sync cadence and fallback initial lookback. |
| `VM_SYNC_START_TIMESTAMP` | Optional Unix-second history boundary passed to `vm-sync` as `SYNC_START_TIMESTAMP` when no watermark exists. |
| `CH_SYNC_INTERVAL`, `CH_SYNC_DATABASES` | ClickHouse sync cadence and database list. |
| `OTEL_RECEIVERS_FILE` | Host receiver YAML mounted only for bootstrap, then validated and privately staged as `/run/runtime-config/receivers.yaml` before the UID drop. |
| `OTEL_SECRETS_DIR` | Dedicated host directory staged privately for collector file substitutions; the supplied configuration expects `bigip_password` and `datafabric_authorization`. |
| `OTEL_CHECK_INTERVAL` | Watcher cadence. |
| `OTEL_BOOTSTRAP_STABLE_CHECKS` | Matching DNS observations required before the collector binary starts. |
| `OTEL_BOOTSTRAP_CHECK_INTERVAL` | Seconds between bootstrap DNS checks. |
| `OTEL_BOOTSTRAP_DNS_SERVER` | Optional resolver reachable from the collector container. |
| `FAILOVER_SECRETS_DIR` | Provider-only host directory bind-mounted read-only for `dns-failover`; `.env.example` uses `./secrets/failover`, and the secure entrypoint stages its files into private runtime `/secrets`. |

### DNS controller settings

| Setting | Default | Purpose |
|---|---:|---|
| `ROLE` | `primary` | `primary` or `dr`. |
| `DNS_PROVIDER` | `dry-run` | Provider implementation. |
| `DNS_SERVER` | `127.0.0.1` | Provider/query server where applicable; watcher fallback input. |
| `DNS_ZONE`, `DNS_RECORD` | example values | Zone and in-zone FQDN. |
| `DNS_TTL` | 30 | A/TXT record TTL. Cloudflare accepts 1 or 60-86400. |
| `PRIMARY_IP`, `DR_IP` | example values | Distinct usable IPv4 addresses. |
| `LEASE_TTL` | 60 | Lease duration. |
| `UPDATE_INTERVAL` | 10 | Loop interval and backoff base. |
| `FAIL_THRESHOLD` | 3 | DR primary-health failures before lease evaluation. |
| `FAILBACK_THRESHOLD` | 3 | Primary healthy streak before reclamation. |
| `AUTO_FAILBACK` | false | When true, let DR stop renewing after its DR-visible recovery probe succeeds; primary still waits for lease expiry/skew and its own recovery probe. |
| `MAX_CLOCK_SKEW` | 5 | Lease-expiry safety allowance used by promotion, self-expiry blocking, and local-proof fencing. |
| `DNS_RETRY_MAX` | 60 | Maximum provider backoff in seconds. |
| `WRITE_VERIFY_ATTEMPTS` | 3 | Read-after-write attempts. |
| `WRITE_VERIFY_DELAY` | 0.5 | Delay between verification reads. |
| `LEASE_PROOF_FILE` | `/state/observed-lease.json` | Atomic local copy of the last coherent provider A/TXT observation shared with collector fencing. |
| `HEALTH_MODE` | `tcp` in code | `tcp`, `http`, or `metrics`; examples use `http`. |
| `HEALTH_TIMEOUT` | 2 | Health request timeout. |
| `LOCAL_HEALTH_URL` | unset | Optional DR-local 2xx readiness URL. |
| `FAILBACK_HEALTH_URL` | unset in code | Role-specific HTTP(S) recovery URL used while DR owns the lease: primary uses its local value before reclaiming, while DR uses its separately configured DR-visible value only with `AUTO_FAILBACK=true` before yielding. Enabling automatic failback requires the value at validation. |
| `STATUS_PORT` | 8080 | Status server port. |

The code supports `NAME_FILE` for any configuration key. Set either `NAME` or `NAME_FILE`, never both. Vault values take precedence over file and environment values. `VAULT_REQUIRED=true` converts Vault loading failure into a startup error; otherwise the loader warns and falls back.

### Provider settings

| Provider | Required or relevant settings |
|---|---|
| `dry-run` | `DRYRUN_STATEFILE` (default `/state/zone.json`). Testing only. |
| `bind-tsig` | `DNS_SERVER`, `TSIG_KEYFILE` (default `/secrets/tsig.key`). |
| `ad-gss` | `DNS_SERVER` plus operator-provided Kerberos configuration and credentials. |
| `infoblox` | `INFOBLOX_HOST`, `INFOBLOX_USERNAME`, `INFOBLOX_PASSWORD[_FILE]`; optional WAPI version and SSL verification. |
| `cloudflare` | `CLOUDFLARE_API_TOKEN[_FILE]`, `CLOUDFLARE_ZONE_ID`; TTL must be 1 or 60-86400. |
| `route53` | `ROUTE53_ZONE_ID`, `AWS_REGION`; either both static AWS key settings or neither to use the standard boto3 credential chain. |
| `azure-dns` | Subscription, resource group, tenant, client ID, and client secret. Tokens refresh once after HTTP 401; zone-apex records use `@`. |
| `gcp-dns` | `GCP_PROJECT_ID`, `GCP_MANAGED_ZONE`; optional `GCP_CREDENTIALS_FILE`, otherwise ambient Google credentials. `GCP_CHANGE_TIMEOUT` defaults to 30 seconds and must be greater than 0 and at most 300. A write is not accepted until the Google change reaches `done`; an unexpected state or timeout fails the operation. |
| `f5-gtm` | `F5_HOST`, `F5_USERNAME`, `F5_PASSWORD[_FILE]`, `F5_POOL_NAME`, `F5_PRIMARY_MEMBER`, and `F5_DR_MEMBER`; optional `F5_PARTITION` (default `Common`) and `F5_VERIFY_SSL`. Member selectors are distinct exact `fullPath` values beginning with `/<partition>/`. The configured GTM A pool must contain exactly those two members; missing, extra, duplicate, or ambiguously enabled members fail closed. |
| `script` | Executable `SCRIPT_SET` and `SCRIPT_GET` paths. |

Example provider-specific settings:

```dotenv
# Google Cloud DNS
GCP_PROJECT_ID=example-project
GCP_MANAGED_ZONE=telemetry-zone
GCP_CHANGE_TIMEOUT=30

# F5 GTM
F5_PARTITION=Common
F5_POOL_NAME=telemetry-pool
F5_PRIMARY_MEMBER=/Common/server-primary:/Common/telemetry
F5_DR_MEMBER=/Common/server-dr:/Common/telemetry
```

For F5, copy the two member `fullPath` values exactly from the configured pool's members API. The controller disables the peer before enabling the selected member, preferring a brief fail-closed outage over a two-active window, and then reads the pool back to require exactly one enabled member.

When the setup wizard is given a host `GCP_CREDENTIALS_FILE`, it requires a
JSON object, copies the file to `secrets/failover/gcp_credentials.json` with
mode `0600`, and emits the container path `/secrets/gcp_credentials.json` into
both generated environment files. Choosing Application Default Credentials
leaves the path unset.

Vault bootstrap options are `VAULT_ADDR`, `VAULT_AUTH_METHOD` (`token`, `approle`, or `kubernetes`), corresponding bootstrap credential/file settings, `VAULT_MOUNT`, and `VAULT_KEY`.

### Watcher settings

| Setting | Default | Purpose |
|---|---:|---|
| `MY_IP` | derived | Explicit site IPv4 override. |
| `OTEL_CONTAINER` | `otel-collector` | Docker container name; Compose overrides it. |
| `OTEL_CHECK_INTERVAL` | 15 | Normal reconciliation interval. |
| `DNS_SERVERS` | `DNS_SERVER` | Ordered comma-separated recursive servers. |
| `DNS_USE_SYSTEM_RESOLVER` | true | Permit OS resolver fallback. |
| `DNSSEC_REQUIRED` | false | Require AD flag and disable unverifiable fallback. |
| `DNS_CONSISTENT_ANSWERS` | 2 | Identical observations before action. |
| `LEASE_PROOF_FILE` | `/state/observed-lease.json` | Controller-published coherent lease required in addition to DNS. |
| `MAX_CLOCK_SKEW` | 5 | Future/expiry safety margin applied to the local lease proof. |
| `DNS_QUERY_TIMEOUT` | 4 | `dig` command timeout. |
| `DOCKER_TIMEOUT` | 30 | Docker CLI timeout. |
| `OTEL_STOP_TIMEOUT` | 10 | Grace period passed to `docker stop`. |
| `DNS_BACKOFF_INITIAL/MAX` | 1 / 30 | DNS failure backoff. |
| `ACTION_BACKOFF_INITIAL/MAX` | 1 / 30 | Docker failure backoff. |
| `STATUS_HOST`, `STATUS_PORT` | 127.0.0.1 / 8080 | Watcher status listener. |

### VictoriaMetrics sync settings

| Setting | Default | Purpose |
|---|---:|---|
| `SOURCE_URL`, `DEST_URL` | localhost URLs | Full HTTP(S) bases, with no embedded credentials. |
| `SOURCE_USER`, `DEST_USER` | empty | Optional Basic-auth users. |
| `SOURCE_PASSWORD[_FILE]`, `DEST_PASSWORD[_FILE]` | empty | Optional Basic-auth passwords. |
| `SOURCE_CA_FILE`, `DEST_CA_FILE` | empty | Optional CA bundle for an HTTPS endpoint. |
| `SYNC_INTERVAL` | 300 | Run cadence. |
| `SYNC_LOOKBACK` | 30 | Initial lookback in minutes. |
| `SYNC_START_TIMESTAMP` | unset | Optional nonnegative Unix-second anchor used instead of lookback when the watermark file is absent. |
| `SYNC_CHUNK_SECONDS` | 300 | Catch-up window size. |
| `SYNC_OVERLAP_SECONDS` | 30 | Replay overlap at window boundaries. |
| `SYNC_SETTLE_DELAY_SECONDS` | 15 | Delay behind current time. |
| `SYNC_STATE_FILE` | `/state/vm-sync.json` | Durable watermark. |
| `REQUEST_TIMEOUT` | 120 | HTTP timeout. |
| `RETRY_MAX_ATTEMPTS` | 4 | Transient request attempts. |
| `RETRY_INITIAL_SECONDS/MAX_SECONDS` | 1 / 30 | Retry bounds. |
| `MAX_ROWS_PER_LINE` | 10000 | VictoriaMetrics export formatting limit. |

### ClickHouse sync settings

| Setting | Default | Purpose |
|---|---:|---|
| `SOURCE_SCHEME/HOST/PORT/USER` | local HTTP defaults | Primary/source HTTP endpoint. |
| `SOURCE_PASSWORD[_FILE]` | empty | Source credential. |
| `DEST_SCHEME/HOST/PORT/USER` | local HTTP defaults | DR/destination HTTP endpoint. |
| `DEST_PASSWORD[_FILE]` | empty | Destination credential. |
| `SOURCE_CA_FILE`, `DEST_CA_FILE` | empty | Optional HTTPS CA bundles. |
| `SYNC_DATABASES` | `ast` | Comma-separated databases. |
| `SYNC_INTERVAL` | 300 | Run cadence. |
| `REQUEST_TIMEOUT`, `COPY_TIMEOUT` | 120 / 1800 | Metadata and partition-copy timeouts. |
| `RETRY_MAX_ATTEMPTS` | 4 | Transient metadata request attempts. |
| `SYNC_GUARD_DNS_RECORD` | `DNS_RECORD` | Active-site guard name. |
| `SYNC_GUARD_DEST_IP` | `DR_IP` | Destination address protected by the guard; set to `PRIMARY_IP` for reverse recovery. |
| `SYNC_GUARD_LEASE_PROOF_FILE` | `/failover-state/observed-lease.json` | Controller-published proof that must agree with guard DNS and remain unexpired. |
| `MAX_CLOCK_SKEW` | 5 | Future/expiry margin for the guard lease proof. |
| `SYNC_ALLOW_ACTIVE_DESTINATION` | must be empty | Removed unsafe bypass; any non-empty value is rejected. |

## Security model and limitations

- ClickHouse and Grafana bootstrap passwords are mounted as Docker secrets.
  Startup refuses missing, short, or common unsafe ClickHouse passwords. The
  ClickHouse entrypoint removes the default user, hashes credentials into a
  private runtime config, limits the main account to the internal subnet plus
  explicit `CLICKHOUSE_REMOTE_CIDRS`, and creates a separate internal-only
  read-only Grafana account.
- File-backed Compose secrets remain operator-owned host files with mode
  `0600`. A narrow bootstrap wrapper in each affected custom image starts as
  root, copies only the secrets granted to that service into a private runtime
  directory with service ownership, then uses a static helper to clear
  supplementary groups and permanently set the service GID/UID before exec.
  The long-running collector, Grafana, and control processes therefore do not
  run as root, and host secret modes do not need to be weakened.
- Provider secrets can be private mounted files or Vault values. Generated environment and secret files are ignored by Git.
- Device credentials are operator-owned `0600` files in the dedicated
  `OTEL_SECRETS_DIR`, bind-mounted read-only only into the collector bootstrap
  and then staged privately for UID 10001. They are not placed in the
  collector environment. Docker-daemon/host-privileged users can still access
  the source files, so minimize such access.
- The default host binding is loopback. Cross-site operation requires deliberate network exposure.
- TLS for Grafana, VictoriaMetrics, ClickHouse, and cross-site links is operator-configured. Use a VPN/private encrypted network or HTTPS endpoints with trusted CA files. The supplied internal endpoints are cleartext.
- The watcher has the Docker socket and therefore effectively host-root control. Run only trusted code in that image and tightly restrict host access.
- DNSSEC is optional and watcher validation relies on the chosen recursive resolver's AD flag. DNS API authentication depends on the selected provider.
- DNS TTL, recursive caches, and the answer-consistency threshold add recovery time. Exactly one A value plus matching unexpired local provider-observed lease proof is required.
- The in-memory remote-write queue is not a durable WAL. `vm-sync` repairs gaps after connectivity returns.
- Replication does not replace independent backups, point-in-time recovery, or restore testing.

## Verification and tests

Static and unit validation:

```sh
python -m compileall common failover vm-sync ch-sync tests
python -m unittest discover -s tests -v
```

Deployment-model validation:

```sh
sh services/preflight.sh docker-compose.primary.yaml
sh services/preflight.sh docker-compose.dr.yaml
docker compose -f docker-compose.primary.yaml config --quiet
docker compose -f docker-compose.dr.yaml config --quiet

# External-replication variants
sh services/preflight.sh -f docker-compose.primary.yaml -f docker-compose.external.primary.yaml
sh services/preflight.sh -f docker-compose.dr.yaml -f docker-compose.external.dr.yaml
docker compose -f docker-compose.primary.yaml -f docker-compose.external.primary.yaml config --quiet
docker compose -f docker-compose.dr.yaml -f docker-compose.external.dr.yaml config --quiet
```

`services/preflight.sh` additionally enforces Docker Compose 2.24 or newer, an
amd64 Docker server, readable application secret files, dedicated OTel and DNS
provider secret-directory mounts, and one receiver-file bind.

Build validation requires a running Docker daemon:

```sh
docker compose -f docker-compose.primary.yaml pull victoriametrics clickhouse
docker compose -f docker-compose.primary.yaml build --pull
docker compose -f docker-compose.dr.yaml pull victoriametrics clickhouse
docker compose -f docker-compose.dr.yaml build --pull
```

The unit suite covers lease validation and failback behavior, watcher
resolver/reconciliation failures, setup-secret externalization, durable
VictoriaMetrics watermarks and retries, ClickHouse staging/replacement safety,
direction-aware destination fencing, and recovery confirmation behavior.

Production acceptance should additionally exercise provider permissions, DNS propagation and caching, host loss, control-plane partitions, Docker socket failure, peer-link loss, long catch-up, DR-local health refusal, and full reverse recovery before failback.
