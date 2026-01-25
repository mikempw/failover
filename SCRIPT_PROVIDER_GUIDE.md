# Custom Script Provider Guide

Your DNS provider isn't built-in? No problem. The `script` provider lets you integrate **any** DNS system using two simple scripts.

---

## Overview

You create two scripts:

| Script | Purpose | When called |
|--------|---------|-------------|
| `set_dns.sh` | Update DNS records | Every heartbeat (primary) or on failover (DR) |
| `get_dns.sh` | Query current DNS state | DR checking if it should take over |

These can be written in **any language**: bash, python, powershell, ruby, go, etc.

---

## Quick Start

### 1. Create your scripts directory

```bash
mkdir -p /opt/dns-failover/scripts
cd /opt/dns-failover/scripts
```

### 2. Create set_dns.sh

```bash
#!/bin/bash
# Receives: $1=record $2=ip $3=owner $4=expiry $5=ttl $6=zone
# Must: Update A record and TXT record
# Exit: 0=success, non-zero=failure

RECORD="$1"
IP="$2"
OWNER="$3"
EXPIRY="$4"
TTL="$5"
ZONE="$6"

# YOUR CODE HERE - call your DNS API
# Example:
# curl -X PUT "https://your-dns/api/record" -d '{"name":"'$RECORD'","ip":"'$IP'"}'

exit 0
```

### 3. Create get_dns.sh

```bash
#!/bin/bash
# Receives: $1=record $2=zone
# Must: Output JSON to stdout
# Exit: 0=success, non-zero=failure

RECORD="$1"

# YOUR CODE HERE - query your DNS
# Must output this exact JSON format:
echo '{"A": "10.10.10.10", "TXT": "owner=primary exp=1699567890"}'

exit 0
```

### 4. Make executable

```bash
chmod +x set_dns.sh get_dns.sh
```

### 5. Configure .env

```bash
DNS_PROVIDER=script
SCRIPT_SET=/scripts/set_dns.sh
SCRIPT_GET=/scripts/get_dns.sh
```

### 6. Run with scripts mounted

```bash
docker run -d --name dns-failover \
  --env-file .env \
  -v /opt/dns-failover/scripts:/scripts \
  dns-failover
```

---

## Script Interface Reference

### set_dns.sh

**Called when:** DNS needs to be updated (heartbeat renewal or failover)

**Arguments (positional):**

| Position | Variable | Example | Description |
|----------|----------|---------|-------------|
| $1 | RECORD | syslog.example.com | FQDN to update |
| $2 | IP | 10.10.10.10 | IP address to set |
| $3 | OWNER | primary | Who owns the lease (primary or dr) |
| $4 | EXPIRY | 1699567890 | Unix timestamp when lease expires |
| $5 | TTL | 30 | DNS TTL in seconds |
| $6 | ZONE | example.com | DNS zone |

**Environment variables (same data, alternative access):**

```bash
$DNS_RECORD    # syslog.example.com
$DNS_IP        # 10.10.10.10
$DNS_OWNER     # primary
$DNS_EXPIRY    # 1699567890
$DNS_TTL       # 30
$DNS_ZONE      # example.com
$DNS_SERVER    # (if configured)
```

**What it must do:**
1. Set/update A record: `RECORD → IP`
2. Set/update TXT record: `RECORD → "owner=OWNER exp=EXPIRY"`

**Exit codes:**
- `0` = Success
- Non-zero = Failure (will be logged, retried next interval)

---

### get_dns.sh

**Called when:** DR needs to check current DNS state

**Arguments (positional):**

| Position | Variable | Example | Description |
|----------|----------|---------|-------------|
| $1 | RECORD | syslog.example.com | FQDN to query |
| $2 | ZONE | example.com | DNS zone |

**Environment variables:**

```bash
$DNS_RECORD    # syslog.example.com
$DNS_ZONE      # example.com
$DNS_SERVER    # (if configured)
```

**What it must output (stdout):**

```json
{"A": "10.10.10.10", "TXT": "owner=primary exp=1699567890"}
```

**If records don't exist:**

```json
{"A": null, "TXT": null}
```

**Exit codes:**
- `0` = Success (even if records don't exist)
- Non-zero = Failure (will be logged as warning)

---

## Examples

### PowerDNS

```bash
#!/bin/bash
# set_dns.sh for PowerDNS

RECORD="$1"
IP="$2"
OWNER="$3"
EXPIRY="$4"
TTL="$5"
ZONE="$6"

API="http://powerdns.local:8081/api/v1"
KEY="your-api-key"

# Update A record
curl -s -X PATCH "$API/servers/localhost/zones/$ZONE." \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "rrsets": [{
      "name": "'$RECORD'.",
      "type": "A",
      "ttl": '$TTL',
      "changetype": "REPLACE",
      "records": [{"content": "'$IP'", "disabled": false}]
    }]
  }'

# Update TXT record
curl -s -X PATCH "$API/servers/localhost/zones/$ZONE." \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "rrsets": [{
      "name": "'$RECORD'.",
      "type": "TXT",
      "ttl": '$TTL',
      "changetype": "REPLACE",
      "records": [{"content": "\"owner='$OWNER' exp='$EXPIRY'\"", "disabled": false}]
    }]
  }'

exit 0
```

```bash
#!/bin/bash
# get_dns.sh for PowerDNS

RECORD="$1"
ZONE="$2"

API="http://powerdns.local:8081/api/v1"
KEY="your-api-key"

# Get zone data
DATA=$(curl -s "$API/servers/localhost/zones/$ZONE." -H "X-API-Key: $KEY")

# Extract A record
A_RECORD=$(echo "$DATA" | jq -r '.rrsets[] | select(.name=="'$RECORD'." and .type=="A") | .records[0].content')

# Extract TXT record
TXT_RECORD=$(echo "$DATA" | jq -r '.rrsets[] | select(.name=="'$RECORD'." and .type=="TXT") | .records[0].content' | tr -d '"')

# Output JSON
[ -z "$A_RECORD" ] && A_RECORD="null" || A_RECORD="\"$A_RECORD\""
[ -z "$TXT_RECORD" ] && TXT_RECORD="null" || TXT_RECORD="\"$TXT_RECORD\""

echo "{\"A\": $A_RECORD, \"TXT\": $TXT_RECORD}"
```

---

### BlueCat Address Manager

```bash
#!/bin/bash
# set_dns.sh for BlueCat

RECORD="$1"
IP="$2"
OWNER="$3"
EXPIRY="$4"
TTL="$5"
ZONE="$6"

# Login and get token
TOKEN=$(curl -s -X POST "https://$BLUECAT_HOST/Services/REST/v1/login" \
  -d "username=$BLUECAT_USER&password=$BLUECAT_PASS" | jq -r '.apiToken')

# Get configuration ID (adjust for your setup)
CONFIG_ID="$BLUECAT_CONFIG_ID"

# Add/update host record
curl -s -X PUT "https://$BLUECAT_HOST/Services/REST/v1/addHostRecord" \
  -H "Authorization: BAMAuthToken: $TOKEN" \
  -d "configurationId=$CONFIG_ID&absoluteName=$RECORD&addresses=$IP&ttl=$TTL"

# Add TXT record for lease tracking
curl -s -X PUT "https://$BLUECAT_HOST/Services/REST/v1/addTXTRecord" \
  -H "Authorization: BAMAuthToken: $TOKEN" \
  -d "configurationId=$CONFIG_ID&absoluteName=$RECORD&txt=owner=$OWNER exp=$EXPIRY"

# Logout
curl -s -X POST "https://$BLUECAT_HOST/Services/REST/v1/logout" \
  -H "Authorization: BAMAuthToken: $TOKEN"

exit 0
```

---

### Windows DNS (PowerShell)

```powershell
# set_dns.ps1 for Windows DNS Server

param(
    [string]$Record,
    [string]$IP,
    [string]$Owner,
    [string]$Expiry,
    [int]$TTL,
    [string]$Zone
)

$DnsServer = $env:DNS_SERVER

# Extract hostname from FQDN
$Hostname = $Record -replace "\.$Zone$", ""

# Remove existing records
Remove-DnsServerResourceRecord -ZoneName $Zone -Name $Hostname -RRType A -Force -ErrorAction SilentlyContinue
Remove-DnsServerResourceRecord -ZoneName $Zone -Name $Hostname -RRType TXT -Force -ErrorAction SilentlyContinue

# Add A record
Add-DnsServerResourceRecord -ZoneName $Zone -Name $Hostname -A -IPv4Address $IP -TimeToLive (New-TimeSpan -Seconds $TTL)

# Add TXT record
Add-DnsServerResourceRecord -ZoneName $Zone -Name $Hostname -Txt -DescriptiveText "owner=$Owner exp=$Expiry" -TimeToLive (New-TimeSpan -Seconds $TTL)

exit 0
```

```powershell
# get_dns.ps1 for Windows DNS Server

param(
    [string]$Record,
    [string]$Zone
)

$Hostname = $Record -replace "\.$Zone$", ""

try {
    $A = (Get-DnsServerResourceRecord -ZoneName $Zone -Name $Hostname -RRType A -ErrorAction Stop).RecordData.IPv4Address.ToString()
} catch {
    $A = $null
}

try {
    $TXT = (Get-DnsServerResourceRecord -ZoneName $Zone -Name $Hostname -RRType TXT -ErrorAction Stop).RecordData.DescriptiveText
} catch {
    $TXT = $null
}

# Output JSON
$result = @{
    A = $A
    TXT = $TXT
} | ConvertTo-Json -Compress

Write-Output $result
```

**Wrapper for PowerShell scripts (bash):**

```bash
#!/bin/bash
# set_dns.sh - wrapper for PowerShell

pwsh /scripts/set_dns.ps1 -Record "$1" -IP "$2" -Owner "$3" -Expiry "$4" -TTL "$5" -Zone "$6"
```

---

### Python Example (Generic REST API)

```python
#!/usr/bin/env python3
# set_dns.py - Generic REST API example

import sys
import os
import requests

def main():
    record = sys.argv[1]
    ip = sys.argv[2]
    owner = sys.argv[3]
    expiry = sys.argv[4]
    ttl = sys.argv[5]
    zone = sys.argv[6]
    
    api_url = os.environ['DNS_API_URL']
    api_key = os.environ['DNS_API_KEY']
    
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json'
    }
    
    # Update A record
    requests.put(
        f'{api_url}/zones/{zone}/records/{record}/A',
        headers=headers,
        json={'value': ip, 'ttl': int(ttl)}
    ).raise_for_status()
    
    # Update TXT record
    requests.put(
        f'{api_url}/zones/{zone}/records/{record}/TXT',
        headers=headers,
        json={'value': f'owner={owner} exp={expiry}', 'ttl': int(ttl)}
    ).raise_for_status()

if __name__ == '__main__':
    main()
```

---

### Using dig (Fallback for get_dns.sh)

If your DNS is standard and queryable, this works for most setups:

```bash
#!/bin/bash
# get_dns.sh - Universal using dig

RECORD="$1"
SERVER="${DNS_SERVER:-}"

if [ -n "$SERVER" ]; then
    DIG_ARGS="@$SERVER"
else
    DIG_ARGS=""
fi

# Query A record
A_RECORD=$(dig $DIG_ARGS +short "$RECORD" A 2>/dev/null | head -1)

# Query TXT record  
TXT_RECORD=$(dig $DIG_ARGS +short "$RECORD" TXT 2>/dev/null | tr -d '"' | head -1)

# Format as JSON
if [ -z "$A_RECORD" ]; then
    A_JSON="null"
else
    A_JSON="\"$A_RECORD\""
fi

if [ -z "$TXT_RECORD" ]; then
    TXT_JSON="null"
else
    TXT_JSON="\"$TXT_RECORD\""
fi

echo "{\"A\": $A_JSON, \"TXT\": $TXT_JSON}"
exit 0
```

---

## Passing Credentials

**Option 1: Environment variables in .env**

```bash
# .env
DNS_PROVIDER=script
SCRIPT_SET=/scripts/set_dns.sh
SCRIPT_GET=/scripts/get_dns.sh

# Your custom credentials (available in scripts as $VAR)
BLUECAT_HOST=bluecat.company.local
BLUECAT_USER=admin
BLUECAT_PASS=secret123
```

**Option 2: Mount a secrets file**

```bash
docker run -d \
  -v /opt/secrets/dns-creds.env:/secrets/dns-creds.env \
  dns-failover
```

In your script:
```bash
source /secrets/dns-creds.env
```

**Option 3: Use Vault**

Configure Vault integration in .env, and your credentials will be loaded automatically.

---

## Testing Your Scripts

### Test set_dns.sh

```bash
# Run manually
./set_dns.sh test.example.com 10.10.10.10 primary 9999999999 30 example.com

# Check your DNS to verify it worked
dig test.example.com A
dig test.example.com TXT
```

### Test get_dns.sh

```bash
# Run manually
./get_dns.sh test.example.com example.com

# Should output:
# {"A": "10.10.10.10", "TXT": "owner=primary exp=9999999999"}
```

### Validate JSON output

```bash
./get_dns.sh test.example.com example.com | jq .
```

### Test in container

```bash
docker run --rm -it \
  -v $(pwd)/scripts:/scripts \
  -e DNS_PROVIDER=script \
  -e SCRIPT_SET=/scripts/set_dns.sh \
  -e SCRIPT_GET=/scripts/get_dns.sh \
  dns-failover python3 /app/dns_failover.py validate
```

---

## Troubleshooting

### Script not found

```
Configuration errors: SCRIPT_SET not found: /scripts/set_dns.sh
```

**Fix:** Check volume mount and path.

### Script not executable

```
Configuration errors: SCRIPT_SET not executable: /scripts/set_dns.sh
```

**Fix:** `chmod +x /path/to/script.sh`

### Invalid JSON from get_dns.sh

```
SCRIPT_GET returned invalid JSON
```

**Fix:** Ensure your script outputs valid JSON. Test with `| jq .`

### Timeout

```
SCRIPT_GET timed out
```

**Fix:** Scripts have 30s timeout. Optimize or increase network timeout in your API calls.

---

## Best Practices

1. **Keep scripts simple** - Do one thing: update or query DNS
2. **Handle errors** - Exit non-zero on failure
3. **Log to stderr** - Use `echo "message" >&2` for debugging (stdout is for JSON output in get_dns.sh)
4. **Test locally first** - Verify scripts work before deploying
5. **Use environment variables** - Don't hardcode credentials
6. **Set timeouts** - Add timeouts to API calls to prevent hangs

---

## Need Help?

If you're stuck:

1. Test scripts manually first
2. Check container logs: `docker logs dns-failover`
3. Validate with: `python3 /app/dns_failover.py validate`
4. Open an issue with your DNS provider details
