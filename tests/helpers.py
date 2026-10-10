import dns_failover as df


def make_cfg(**overrides):
    base = dict(
        provider='dry-run', dns_server='10.0.0.53', dns_zone='example.local',
        dns_record='syslog.ast.example.local', dns_ttl=30,
        primary_ip='10.10.10.10', dr_ip='10.20.20.10',
        lease_ttl=60, update_interval=10, fail_threshold=3,
        health_host='10.10.10.10', health_port=8888, health_timeout=2,
        health_mode='tcp', health_url=None, health_metric='m', health_stale_count=3,
        role='primary',
    )
    base.update(overrides)
    return df.Config(**base)


class FakeProvider(df.DNSProvider):
    """In-memory provider. Set read_exc/write_exc to simulate outages."""

    def __init__(self, ip=None, owner=None, exp=None, read_exc=None, write_exc=None):
        super().__init__(make_cfg())
        self.records = {'A': ip, 'TXT': f"owner={owner} exp={exp}" if owner else None}
        self.read_exc = read_exc
        self.write_exc = write_exc
        self.writes = []

    def get_records(self):
        if self.read_exc:
            raise self.read_exc
        return dict(self.records)

    def set_records(self, ip, owner, exp_unix):
        if self.write_exc:
            raise self.write_exc
        self.writes.append((ip, owner, exp_unix))
        self.records = {'A': ip, 'TXT': f"owner={owner} exp={exp_unix}"}
