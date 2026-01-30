#!/usr/bin/env python3
"""
AST DNS Failover - Interactive Setup Wizard
Walks users through configuration and generates .env files.
"""

import os
import sys
import json
import getpass

# ANSI colors
GREEN = '\033[92m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
RED = '\033[91m'
BOLD = '\033[1m'
RESET = '\033[0m'

def print_header(text):
    print(f"\n{BOLD}{BLUE}{'='*60}{RESET}")
    print(f"{BOLD}{BLUE}{text.center(60)}{RESET}")
    print(f"{BOLD}{BLUE}{'='*60}{RESET}\n")

def print_step(num, text):
    print(f"\n{BOLD}{GREEN}[Step {num}]{RESET} {text}")

def print_info(text):
    print(f"  {BLUE}ℹ{RESET}  {text}")

def print_warn(text):
    print(f"  {YELLOW}⚠{RESET}  {text}")

def print_error(text):
    print(f"  {RED}✗{RESET}  {text}")

def print_success(text):
    print(f"  {GREEN}✓{RESET}  {text}")

def ask(prompt, default=None, required=True, password=False):
    """Ask user for input with optional default."""
    if default:
        prompt = f"{prompt} [{default}]: "
    else:
        prompt = f"{prompt}: "
    
    while True:
        if password:
            value = getpass.getpass(prompt)
        else:
            value = input(prompt)
        
        if not value and default:
            return default
        if not value and required:
            print_error("This field is required")
            continue
        if value or not required:
            return value

def ask_choice(prompt, choices, default=None):
    """Ask user to choose from a list."""
    print(f"\n{prompt}")
    for i, (key, desc) in enumerate(choices, 1):
        marker = " (default)" if key == default else ""
        print(f"  {i}. {key} - {desc}{marker}")
    
    while True:
        choice = input(f"\nEnter choice [1-{len(choices)}]: ").strip()
        if not choice and default:
            return default
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(choices):
                return choices[idx][0]
        except ValueError:
            # Check if they typed the key directly
            for key, _ in choices:
                if choice.lower() == key.lower():
                    return key
        print_error(f"Please enter a number between 1 and {len(choices)}")

def ask_bool(prompt, default=True):
    """Ask yes/no question."""
    default_str = "Y/n" if default else "y/N"
    while True:
        value = input(f"{prompt} [{default_str}]: ").strip().lower()
        if not value:
            return default
        if value in ('y', 'yes'):
            return True
        if value in ('n', 'no'):
            return False
        print_error("Please enter 'y' or 'n'")

def test_provider_connection(provider, config):
    """Test connection to the DNS provider."""
    print_info("Testing connection...")
    
    try:
        if provider == 'cloudflare':
            import requests
            resp = requests.get(
                f"https://api.cloudflare.com/client/v4/zones/{config['CLOUDFLARE_ZONE_ID']}",
                headers={'Authorization': f"Bearer {config['CLOUDFLARE_API_TOKEN']}"},
                timeout=10
            )
            if resp.status_code == 200:
                zone_name = resp.json()['result']['name']
                print_success(f"Connected to Cloudflare zone: {zone_name}")
                return True
            else:
                print_error(f"Cloudflare API error: {resp.json().get('errors', [{}])[0].get('message', 'Unknown error')}")
                return False
        
        elif provider == 'route53':
            import boto3
            client = boto3.client(
                'route53',
                aws_access_key_id=config['AWS_ACCESS_KEY_ID'],
                aws_secret_access_key=config['AWS_SECRET_ACCESS_KEY'],
                region_name=config.get('AWS_REGION', 'us-east-1')
            )
            resp = client.get_hosted_zone(Id=config['ROUTE53_ZONE_ID'])
            zone_name = resp['HostedZone']['Name']
            print_success(f"Connected to Route53 zone: {zone_name}")
            return True
        
        elif provider == 'azure-dns':
            import requests
            token_url = f"https://login.microsoftonline.com/{config['AZURE_TENANT_ID']}/oauth2/v2.0/token"
            token_resp = requests.post(token_url, data={
                'grant_type': 'client_credentials',
                'client_id': config['AZURE_CLIENT_ID'],
                'client_secret': config['AZURE_CLIENT_SECRET'],
                'scope': 'https://management.azure.com/.default'
            }, timeout=10)
            if token_resp.status_code == 200:
                print_success("Azure authentication successful")
                return True
            else:
                print_error("Azure authentication failed")
                return False
        
        elif provider == 'gcp-dns':
            print_info("GCP credentials will be validated at runtime")
            return True
        
        elif provider == 'f5-gtm':
            import requests
            resp = requests.get(
                f"https://{config['F5_HOST']}/mgmt/tm/sys/version",
                auth=(config['F5_USERNAME'], config['F5_PASSWORD']),
                verify=config.get('F5_VERIFY_SSL', 'true').lower() == 'true',
                timeout=10
            )
            if resp.status_code == 200:
                version = resp.json()['entries'].values().__iter__().__next__()['nestedStats']['entries']['Version']['description']
                print_success(f"Connected to F5 BIG-IP version: {version}")
                return True
            else:
                print_error("F5 connection failed")
                return False
        
        elif provider == 'infoblox':
            import requests
            resp = requests.get(
                f"https://{config['INFOBLOX_HOST']}/wapi/{config.get('INFOBLOX_WAPI_VERSION', 'v2.11')}/grid",
                auth=(config['INFOBLOX_USERNAME'], config['INFOBLOX_PASSWORD']),
                verify=config.get('INFOBLOX_VERIFY_SSL', 'true').lower() == 'true',
                timeout=10
            )
            if resp.status_code == 200:
                print_success("Connected to Infoblox")
                return True
            else:
                print_error("Infoblox connection failed")
                return False
        
        elif provider in ('bind-tsig', 'ad-gss'):
            print_info("DNS server connection will be validated at runtime")
            return True
        
        elif provider == 'dry-run':
            print_success("Dry-run mode - no connection needed")
            return True
        
        elif provider == 'script':
            print_info("Script provider - connection will be tested when scripts are executed")
            # Check if script files exist (if paths were provided)
            if config.get('SCRIPT_SET') and not config['SCRIPT_SET'].startswith('/scripts'):
                if os.path.exists(config['SCRIPT_SET']):
                    print_success(f"Found set script: {config['SCRIPT_SET']}")
                else:
                    print_warn(f"Set script not found yet: {config['SCRIPT_SET']}")
            return True
        
        return True
        
    except ImportError as e:
        print_warn(f"Cannot test connection - missing library: {e}")
        print_info("Connection will be validated at runtime")
        return True
    except Exception as e:
        print_error(f"Connection test failed: {e}")
        return False

def setup_cloudflare(config):
    """Setup Cloudflare provider."""
    print_step(3, "Cloudflare Configuration")
    print_info("You'll need an API token with 'Edit zone DNS' permissions")
    print_info("Create one at: https://dash.cloudflare.com/profile/api-tokens")
    print()
    
    config['CLOUDFLARE_API_TOKEN'] = ask("API Token", password=True)
    
    # Try to list zones to help user find zone ID
    print_info("Fetching your zones...")
    try:
        import requests
        resp = requests.get(
            "https://api.cloudflare.com/client/v4/zones",
            headers={'Authorization': f"Bearer {config['CLOUDFLARE_API_TOKEN']}"},
            timeout=10
        )
        if resp.status_code == 200:
            zones = resp.json()['result']
            if zones:
                print("\nYour zones:")
                for z in zones[:10]:
                    print(f"  - {z['name']} (ID: {z['id']})")
                print()
    except:
        pass
    
    config['CLOUDFLARE_ZONE_ID'] = ask("Zone ID")

def setup_route53(config):
    """Setup AWS Route53 provider."""
    print_step(3, "AWS Route53 Configuration")
    print_info("You'll need an IAM user with Route53 permissions")
    print_info("Required permissions: route53:ChangeResourceRecordSets, route53:ListResourceRecordSets")
    print()
    
    config['AWS_ACCESS_KEY_ID'] = ask("AWS Access Key ID")
    config['AWS_SECRET_ACCESS_KEY'] = ask("AWS Secret Access Key", password=True)
    config['AWS_REGION'] = ask("AWS Region", default="us-east-1")
    
    # Try to list hosted zones
    print_info("Fetching your hosted zones...")
    try:
        import boto3
        client = boto3.client(
            'route53',
            aws_access_key_id=config['AWS_ACCESS_KEY_ID'],
            aws_secret_access_key=config['AWS_SECRET_ACCESS_KEY'],
            region_name=config['AWS_REGION']
        )
        resp = client.list_hosted_zones()
        zones = resp['HostedZones']
        if zones:
            print("\nYour hosted zones:")
            for z in zones[:10]:
                print(f"  - {z['Name']} (ID: {z['Id'].split('/')[-1]})")
            print()
    except:
        pass
    
    config['ROUTE53_ZONE_ID'] = ask("Hosted Zone ID (e.g., Z1234567890ABC)")

def setup_azure(config):
    """Setup Azure DNS provider."""
    print_step(3, "Azure DNS Configuration")
    print_info("You'll need a Service Principal with DNS Zone Contributor role")
    print_info("Create one with: az ad sp create-for-rbac --name dns-failover --role 'DNS Zone Contributor'")
    print()
    
    config['AZURE_SUBSCRIPTION_ID'] = ask("Subscription ID")
    config['AZURE_RESOURCE_GROUP'] = ask("Resource Group (containing DNS zone)")
    config['AZURE_TENANT_ID'] = ask("Tenant ID (Directory ID)")
    config['AZURE_CLIENT_ID'] = ask("Client ID (Application ID)")
    config['AZURE_CLIENT_SECRET'] = ask("Client Secret", password=True)

def setup_gcp(config):
    """Setup GCP DNS provider."""
    print_step(3, "Google Cloud DNS Configuration")
    print_info("You'll need a Service Account with 'DNS Administrator' role")
    print_info("Create one in: IAM & Admin > Service Accounts")
    print()
    
    config['GCP_PROJECT_ID'] = ask("Project ID")
    config['GCP_MANAGED_ZONE'] = ask("Managed Zone Name (not the DNS name)")
    
    use_file = ask_bool("Use credentials file? (No = use Application Default Credentials)", default=True)
    if use_file:
        config['GCP_CREDENTIALS_FILE'] = ask("Path to credentials JSON file")
    else:
        print_info("Make sure ADC is configured: gcloud auth application-default login")

def setup_f5(config):
    """Setup F5 GTM provider."""
    print_step(3, "F5 BIG-IP DNS (GTM) Configuration")
    print_info("You'll need admin access to your F5 device")
    print()
    
    config['F5_HOST'] = ask("F5 Management IP/Hostname")
    config['F5_USERNAME'] = ask("Username", default="admin")
    config['F5_PASSWORD'] = ask("Password", password=True)
    config['F5_PARTITION'] = ask("Partition", default="Common")
    config['F5_VERIFY_SSL'] = "true" if ask_bool("Verify SSL certificate?", default=True) else "false"
    
    use_pool = ask_bool("Do you have a GTM pool to manage?", default=False)
    if use_pool:
        config['F5_POOL_NAME'] = ask("GTM Pool Name")

def setup_infoblox(config):
    """Setup Infoblox provider."""
    print_step(3, "Infoblox Configuration")
    print_info("You'll need API access to your Infoblox Grid Manager")
    print()
    
    config['INFOBLOX_HOST'] = ask("Infoblox Grid Manager hostname/IP")
    config['INFOBLOX_USERNAME'] = ask("Username")
    config['INFOBLOX_PASSWORD'] = ask("Password", password=True)
    config['INFOBLOX_WAPI_VERSION'] = ask("WAPI Version", default="v2.11")
    config['INFOBLOX_VERIFY_SSL'] = "true" if ask_bool("Verify SSL certificate?", default=True) else "false"

def setup_bind(config):
    """Setup BIND TSIG provider."""
    print_step(3, "BIND DNS Configuration")
    print_info("You'll need a TSIG key configured on your BIND server")
    print_info("Generate one with: tsig-keygen failover-key > /etc/bind/keys/failover.key")
    print()
    
    config['DNS_SERVER'] = ask("BIND Server IP")
    config['TSIG_KEYFILE'] = ask("Path to TSIG key file", default="/secrets/tsig.key")
    
    print()
    print_info("Make sure your named.conf has:")
    print(f'  include "{config["TSIG_KEYFILE"]}";')
    print(f'  zone "{config.get("DNS_ZONE", "example.local")}" {{')
    print('      allow-update { key "failover-key"; };')
    print('  };')

def setup_ad(config):
    """Setup Active Directory DNS provider."""
    print_step(3, "Active Directory DNS Configuration")
    print_info("You'll need a service account with DNS update permissions")
    print_info("And Kerberos configured in the container")
    print()
    
    config['DNS_SERVER'] = ask("AD DNS Server IP")
    
    print()
    print_warn("AD DNS requires additional container setup:")
    print("  1. Configure /etc/krb5.conf with your AD realm")
    print("  2. Create a keytab: ktutil or msktutil")
    print("  3. Mount the keytab and refresh tickets periodically")

def setup_script(config, output_dir):
    """Setup custom script provider."""
    print_step(3, "Custom Script Provider")
    print()
    print(f"{BOLD}═══════════════════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}  Your DNS provider isn't built-in? No problem!{RESET}")
    print(f"{BOLD}  You can integrate ANY DNS system using simple scripts.{RESET}")
    print(f"{BOLD}═══════════════════════════════════════════════════════════════{RESET}")
    print()
    print("You'll create two small scripts:")
    print()
    print(f"  {GREEN}1. set_dns.sh{RESET} - Updates your DNS when failover happens")
    print(f"  {GREEN}2. get_dns.sh{RESET} - Checks current DNS state")
    print()
    print("These can be written in ANY language: bash, python, powershell, etc.")
    print()
    
    input(f"Press {BOLD}Enter{RESET} to continue and see examples...")
    
    print()
    print(f"{BOLD}════════════════════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}  SCRIPT 1: set_dns.sh (Updates DNS){RESET}")
    print(f"{BOLD}════════════════════════════════════════════════════════════════{RESET}")
    print()
    print("This script is called when DNS needs to be updated.")
    print("It receives these arguments:")
    print()
    print(f"  {BLUE}$1{RESET} = DNS record    (e.g., syslog.example.com)")
    print(f"  {BLUE}$2{RESET} = IP address    (e.g., 10.10.10.10)")
    print(f"  {BLUE}$3{RESET} = Owner         (primary or dr)")
    print(f"  {BLUE}$4{RESET} = Expiry        (unix timestamp)")
    print(f"  {BLUE}$5{RESET} = TTL           (seconds)")
    print(f"  {BLUE}$6{RESET} = DNS zone      (e.g., example.com)")
    print()
    print("Also available as environment variables:")
    print(f"  {BLUE}$DNS_RECORD, $DNS_IP, $DNS_OWNER, $DNS_EXPIRY, $DNS_TTL, $DNS_ZONE{RESET}")
    print()
    print(f"{YELLOW}Example for BlueCat:{RESET}")
    print("""
    #!/bin/bash
    # set_dns.sh - Update BlueCat DNS
    
    RECORD="$1"
    IP="$2"
    OWNER="$3"
    EXPIRY="$4"
    
    # Call BlueCat API (example - adjust for your setup)
    curl -X PUT "https://bluecat.company.local/api/v2/records/$RECORD" \\
      -H "Authorization: Bearer $BLUECAT_TOKEN" \\
      -H "Content-Type: application/json" \\
      -d '{"type": "A", "value": "'$IP'", "ttl": 30}'
    
    # Also set TXT record for lease tracking
    curl -X PUT "https://bluecat.company.local/api/v2/records/$RECORD/txt" \\
      -H "Authorization: Bearer $BLUECAT_TOKEN" \\
      -d '{"value": "owner='$OWNER' exp='$EXPIRY'"}'
    
    exit 0  # Exit 0 = success, anything else = failure
    """)
    
    input(f"Press {BOLD}Enter{RESET} to see the GET script example...")
    
    print()
    print(f"{BOLD}════════════════════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}  SCRIPT 2: get_dns.sh (Query DNS){RESET}")
    print(f"{BOLD}════════════════════════════════════════════════════════════════{RESET}")
    print()
    print("This script checks the current DNS state.")
    print("It receives these arguments:")
    print()
    print(f"  {BLUE}$1{RESET} = DNS record    (e.g., syslog.example.com)")
    print(f"  {BLUE}$2{RESET} = DNS zone      (e.g., example.com)")
    print()
    print(f"{YELLOW}IMPORTANT:{RESET} Output must be JSON on stdout:")
    print(f'  {GREEN}{{"A": "10.10.10.10", "TXT": "owner=primary exp=1699567890"}}{RESET}')
    print()
    print(f"{YELLOW}Example for BlueCat:{RESET}")
    print("""
    #!/bin/bash
    # get_dns.sh - Query BlueCat DNS
    
    RECORD="$1"
    
    # Get A record
    A_RECORD=$(curl -s "https://bluecat.company.local/api/v2/records/$RECORD" \\
      -H "Authorization: Bearer $BLUECAT_TOKEN" | jq -r '.value')
    
    # Get TXT record
    TXT_RECORD=$(curl -s "https://bluecat.company.local/api/v2/records/$RECORD/txt" \\
      -H "Authorization: Bearer $BLUECAT_TOKEN" | jq -r '.value')
    
    # Output JSON (this format is required!)
    echo '{"A": "'$A_RECORD'", "TXT": "'$TXT_RECORD'"}'
    
    exit 0
    """)
    
    print()
    print(f"{BOLD}════════════════════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}  Let's set up your scripts{RESET}")
    print(f"{BOLD}════════════════════════════════════════════════════════════════{RESET}")
    print()
    
    # Ask if they want template scripts generated
    generate_templates = ask_bool("Would you like me to generate template scripts to get you started?", default=True)
    
    if generate_templates:
        scripts_dir = os.path.join(output_dir, 'scripts')
        os.makedirs(scripts_dir, exist_ok=True)
        
        # Generate set_dns.sh template
        set_script = os.path.join(scripts_dir, 'set_dns.sh')
        with open(set_script, 'w') as f:
            f.write('''#!/bin/bash
#
# set_dns.sh - Custom DNS Update Script
# 
# This script is called when DNS needs to be updated.
# Modify this to work with YOUR DNS provider.
#
# Arguments:
#   $1 = DNS record (FQDN, e.g., syslog.example.com)
#   $2 = IP address (e.g., 10.10.10.10)
#   $3 = Owner (primary or dr)
#   $4 = Expiry (unix timestamp)
#   $5 = TTL (seconds)
#   $6 = DNS zone (e.g., example.com)
#
# Environment variables (same data, for convenience):
#   DNS_RECORD, DNS_IP, DNS_OWNER, DNS_EXPIRY, DNS_TTL, DNS_ZONE, DNS_SERVER
#
# Exit codes:
#   0 = Success
#   Non-zero = Failure (will be logged as error)
#

set -e  # Exit on any error

RECORD="$1"
IP="$2"
OWNER="$3"
EXPIRY="$4"
TTL="$5"
ZONE="$6"

echo "Updating DNS: $RECORD -> $IP (owner=$OWNER, exp=$EXPIRY)" >&2

# ╔════════════════════════════════════════════════════════════════╗
# ║  CUSTOMIZE THIS SECTION FOR YOUR DNS PROVIDER                 ║
# ╚════════════════════════════════════════════════════════════════╝

# Example: Using curl to call a REST API
# 
# curl -X PUT "https://your-dns-provider.local/api/records/$RECORD" \\
#   -H "Authorization: Bearer $YOUR_API_TOKEN" \\
#   -H "Content-Type: application/json" \\
#   -d '{
#     "type": "A",
#     "value": "'$IP'",
#     "ttl": '$TTL'
#   }'
#
# curl -X PUT "https://your-dns-provider.local/api/records/$RECORD/txt" \\
#   -H "Authorization: Bearer $YOUR_API_TOKEN" \\
#   -d '{"value": "owner='$OWNER' exp='$EXPIRY'"}'

# Example: Using nsupdate
#
# nsupdate <<EOF
# server $DNS_SERVER
# zone $ZONE
# update delete $RECORD A
# update delete $RECORD TXT
# update add $RECORD $TTL A $IP
# update add $RECORD $TTL TXT "owner=$OWNER exp=$EXPIRY"
# send
# EOF

# Example: Using PowerShell (for Windows DNS)
#
# powershell.exe -Command "
#   Remove-DnsServerResourceRecord -ZoneName $ZONE -Name $RECORD -RRType A -Force
#   Add-DnsServerResourceRecord -ZoneName $ZONE -Name $RECORD -A -IPv4Address $IP -TimeToLive 00:00:$TTL
# "

# ╔════════════════════════════════════════════════════════════════╗
# ║  REMOVE THIS ERROR ONCE YOU'VE CUSTOMIZED THE SCRIPT          ║
# ╚════════════════════════════════════════════════════════════════╝
echo "ERROR: This is a template script. Please customize it for your DNS provider." >&2
echo "Edit: scripts/set_dns.sh" >&2
exit 1

# If we get here, everything worked
echo "DNS updated successfully" >&2
exit 0
''')
        os.chmod(set_script, 0o755)
        print_success(f"Created {set_script}")
        
        # Generate get_dns.sh template
        get_script = os.path.join(scripts_dir, 'get_dns.sh')
        with open(get_script, 'w') as f:
            f.write('''#!/bin/bash
#
# get_dns.sh - Custom DNS Query Script
#
# This script queries the current DNS state.
# Modify this to work with YOUR DNS provider.
#
# Arguments:
#   $1 = DNS record (FQDN, e.g., syslog.example.com)
#   $2 = DNS zone (e.g., example.com)
#
# Environment variables:
#   DNS_RECORD, DNS_ZONE, DNS_SERVER
#
# REQUIRED OUTPUT (JSON on stdout):
#   {"A": "10.10.10.10", "TXT": "owner=primary exp=1699567890"}
#
# If records don't exist, output:
#   {"A": null, "TXT": null}
#
# Exit codes:
#   0 = Success (even if records don't exist)
#   Non-zero = Failure (will be logged as warning)
#

RECORD="$1"
ZONE="$2"

# ╔════════════════════════════════════════════════════════════════╗
# ║  CUSTOMIZE THIS SECTION FOR YOUR DNS PROVIDER                 ║
# ╚════════════════════════════════════════════════════════════════╝

# Example: Using dig (works for any DNS server)
#
# A_RECORD=$(dig +short "$RECORD" A | head -1)
# TXT_RECORD=$(dig +short "$RECORD" TXT | tr -d '"')
# 
# # Handle empty results
# [ -z "$A_RECORD" ] && A_RECORD="null" || A_RECORD="\"$A_RECORD\""
# [ -z "$TXT_RECORD" ] && TXT_RECORD="null" || TXT_RECORD="\"$TXT_RECORD\""
# 
# echo "{\"A\": $A_RECORD, \"TXT\": $TXT_RECORD}"

# Example: Using curl to call a REST API
#
# RESPONSE=$(curl -s "https://your-dns-provider.local/api/records/$RECORD" \\
#   -H "Authorization: Bearer $YOUR_API_TOKEN")
# 
# A_RECORD=$(echo "$RESPONSE" | jq -r '.a_record // empty')
# TXT_RECORD=$(echo "$RESPONSE" | jq -r '.txt_record // empty')
# 
# [ -z "$A_RECORD" ] && A_RECORD="null" || A_RECORD="\"$A_RECORD\""
# [ -z "$TXT_RECORD" ] && TXT_RECORD="null" || TXT_RECORD="\"$TXT_RECORD\""
# 
# echo "{\"A\": $A_RECORD, \"TXT\": $TXT_RECORD}"

# ╔════════════════════════════════════════════════════════════════╗
# ║  REMOVE THIS SECTION ONCE YOU'VE CUSTOMIZED THE SCRIPT        ║
# ╚════════════════════════════════════════════════════════════════╝

# Template: Just use dig as a default (works for most setups)
A_RECORD=$(dig +short "$RECORD" A 2>/dev/null | head -1)
TXT_RECORD=$(dig +short "$RECORD" TXT 2>/dev/null | tr -d '"' | head -1)

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
''')
        os.chmod(get_script, 0o755)
        print_success(f"Created {get_script}")
        
        config['SCRIPT_SET'] = '/scripts/set_dns.sh'
        config['SCRIPT_GET'] = '/scripts/get_dns.sh'
        
        print()
        print_info("Template scripts have been created in the 'scripts' directory.")
        print_info("You MUST customize set_dns.sh for your DNS provider.")
        print_info("The get_dns.sh template uses 'dig' which may work as-is.")
        print()
    else:
        print()
        config['SCRIPT_SET'] = ask("Path to your SET script (updates DNS)", default="/scripts/set_dns.sh")
        config['SCRIPT_GET'] = ask("Path to your GET script (queries DNS)", default="/scripts/get_dns.sh")
    
    print()
    print_info("When running Docker, mount your scripts directory:")
    print(f"  docker run -v $(pwd)/scripts:/scripts ...")
    print()
    
    # Optional: Ask about environment variables they might need
    print("If your scripts need API tokens or credentials, you can add them to your .env file.")
    print("They'll be available as environment variables in your scripts.")
    print()
    
    add_vars = ask_bool("Do you want to add custom environment variables for your scripts?", default=False)
    if add_vars:
        print()
        print("Enter your custom variables (e.g., BLUECAT_TOKEN=abc123)")
        print("Press Enter with empty input when done.")
        print()
        while True:
            var = input("Variable (KEY=value): ").strip()
            if not var:
                break
            if '=' in var:
                key, value = var.split('=', 1)
                config[key.upper()] = value
                print_success(f"Added {key.upper()}")
            else:
                print_error("Format must be KEY=value")

def write_env_file(config, filename):
    """Write configuration to .env file."""
    with open(filename, 'w') as f:
        f.write("# AST DNS Failover Configuration\n")
        f.write(f"# Generated by setup wizard\n\n")
        
        # Group settings
        groups = {
            'Core': ['ROLE', 'DNS_PROVIDER', 'DNS_SERVER', 'DNS_ZONE', 'DNS_RECORD', 'DNS_TTL'],
            'Failover': ['PRIMARY_IP', 'DR_IP', 'LEASE_TTL', 'UPDATE_INTERVAL', 'FAIL_THRESHOLD'],
            'Health Check': ['HEALTH_HOST', 'HEALTH_PORT', 'HEALTH_TIMEOUT'],
            'Cloudflare': ['CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_ZONE_ID'],
            'AWS Route53': ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_REGION', 'ROUTE53_ZONE_ID'],
            'Azure DNS': ['AZURE_SUBSCRIPTION_ID', 'AZURE_RESOURCE_GROUP', 'AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET'],
            'GCP DNS': ['GCP_PROJECT_ID', 'GCP_CREDENTIALS_FILE', 'GCP_MANAGED_ZONE'],
            'F5 GTM': ['F5_HOST', 'F5_USERNAME', 'F5_PASSWORD', 'F5_PARTITION', 'F5_VERIFY_SSL', 'F5_POOL_NAME'],
            'Infoblox': ['INFOBLOX_HOST', 'INFOBLOX_USERNAME', 'INFOBLOX_PASSWORD', 'INFOBLOX_WAPI_VERSION', 'INFOBLOX_VERIFY_SSL'],
            'BIND': ['TSIG_KEYFILE'],
            'Custom Script': ['SCRIPT_SET', 'SCRIPT_GET'],
            'Vault': ['VAULT_ADDR', 'VAULT_AUTH_METHOD', 'VAULT_ROLE_ID', 'VAULT_SECRET_ID', 'VAULT_MOUNT', 'VAULT_KEY'],
        }
        
        written = set()
        for group, keys in groups.items():
            group_values = [(k, config.get(k)) for k in keys if config.get(k)]
            if group_values:
                f.write(f"# {group}\n")
                for key, value in group_values:
                    f.write(f"{key}={value}\n")
                    written.add(key)
                f.write("\n")
        
        # Write any remaining config
        remaining = [(k, v) for k, v in config.items() if k not in written and v]
        if remaining:
            f.write("# Other\n")
            for key, value in remaining:
                f.write(f"{key}={value}\n")

def main():
    print_header("AST DNS Failover Setup Wizard")
    
    print("This wizard will help you configure DNS-based failover.")
    print("It will generate .env files for both Primary and DR sites.")
    
    config = {}
    output_dir = None  # Will be set later, or early for script provider
    
    # Step 1: Choose provider
    print_step(1, "Choose your DNS provider")
    
    providers = [
        ('cloudflare', 'Cloudflare DNS (easiest setup)'),
        ('route53', 'AWS Route 53'),
        ('azure-dns', 'Azure DNS'),
        ('gcp-dns', 'Google Cloud DNS'),
        ('f5-gtm', 'F5 BIG-IP DNS (GTM)'),
        ('infoblox', 'Infoblox DDI'),
        ('bind-tsig', 'BIND with TSIG'),
        ('ad-gss', 'Active Directory DNS'),
        ('dry-run', 'Dry-run (testing only)'),
        ('script', 'Custom Script (my provider is not listed)'),
    ]
    
    provider = ask_choice("Select your DNS provider:", providers, default='cloudflare')
    config['DNS_PROVIDER'] = provider
    
    # Step 2: Basic DNS settings
    print_step(2, "Basic DNS Settings")
    
    config['DNS_ZONE'] = ask("DNS Zone (e.g., example.com)")
    config['DNS_RECORD'] = ask("DNS Record to manage (FQDN)", default=f"failover.{config['DNS_ZONE']}")
    config['DNS_TTL'] = ask("DNS TTL in seconds", default="30")
    
    # Step 3: Provider-specific setup
    if provider == 'cloudflare':
        setup_cloudflare(config)
    elif provider == 'route53':
        setup_route53(config)
    elif provider == 'azure-dns':
        setup_azure(config)
    elif provider == 'gcp-dns':
        setup_gcp(config)
    elif provider == 'f5-gtm':
        setup_f5(config)
    elif provider == 'infoblox':
        setup_infoblox(config)
    elif provider == 'bind-tsig':
        setup_bind(config)
    elif provider == 'ad-gss':
        setup_ad(config)
    elif provider == 'script':
        # Get output dir early for script generation
        print()
        output_dir = ask("Output directory for config files", default=".")
        setup_script(config, output_dir)
    elif provider == 'dry-run':
        print_info("Dry-run mode - no credentials needed")
        config['DRYRUN_STATEFILE'] = '/state/zone.json'
    
    # Step 4: Failover settings
    print_step(4, "Failover Settings")
    
    config['PRIMARY_IP'] = ask("Primary site IP address")
    config['DR_IP'] = ask("DR site IP address")
    config['LEASE_TTL'] = ask("Lease TTL (seconds before DR can take over)", default="60")
    config['UPDATE_INTERVAL'] = ask("Update interval (seconds between heartbeats)", default="10")
    config['FAIL_THRESHOLD'] = ask("Fail threshold (consecutive failures before action)", default="3")
    
    # Step 5: Health check
    print_step(5, "Health Check Settings")
    print_info("DR site will check this endpoint to determine if Primary is healthy")
    
    config['HEALTH_HOST'] = ask("Health check host", default=config['PRIMARY_IP'])
    config['HEALTH_PORT'] = ask("Health check port", default="6514")
    config['HEALTH_TIMEOUT'] = ask("Health check timeout (seconds)", default="2")
    
    # Step 6: Vault integration (optional)
    print_step(6, "Vault Integration (Optional)")
    
    use_vault = ask_bool("Do you want to use HashiCorp Vault for secrets?", default=False)
    if use_vault:
        config['VAULT_ADDR'] = ask("Vault address (e.g., https://vault.example.com:8200)")
        config['VAULT_AUTH_METHOD'] = ask_choice("Authentication method:", [
            ('token', 'Token-based'),
            ('approle', 'AppRole (recommended for production)'),
            ('kubernetes', 'Kubernetes auth'),
        ], default='approle')
        
        if config['VAULT_AUTH_METHOD'] == 'token':
            config['VAULT_TOKEN'] = ask("Vault token", password=True)
        elif config['VAULT_AUTH_METHOD'] == 'approle':
            config['VAULT_ROLE_ID'] = ask("Role ID")
            config['VAULT_SECRET_ID'] = ask("Secret ID", password=True)
        elif config['VAULT_AUTH_METHOD'] == 'kubernetes':
            config['VAULT_K8S_ROLE'] = ask("Kubernetes auth role")
        
        config['VAULT_MOUNT'] = ask("Vault mount point", default="secret")
        config['VAULT_KEY'] = ask("Secret path", default="dns-failover")
    
    # Test connection
    print_step(7, "Testing Connection")
    if not test_provider_connection(provider, config):
        if not ask_bool("Connection test failed. Continue anyway?", default=False):
            print_error("Setup cancelled")
            sys.exit(1)
    
    # Generate files
    print_step(8, "Generating Configuration Files")
    
    # output_dir may have been set already (e.g., for script provider)
    if output_dir is None:
        output_dir = ask("Output directory", default=".")
    os.makedirs(output_dir, exist_ok=True)
    
    # Primary config
    primary_config = config.copy()
    primary_config['ROLE'] = 'primary'
    primary_file = os.path.join(output_dir, '.env.primary')
    write_env_file(primary_config, primary_file)
    print_success(f"Created {primary_file}")
    
    # DR config
    dr_config = config.copy()
    dr_config['ROLE'] = 'dr'
    dr_file = os.path.join(output_dir, '.env.dr')
    write_env_file(dr_config, dr_file)
    print_success(f"Created {dr_file}")
    
    # Summary
    print_header("Setup Complete!")
    
    print("Your configuration files have been generated:\n")
    print(f"  Primary site: {primary_file}")
    print(f"  DR site:      {dr_file}")
    
    if provider == 'script':
        print(f"  Scripts:      {output_dir}/scripts/")
    
    print("\n" + BOLD + "Next steps:" + RESET)
    
    if provider == 'script':
        print(f"""
  {YELLOW}IMPORTANT: Customize your scripts first!{RESET}
  
  1. Edit {output_dir}/scripts/set_dns.sh for your DNS provider
  2. Test it manually:
     ./scripts/set_dns.sh test.example.com 10.10.10.10 primary 9999999999 30 example.com
  
  3. Build and run with scripts mounted:
  
     # On Primary site:
     docker build -t dns-failover .
     docker run -d --name dns-failover \\
       --env-file .env.primary \\
       -v $(pwd)/scripts:/scripts \\
       dns-failover
     
     # On DR site:
     docker build -t dns-failover .
     docker run -d --name dns-failover \\
       --env-file .env.dr \\
       -v $(pwd)/scripts:/scripts \\
       dns-failover
  
  4. Initialize DNS (run once from Primary):
     docker exec dns-failover python3 /app/dns_failover.py init
  
  5. Monitor logs:
     docker logs -f dns-failover
""")
    else:
        print("""
  1. Copy .env.primary to your primary site
  2. Copy .env.dr to your DR site
  3. Build and run the containers:
  
     # On Primary site:
     docker build -t dns-failover .
     docker run -d --name dns-failover --env-file .env.primary dns-failover
     
     # On DR site:
     docker build -t dns-failover .
     docker run -d --name dns-failover --env-file .env.dr dns-failover
  
  4. Initialize DNS (run once from Primary):
     docker exec dns-failover python3 /app/dns_failover.py init
  
  5. Monitor logs:
     docker logs -f dns-failover
""")
    
    if use_vault:
        print(BOLD + "Vault setup:" + RESET)
        print("""
  Store your secrets in Vault:
  
    vault kv put secret/dns-failover \\
      cloudflare_api_token="your-token" \\
      # ... other secrets
      
  Then remove sensitive values from .env files.
""")
    
    print_success("Setup complete! Good luck with your failover configuration.")

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nSetup cancelled.")
        sys.exit(1)
