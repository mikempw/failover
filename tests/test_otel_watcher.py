import subprocess
import unittest

from common.health_server import ServiceStatus
from failover import otel_watcher_docker as watcher


def completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def base_environment(**overrides):
    values = {
        "ROLE": "dr",
        "PRIMARY_IP": "192.0.2.10",
        "DR_IP": "192.0.2.20",
        "DNS_RECORD": "failover.example.test",
        "DNS_SERVERS": "192.0.2.53",
        "DNS_USE_SYSTEM_RESOLVER": "false",
        "DNS_CONSISTENT_ANSWERS": "1",
    }
    values.update(overrides)
    return values


def local_proof(config, *, expires_at=2_000):
    return watcher.LeaseProof(
        record=config.dns_record,
        ip=config.my_ip,
        owner=config.role,
        expires_at=expires_at,
        observed_at=1_000,
    )


class DeriveIPTests(unittest.TestCase):
    def test_explicit_my_ip_wins(self):
        env = base_environment(MY_IP="198.51.100.7", DR_IP="not-an-ip")
        self.assertEqual(watcher.derive_my_ip(env), "198.51.100.7")

    def test_role_selects_primary_or_dr_address(self):
        primary = base_environment(ROLE="primary")
        dr = base_environment(ROLE="dr")
        self.assertEqual(watcher.derive_my_ip(primary), "192.0.2.10")
        self.assertEqual(watcher.derive_my_ip(dr), "192.0.2.20")

    def test_invalid_or_ipv6_addresses_are_rejected(self):
        with self.assertRaises(watcher.ConfigurationError):
            watcher.derive_my_ip(base_environment(DR_IP="999.1.1.1"))
        with self.assertRaises(watcher.ConfigurationError):
            watcher.derive_my_ip(base_environment(DR_IP="2001:db8::1"))

    def test_role_is_required_without_explicit_my_ip(self):
        with self.assertRaises(watcher.ConfigurationError):
            watcher.derive_my_ip({"PRIMARY_IP": "192.0.2.10"})


class DNSResolverTests(unittest.TestCase):
    GOOD_OUTPUT = """\
;; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 1
;; flags: qr rd ra ad; QUERY: 1, ANSWER: 2, AUTHORITY: 0, ADDITIONAL: 1

failover.example.test. 30 IN A 192.0.2.20
failover.example.test. 30 IN A 192.0.2.21
"""

    def test_configured_resolvers_are_tried_in_order(self):
        calls = []

        def run(command, **_kwargs):
            calls.append(command)
            if command[1] == "@192.0.2.53":
                return completed(command, returncode=9, stderr="timed out")
            return completed(command, stdout=self.GOOD_OUTPUT)

        resolver = watcher.DNSResolver(
            "failover.example.test",
            ("192.0.2.53", "198.51.100.53"),
            use_system_resolver=False,
            dnssec_required=False,
            timeout=2,
            run_command=run,
            logger=lambda _message, _level: None,
        )
        answer = resolver.resolve()

        self.assertEqual(answer.source, "dns:198.51.100.53")
        self.assertEqual(answer.addresses, ("192.0.2.20", "192.0.2.21"))
        self.assertEqual([call[1] for call in calls], ["@192.0.2.53", "@198.51.100.53"])

    def test_system_resolver_is_only_used_after_configured_resolvers(self):
        calls = []

        def run(command, **_kwargs):
            calls.append("dig")
            return completed(command, returncode=1, stderr="unavailable")

        def getaddrinfo(*_args):
            calls.append("system")
            return [
                (2, 1, 6, "", ("203.0.113.9", 0)),
                (2, 1, 6, "", ("203.0.113.9", 0)),
            ]

        resolver = watcher.DNSResolver(
            "failover.example.test",
            ("192.0.2.53",),
            use_system_resolver=True,
            dnssec_required=False,
            timeout=2,
            run_command=run,
            getaddrinfo=getaddrinfo,
            logger=lambda _message, _level: None,
        )

        answer = resolver.resolve()
        self.assertEqual(calls, ["dig", "system"])
        self.assertEqual(answer.addresses, ("203.0.113.9",))
        self.assertEqual(answer.source, "system")

    def test_any_invalid_a_answer_rejects_the_resolver_response(self):
        bad_output = """\
;; flags: qr rd ra ad; QUERY: 1, ANSWER: 2, AUTHORITY: 0, ADDITIONAL: 1
failover.example.test. 30 IN A 192.0.2.20
failover.example.test. 30 IN A definitely-not-an-ip
"""

        def run(command, **_kwargs):
            return completed(command, stdout=bad_output)

        resolver = watcher.DNSResolver(
            "failover.example.test",
            ("192.0.2.53",),
            use_system_resolver=False,
            dnssec_required=False,
            timeout=2,
            run_command=run,
            logger=lambda _message, _level: None,
        )
        with self.assertRaises(watcher.DNSResolutionError):
            resolver.resolve()

    def test_dnssec_mode_requires_ad_flag_and_never_uses_system_fallback(self):
        output_without_ad = """\
;; flags: qr rd ra; QUERY: 1, ANSWER: 1, AUTHORITY: 0, ADDITIONAL: 1
failover.example.test. 30 IN A 192.0.2.20
"""
        system_called = False

        def run(command, **_kwargs):
            return completed(command, stdout=output_without_ad)

        def getaddrinfo(*_args):
            nonlocal system_called
            system_called = True
            return []

        resolver = watcher.DNSResolver(
            "failover.example.test",
            ("192.0.2.53",),
            use_system_resolver=True,
            dnssec_required=True,
            timeout=2,
            run_command=run,
            getaddrinfo=getaddrinfo,
            logger=lambda _message, _level: None,
        )
        with self.assertRaises(watcher.DNSResolutionError):
            resolver.resolve()
        self.assertFalse(system_called)


class DockerManagerTests(unittest.TestCase):
    def test_inspect_error_is_not_reported_as_stopped(self):
        def run(command, **_kwargs):
            return completed(command, returncode=1, stderr="daemon connection failed")

        manager = watcher.DockerManager(
            "otel", command_timeout=2, stop_timeout=1, run_command=run
        )
        result = manager.inspect()
        self.assertEqual(result.state, watcher.ContainerState.ERROR)
        self.assertIn("daemon connection failed", result.error)

    def test_missing_container_has_a_distinct_state(self):
        def run(command, **_kwargs):
            return completed(command, returncode=1, stderr="No such object: otel")

        manager = watcher.DockerManager(
            "otel", command_timeout=2, stop_timeout=1, run_command=run
        )
        self.assertEqual(manager.inspect().state, watcher.ContainerState.MISSING)

    def test_failed_start_is_retried_and_converges(self):
        responses = iter(
            [
                completed([], stdout="false\n"),
                completed([], returncode=1, stderr="temporary start failure"),
                completed([], stdout="false\n"),
                completed([], stdout="otel\n"),
                completed([], stdout="true\n"),
            ]
        )
        commands = []

        def run(command, **_kwargs):
            commands.append(command)
            return next(responses)

        manager = watcher.DockerManager(
            "otel", command_timeout=2, stop_timeout=1, run_command=run
        )
        first = manager.reconcile(True)
        second = manager.reconcile(True)

        self.assertFalse(first.success)
        self.assertTrue(second.success)
        self.assertEqual(second.state, watcher.ContainerState.RUNNING)
        self.assertEqual(sum(command[1] == "start" for command in commands), 2)

    def test_failed_stop_is_retried_and_converges(self):
        responses = iter(
            [
                completed([], stdout="true\n"),
                completed([], returncode=1, stderr="temporary stop failure"),
                completed([], stdout="true\n"),
                completed([], stdout="otel\n"),
                completed([], stdout="false\n"),
            ]
        )
        commands = []

        def run(command, **_kwargs):
            commands.append(command)
            return next(responses)

        manager = watcher.DockerManager(
            "otel", command_timeout=2, stop_timeout=1, run_command=run
        )
        first = manager.reconcile(False)
        second = manager.reconcile(False)

        self.assertFalse(first.success)
        self.assertTrue(second.success)
        self.assertEqual(second.state, watcher.ContainerState.STOPPED)
        self.assertEqual(sum(command[1] == "stop" for command in commands), 2)


class FakeResolver:
    def __init__(self, addresses):
        self.addresses = tuple(addresses)

    def resolve(self):
        return watcher.DNSAnswer(self.addresses, "fake", True)


class SequenceResolver:
    def __init__(self, results):
        self.results = iter(results)

    def resolve(self):
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return watcher.DNSAnswer(tuple(result), "fake", True)


class SequenceDocker:
    def __init__(self, results):
        self.results = iter(results)
        self.desired_states = []

    def reconcile(self, desired):
        self.desired_states.append(desired)
        return next(self.results)


class WatcherLoopTests(unittest.TestCase):
    def test_same_desired_state_is_reconciled_after_failure(self):
        config = watcher.WatcherConfig.from_env(base_environment())
        docker = SequenceDocker(
            [
                watcher.ReconcileResult(
                    False,
                    watcher.ContainerState.STOPPED,
                    error="start failed",
                ),
                watcher.ReconcileResult(True, watcher.ContainerState.RUNNING, changed=True),
            ]
        )
        status = ServiceStatus("test_watcher", 60)
        service = watcher.OtelWatcher(
            config,
            FakeResolver((config.my_ip,)),
            docker,
            status,
            wall_clock=lambda: 1_000,
            proof_reader=lambda: local_proof(config),
        )

        self.assertEqual(service.run_once().kind, watcher.RunKind.ACTION_FAILURE)
        self.assertEqual(service.run_once().kind, watcher.RunKind.SUCCESS)
        self.assertEqual(docker.desired_states, [True, True])
        self.assertTrue(status.snapshot()["ready"])

    def test_consistency_threshold_delays_transition(self):
        config = watcher.WatcherConfig.from_env(
            base_environment(DNS_CONSISTENT_ANSWERS="2")
        )
        docker = SequenceDocker(
            [watcher.ReconcileResult(True, watcher.ContainerState.RUNNING, changed=True)]
        )
        service = watcher.OtelWatcher(
            config,
            FakeResolver((config.my_ip,)),
            docker,
            ServiceStatus("test_watcher", 60),
            wall_clock=lambda: 1_000,
            proof_reader=lambda: local_proof(config),
        )

        self.assertEqual(service.run_once().kind, watcher.RunKind.PENDING)
        self.assertEqual(docker.desired_states, [])
        self.assertEqual(service.run_once().kind, watcher.RunKind.SUCCESS)
        self.assertEqual(docker.desired_states, [True])

    def test_multiple_a_records_immediately_fence_the_collector(self):
        config = watcher.WatcherConfig.from_env(
            base_environment(DNS_CONSISTENT_ANSWERS="1")
        )
        docker = SequenceDocker(
            [
                watcher.ReconcileResult(
                    True, watcher.ContainerState.STOPPED, changed=True
                )
            ]
        )
        status = ServiceStatus("test_watcher", 60)
        service = watcher.OtelWatcher(
            config,
            FakeResolver((config.my_ip, "192.0.2.21")),
            docker,
            status,
            wall_clock=lambda: 1_000,
            proof_reader=lambda: local_proof(config),
        )

        result = service.run_once()

        self.assertEqual(result.kind, watcher.RunKind.DNS_FAILURE)
        self.assertEqual(docker.desired_states, [False])
        self.assertIn("exactly one A record", result.message)
        self.assertFalse(status.snapshot()["ready"])

    def test_dns_partition_immediately_fences_formerly_active_collector(self):
        config = watcher.WatcherConfig.from_env(
            base_environment(DNS_CONSISTENT_ANSWERS="1")
        )
        docker = SequenceDocker(
            [
                watcher.ReconcileResult(
                    True, watcher.ContainerState.RUNNING
                ),
                watcher.ReconcileResult(
                    True, watcher.ContainerState.STOPPED, changed=True
                ),
            ]
        )
        service = watcher.OtelWatcher(
            config,
            SequenceResolver(
                [
                    (config.my_ip,),
                    watcher.DNSResolutionError("resolver partitioned"),
                ]
            ),
            docker,
            ServiceStatus("test_watcher", 60),
            wall_clock=lambda: 1_000,
            proof_reader=lambda: local_proof(config),
        )

        self.assertEqual(service.run_once().kind, watcher.RunKind.SUCCESS)
        self.assertEqual(service.run_once().kind, watcher.RunKind.DNS_FAILURE)
        self.assertEqual(docker.desired_states, [True, False])

    def test_expired_local_proof_fences_even_when_cached_dns_is_local(self):
        config = watcher.WatcherConfig.from_env(base_environment())
        docker = SequenceDocker(
            [watcher.ReconcileResult(True, watcher.ContainerState.STOPPED, changed=True)]
        )
        service = watcher.OtelWatcher(
            config,
            FakeResolver((config.my_ip,)),
            docker,
            ServiceStatus("test_watcher", 60),
            wall_clock=lambda: 1_000,
            proof_reader=lambda: local_proof(config, expires_at=1_005),
        )

        result = service.run_once()

        self.assertEqual(result.kind, watcher.RunKind.DNS_FAILURE)
        self.assertEqual(docker.desired_states, [False])
        self.assertIn("skew guard", result.message)

    def test_missing_local_proof_fences_even_when_dns_is_local(self):
        config = watcher.WatcherConfig.from_env(base_environment())
        docker = SequenceDocker(
            [watcher.ReconcileResult(True, watcher.ContainerState.STOPPED, changed=True)]
        )
        service = watcher.OtelWatcher(
            config,
            FakeResolver((config.my_ip,)),
            docker,
            ServiceStatus("test_watcher", 60),
            wall_clock=lambda: 1_000,
            proof_reader=lambda: (_ for _ in ()).throw(
                watcher.LeaseProofError("proof missing")
            ),
        )

        result = service.run_once()

        self.assertEqual(result.kind, watcher.RunKind.DNS_FAILURE)
        self.assertEqual(docker.desired_states, [False])
        self.assertIn("proof missing", result.message)

    def test_backoff_is_exponential_and_bounded(self):
        backoff = watcher.ExponentialBackoff(1, 4)
        self.assertEqual([backoff.next_delay() for _ in range(5)], [1, 2, 4, 4, 4])
        backoff.reset()
        self.assertEqual(backoff.next_delay(), 1)


if __name__ == "__main__":
    unittest.main()
