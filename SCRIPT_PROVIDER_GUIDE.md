# Custom DNS script provider

Use `DNS_PROVIDER=script` when the DNS backend is not supported directly. The failover service delegates updates and reads to two executable programs while retaining the same strict lease validation, read-after-write verification, health gates, and retry behavior used by built-in providers.

No deployment-specific scripts are committed. The setup wizard generates templates under the ignored root `scripts/` directory, and both Compose stacks mount that directory read-only at `/scripts`.

## Configuration

In both `.env.primary` and `.env.dr`:

```dotenv
DNS_PROVIDER=script
SCRIPT_SET=/scripts/set_dns.sh
SCRIPT_GET=/scripts/get_dns.sh
```

The remaining common settings must match across sites:

```dotenv
DNS_ZONE=example.com
DNS_RECORD=ast-failover.example.com
DNS_TTL=30
PRIMARY_IP=192.0.2.10
DR_IP=192.0.2.20
```

Use `ROLE=primary` at primary and `ROLE=dr` at DR.

Generate starting templates from the repository root:

```sh
python -m pip install -r failover/requirements.txt
python failover/setup.py
```

Select the custom-script provider and accept `.` as the output directory. The wizard writes `.env.primary`, `.env.dr`, `scripts/set_dns.sh`, and `scripts/get_dns.sh`. The generated set script deliberately refuses to succeed until it is customized. The generated get script uses `dig` as a starting point.

Review all generated files and keep them out of version control.

## Set contract

The service calls:

```text
SCRIPT_SET RECORD IP OWNER EXPIRY_UNIX TTL ZONE
```

Positional arguments are:

| Position | Meaning | Example |
|---:|---|---|
| `$1` | Managed FQDN | `ast-failover.example.com` |
| `$2` | New IPv4 address | `192.0.2.20` |
| `$3` | Lease owner | `primary` or `dr` |
| `$4` | Lease expiry as Unix seconds | `1792688400` |
| `$5` | DNS TTL in seconds | `30` |
| `$6` | DNS zone | `example.com` |

The same values are provided as environment variables:

```text
DNS_RECORD DNS_IP DNS_OWNER DNS_EXPIRY DNS_TTL DNS_ZONE DNS_SERVER
```

The set program must update both records:

```text
RECORD A   IP
RECORD TXT "owner=OWNER exp=EXPIRY_UNIX"
```

Return 0 only after the provider accepted both updates. Return nonzero for any partial or uncertain outcome and put a concise diagnostic on stderr. Standard output is ignored.

The subprocess timeout is 30 seconds. If an API can remain asynchronous beyond that time, the script should poll its authoritative state within the deadline or return failure and let the controller retry.

### Atomicity and ordering

Use a transactional record-set update whenever the backend supports one. If it cannot update A and TXT atomically, write the new TXT lease first and the A record last. This lets controllers reject the temporary owner/address mismatch and delays the collector-visible A switch until the ownership metadata has been written.

The service re-reads the exact intended owner, address, and expiry after every successful set call. It retries readback according to `WRITE_VERIFY_ATTEMPTS` and `WRITE_VERIFY_DELAY`; a set program must not treat an unverifiable write as complete.

## Get contract

The service calls:

```text
SCRIPT_GET RECORD ZONE
```

It also provides:

```text
DNS_RECORD DNS_ZONE DNS_SERVER
```

On success, print exactly one JSON object to stdout:

```json
{"A":"192.0.2.10","TXT":"owner=primary exp=1792688400"}
```

When neither record exists, return:

```json
{"A":null,"TXT":null}
```

Requirements:

- Use the authoritative provider/API where possible, not a stale recursive cache.
- Return one IPv4 A value, never a comma-separated list.
- Preserve the TXT content as `owner=<role> exp=<integer>`.
- Escape JSON correctly.
- Write logs only to stderr; extra stdout makes the JSON invalid.
- Return nonzero when the result is partial, stale, ambiguous, or unavailable.

The get subprocess also has a 30-second timeout. Nonzero exit, timeout, invalid JSON, missing fields, an invalid owner/expiry, or an address/owner mismatch becomes an unreadable lease. Continuous failover then fails closed: primary does not renew from uncertain state and DR does not promote.

## Minimal script shape

Use a strict shell mode and validate every input before calling the provider:

```sh
#!/bin/sh
set -eu

record=$1
ip=$2
owner=$3
expiry=$4
ttl=$5
zone=$6

case "$owner" in
  primary|dr) ;;
  *) echo "invalid owner" >&2; exit 2 ;;
esac

# Replace this block with one transactional provider update, or TXT first and
# A last. Exit nonzero if either operation is uncertain.
echo "set_dns.sh has not been implemented" >&2
exit 1
```

Do not leave a placeholder that exits 0. A false success is more dangerous than a visible deployment failure.

The script can be POSIX shell, Python, or another executable whose interpreter is installed in the `dns-failover` image. The stock image includes `/bin/sh`, Python, `dig`, and `nsupdate`; it does not include every language runtime or vendor CLI. Extend `failover/Dockerfile` deliberately if your integration needs one.

## Credentials

Do not embed credentials in the script or failover environment file. Put them
under the configured host secrets directory and reference the staged container
path, for example:

```dotenv
PROVIDER_TOKEN_FILE=/secrets/custom_dns_token
```

The copied `.env` defaults `FAILOVER_SECRETS_DIR` to
`./secrets/failover`; only that provider-specific directory is mounted into
the controller bootstrap.

Then read that file in the script. The child process inherits the failover environment, so never dump the entire environment to logs.

Host provider-secret files may remain operator-owned mode `0600`. The
`dns-failover` bootstrap stages the read-only secrets and scripts into private
in-container paths before permanently dropping to `appuser`; do not make the
host secret world-readable. Scripts must still have executable mode and must
not contain embedded credentials.

If the wizard collects a recognized sensitive field, it creates a private file and emits the corresponding `_FILE` setting. For an arbitrary custom variable, create and permission the file yourself.

Recommended host setup:

```sh
umask 077
mkdir -p secrets/failover
chmod 700 secrets secrets/failover
printf '%s' 'replace-with-real-token' > secrets/failover/custom_dns_token
chmod 755 scripts/set_dns.sh scripts/get_dns.sh
```

Compose mounts `./scripts` read-only. The scripts themselves cannot modify that directory at runtime.

## Test before enabling automation

Use a delegated test record or test zone.

### 1. Test get output

```sh
./scripts/get_dns.sh ast-failover.example.com example.com | python -m json.tool
```

Confirm stdout contains only JSON and errors go to stderr.

### 2. Test a manual update

```sh
expiry=$(( $(date +%s) + 300 ))
./scripts/set_dns.sh \
  ast-failover.example.com 192.0.2.10 primary "$expiry" 30 example.com
./scripts/get_dns.sh ast-failover.example.com example.com | python -m json.tool
```

Verify A and TXT through the provider's authoritative API as well as DNS.

### 3. Validate inside the deployment image

Host execution does not prove that `/scripts`, interpreters, permissions, and secret paths work in the container. Run:

```sh
docker compose -f docker-compose.primary.yaml pull victoriametrics clickhouse
docker compose -f docker-compose.primary.yaml build --pull
docker compose -f docker-compose.primary.yaml run --rm --no-deps dns-failover validate

docker compose -f docker-compose.dr.yaml pull victoriametrics clickhouse
docker compose -f docker-compose.dr.yaml build --pull
docker compose -f docker-compose.dr.yaml run --rm --no-deps dns-failover validate
```

Validation requires both container paths to exist and be executable.

### 4. Prepare storage, initialize, and start

Only after the manual tests pass:

```sh
docker compose -f docker-compose.primary.yaml up -d --no-deps --wait \
  --wait-timeout 300 victoriametrics clickhouse
docker compose -f docker-compose.primary.yaml run --rm --no-deps dns-failover init
docker compose -f docker-compose.primary.yaml up -d
docker compose -f docker-compose.primary.yaml exec dns-failover \
  python3 /app/dns_failover.py show
```

The first command starts only storage and waits for its health checks; the
collector and both control services remain stopped. `init` then performs a
real DNS write. Start the already-built, prepulled full stack immediately and
only then read back the lease. It must show the primary address, primary owner,
and a future expiry. Do not add a pull or build after `init`: if the short
initial lease expires past its skew allowance, primary intentionally refuses
an automatic renewal that could race DR promotion.

### 5. Observe and start DR

```sh
docker compose -f docker-compose.primary.yaml logs -f dns-failover otel-watcher
```

Start DR only after primary is healthy. Prestart its storage without controls or
ingestion, wait for health, and then start the remaining services:

```sh
docker compose -f docker-compose.dr.yaml up -d --no-deps --wait \
  --wait-timeout 300 victoriametrics clickhouse
docker compose -f docker-compose.dr.yaml up -d
```

Confirm the DR watcher keeps its collector stopped while DNS points to primary.

## Failure behavior

| Script outcome | Controller behavior |
|---|---|
| Set exits nonzero | Write fails; loop reports failure and backs off. |
| Set times out | Write fails; the next loop must re-read before retrying. |
| Set exits 0 but readback differs | Write is rejected as unverifiable. |
| Get exits nonzero | Lease becomes unreadable; no promotion. |
| Get prints invalid JSON | Lease becomes unreadable; no promotion. |
| Get returns only A or only TXT | Lease is incomplete; no promotion. |
| Get returns wrong owner/address pair | Lease is incoherent; no promotion. |

## Production checklist

- [ ] Scripts use absolute container paths and executable shebangs.
- [ ] A and TXT are updated atomically, or TXT is written before A.
- [ ] Get reads authoritative state and emits one JSON object.
- [ ] Exactly one A value is managed.
- [ ] Provider authentication follows least privilege.
- [ ] Secrets are files, not source code, arguments, or log output.
- [ ] API requests verify TLS certificates.
- [ ] Errors and partial outcomes return nonzero.
- [ ] Both containerized `validate` commands pass.
- [ ] `init` readback is coherent before either full stack starts.
- [ ] Failover, DNS propagation, and reverse recovery have been rehearsed in a test zone.
