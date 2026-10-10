"""Provider behaviour: read failures must raise, TXT selection, token refresh, F5 ordering."""
import json
import os
import stat
import textwrap

import pytest

import dns_failover as df
from helpers import make_cfg


def write_exec(path, body):
    path.write_text(textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


# ---- dig / nsupdate providers ----

DIG_OK = """\
; <<>> DiG 9.18 <<>> @10.0.0.53 syslog.ast.example.local {rtype}
;; Got answer:
;; ->>HEADER<<- opcode: QUERY, status: {status}, id: 4242
;; flags: qr aa rd; QUERY: 1, ANSWER: 2, AUTHORITY: 0, ADDITIONAL: 1

;; ANSWER SECTION:
{answers}
"""


@pytest.fixture
def fake_dig(tmp_path, monkeypatch):
    """Install a fake `dig` on PATH whose output is controlled per record type."""
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    responses = {}

    def set_response(rtype, status='NOERROR', answers='', rc=0):
        responses[rtype] = {'out': DIG_OK.format(rtype=rtype, status=status, answers=answers), 'rc': rc}
        (tmp_path / 'responses.json').write_text(json.dumps(responses))

    write_exec(bindir / 'dig', f"""\
        #!/usr/bin/env python3
        import json, sys
        r = json.load(open({str(tmp_path / 'responses.json')!r}))[sys.argv[3]]
        sys.stdout.write(r['out']); sys.exit(r['rc'])
    """)
    monkeypatch.setenv('PATH', f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return set_response


def bind_provider():
    return df.BindTSIGProvider(make_cfg(provider='bind-tsig', tsig_keyfile='/dev/null'))


def test_bind_reads_records_and_picks_lease_txt(fake_dig):
    fake_dig('A', answers='syslog.ast.example.local. 30 IN A 10.10.10.10')
    fake_dig('TXT', answers=(
        'syslog.ast.example.local. 30 IN TXT "v=spf1 -all"\n'
        'syslog.ast.example.local. 30 IN TXT "owner=primary exp=123"'
    ))
    assert bind_provider().get_records() == {'A': '10.10.10.10', 'TXT': 'owner=primary exp=123'}


def test_bind_nxdomain_is_absent_not_error(fake_dig):
    fake_dig('A', status='NXDOMAIN')
    fake_dig('TXT', status='NXDOMAIN')
    assert bind_provider().get_records() == {'A': None, 'TXT': None}


@pytest.mark.parametrize('status,rc', [('SERVFAIL', 0), ('REFUSED', 0), ('NOERROR', 9)])
def test_bind_query_failure_raises(fake_dig, status, rc):
    """Regression: SERVFAIL/timeout used to return None, which DR read as 'lease expired'."""
    fake_dig('A', status=status, rc=rc)
    fake_dig('TXT', status=status, rc=rc)
    with pytest.raises(df.LeaseReadError):
        bind_provider().get_records()


def test_txt_multi_string_joined(fake_dig):
    fake_dig('A', answers='')
    fake_dig('TXT', answers='syslog.ast.example.local. 30 IN TXT "owner=dr " "exp=99"')
    assert bind_provider().get_records()['TXT'] == 'owner=dr exp=99'


# ---- script provider ----

def script_provider(tmp_path, get_body):
    get = write_exec(tmp_path / 'get.sh', get_body)
    setp = write_exec(tmp_path / 'set.sh', '#!/bin/sh\nexit 0\n')
    return df.ScriptProvider(make_cfg(provider='script', script_get=get, script_set=setp))


def test_script_get_ok(tmp_path):
    p = script_provider(tmp_path, '#!/bin/sh\necho \'{"A": "1.2.3.4", "TXT": "owner=dr exp=5"}\'\n')
    assert p.get_records() == {'A': '1.2.3.4', 'TXT': 'owner=dr exp=5'}


def test_script_get_empty_means_absent(tmp_path):
    assert script_provider(tmp_path, '#!/bin/sh\nexit 0\n').get_records() == {'A': None, 'TXT': None}


@pytest.mark.parametrize('body', [
    '#!/bin/sh\necho oops >&2\nexit 1\n',
    '#!/bin/sh\necho not-json\n',
    '#!/bin/sh\necho \'["list"]\'\n',
])
def test_script_get_failure_raises(tmp_path, body):
    with pytest.raises(df.LeaseReadError):
        script_provider(tmp_path, body).get_records()


def test_script_set_failure_raises(tmp_path):
    get = write_exec(tmp_path / 'get.sh', '#!/bin/sh\nexit 0\n')
    setp = write_exec(tmp_path / 'set.sh', '#!/bin/sh\necho denied >&2\nexit 3\n')
    p = df.ScriptProvider(make_cfg(provider='script', script_get=get, script_set=setp))
    with pytest.raises(RuntimeError, match='denied'):
        p.set_records('1.2.3.4', 'dr', 5)


# ---- HTTP providers with a fake session ----

class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.headers = {}

    def _call(self, method, url, **kw):
        self.calls.append((method, url, kw.get('json')))
        return self.handler(method, url, kw)

    def get(self, url, **kw):
        return self._call('GET', url, **kw)

    def put(self, url, **kw):
        return self._call('PUT', url, **kw)

    def post(self, url, **kw):
        return self._call('POST', url, **kw)

    def patch(self, url, **kw):
        return self._call('PATCH', url, **kw)


def cloudflare(handler):
    p = df.CloudflareProvider(make_cfg(provider='cloudflare', cloudflare_api_token='t', cloudflare_zone_id='z'))
    p.session = FakeSession(handler)
    return p


def test_cloudflare_ignores_unrelated_txt():
    def handler(method, url, kw):
        if kw['params']['type'] == 'A':
            return Resp(body={'success': True, 'result': [{'id': 'a1', 'content': '10.10.10.10'}]})
        return Resp(body={'success': True, 'result': [
            {'id': 't-spf', 'content': 'v=spf1 -all'},
            {'id': 't-lease', 'content': 'owner=primary exp=7'},
        ]})
    p = cloudflare(handler)
    assert p.get_records() == {'A': '10.10.10.10', 'TXT': 'owner=primary exp=7'}


def test_cloudflare_set_updates_lease_txt_not_spf():
    def handler(method, url, kw):
        if method == 'GET':
            if kw['params']['type'] == 'A':
                return Resp(body={'success': True, 'result': [{'id': 'a1', 'content': '1.1.1.1'}]})
            return Resp(body={'success': True, 'result': [
                {'id': 't-spf', 'content': 'v=spf1 -all'},
                {'id': 't-lease', 'content': 'owner=primary exp=7'},
            ]})
        return Resp()
    p = cloudflare(handler)
    p.set_records('10.20.20.10', 'dr', 9)
    puts = [c[1] for c in p.session.calls if c[0] == 'PUT']
    assert any(u.endswith('/t-lease') for u in puts)
    assert not any(u.endswith('/t-spf') for u in puts)


def test_cloudflare_api_error_raises():
    p = cloudflare(lambda m, u, kw: Resp(body={'success': False, 'errors': ['nope']}))
    with pytest.raises(df.LeaseReadError):
        p.get_records()


def test_azure_token_refreshes_before_expiry(monkeypatch):
    import requests
    clock = {'t': 1000.0}
    monkeypatch.setattr(df.time, 'time', lambda: clock['t'])
    tokens = []

    def fake_post(url, data=None, timeout=None):
        tokens.append(f"tok{len(tokens)}")
        return Resp(body={'access_token': tokens[-1], 'expires_in': 3600})

    monkeypatch.setattr(requests, 'post', fake_post)
    cfg = make_cfg(provider='azure-dns', azure_subscription_id='s', azure_resource_group='g',
                   azure_tenant_id='t', azure_client_id='c', azure_client_secret='x')
    p = df.AzureDNSProvider(cfg)
    real_headers = p.session.headers
    p.session = FakeSession(lambda m, u, kw: Resp(status=404))
    p.session.headers = real_headers

    p.get_records()
    assert tokens == ['tok0']
    clock['t'] += 3600 - 200   # inside the refresh margin
    p.get_records()
    assert tokens == ['tok0', 'tok1']
    assert p.session.headers['Authorization'] == 'Bearer tok1'


def test_azure_record_name_relative_to_zone():
    p = df.AzureDNSProvider.__new__(df.AzureDNSProvider)
    p.cfg = make_cfg(dns_record='syslog.ast.example.local', dns_zone='example.local')
    assert p._get_record_name() == 'syslog.ast'
    p.cfg = make_cfg(dns_record='example.local', dns_zone='example.local')
    assert p._get_record_name() == '@'


def test_f5_enables_target_before_disabling_others():
    def handler(method, url, kw):
        if method == 'GET' and url.endswith('/members'):
            return Resp(body={'items': [
                {'name': 'm-primary', 'address': '10.10.10.10%0'},
                {'name': 'm-dr', 'address': '10.20.20.10%0'},
            ]})
        return Resp()
    p = df.F5GTMProvider(make_cfg(provider='f5-gtm', f5_host='h', f5_username='u', f5_password='p', f5_pool_name='pool'))
    p.session = FakeSession(handler)
    p.set_records('10.20.20.10', 'dr', 5)
    member_patches = [(u.rsplit('/', 1)[-1], body) for m, u, body in p.session.calls if m == 'PATCH' and '/members/' in u]
    assert member_patches == [('m-dr', {'enabled': True}), ('m-primary', {'disabled': True})]


def test_f5_member_patch_failure_raises():
    def handler(method, url, kw):
        if method == 'GET' and url.endswith('/members'):
            return Resp(body={'items': [{'name': 'm', 'address': '10.20.20.10'}]})
        if method == 'PATCH' and '/members/' in url:
            return Resp(status=401)
        return Resp()
    p = df.F5GTMProvider(make_cfg(provider='f5-gtm', f5_host='h', f5_username='u', f5_password='p', f5_pool_name='pool'))
    p.session = FakeSession(handler)
    with pytest.raises(RuntimeError):
        p.set_records('10.20.20.10', 'dr', 5)


def test_f5_read_error_raises():
    p = df.F5GTMProvider(make_cfg(provider='f5-gtm', f5_host='h', f5_username='u', f5_password='p'))
    p.session = FakeSession(lambda m, u, kw: Resp(status=503))
    with pytest.raises(RuntimeError):
        p.get_records()
