"""Config validation, metric parsing and the metrics health checker."""
import pytest

import dns_failover as df
from helpers import make_cfg


# ---- validation ----

def test_valid_config_passes():
    make_cfg().validate()


@pytest.mark.parametrize('field,value', [
    ('dns_record', 'syslog.example.local\nupdate delete example.local A'),  # nsupdate injection
    ('dns_record', 'bad name.example.local'),
    ('dns_zone', 'zone;rm'),
    ('primary_ip', '10.10.10.10\nsend'),
    ('dr_ip', 'not-an-ip'),
])
def test_validate_rejects_injection_and_garbage(field, value):
    with pytest.raises(ValueError):
        make_cfg(**{field: value}).validate()


def test_validate_rejects_bad_dns_server_for_nsupdate(tmp_path):
    key = tmp_path / 'k'
    key.write_text('x')
    with pytest.raises(ValueError, match='DNS_SERVER'):
        make_cfg(provider='bind-tsig', tsig_keyfile=str(key), dns_server='1.2.3.4\nzone evil').validate()


def test_validate_same_ips():
    with pytest.raises(ValueError, match='must differ'):
        make_cfg(dr_ip='10.10.10.10').validate()


def test_validate_lease_vs_interval():
    with pytest.raises(ValueError, match='LEASE_TTL'):
        make_cfg(lease_ttl=10, update_interval=10).validate()


@pytest.mark.parametrize('name,ok', [
    ('syslog.ast.example.local', True),
    ('syslog.ast.example.local.', True),
    ('_acme-challenge.example.com', True),
    ('-bad.example.com', False),
    ('', False),
    ('a..b', False),
    ('a b', False),
])
def test_is_valid_dns_name(name, ok):
    assert df.is_valid_dns_name(name) is ok


def test_ipv6_accepted():
    assert df.is_valid_ip('fd00::1')


# ---- metric parsing ----

METRICS = """\
# HELP otelcol_receiver_accepted_metric_points Number of points.
# TYPE otelcol_receiver_accepted_metric_points counter
otelcol_receiver_accepted_metric_points{receiver="bigip/1",transport=""} 100
otelcol_receiver_accepted_metric_points{receiver="bigip/2",transport=""} 50 1700000000000
otelcol_receiver_accepted_metric_points_total 99999
otelcol_receiver_refused_metric_points{receiver="bigip/1"} 7
"""


def test_parse_sums_exact_name_only():
    # _total suffix and other metrics must not be included; timestamped line must be.
    assert df.parse_metric_value(METRICS, 'otelcol_receiver_accepted_metric_points') == 150


def test_parse_unlabelled_metric():
    assert df.parse_metric_value('up 1\n', 'up') == 1


def test_parse_label_value_with_braces_and_spaces():
    text = 'm{path="/a b}",x="1"} 3\n'
    assert df.parse_metric_value(text, 'm') == 3


def test_parse_missing_returns_none():
    assert df.parse_metric_value(METRICS, 'nope') is None
    assert df.parse_metric_value('', 'x') is None


# ---- health checker ----

def checker_with(values, monkeypatch, stale_count=3):
    seq = iter(values)

    def fake_fetch(url, timeout):
        v = next(seq)
        return None if v is None else f"m {v}\n"

    monkeypatch.setattr(df, 'fetch_metrics', fake_fetch)
    return df.MetricsHealthChecker('http://x/metrics', 'm', stale_count, 1)


def test_checker_increasing_is_healthy(monkeypatch):
    c = checker_with([1, 2, 3], monkeypatch)
    assert [c.check() for _ in range(3)] == [True, True, True]


def test_checker_flat_becomes_unhealthy(monkeypatch):
    c = checker_with([5, 5, 5, 5], monkeypatch)
    assert [c.check() for _ in range(4)] == [True, True, True, False]


def test_checker_counter_reset_is_not_stale(monkeypatch):
    """Regression: a collector restart (counter drops) used to count as stale."""
    c = checker_with([1000, 10, 20], monkeypatch)
    assert [c.check() for _ in range(3)] == [True, True, True]
    assert c.stale_checks == 0


def test_checker_unreachable_counts(monkeypatch):
    c = checker_with([None, None, None], monkeypatch)
    assert [c.check() for _ in range(3)] == [True, True, False]


def test_checker_recovers(monkeypatch):
    c = checker_with([5, 5, 5, 6], monkeypatch)
    [c.check() for _ in range(3)]
    assert c.check() is True and c.stale_checks == 0


# ---- env loading ----

def test_from_env(monkeypatch):
    monkeypatch.delenv('VAULT_ADDR', raising=False)
    monkeypatch.setenv('ROLE', 'dr')
    monkeypatch.setenv('DNS_PROVIDER', 'dry-run')
    monkeypatch.setenv('LEASE_TTL', '90')
    monkeypatch.setenv('F5_VERIFY_SSL', 'false')
    cfg = df.Config.from_env()
    assert cfg.role == 'dr' and cfg.lease_ttl == 90 and cfg.f5_verify_ssl is False
