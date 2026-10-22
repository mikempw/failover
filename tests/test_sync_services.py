from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


vm_sync = load_module("vm_sync_test_module", "vm-sync/vm_sync.py")
ch_sync = load_module("ch_sync_test_module", "ch-sync/ch_sync.py")


class FakeResponse:
    def __init__(self, body: bytes = b"", status: int = 200):
        self.body = body
        self.status = status

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def vm_config(state_file: Path, **overrides):
    values = dict(
        source=vm_sync.Endpoint("http://source:8428"),
        destination=vm_sync.Endpoint("http://destination:8428"),
        interval_seconds=300,
        initial_lookback_seconds=100,
        chunk_seconds=100,
        overlap_seconds=10,
        settle_delay_seconds=0,
        state_file=state_file,
        request_timeout_seconds=5,
        max_attempts=2,
        retry_initial_seconds=0,
        retry_max_seconds=0,
        health_port=8080,
        readiness_max_age_seconds=900,
        max_rows_per_line=10000,
    )
    values.update(overrides)
    return vm_sync.Config(**values)


def ch_config(**overrides):
    values = dict(
        source=ch_sync.Endpoint("http", "source", 8123, "sync", "source-secret"),
        destination=ch_sync.Endpoint("http", "destination", 8123, "sync", "dest-secret"),
        databases=["ast"],
        interval_seconds=300,
        request_timeout_seconds=5,
        copy_timeout_seconds=30,
        max_attempts=2,
        retry_initial_seconds=0,
        retry_max_seconds=0,
        health_port=8080,
        readiness_max_age_seconds=900,
    )
    if overrides.get("guard_dns_record") and "guard_lease_proof_file" not in overrides:
        values["guard_lease_proof_file"] = "/failover-state/observed-lease.json"
    values.update(overrides)
    return ch_sync.Config(**values)


def ch_proof(owner: str, ip: str, *, expires_at: int = 2_000):
    return ch_sync.LeaseProof(
        record="ast.example.test",
        ip=ip,
        owner=owner,
        expires_at=expires_at,
        observed_at=1_000,
    )


class VictoriaMetricsSyncTests(unittest.TestCase):
    def test_external_replication_mode_refuses_to_start(self):
        with mock.patch.dict(
            os.environ, {"DATA_REPLICATION_MODE": "external"}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "vm-sync is disabled"):
                vm_sync.Config.from_env()

    def test_explicit_start_timestamp_anchors_a_missing_watermark(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            service = vm_sync.VictoriaMetricsSync(
                vm_config(state_file, initial_start_timestamp=100),
                clock=lambda: 1000.0,
            )

            self.assertEqual(service.load_watermark(), 100)
            self.assertEqual(
                json.loads(state_file.read_text(encoding="utf-8"))[
                    "successful_through"
                ],
                100,
            )

    def test_complete_export_import_path_and_watermark(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            requests = []
            exported = b'{"metric":{"__name__":"up"},"values":[1],"timestamps":[1]}\n'

            def opener(request, **kwargs):
                requests.append(request)
                if request.full_url.startswith("http://source:8428/api/v1/export"):
                    return FakeResponse(exported)
                if request.full_url == "http://destination:8428/api/v1/import":
                    self.assertEqual(request.data, exported)
                    self.assertEqual(request.method, "POST")
                    return FakeResponse()
                self.fail(f"unexpected request {request.full_url}")

            config = vm_config(state_file)
            http = vm_sync.RetryingHttpClient(5, 1, 0, 0, opener=opener)
            service = vm_sync.VictoriaMetricsSync(
                config, http=http, clock=lambda: 1000.0
            )

            result = service.run_once()

            self.assertTrue(result.success)
            self.assertEqual(result.series, 1)
            self.assertEqual(result.windows, 1)
            self.assertEqual(
                json.loads(state_file.read_text(encoding="utf-8"))["successful_through"],
                1000,
            )
            self.assertEqual(len(requests), 2)

    def test_api_failure_does_not_advance_watermark(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text('{"successful_through": 900}\n', encoding="utf-8")

            def opener(request, **kwargs):
                raise urllib.error.HTTPError(
                    request.full_url, 400, "bad selector", {}, None
                )

            service = vm_sync.VictoriaMetricsSync(
                vm_config(state_file),
                http=vm_sync.RetryingHttpClient(5, 1, 0, 0, opener=opener),
                clock=lambda: 1000.0,
            )

            result = service.run_once()

            self.assertFalse(result.success)
            self.assertEqual(result.watermark, 900)
            self.assertEqual(
                json.loads(state_file.read_text(encoding="utf-8"))["successful_through"],
                900,
            )

    def test_first_failed_run_persists_initial_catchup_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"

            def opener(request, **kwargs):
                raise urllib.error.HTTPError(
                    request.full_url, 400, "source unavailable", {}, None
                )

            service = vm_sync.VictoriaMetricsSync(
                vm_config(state_file, initial_lookback_seconds=100),
                http=vm_sync.RetryingHttpClient(5, 1, 0, 0, opener=opener),
                clock=lambda: 1000.0,
            )

            result = service.run_once()

            self.assertFalse(result.success)
            self.assertEqual(result.watermark, 900)
            self.assertEqual(
                json.loads(state_file.read_text(encoding="utf-8"))["successful_through"],
                900,
            )

    def test_partial_catchup_failure_does_not_commit_earlier_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text('{"successful_through": 100}\n', encoding="utf-8")
            exports = 0

            def opener(request, **kwargs):
                nonlocal exports
                if request.full_url.startswith("http://source:8428/api/v1/export"):
                    exports += 1
                    if exports == 2:
                        raise urllib.error.HTTPError(
                            request.full_url, 400, "broken window", {}, None
                        )
                    return FakeResponse(b'{"metric":{},"values":[],"timestamps":[]}\n')
                return FakeResponse()

            service = vm_sync.VictoriaMetricsSync(
                vm_config(state_file, chunk_seconds=300),
                http=vm_sync.RetryingHttpClient(5, 1, 0, 0, opener=opener),
                clock=lambda: 1000.0,
            )

            result = service.run_once()

            self.assertFalse(result.success)
            self.assertEqual(result.windows, 1)
            self.assertEqual(
                json.loads(state_file.read_text(encoding="utf-8"))["successful_through"],
                100,
            )

    def test_persisted_watermark_catches_up_beyond_initial_lookback(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text('{"successful_through": 100}\n', encoding="utf-8")
            starts = []

            def opener(request, **kwargs):
                if request.full_url.startswith("http://source:8428/api/v1/export"):
                    query = urllib.parse.parse_qs(
                        urllib.parse.urlsplit(request.full_url).query
                    )
                    starts.append(int(query["start"][0]))
                    return FakeResponse()
                self.fail("empty exports should not call the destination")

            service = vm_sync.VictoriaMetricsSync(
                vm_config(state_file, chunk_seconds=300),
                http=vm_sync.RetryingHttpClient(5, 1, 0, 0, opener=opener),
                clock=lambda: 1000.0,
            )

            result = service.run_once()

            self.assertTrue(result.success)
            self.assertEqual(result.windows, 3)
            self.assertEqual(starts, [90, 390, 690])
            self.assertEqual(result.watermark, 1000)

    def test_transient_http_failure_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            attempts = 0

            def opener(request, **kwargs):
                nonlocal attempts
                if request.full_url.startswith("http://source:8428/api/v1/export"):
                    attempts += 1
                    if attempts == 1:
                        raise urllib.error.HTTPError(
                            request.full_url, 503, "try again", {"Retry-After": "0"}, None
                        )
                    return FakeResponse()
                self.fail("empty exports should not call destination")

            http = vm_sync.RetryingHttpClient(
                5, 2, 0, 0, opener=opener, sleep=lambda _: None
            )
            service = vm_sync.VictoriaMetricsSync(
                vm_config(Path(directory) / "state.json"),
                http=http,
                clock=lambda: 1000.0,
            )

            result = service.run_once()

            self.assertTrue(result.success)
            self.assertEqual(result.retries, 1)
            self.assertEqual(attempts, 2)

    def test_basic_credentials_are_headers_not_url(self):
        endpoint = vm_sync.Endpoint(
            "https://metrics.example", "reader", "s/ecret?value", ""
        )
        seen = []

        def opener(request, **kwargs):
            seen.append(request)
            return FakeResponse()

        client = vm_sync.RetryingHttpClient(5, 1, 0, 0, opener=opener)
        client.request(endpoint, "/health")

        self.assertNotIn("secret", seen[0].full_url)
        self.assertNotIn("reader", seen[0].full_url)
        self.assertTrue(seen[0].get_header("Authorization").startswith("Basic "))

    def test_vm_password_file_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_file = Path(directory) / "vm-password"
            secret_file.write_text("file-secret\n", encoding="utf-8")
            environment = {
                "SOURCE_URL": "https://source.example",
                "SOURCE_USER": "reader",
                "SOURCE_PASSWORD_FILE": str(secret_file),
                "DEST_URL": "https://destination.example",
                "DEST_USER": "writer",
                "DEST_PASSWORD_FILE": str(secret_file),
                "SYNC_STATE_FILE": str(Path(directory) / "state.json"),
            }
            with mock.patch.dict(os.environ, environment, clear=True):
                config = vm_sync.Config.from_env()

        self.assertEqual(config.source.password, "file-secret")
        self.assertEqual(config.destination.password, "file-secret")


class RecordingClient:
    def __init__(self, *, payload: bytes = b"native", import_error: Exception | None = None):
        self.executed = []
        self.queries = []
        self.imports = []
        self.payload = payload
        self.import_error = import_error

    def query(self, sql):
        self.queries.append(sql)
        return []

    def execute(self, sql):
        self.executed.append(sql)

    def export_native(self, sql):
        self.queries.append(sql)
        return self.payload

    def import_native(self, database, table, payload):
        self.imports.append((database, table, payload))
        if self.import_error:
            raise self.import_error


class ClickHouseSyncTests(unittest.TestCase):
    def test_external_replication_mode_refuses_to_start(self):
        with mock.patch.dict(
            os.environ, {"DATA_REPLICATION_MODE": "external"}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "ch-sync is disabled"):
                ch_sync.Config.from_env()

    def make_service(self):
        http = ch_sync.RetryingHttpClient(5, 1, 0, 0)
        return ch_sync.ClickHouseSync(ch_config(), http=http, clock=lambda: 1000.0)

    def test_partition_discovery_uses_partition_id(self):
        service = self.make_service()

        class PartClient(RecordingClient):
            def query(self, sql):
                self.queries.append(sql)
                return [{"partition_id": "20261022", "rows": "7"}]

        client = PartClient()
        partitions = service.get_partitions(client, "ast", "otel_metrics")

        self.assertEqual(partitions["20261022"].rows, 7)
        self.assertIn("SELECT partition_id", client.queries[0])
        self.assertNotIn("SELECT partition,", client.queries[0])

    def test_import_failure_never_modifies_live_partition(self):
        service = self.make_service()
        source = RecordingClient()
        destination = RecordingClient(import_error=RuntimeError("copy failed"))
        service.source = source
        service.destination = destination

        with self.assertRaisesRegex(RuntimeError, "copy failed"):
            service.sync_partition("ast", "otel_metrics", "20261022", 7)

        statements = "\n".join(destination.executed)
        self.assertNotIn("REPLACE PARTITION", statements)
        self.assertNotIn("DROP PARTITION", statements)
        self.assertIn("DROP TABLE IF EXISTS", statements)

    def test_staging_is_verified_before_atomic_replace(self):
        service = self.make_service()
        source = RecordingClient()
        destination = RecordingClient()
        service.source = source
        service.destination = destination
        counts = iter([7, 7])
        service.count_partition = lambda *args: next(counts)

        service.sync_partition("ast", "otel_metrics", "20261022", 7)

        statements = destination.executed
        replace_positions = [
            index for index, sql in enumerate(statements) if "REPLACE PARTITION ID" in sql
        ]
        self.assertEqual(len(replace_positions), 1)
        self.assertNotIn("DROP PARTITION", "\n".join(statements))
        self.assertIn("'20261022'", statements[replace_positions[0]])
        self.assertEqual(destination.imports[0][2], b"native")

    def test_failed_staging_verification_does_not_replace(self):
        service = self.make_service()
        service.source = RecordingClient()
        destination = RecordingClient()
        service.destination = destination
        service.count_partition = lambda *args: 6

        with self.assertRaisesRegex(RuntimeError, "staging verification failed"):
            service.sync_partition("ast", "otel_metrics", "20261022", 7)

        self.assertNotIn("REPLACE PARTITION", "\n".join(destination.executed))

    def test_dns_transition_to_destination_blocks_partition_replace(self):
        config = ch_config(
            guard_dns_record="ast.example.test",
            guard_destination_ip="192.0.2.25",
        )
        resolver = mock.Mock(
            side_effect=[{"192.0.2.10"}, {"192.0.2.25"}]
        )
        proof_reader = mock.Mock(
            side_effect=[
                ch_proof("primary", "192.0.2.10"),
                ch_proof("dr", "192.0.2.25"),
            ]
        )
        service = ch_sync.ClickHouseSync(
            config,
            http=ch_sync.RetryingHttpClient(5, 1, 0, 0),
            clock=lambda: 1000.0,
            resolver=resolver,
            proof_reader=proof_reader,
        )
        service.source = RecordingClient()
        destination = RecordingClient()
        service.destination = destination
        service.count_partition = lambda *args: 7

        def sync_after_initial_guard(database):
            service.sync_partition(database, "otel_metrics", "20261022", 7)
            return (1, 7, 1, 7)

        service.sync_database = sync_after_initial_guard

        result = service.run_once()

        self.assertFalse(result.success)
        self.assertIn("became active before replacing", result.error)
        self.assertNotIn("REPLACE PARTITION", "\n".join(destination.executed))
        self.assertEqual(resolver.call_count, 2)

    def test_clickhouse_credentials_are_headers_not_url_or_sql(self):
        seen = []

        def opener(request, **kwargs):
            seen.append(request)
            return FakeResponse(b'{"1":1}\n')

        endpoint = ch_sync.Endpoint(
            "https", "clickhouse.example", 8443, "sync-user", "p'a&ss?word"
        )
        http = ch_sync.RetryingHttpClient(5, 1, 0, 0, opener=opener)
        client = ch_sync.ClickHouseClient(endpoint, http, 30)
        client.query("SELECT 1")

        request = seen[0]
        self.assertEqual(request.full_url, "https://clickhouse.example:8443/")
        self.assertNotIn("p'a&ss?word", request.full_url)
        self.assertNotIn(b"p'a&ss?word", request.data)
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["x-clickhouse-user"], "sync-user")
        self.assertEqual(headers["x-clickhouse-key"], "p'a&ss?word")

    def test_clickhouse_password_file_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_file = Path(directory) / "clickhouse-password"
            secret_file.write_text("file-secret\n", encoding="utf-8")
            environment = {
                "SOURCE_PASSWORD_FILE": str(secret_file),
                "SOURCE_USER": "sync-user",
                "DEST_PASSWORD_FILE": str(secret_file),
                "DEST_USER": "sync-user",
            }
            with mock.patch.dict(os.environ, environment, clear=True):
                config = ch_sync.Config.from_env()

        self.assertEqual(config.source.password, "file-secret")
        self.assertEqual(config.destination.password, "file-secret")

    def test_guard_config_falls_back_to_shared_failover_environment(self):
        environment = {
            "DNS_RECORD": "ast.example.test.",
            "DR_IP": "192.0.2.25",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            config = ch_sync.Config.from_env()

        self.assertEqual(config.guard_dns_record, "ast.example.test")
        self.assertEqual(config.guard_destination_ip, "192.0.2.25")
        self.assertEqual(
            config.guard_lease_proof_file,
            "/failover-state/observed-lease.json",
        )

    def test_guard_config_requires_record_and_ipv4_address_together(self):
        with mock.patch.dict(
            os.environ, {"SYNC_GUARD_DNS_RECORD": "ast.example.test"}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "must be set together"):
                ch_sync.Config.from_env()
        with self.assertRaisesRegex(ValueError, "must be an IPv4 address"):
            ch_config(
                guard_dns_record="ast.example.test",
                guard_destination_ip="2001:db8::25",
            )

    def test_active_destination_guard_skips_before_any_sync_operation(self):
        config = ch_config(
            guard_dns_record="ast.example.test",
            guard_destination_ip="192.0.2.25",
        )
        service = ch_sync.ClickHouseSync(
            config,
            http=ch_sync.RetryingHttpClient(5, 1, 0, 0),
            clock=lambda: 1000.0,
            resolver=lambda hostname: {"192.0.2.25"},
            proof_reader=lambda _path: ch_proof("dr", "192.0.2.25"),
        )
        service.sync_database = mock.Mock(
            side_effect=AssertionError("sync must not start while DR is active")
        )

        result = service.run_once()

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        service.sync_database.assert_not_called()
        snapshot = service.status.snapshot()
        self.assertEqual(snapshot["gauges"]["standby_guard_active"], 1)

    def test_guard_resolution_failure_fails_closed_before_sync(self):
        config = ch_config(
            guard_dns_record="ast.example.test",
            guard_destination_ip="192.0.2.25",
        )

        def failed_resolver(hostname):
            raise OSError("temporary resolver failure")

        service = ch_sync.ClickHouseSync(
            config,
            http=ch_sync.RetryingHttpClient(5, 1, 0, 0),
            clock=lambda: 1000.0,
            resolver=failed_resolver,
        )
        service.sync_database = mock.Mock(
            side_effect=AssertionError("sync must not start when guard resolution fails")
        )

        result = service.run_once()

        self.assertFalse(result.success)
        self.assertFalse(result.skipped)
        self.assertIn("refusing to sync", result.error)
        service.sync_database.assert_not_called()
        snapshot = service.status.snapshot()
        self.assertEqual(snapshot["gauges"]["standby_guard_resolution_ok"], 0)

    def test_inactive_destination_guard_allows_sync(self):
        config = ch_config(
            guard_dns_record="ast.example.test",
            guard_destination_ip="192.0.2.25",
        )
        service = ch_sync.ClickHouseSync(
            config,
            http=ch_sync.RetryingHttpClient(5, 1, 0, 0),
            clock=lambda: 1000.0,
            resolver=lambda hostname: {"192.0.2.10"},
            proof_reader=lambda _path: ch_proof("primary", "192.0.2.10"),
        )
        service.sync_database = mock.Mock(return_value=(1, 7, 1, 7))

        result = service.run_once()

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(result.partitions, 1)
        service.sync_database.assert_called_once_with("ast")

    def test_stale_dns_cannot_override_active_dr_lease_proof(self):
        config = ch_config(
            guard_dns_record="ast.example.test",
            guard_destination_ip="192.0.2.25",
        )
        service = ch_sync.ClickHouseSync(
            config,
            http=ch_sync.RetryingHttpClient(5, 1, 0, 0),
            clock=lambda: 1000.0,
            resolver=lambda hostname: {"192.0.2.10"},
            proof_reader=lambda _path: ch_proof("dr", "192.0.2.25"),
        )
        service.sync_database = mock.Mock()

        result = service.run_once()

        self.assertFalse(result.success)
        self.assertIn("disagrees with the DNS A record", result.error)
        service.sync_database.assert_not_called()

    def test_missing_lease_proof_fails_closed_before_sync(self):
        config = ch_config(
            guard_dns_record="ast.example.test",
            guard_destination_ip="192.0.2.25",
        )
        service = ch_sync.ClickHouseSync(
            config,
            http=ch_sync.RetryingHttpClient(5, 1, 0, 0),
            clock=lambda: 1000.0,
            resolver=lambda hostname: {"192.0.2.10"},
            proof_reader=lambda _path: (_ for _ in ()).throw(
                RuntimeError("proof missing")
            ),
        )
        service.sync_database = mock.Mock()

        result = service.run_once()

        self.assertFalse(result.success)
        self.assertIn("proof missing", result.error)
        service.sync_database.assert_not_called()

    def test_active_destination_override_is_rejected(self):
        with self.assertRaisesRegex(
            ValueError, "SYNC_ALLOW_ACTIVE_DESTINATION has been removed"
        ):
            ch_config(
                guard_dns_record="ast.example.test",
                guard_destination_ip="192.0.2.25",
                allow_active_destination_confirmation="true",
            )

        with self.assertRaisesRegex(
            ValueError, "SYNC_ALLOW_ACTIVE_DESTINATION has been removed"
        ):
            ch_config(
                guard_dns_record="ast.example.test",
                guard_destination_ip="192.0.2.25",
                allow_active_destination_confirmation="REPLACE_DESTINATION_PARTITIONS",
            )

    def test_recovery_guard_protects_primary_destination(self):
        config = ch_config(
            guard_dns_record="ast.example.test",
            guard_destination_ip="192.0.2.10",
        )
        service = ch_sync.ClickHouseSync(
            config,
            http=ch_sync.RetryingHttpClient(5, 1, 0, 0),
            clock=lambda: 1000.0,
            resolver=lambda hostname: {"192.0.2.25"},
            proof_reader=lambda _path: ch_proof("dr", "192.0.2.25"),
        )
        service.sync_database = mock.Mock(return_value=(1, 7, 1, 7))

        result = service.run_once()

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        service.sync_database.assert_called_once_with("ast")

        service.resolver = lambda hostname: {"192.0.2.10"}
        service.proof_reader = lambda _path: ch_proof("primary", "192.0.2.10")
        service.sync_database.reset_mock()

        result = service.run_once()

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        service.sync_database.assert_not_called()

    def test_run_once_propagates_database_failure_to_status(self):
        service = self.make_service()
        service.sync_database = lambda database: (_ for _ in ()).throw(
            RuntimeError("database unavailable")
        )

        result = service.run_once()

        self.assertFalse(result.success)
        self.assertIn("database unavailable", result.error)
        snapshot = service.status.snapshot()
        self.assertEqual(snapshot["failures_total"], 1)
        self.assertFalse(snapshot["ready"])


if __name__ == "__main__":
    unittest.main()
