# Custom DNS provider scripts

When the setup wizard is configured for the custom-script provider, it writes
`set_dns.sh` and `get_dns.sh` into this directory. Both Compose stacks mount the
directory read-only at `/scripts`, matching the paths written to the generated
failover environment files.

Generated scripts are deployment-specific and ignored by Git. Review and test
them before starting the failover service.
