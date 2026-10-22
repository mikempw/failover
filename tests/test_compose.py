"""Policy tests for the two deployable Compose stacks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = {
    "primary": ROOT / "docker-compose.primary.yaml",
    "dr": ROOT / "docker-compose.dr.yaml",
}


class ComposePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        docker = shutil.which("docker")
        if docker is None:
            raise unittest.SkipTest("Docker Compose is required for compose policy tests")

        cls._tempdir = tempfile.TemporaryDirectory(prefix="failover-compose-tests-")
        secret_dir = Path(cls._tempdir.name)
        for name in (
            "clickhouse_password.txt",
            "grafana_clickhouse_password.txt",
            "primary_clickhouse_password.txt",
            "grafana_admin_password.txt",
        ):
            (secret_dir / name).write_text(
                f"ci-only-{name}-4f6da376dd73416b", encoding="utf-8"
            )

        env = os.environ.copy()
        env.update(
            {
                "CLICKHOUSE_PASSWORD_FILE": str(secret_dir / "clickhouse_password.txt"),
                "GRAFANA_CLICKHOUSE_PASSWORD_FILE": str(
                    secret_dir / "grafana_clickhouse_password.txt"
                ),
                "PRIMARY_CLICKHOUSE_PASSWORD_FILE": str(
                    secret_dir / "primary_clickhouse_password.txt"
                ),
                "GRAFANA_ADMIN_PASSWORD_FILE": str(
                    secret_dir / "grafana_admin_password.txt"
                ),
                "BIND_ADDRESS": "127.0.0.1",
                "CLICKHOUSE_REMOTE_CIDRS": "198.51.100.0/24",
            }
        )
        cls.configs = {
            site: cls._render(docker, compose_file, env)
            for site, compose_file in COMPOSE_FILES.items()
        }

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "_tempdir"):
            cls._tempdir.cleanup()

    @staticmethod
    def _render(docker: str, compose_file: Path, env: dict[str, str]) -> dict:
        result = subprocess.run(
            [docker, "compose", "-f", str(compose_file), "config", "--format", "json"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise AssertionError(
                f"{compose_file.name} failed validation:\n{result.stdout}\n{result.stderr}"
            )
        return json.loads(result.stdout)

    def test_expected_services_are_present(self) -> None:
        self.assertIn("ch-sync", self.configs["dr"]["services"])
        self.assertNotIn("ch-sync", self.configs["primary"]["services"])
        for config in self.configs.values():
            self.assertTrue(
                {
                    "victoriametrics",
                    "clickhouse",
                    "otel-collector",
                    "grafana",
                    "dns-failover",
                    "otel-watcher",
                    "vm-sync",
                }.issubset(config["services"])
            )

    def test_watcher_uses_the_authoritative_failover_environment(self) -> None:
        for config in self.configs.values():
            dns_env = config["services"]["dns-failover"]["environment"]
            watcher_env = config["services"]["otel-watcher"]["environment"]
            collector_env = config["services"]["otel-collector"]["environment"]
            for key in ("ROLE", "DNS_RECORD", "PRIMARY_IP", "DR_IP"):
                self.assertEqual(dns_env[key], watcher_env[key], key)
                self.assertEqual(dns_env[key], collector_env[key], key)
            self.assertNotEqual(dns_env["PRIMARY_IP"], dns_env["DR_IP"])

        dr_services = self.configs["dr"]["services"]
        ch_env = dr_services["ch-sync"]["environment"]
        dns_env = dr_services["dns-failover"]["environment"]
        self.assertEqual(ch_env["DNS_RECORD"], dns_env["DNS_RECORD"])
        self.assertEqual(ch_env["DR_IP"], dns_env["DR_IP"])

    def test_old_hardcoded_watcher_values_are_gone(self) -> None:
        for compose_file in COMPOSE_FILES.values():
            text = compose_file.read_text(encoding="utf-8")
            self.assertNotIn("failover.mpwlabs.com", text)
            self.assertNotIn("192.168.100.143", text)
            self.assertNotIn("192.168.100.144", text)

    def test_collector_bootstrap_gate_fails_closed_on_dns_ownership(self) -> None:
        dockerfile = (ROOT / "services/otel_collector/Dockerfile").read_text(
            encoding="utf-8"
        )
        gate = (ROOT / "services/otel_collector/bootstrap-gate.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('ENTRYPOINT ["/bin/busybox", "sh", "/otel-bootstrap-gate.sh"]', dockerfile)
        self.assertIn('address_count" -eq 1', gate)
        self.assertIn('addresses" = "$expected_ip', gate)
        self.assertIn("lease_proof_matches", gate)
        self.assertIn("stable_count=$((stable_count + 1))", gate)
        self.assertIn("OTEL_SECRETS_STAGED", gate)
        self.assertIn("drop-privileges 10001 0", gate)
        self.assertIn("/run/runtime-config/receivers.yaml", gate)
        self.assertIn("OTEL_CONFIG_VALIDATE_ONLY", gate)
        self.assertIn('exec /otelcol-custom "$@"', gate)

        self.assertIn("./.env.primary", COMPOSE_FILES["primary"].read_text(encoding="utf-8"))
        self.assertIn("./.env.dr", COMPOSE_FILES["dr"].read_text(encoding="utf-8"))

    def test_passwords_are_secret_backed_and_not_defaulted(self) -> None:
        users_xml = (ROOT / "services/clickhouse/users.xml").read_text(encoding="utf-8")
        entrypoint = (ROOT / "services/clickhouse/secure-entrypoint.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("<password></password>", users_xml)
        self.assertNotIn("<ip>::/0</ip>", users_xml)
        self.assertNotIn("<ip>::/0</ip>", entrypoint)
        self.assertIn("<password_sha256_hex>", entrypoint)
        self.assertIn('<default remove="remove" />', entrypoint)
        self.assertIn("<ip>127.0.0.1/32</ip>", entrypoint)
        self.assertIn("<ip>::1/128</ip>", entrypoint)
        self.assertIn("<readonly>1</readonly>", users_xml)
        self.assertIn("<changeable_in_readonly />", users_xml)

        for config in self.configs.values():
            services = config["services"]
            clickhouse = services["clickhouse"]
            self.assertEqual(clickhouse["environment"]["CLICKHOUSE_USER"], "ast")
            self.assertEqual(
                clickhouse["environment"]["CLICKHOUSE_SKIP_USER_SETUP"], "1"
            )
            self.assertNotIn("CLICKHOUSE_PASSWORD", clickhouse["environment"])
            self.assertEqual(
                clickhouse["environment"]["CLICKHOUSE_REMOTE_CIDRS"],
                "198.51.100.0/24",
            )
            subnet = config["networks"]["ast_network"]["ipam"]["config"][0][
                "subnet"
            ]
            self.assertEqual(
                clickhouse["environment"]["CLICKHOUSE_INTERNAL_CIDR"], subnet
            )
            clickhouse_secrets = {
                secret["source"] if isinstance(secret, dict) else secret
                for secret in clickhouse["secrets"]
            }
            self.assertIn("clickhouse_password", clickhouse_secrets)
            self.assertIn("grafana_clickhouse_password", clickhouse_secrets)

            grafana = services["grafana"]
            self.assertEqual(
                grafana["environment"]["GF_SECURITY_ADMIN_PASSWORD__FILE"],
                "/run/secrets/grafana_admin_password",
            )
            self.assertNotIn("GF_SECURITY_ADMIN_PASSWORD", grafana["environment"])
            grafana_secrets = {
                secret["source"] if isinstance(secret, dict) else secret
                for secret in grafana["secrets"]
            }
            self.assertIn("grafana_admin_password", grafana_secrets)
            self.assertIn("grafana_clickhouse_password", grafana_secrets)
            self.assertEqual(grafana["environment"]["CLICKHOUSE_USER"], "grafana")
            self.assertEqual(grafana["environment"]["CLICKHOUSE_DATABASE"], "ast")
            self.assertEqual(
                grafana["environment"]["CLICKHOUSE_PASSWORD_FILE"],
                "/run/secrets/grafana_clickhouse_password",
            )
            self.assertNotIn("GF_INSTALL_PLUGINS", grafana["environment"])

        grafana_dockerfile = (ROOT / "services/grafana/Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn("grafana-clickhouse-datasource 4.14.0", grafana_dockerfile)
        self.assertIn("GF_PATHS_PLUGINS=/var/lib/grafana-plugins", grafana_dockerfile)
        self.assertNotIn("--pluginsDir /var/lib/grafana/plugins", grafana_dockerfile)
        self.assertIn("drop-privileges", grafana_dockerfile)

        datasource = (
            ROOT / "services/grafana/provisioning/datasources/datasources.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("password: ${CLICKHOUSE_PASSWORD}", datasource)
        self.assertNotIn("tlsSkipVerify", datasource)

    def test_all_runtime_services_have_healthchecks(self) -> None:
        for site, config in self.configs.items():
            for service_name, service in config["services"].items():
                self.assertIn(
                    "healthcheck", service, f"{site}:{service_name} has no healthcheck"
                )

        endpoint_expectations = {
            "victoriametrics": "/health",
            "clickhouse": "/ping",
            "grafana": "/api/health",
            "otel-collector": "/metrics",
            "dns-failover": "/readyz",
            "otel-watcher": "/readyz",
            "vm-sync": "/readyz",
        }
        for config in self.configs.values():
            for service_name, endpoint in endpoint_expectations.items():
                command = " ".join(config["services"][service_name]["healthcheck"]["test"])
                self.assertIn(endpoint, command)
        dr_ch_command = " ".join(
            self.configs["dr"]["services"]["ch-sync"]["healthcheck"]["test"]
        )
        self.assertIn("/readyz", dr_ch_command)

    def test_dependencies_wait_for_healthy_backends(self) -> None:
        for config in self.configs.values():
            services = config["services"]
            for dependent in ("otel-collector", "grafana"):
                self.assertEqual(
                    services[dependent]["depends_on"]["victoriametrics"]["condition"],
                    "service_healthy",
                )
                self.assertEqual(
                    services[dependent]["depends_on"]["clickhouse"]["condition"],
                    "service_healthy",
                )

    def test_custom_builds_use_repository_root_and_existing_dockerfiles(self) -> None:
        for config in self.configs.values():
            for service_name, service in config["services"].items():
                build = service.get("build")
                if not build:
                    continue
                self.assertEqual(Path(build["context"]).resolve(), ROOT.resolve())
                dockerfile = ROOT / build["dockerfile"]
                self.assertTrue(dockerfile.is_file(), f"missing {service_name} Dockerfile")

    def test_bind_mount_sources_exist(self) -> None:
        for config in self.configs.values():
            for service_name, service in config["services"].items():
                for mount in service.get("volumes", []):
                    if mount.get("type") != "bind":
                        continue
                    source = Path(mount["source"])
                    if str(source).replace("\\", "/") == "/var/run/docker.sock":
                        continue
                    self.assertTrue(
                        source.exists(), f"missing bind source for {service_name}: {source}"
                    )

    def test_preflight_validates_the_rendered_compose_paths(self) -> None:
        preflight = (ROOT / "services/preflight.sh").read_text(encoding="utf-8")
        self.assertIn("config --format json", preflight)
        self.assertIn("json.loads", preflight)
        self.assertIn('/run/source-otel-config/receivers.yaml', preflight)
        self.assertIn("2.24.0", preflight)
        self.assertIn("amd64", preflight)
        self.assertNotIn("required_secrets=", preflight)

    def test_dns_custom_provider_scripts_are_mounted_read_only(self) -> None:
        for config in self.configs.values():
            mounts = config["services"]["dns-failover"]["volumes"]
            scripts_mount = next(
                mount
                for mount in mounts
                if mount.get("target") == "/run/source-scripts"
            )
            self.assertEqual(scripts_mount["type"], "bind")
            self.assertTrue(scripts_mount["read_only"])
            self.assertEqual(
                Path(scripts_mount["source"]).resolve(), (ROOT / "scripts").resolve()
            )

        wrapper = (ROOT / "failover/secure-entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn('copy_tree /run/source-scripts /scripts "scripts" 0700', wrapper)

    def test_lease_proof_is_shared_and_fencing_starts_independently(self) -> None:
        for config in self.configs.values():
            services = config["services"]
            state_sources = {}
            for service_name in ("dns-failover", "otel-watcher", "otel-collector"):
                mount = next(
                    volume
                    for volume in services[service_name]["volumes"]
                    if volume.get("target") == "/state"
                )
                state_sources[service_name] = mount["source"]
            self.assertEqual(len(set(state_sources.values())), 1)
            self.assertFalse(
                next(
                    volume
                    for volume in services["dns-failover"]["volumes"]
                    if volume.get("target") == "/state"
                ).get("read_only", False)
            )
            self.assertTrue(
                next(
                    volume
                    for volume in services["otel-watcher"]["volumes"]
                    if volume.get("target") == "/state"
                )["read_only"]
            )
            self.assertNotIn("dns-failover", services["otel-watcher"].get("depends_on", {}))

        dr_ch_mount = next(
            volume
            for volume in self.configs["dr"]["services"]["ch-sync"]["volumes"]
            if volume.get("target") == "/failover-state"
        )
        self.assertTrue(dr_ch_mount["read_only"])

    def test_runtime_secrets_are_dedicated_and_staged_before_uid_drop(self) -> None:
        for config in self.configs.values():
            services = config["services"]
            failover_mount = next(
                volume
                for volume in services["dns-failover"]["volumes"]
                if volume.get("target") == "/run/source-secrets"
            )
            self.assertTrue(failover_mount["read_only"])
            self.assertEqual(Path(failover_mount["source"]).name, "failover")
            otel_mount = next(
                volume
                for volume in services["otel-collector"]["volumes"]
                if volume.get("target") == "/run/source-otel-secrets"
            )
            self.assertTrue(otel_mount["read_only"])
            receiver_mount = next(
                volume
                for volume in services["otel-collector"]["volumes"]
                if volume.get("target") == "/run/source-otel-config/receivers.yaml"
            )
            self.assertTrue(receiver_mount["read_only"])
            collector_env = services["otel-collector"]["environment"]
            self.assertNotIn("SENSOR_SECRET_TOKEN", collector_env)
            self.assertNotIn("BIGIP_PASSWORD_EXAMPLE", collector_env)

        receiver = (ROOT / "services/otel_collector/receivers.example.yaml").read_text(
            encoding="utf-8"
        )
        collector_config = (
            ROOT / "services/otel_collector/defaults/bigip-scraper-config.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("${file:/run/runtime-secrets/bigip_password}", receiver)
        self.assertIn("${file:/run/runtime-config/receivers.yaml}", collector_config)
        self.assertIn("${file:/run/runtime-secrets/datafabric_authorization}", collector_config)

    def test_datafabric_exporter_is_wired_to_the_bigip_metrics_pipeline(self) -> None:
        pipelines = (ROOT / "services/otel_collector/pipelines.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            """metrics/f5-datafabric:
  receivers:
    - bigip
  processors:
    - batch/f5-datafabric
    - interval/f5-datafabric
    - attributes/f5-datafabric
  exporters:
    - otlp/f5-datafabric
""",
            pipelines,
        )

    def test_reverse_clickhouse_recovery_keeps_destination_fencing_enabled(self) -> None:
        helper = (ROOT / "ch-sync/ch_sync_recovery.sh").read_text(encoding="utf-8")
        self.assertIn('export SYNC_GUARD_DNS_RECORD=', helper)
        self.assertIn('export SYNC_GUARD_DEST_IP=', helper)
        self.assertIn('PRIMARY_IP', helper)
        self.assertIn('SYNC_GUARD_LEASE_PROOF_FILE', helper)
        self.assertIn('unset SYNC_ALLOW_ACTIVE_DESTINATION', helper)
        self.assertNotIn('export SYNC_ALLOW_ACTIVE_DESTINATION=', helper)

    def test_published_ports_default_to_loopback(self) -> None:
        for config in self.configs.values():
            for service in config["services"].values():
                for port in service.get("ports", []):
                    self.assertEqual(port.get("host_ip"), "127.0.0.1")

    def test_examples_are_safe_and_runtime_files_are_ignored(self) -> None:
        for site in ("primary", "dr"):
            env = self.configs[site]["services"]["dns-failover"]["environment"]
            self.assertEqual(env["DNS_PROVIDER"], "dry-run")
            self.assertEqual(env["HEALTH_MODE"], "http")
            self.assertIn("example.test", env["DNS_RECORD"])

        for path in (
            ".env.primary",
            ".env.dr",
            "failover/.env.primary",
            "failover/.env.dr",
            "secrets/clickhouse_password.txt",
            "secrets/failover/cloudflare_api_token",
            "secrets/failover/gcp_credentials.json",
            "scripts/set_dns.sh",
            "scripts/get_dns.sh",
        ):
            ignored = subprocess.run(
                ["git", "check-ignore", "--no-index", "-q", path],
                cwd=ROOT,
                check=False,
            )
            self.assertEqual(ignored.returncode, 0, path)


if __name__ == "__main__":
    unittest.main()
