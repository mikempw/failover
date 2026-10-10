"""
ch-sync tests.

Unit tests cover quoting and query construction. Integration tests start two
real ClickHouse servers (source = "primary", dest = "dr") and exercise remote()
across them. They run when a `clickhouse` binary is available via
$CLICKHOUSE_BIN or PATH, and are skipped otherwise.
"""
import os
import shutil
import socket
import subprocess
import time
import urllib.request

import pytest

import ch_sync as cs


# ---------------- unit ----------------

def test_quote_ident_escapes_backticks():
    assert cs.quote_ident('a`b') == '`a\\`b`'


def test_quote_str_escapes_quotes_and_backslashes():
    assert cs.quote_str("it's \\") == "'it\\'s \\\\'"


def make_syncer(**over):
    cfg = cs.SyncConfig(
        source=cs.CHEndpoint('10.0.0.1', '8123', 'default', "pa'ss"),
        dest=cs.CHEndpoint('localhost', '8123', 'default', ''),
        source_native_port='9000', databases=['ast'], lookback_hours=72,
        time_columns=['TimeUnix', 'Timestamp'], insert_timeout=60, interval=300,
    )
    for k, v in over.items():
        setattr(cfg, k, v)
    return cs.Syncer(cfg)


def test_fill_query_is_append_only_and_deduplicated():
    q = make_syncer().build_fill_query('ast', 'otel_logs', ['Timestamp', 'Body'], 'Timestamp', 3600)
    assert q.startswith('INSERT INTO `ast`.`otel_logs` (`Timestamp`, `Body`)')
    assert 'DROP' not in q.upper()
    # GLOBAL is required: a plain NOT IN would be evaluated on the remote side
    # against the source's own table and filter out every row.
    assert 'GLOBAL NOT IN' in q
    assert "remote('10.0.0.1:9000', 'ast', 'otel_logs', 'default', 'pa\\'ss')" in q
    assert 'toDateTime(3600)' in q and 'toDateTime(7200)' in q


def test_from_env_defaults():
    cfg = cs.SyncConfig.from_env({})
    assert cfg.lookback_hours == 72 and cfg.databases == ['ast'] and cfg.time_columns == ['TimeUnix', 'Timestamp']


def test_credentials_sent_in_headers_not_url(monkeypatch):
    seen = {}

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b''

    def fake_urlopen(req, timeout):
        seen['url'] = req.full_url
        seen['headers'] = dict(req.header_items())
        return R()

    monkeypatch.setattr(cs.urllib.request, 'urlopen', fake_urlopen)
    cs.CHEndpoint('h', '8123', 'u', 'secret').query('SELECT 1')
    assert 'secret' not in seen['url']
    assert seen['headers']['X-clickhouse-key'] == 'secret'


# ---------------- integration ----------------

CH_BIN = os.environ.get('CLICKHOUSE_BIN') or shutil.which('clickhouse')
SOURCE_PASSWORD = "s3cr'et\"pw"   # exercises quoting in remote() and header auth

CONFIG = """<clickhouse>
  <logger><level>warning</level><console>1</console></logger>
  <listen_host>127.0.0.1</listen_host>
  <http_port>{http}</http_port>
  <tcp_port>{tcp}</tcp_port>
  <path>{path}/</path>
  <tmp_path>{path}/tmp/</tmp_path>
  <user_files_path>{path}/user_files/</user_files_path>
  <format_schema_path>{path}/format_schemas/</format_schema_path>
  <mark_cache_size>67108864</mark_cache_size>
  <users>
    <default>
      <password>{password}</password>
      <networks><ip>127.0.0.1</ip><ip>::1</ip></networks>
      <profile>default</profile>
      <quota>default</quota>
    </default>
  </users>
  <profiles><default/></profiles>
  <quotas><default/></quotas>
</clickhouse>
"""


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def start_server(tmp_path, name, password):
    path = tmp_path / name
    path.mkdir()
    http, tcp = free_port(), free_port()
    pw_xml = password.replace('&', '&amp;').replace('<', '&lt;').replace('"', '&quot;').replace("'", '&apos;')
    (path / 'config.xml').write_text(CONFIG.format(http=http, tcp=tcp, path=path, password=pw_xml))
    proc = subprocess.Popen([CH_BIN, 'server', '--config-file', str(path / 'config.xml')],
                            stdout=subprocess.DEVNULL, stderr=open(path / 'stderr.log', 'w'))
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{http}/ping', timeout=1)
            return proc, cs.CHEndpoint('127.0.0.1', str(http), 'default', password), str(tcp)
        except Exception:
            if proc.poll() is not None:
                break
            time.sleep(0.3)
    proc.kill()
    raise RuntimeError(f"clickhouse {name} did not start: {(path / 'stderr.log').read_text()[-2000:]}")


@pytest.fixture(scope='module')
def servers(tmp_path_factory):
    if not CH_BIN:
        pytest.skip('clickhouse binary not available (set CLICKHOUSE_BIN)')
    tmp = tmp_path_factory.mktemp('ch')
    src_proc, src, src_tcp = start_server(tmp, 'primary', SOURCE_PASSWORD)
    dst_proc, dst, dst_tcp = start_server(tmp, 'dr', '')
    yield {'src': src, 'src_tcp': src_tcp, 'dst': dst, 'dst_tcp': dst_tcp}
    for p in (src_proc, dst_proc):
        p.terminate()
        p.wait(timeout=30)


# Shapes modelled on the OTel ClickHouse exporter tables.
METRICS_DDL = """CREATE TABLE ast.otel_metrics_gauge (
  ResourceAttributes Map(LowCardinality(String), String),
  MetricName String,
  Attributes Map(LowCardinality(String), String),
  TimeUnix DateTime64(9),
  Value Float64
) ENGINE = MergeTree PARTITION BY toDate(TimeUnix)
ORDER BY (MetricName, Attributes, toUnixTimestamp64Nano(TimeUnix))"""

LOGS_DDL = """CREATE TABLE ast.otel_logs (
  Timestamp DateTime64(9),
  SeverityText LowCardinality(String),
  Body String,
  LogAttributes Map(LowCardinality(String), String)
) ENGINE = MergeTree PARTITION BY toDate(Timestamp) ORDER BY Timestamp"""

HOUR = 3600
NOW = (int(time.time()) // HOUR) * HOUR  # top of current hour


def gauge_rows(hours, site):
    rows = []
    for h in hours:
        for i in range(5):
            ts = NOW - h * HOUR + i * 60
            rows.append(f"({{'host':'bigip-{site}'}}, 'cpu', {{'core':'{i}'}}, toDateTime64({ts}, 9), {h * 10 + i})")
    return 'INSERT INTO ast.otel_metrics_gauge VALUES ' + ', '.join(rows)


@pytest.fixture
def fresh(servers):
    for ep in (servers['src'], servers['dst']):
        ep.execute('DROP DATABASE IF EXISTS ast SYNC')
        ep.execute('DROP DATABASE IF EXISTS other SYNC')
        ep.execute('CREATE DATABASE ast')
    return servers


def syncer(src, dst, src_tcp, **over):
    cfg = cs.SyncConfig(source=src, dest=dst, source_native_port=src_tcp, databases=['ast'],
                        lookback_hours=72, time_columns=['TimeUnix', 'Timestamp'],
                        insert_timeout=120, interval=1)
    for k, v in over.items():
        setattr(cfg, k, v)
    return cs.Syncer(cfg)


def count(ep, table, where='1'):
    return int(ep.query(f'SELECT count() AS c FROM ast.{table} WHERE {where}')[0]['c'])


def distinct_rows(ep, table):
    return int(ep.query(f'SELECT uniqExact(*) AS c FROM ast.{table}')[0]['c'])


def test_schema_created_on_empty_dest(fresh):
    src, dst = fresh['src'], fresh['dst']
    src.execute(METRICS_DDL)
    src.execute(LOGS_DDL)
    src.execute('CREATE TABLE ast.no_time (k String) ENGINE = MergeTree ORDER BY k')
    src.execute(gauge_rows([3, 2], 'p'))
    src.execute("INSERT INTO ast.no_time VALUES ('x')")

    syncer(src, dst, fresh['src_tcp']).run_once()

    assert set(t['name'] for t in dst.query("SELECT name FROM system.tables WHERE database='ast'")) == {
        'otel_metrics_gauge', 'otel_logs', 'no_time'}
    assert count(dst, 'otel_metrics_gauge') == 10
    assert count(dst, 'no_time') == 0   # skipped: no time column


def test_bidirectional_union_after_failover_no_loss_no_dupes(fresh):
    """
    Primary collected hours 5,4 then died; DR collected 3,2 (and overlapping
    hour 4 with different rows); primary came back for hour 1.
    After syncing both ways each site holds the union, nothing is deleted,
    and re-running inserts nothing.
    """
    p, d = fresh['src'], fresh['dst']
    for ep in (p, d):
        ep.execute(METRICS_DDL)
        ep.execute(LOGS_DDL)
    p.execute(gauge_rows([5, 4, 1], 'p'))
    d.execute(gauge_rows([4, 3, 2], 'd'))
    p.execute(f"INSERT INTO ast.otel_logs VALUES (toDateTime64({NOW - 5 * HOUR}, 9), 'INFO', 'p-log', {{}})")
    d.execute(f"INSERT INTO ast.otel_logs VALUES (toDateTime64({NOW - 3 * HOUR}, 9), 'WARN', 'd-log', {{}})")

    dr_pull = syncer(p, d, fresh['src_tcp'])      # on DR: pull from primary
    primary_pull = syncer(d, p, fresh['dst_tcp'])  # on primary: pull from DR

    dr_pull.run_once()
    primary_pull.run_once()

    for ep in (p, d):
        assert count(ep, 'otel_metrics_gauge') == 30
        assert distinct_rows(ep, 'otel_metrics_gauge') == 30
        assert count(ep, 'otel_logs') == 2

    # Idempotent: nothing more to do.
    assert dr_pull.sync_table('ast', 'otel_metrics_gauge') == 0
    assert primary_pull.sync_table('ast', 'otel_metrics_gauge') == 0
    assert count(p, 'otel_metrics_gauge') == 30 and count(d, 'otel_metrics_gauge') == 30


def test_dest_only_rows_never_deleted(fresh):
    """Regression for the old drop-partition-then-copy behaviour."""
    p, d = fresh['src'], fresh['dst']
    for ep in (p, d):
        ep.execute(METRICS_DDL)
    p.execute(gauge_rows([2], 'p'))
    d.execute(gauge_rows([2], 'd'))   # same hour/partition, different rows
    syncer(p, d, fresh['src_tcp']).run_once()
    assert count(d, 'otel_metrics_gauge', "ResourceAttributes['host'] = 'bigip-d'") == 5
    assert count(d, 'otel_metrics_gauge') == 10


def test_lookback_window_respected(fresh):
    p, d = fresh['src'], fresh['dst']
    for ep in (p, d):
        ep.execute(METRICS_DDL)
    p.execute(gauge_rows([10, 2], 'p'))
    syncer(p, d, fresh['src_tcp'], lookback_hours=5).run_once()
    assert count(d, 'otel_metrics_gauge') == 5


def test_partial_bucket_filled(fresh):
    """Dest already has some of the rows in an hour (e.g. a sync interrupted mid-way)."""
    p, d = fresh['src'], fresh['dst']
    for ep in (p, d):
        ep.execute(METRICS_DDL)
    p.execute(gauge_rows([2], 'p'))
    d.execute(f"INSERT INTO ast.otel_metrics_gauge SELECT * FROM remote('127.0.0.1:{fresh['src_tcp']}', 'ast', "
              f"'otel_metrics_gauge', 'default', {cs.quote_str(SOURCE_PASSWORD)}) WHERE Value < 22")
    assert count(d, 'otel_metrics_gauge') == 2
    syncer(p, d, fresh['src_tcp']).run_once()
    assert count(d, 'otel_metrics_gauge') == 5
    assert distinct_rows(d, 'otel_metrics_gauge') == 5
