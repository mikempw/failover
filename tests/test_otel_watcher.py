import otel_watcher_docker as w


def test_my_ip_explicit_wins():
    assert w.resolve_my_ip({'MY_IP': '1.1.1.1', 'ROLE': 'dr', 'DR_IP': '2.2.2.2'}) == '1.1.1.1'


def test_my_ip_from_role_primary():
    """Regression: DR_IP used to win over MY_IP, so primary watched the DR address."""
    env = {'ROLE': 'primary', 'PRIMARY_IP': '10.10.10.10', 'DR_IP': '10.20.20.10'}
    assert w.resolve_my_ip(env) == '10.10.10.10'


def test_my_ip_from_role_dr():
    env = {'ROLE': 'dr', 'PRIMARY_IP': '10.10.10.10', 'DR_IP': '10.20.20.10'}
    assert w.resolve_my_ip(env) == '10.20.20.10'


def test_my_ip_unset():
    assert w.resolve_my_ip({}) is None
