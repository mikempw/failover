"""Lease decisions and heartbeat iterations for primary and DR."""
import pytest

import dns_failover as df
from helpers import FakeProvider, make_cfg

NOW = 1_800_000_000


@pytest.fixture(autouse=True)
def frozen_time(monkeypatch):
    monkeypatch.setattr(df, 'now_unix', lambda: NOW)


# ---- pure decisions ----

@pytest.mark.parametrize('owner,exp,expected', [
    ('primary', NOW + 30, 'renew'),
    (None, 0, 'renew'),             # no lease yet
    ('dr', NOW + 30, 'yield'),      # DR holds a live lease
    ('dr', NOW, 'yield'),           # boundary: not yet expired
    ('dr', NOW - 1, 'reclaim'),     # DR stopped renewing (DR down)
])
def test_decide_primary(owner, exp, expected):
    assert df.decide_primary(df.Lease('1.1.1.1', owner, exp), NOW) == expected


@pytest.mark.parametrize('owner,exp,failed,expected', [
    ('dr', NOW + 30, False, 'renew'),       # DR keeps its lease even if primary looks healthy
    ('dr', NOW - 100, True, 'renew'),
    ('primary', NOW + 30, False, 'idle'),
    ('primary', NOW - 1, False, 'idle'),    # expired but primary healthy: don't flap
    ('primary', NOW + 30, True, 'wait'),
    ('primary', NOW - 1, True, 'promote'),
    (None, 0, True, 'promote'),             # record genuinely absent
])
def test_decide_dr(owner, exp, failed, expected):
    assert df.decide_dr(df.Lease('1.1.1.1', owner, exp), NOW, failed) == expected


def test_decide_primary_reclaim_grace_tolerates_clock_skew():
    lease = df.Lease('1.1.1.1', 'dr', NOW - 30)
    assert df.decide_primary(lease, NOW, reclaim_grace=60) == 'yield'
    assert df.decide_primary(lease, NOW + 31, reclaim_grace=60) == 'reclaim'


# ---- read_lease ----

def test_read_lease_parses_txt():
    p = FakeProvider(ip='10.10.10.10', owner='primary', exp=NOW + 5)
    lease = df.read_lease(p)
    assert (lease.ip, lease.owner, lease.exp) == ('10.10.10.10', 'primary', NOW + 5)


def test_read_lease_wraps_provider_errors():
    p = FakeProvider(read_exc=ConnectionError('boom'))
    with pytest.raises(df.LeaseReadError):
        df.read_lease(p)


def test_read_lease_bad_exp_is_zero():
    p = FakeProvider()
    p.records = {'A': '1.2.3.4', 'TXT': 'owner=primary exp=notanumber'}
    assert df.read_lease(p).exp == 0


# ---- DR tick ----

def run_dr(provider, healthy_seq, cfg=None):
    cfg = cfg or make_cfg(role='dr')
    state = df.DRState()
    seq = iter(healthy_seq)
    return [df.dr_tick(cfg, provider, state, lambda: next(seq)) for _ in healthy_seq]


def test_dr_promotes_only_after_threshold_and_expiry():
    p = FakeProvider(ip='10.10.10.10', owner='primary', exp=NOW - 1)
    actions = run_dr(p, [False, False, False])
    assert actions == ['idle', 'idle', 'promote']
    assert p.writes[-1][:2] == ('10.20.20.10', 'dr')


def test_dr_waits_for_live_primary_lease():
    p = FakeProvider(ip='10.10.10.10', owner='primary', exp=NOW + 40)
    assert run_dr(p, [False] * 3)[-1] == 'wait'
    assert p.writes == []


def test_dr_does_not_promote_when_lease_unreadable():
    """Regression: a failed read used to look like exp=0 and trigger failover."""
    p = FakeProvider(read_exc=df.LeaseReadError('SERVFAIL'))
    actions = run_dr(p, [False] * 5)
    assert actions == ['read-error'] * 5
    assert p.writes == []


def test_dr_survives_write_failure():
    """Regression: an exception in set_records used to kill the DR process."""
    p = FakeProvider(ip='10.10.10.10', owner='primary', exp=NOW - 1, write_exc=RuntimeError('nsupdate failed'))
    cfg = make_cfg(role='dr', fail_threshold=1)
    state = df.DRState()
    assert df.dr_tick(cfg, p, state, lambda: False) == 'write-error'
    # failure count retained so the next tick retries immediately
    assert state.consecutive_failures == 1


def test_dr_keeps_renewing_after_primary_recovers():
    """Regression: DR used to stop renewing once primary looked healthy, leaving a dead lease."""
    p = FakeProvider(ip='10.20.20.10', owner='dr', exp=NOW + 5)
    assert run_dr(p, [True, True]) == ['renew', 'renew']
    assert all(w[1] == 'dr' for w in p.writes)


def test_dr_health_recovery_resets_counter():
    p = FakeProvider(ip='10.10.10.10', owner='primary', exp=NOW - 1)
    assert run_dr(p, [False, False, True, False, False]) == ['idle'] * 5


# ---- primary tick ----

class StubHealth:
    def __init__(self, ok):
        self.ok = ok

    def check(self):
        return self.ok


def test_primary_renews_own_lease():
    p = FakeProvider(ip='10.10.10.10', owner='primary', exp=NOW + 10)
    assert df.primary_tick(make_cfg(), p, None) == 'renew'
    assert p.writes == [('10.10.10.10', 'primary', NOW + 60)]


def test_primary_yields_to_live_dr_lease():
    p = FakeProvider(ip='10.20.20.10', owner='dr', exp=NOW + 30)
    assert df.primary_tick(make_cfg(), p, None) == 'yield'
    assert p.writes == []


def test_primary_reclaims_expired_dr_lease():
    p = FakeProvider(ip='10.20.20.10', owner='dr', exp=NOW - 61)   # beyond LEASE_TTL grace
    assert df.primary_tick(make_cfg(), p, None) == 'reclaim'
    assert p.writes[-1][:2] == ('10.10.10.10', 'primary')


def test_primary_does_not_reclaim_within_grace():
    p = FakeProvider(ip='10.20.20.10', owner='dr', exp=NOW - 30)
    assert df.primary_tick(make_cfg(), p, None) == 'yield'


class FailingChecker:
    """Like a MetricsHealthChecker whose collector is stopped."""
    def __init__(self):
        self.resets = 0
        self.checks = 0

    def check(self):
        self.checks += 1
        return False

    def reset(self):
        self.resets += 1


def test_primary_reclaims_even_though_local_collector_is_stopped():
    """Regression: with HEALTH_URL set, the stopped collector blocked reclaim forever."""
    p = FakeProvider(ip='10.20.20.10', owner='dr', exp=NOW - 61)
    chk = FailingChecker()
    assert df.primary_tick(make_cfg(), p, chk) == 'reclaim'
    assert chk.resets == 1 and chk.checks == 0
    assert p.writes[-1][:2] == ('10.10.10.10', 'primary')


def test_primary_yield_does_not_run_self_check():
    p = FakeProvider(ip='10.20.20.10', owner='dr', exp=NOW + 30)
    chk = FailingChecker()
    assert df.primary_tick(make_cfg(), p, chk) == 'yield'
    assert chk.checks == 0


def test_read_lease_rejects_non_string_txt():
    p = FakeProvider()
    p.records = {'A': '1.2.3.4', 'TXT': ['owner=dr', 'exp=1']}
    with pytest.raises(df.LeaseReadError):
        df.read_lease(p)


class RacingProvider(FakeProvider):
    """DR's in-flight renewal lands right after the first failback write."""
    def __init__(self):
        super().__init__(ip='10.20.20.10', owner='dr', exp=NOW + 30)
        self.raced = False

    def set_records(self, ip, owner, exp_unix):
        super().set_records(ip, owner, exp_unix)
        if owner == 'primary' and not self.raced:
            self.raced = True
            super().set_records('10.20.20.10', 'dr', exp_unix)


def test_failback_detects_and_retries_overwrite(monkeypatch):
    monkeypatch.setattr(df.time, 'sleep', lambda s: None)
    p = RacingProvider()
    assert df.failback_to_primary(make_cfg(), p) is True
    assert df.read_lease(p).owner == 'primary'
    assert [w[1] for w in p.writes] == ['primary', 'dr', 'primary']


def test_failback_reports_failure(monkeypatch):
    monkeypatch.setattr(df.time, 'sleep', lambda s: None)

    class AlwaysDR(FakeProvider):
        def set_records(self, ip, owner, exp_unix):
            super().set_records('10.20.20.10', 'dr', exp_unix)

    assert df.failback_to_primary(make_cfg(), AlwaysDR(), attempts=2) is False


def test_primary_does_not_write_blind_on_read_error():
    """Regression: primary used to renew anyway and could overwrite a DR lease."""
    p = FakeProvider(read_exc=df.LeaseReadError('timeout'))
    assert df.primary_tick(make_cfg(), p, None) == 'read-error'
    assert p.writes == []


def test_primary_unhealthy_lets_lease_expire():
    p = FakeProvider(ip='10.10.10.10', owner='primary', exp=NOW + 10)
    assert df.primary_tick(make_cfg(), p, StubHealth(False)) == 'unhealthy'
    assert p.writes == []


def test_primary_write_error_is_reported_not_raised():
    p = FakeProvider(ip='10.10.10.10', owner='primary', exp=NOW + 10, write_exc=RuntimeError('x'))
    assert df.primary_tick(make_cfg(), p, None) == 'write-error'


# ---- end-to-end with dry-run provider ----

def test_full_failover_and_failback_cycle(tmp_path, monkeypatch):
    state = tmp_path / 'zone.json'
    pcfg = make_cfg(role='primary', dryrun_statefile=str(state))
    dcfg = make_cfg(role='dr', dryrun_statefile=str(state), fail_threshold=2)
    prov = df.DryRunProvider(pcfg)

    df.init_dns(pcfg, prov)
    assert df.read_lease(prov).owner == 'primary'

    # Primary dies; time passes beyond lease TTL.
    monkeypatch.setattr(df, 'now_unix', lambda: NOW + 120)
    dstate = df.DRState()
    assert df.dr_tick(dcfg, prov, dstate, lambda: False) == 'idle'
    assert df.dr_tick(dcfg, prov, dstate, lambda: False) == 'promote'
    assert df.read_lease(prov).ip == '10.20.20.10'

    # Primary comes back: DR keeps lease, primary yields.
    assert df.dr_tick(dcfg, prov, dstate, lambda: True) == 'renew'
    assert df.primary_tick(pcfg, prov, None) == 'yield'

    # Manual failback; DR then goes idle.
    assert df.failback_to_primary(pcfg, prov, verify_delay=0)
    assert df.dr_tick(dcfg, prov, dstate, lambda: True) == 'idle'
    assert df.read_lease(prov).owner == 'primary'
