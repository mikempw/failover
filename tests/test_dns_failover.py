import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from common.health_server import ServiceStatus
from failover import dns_failover as dns


class StaticHealth:
    def __init__(self, healthy=True):
        self.healthy = healthy
        self.calls = 0

    def check(self):
        self.calls += 1
        return self.healthy


class FakeProvider:
    def __init__(self, records=None, error=None):
        self.records = records
        self.error = error
        self.writes = []

    def get_records(self):
        if self.error:
            raise self.error
        return dict(self.records or {})

    def set_records(self, ip, owner, expires_at):
        self.writes.append((ip, owner, expires_at))
        self.records = {'A': ip, 'TXT': f'owner={owner} exp={expires_at}'}


class FakeHTTPResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')


class FakeGCPChanges:
    def __init__(self, status='pending', reload_status=None):
        self.status = status
        self.reload_status = reload_status
        self.reload_calls = 0
        self.created = False

    def delete_record_set(self, _record_set):
        pass

    def add_record_set(self, _record_set):
        pass

    def create(self):
        self.created = True

    def reload(self):
        self.reload_calls += 1
        if self.reload_status is not None:
            self.status = self.reload_status


class FakeGCPZone:
    def __init__(self, changes):
        self._changes = changes

    def changes(self):
        return self._changes

    def list_resource_record_sets(self):
        return []

    def resource_record_set(self, *args):
        return args


def make_config(**overrides):
    values = dict(
        provider='dry-run',
        dns_server='127.0.0.1',
        dns_zone='example.test',
        dns_record='failover.example.test',
        dns_ttl=30,
        primary_ip='192.0.2.10',
        dr_ip='192.0.2.20',
        lease_ttl=60,
        update_interval=10,
        fail_threshold=3,
        health_host='192.0.2.10',
        health_port=8888,
        health_timeout=2,
        health_mode='tcp',
        health_url=None,
        health_metric='accepted_total',
        health_stale_count=3,
        role='primary',
        failback_threshold=3,
        max_clock_skew=5,
        retry_max=60,
        write_verify_attempts=1,
        write_verify_delay=0,
    )
    values.update(overrides)
    return dns.Config(**values)


def make_status():
    return ServiceStatus('dns_failover_test', readiness_max_age=120)


def make_f5_config(**overrides):
    values = dict(
        provider='f5-gtm',
        f5_host='f5.example.test',
        f5_username='api-user',
        f5_password='api-password',
        f5_partition='Common',
        f5_pool_name='failover_pool',
        f5_primary_member='/Common/primary-server:/Common/telemetry',
        f5_dr_member='/Common/dr-server:/Common/telemetry',
    )
    values.update(overrides)
    return make_config(**values)


def f5_members(active_role='primary'):
    pool_path = '/mgmt/tm/gtm/pool/a/~Common~failover_pool/members'
    return [
        {
            'name': '../../must-not-be-used',
            'fullPath': '/Common/primary-server:/Common/telemetry',
            'selfLink': (
                f'https://localhost{pool_path}/'
                '~Common~primary-server:~Common~telemetry?ver=17.1.0'
            ),
            'enabled' if active_role == 'primary' else 'disabled': True,
        },
        {
            'name': '../../must-not-be-used',
            'fullPath': '/Common/dr-server:/Common/telemetry',
            'selfLink': (
                f'https://localhost{pool_path}/'
                '~Common~dr-server:~Common~telemetry?ver=17.1.0'
            ),
            'enabled' if active_role == 'dr' else 'disabled': True,
        },
    ]


class ConfigAndLeaseTests(unittest.TestCase):
    def test_valid_config(self):
        make_config().validate()

    def test_invalid_ips_and_out_of_zone_record_are_rejected(self):
        cfg = make_config(primary_ip='not-an-ip', dns_record='other.invalid')
        with self.assertRaisesRegex(ValueError, 'Invalid PRIMARY_IP'):
            cfg.validate()
        with self.assertRaisesRegex(ValueError, 'not within DNS_ZONE'):
            cfg.validate()

    def test_health_urls_must_be_absolute_http_urls(self):
        cfg = make_config(health_mode='http', health_url='file:///etc/passwd')
        with self.assertRaisesRegex(ValueError, 'HEALTH_URL must be an absolute'):
            cfg.validate()
        cfg = make_config(local_health_url='victoriametrics:8428/health')
        with self.assertRaisesRegex(ValueError, 'LOCAL_HEALTH_URL must be an absolute'):
            cfg.validate()

    def test_dns_only_requires_external_site_specific_health_gates(self):
        valid = make_config(
            deployment_mode='dns-only',
            role='dr',
            health_mode='http',
            health_url='http://192.0.2.10:8080/health',
            local_health_url='http://192.0.2.20:8080/ready-for-traffic',
            failback_health_url='http://192.0.2.10:8080/recovery-ready',
        )
        valid.validate()

        with self.assertRaisesRegex(ValueError, 'LOCAL_HEALTH_URL is required'):
            make_config(
                deployment_mode='dns-only',
                role='dr',
                health_mode='http',
                health_url='http://192.0.2.10:8080/health',
                failback_health_url='http://192.0.2.10:8080/recovery-ready',
            ).validate()

        with self.assertRaisesRegex(ValueError, 'FAILBACK_HEALTH_URL is required'):
            make_config(
                deployment_mode='dns-only',
                health_mode='http',
                health_url='http://192.0.2.10:8080/health',
            ).validate()

        with self.assertRaisesRegex(ValueError, 'HEALTH_MODE must be http'):
            make_config(
                deployment_mode='dns-only',
                health_mode='tcp',
                failback_health_url='http://192.0.2.10:8080/recovery-ready',
            ).validate()

    def test_dns_only_rejects_moving_or_compose_local_health_targets(self):
        for setting, overrides in (
            ('HEALTH_URL', {'health_url': 'http://failover.example.test/health'}),
            ('LOCAL_HEALTH_URL', {'local_health_url': 'http://victoriametrics:8428/health'}),
            ('FAILBACK_HEALTH_URL', {'failback_health_url': 'http://localhost:8080/ready'}),
        ):
            values = dict(
                deployment_mode='dns-only',
                role='dr',
                health_mode='http',
                health_url='http://192.0.2.10:8080/health',
                local_health_url='http://192.0.2.20:8080/ready',
                failback_health_url='http://192.0.2.10:8080/recovery-ready',
            )
            values.update(overrides)
            with self.subTest(setting=setting), self.assertRaisesRegex(
                ValueError, f'{setting} must target an external'
            ):
                make_config(**values).validate()

    def test_invalid_deployment_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Invalid DEPLOYMENT_MODE'):
            make_config(deployment_mode='not-a-mode').validate()

    @mock.patch('failover.dns_failover.check_http', return_value=True)
    def test_dns_only_probe_checks_all_configured_external_gates(self, check_http):
        cfg = make_config(
            deployment_mode='dns-only',
            role='dr',
            health_mode='http',
            health_url='http://192.0.2.10:8080/health',
            local_health_url='http://192.0.2.20:8080/ready',
            failback_health_url='http://192.0.2.10:8080/recovery-ready',
        )
        dns.probe_dns_only_health(cfg)
        self.assertEqual(
            [call.args[0] for call in check_http.call_args_list],
            [cfg.health_url, cfg.local_health_url, cfg.failback_health_url],
        )

    @mock.patch('failover.dns_failover.check_http', side_effect=[True, False])
    def test_dns_only_probe_fails_when_an_external_gate_is_unready(self, _check_http):
        cfg = make_config(
            deployment_mode='dns-only',
            health_mode='http',
            health_url='http://192.0.2.10:8080/health',
            failback_health_url='http://192.0.2.10:8080/recovery-ready',
        )
        with self.assertRaisesRegex(RuntimeError, 'primary recovery'):
            dns.probe_dns_only_health(cfg)

    @mock.patch('failover.dns_failover.check_http', return_value=True)
    def test_dns_only_dr_outage_probe_checks_local_readiness_only(self, check_http):
        cfg = make_config(
            deployment_mode='dns-only',
            role='dr',
            health_mode='http',
            health_url='http://192.0.2.10:8080/health',
            local_health_url='http://192.0.2.20:8080/ready',
            failback_health_url='http://192.0.2.10:8080/recovery-ready',
        )
        dns.probe_dns_only_dr_readiness(cfg)
        check_http.assert_called_once_with(cfg.local_health_url, cfg.health_timeout)

    def test_incoherent_or_partial_lease_is_rejected(self):
        cfg = make_config()
        with self.assertRaises(dns.LeaseReadError):
            dns.parse_lease({'A': cfg.primary_ip, 'TXT': None}, cfg)
        with self.assertRaisesRegex(dns.LeaseReadError, 'Incoherent'):
            dns.parse_lease(
                {'A': cfg.dr_ip, 'TXT': 'owner=primary exp=9999999999'}, cfg
            )

    def test_lease_txt_rejects_duplicates_unknowns_and_extra_values(self):
        cfg = make_config()
        bad_values = (
            'owner=primary owner=dr exp=2000',
            'owner=primary exp=2000 extra=value',
            'owner=primary exp=2000\nowner=dr exp=3000',
        )
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(dns.LeaseReadError):
                    dns.parse_lease({'A': cfg.primary_ip, 'TXT': value}, cfg)

    def test_bind_and_ad_reject_multiple_dns_answers(self):
        cfg = make_config(tsig_keyfile='unused')
        for provider_type in (dns.BindTSIGProvider, dns.ADGSSProvider):
            with self.subTest(provider=provider_type.__name__), mock.patch(
                'failover.dns_failover.subprocess.run',
                side_effect=[
                    subprocess.CompletedProcess(
                        [], 0, f'{cfg.primary_ip}\n{cfg.dr_ip}\n', ''
                    ),
                    subprocess.CompletedProcess(
                        [], 0, '"owner=primary exp=2000"\n', ''
                    ),
                ],
            ):
                with self.assertRaises(dns.LeaseReadError):
                    dns.read_lease(provider_type(cfg), cfg)

    @mock.patch('failover.dns_failover.now_unix', return_value=1_000)
    def test_coherent_reads_atomically_publish_local_lease_proof(self, _now):
        with tempfile.TemporaryDirectory() as directory:
            proof_path = os.path.join(directory, 'observed-lease.json')
            cfg = make_config(lease_proof_file=proof_path)
            provider = FakeProvider(
                {'A': cfg.primary_ip, 'TXT': 'owner=primary exp=2_000'}
            )

            lease = dns.read_lease(provider, cfg)

            with open(proof_path, encoding='utf-8') as proof_file:
                proof = json.load(proof_file)
            self.assertEqual(lease.owner, 'primary')
            self.assertEqual(proof['record'], cfg.dns_record)
            self.assertEqual(proof['ip'], cfg.primary_ip)
            self.assertEqual(proof['expires_at'], 2_000)
            self.assertEqual(proof['observed_at'], 1_000)

    def test_failed_read_invalidates_existing_local_lease_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            proof_path = os.path.join(directory, 'observed-lease.json')
            with open(proof_path, 'w', encoding='utf-8') as proof_file:
                proof_file.write('{}')
            cfg = make_config(lease_proof_file=proof_path)

            with self.assertRaises(dns.LeaseReadError):
                dns.read_lease(FakeProvider(error=TimeoutError('offline')), cfg)

            self.assertFalse(os.path.exists(proof_path))

    def test_secret_file_and_value_are_mutually_exclusive(self):
        with tempfile.NamedTemporaryFile('w', delete=False) as handle:
            handle.write('from-file\n')
            path = handle.name
        try:
            with mock.patch.dict(os.environ, {'TOKEN_FILE': path}, clear=True):
                self.assertEqual(dns.get_config_value('TOKEN', {}), 'from-file')
            with mock.patch.dict(
                os.environ, {'TOKEN_FILE': path, 'TOKEN': 'from-env'}, clear=True
            ):
                with self.assertRaisesRegex(ValueError, 'only one'):
                    dns.get_config_value('TOKEN', {})
        finally:
            os.unlink(path)

    def test_invalid_boolean_is_rejected(self):
        with mock.patch.dict(os.environ, {'AUTO_FAILBACK': 'sometimes'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'AUTO_FAILBACK must be true or false'):
                    dns.Config.from_env()

    def test_role_and_deployment_mode_cannot_be_overridden_by_vault(self):
        with mock.patch.dict(
            os.environ,
            {
                'ROLE': 'dr',
                'DEPLOYMENT_MODE': 'dns-only',
                'HEALTH_MODE': 'http',
                'HEALTH_URL': 'http://192.0.2.10:8080/health',
                'LOCAL_HEALTH_URL': 'http://192.0.2.20:8080/ready',
                'FAILBACK_HEALTH_URL': 'http://192.0.2.10:8080/recovery-ready',
                'AUTO_FAILBACK': 'false',
            },
            clear=True,
        ), mock.patch.object(
            dns,
            'load_from_vault',
            return_value={
                'role': 'primary',
                'deployment_mode': 'full',
                'health_mode': 'tcp',
                'health_url': 'http://wrong.example.test/health',
                'local_health_url': 'http://wrong.example.test/local',
                'failback_health_url': 'http://wrong.example.test/recovery',
                'auto_failback': 'true',
            },
        ):
            cfg = dns.Config.from_env()
        self.assertEqual(cfg.role, 'dr')
        self.assertEqual(cfg.deployment_mode, 'dns-only')
        self.assertEqual(cfg.health_mode, 'http')
        self.assertEqual(cfg.health_url, 'http://192.0.2.10:8080/health')
        self.assertEqual(cfg.local_health_url, 'http://192.0.2.20:8080/ready')
        self.assertEqual(
            cfg.failback_health_url,
            'http://192.0.2.10:8080/recovery-ready',
        )
        self.assertFalse(cfg.auto_failback)

    def test_auto_failback_requires_recovery_url_at_either_role(self):
        with self.assertRaisesRegex(ValueError, 'FAILBACK_HEALTH_URL'):
            make_config(role='dr', auto_failback=True).validate()

    def test_cloudflare_uses_portable_ttl_bounds(self):
        cfg = make_config(
            provider='cloudflare',
            cloudflare_api_token='token',
            cloudflare_zone_id='zone-id',
            dns_ttl=30,
        )
        with self.assertRaisesRegex(ValueError, 'Cloudflare DNS_TTL'):
            cfg.validate()

    def test_route53_can_use_ambient_credentials(self):
        make_config(provider='route53', route53_zone_id='zone-id').validate()
        cfg = make_config(
            provider='route53',
            route53_zone_id='zone-id',
            aws_access_key_id='access-only',
        )
        with self.assertRaisesRegex(ValueError, 'standard AWS credential chain'):
            cfg.validate()

    def test_azure_apex_record_name_is_at_sign(self):
        provider = object.__new__(dns.AzureDNSProvider)
        provider.cfg = make_config(
            provider='azure-dns',
            dns_record='example.test',
        )
        self.assertEqual(provider._get_record_name(), '@')

    def test_f5_requires_pool_and_explicit_distinct_members(self):
        cfg = make_f5_config(
            f5_pool_name=None,
            f5_primary_member=None,
            f5_dr_member=None,
        )
        with self.assertRaisesRegex(ValueError, 'F5_POOL_NAME required'):
            cfg.validate()

        cfg = make_f5_config(
            f5_dr_member='/Common/primary-server:/Common/telemetry'
        )
        with self.assertRaisesRegex(ValueError, 'must be different'):
            cfg.validate()

    def test_gcp_change_timeout_must_be_bounded(self):
        cfg = make_config(
            provider='gcp-dns',
            gcp_project_id='project',
            gcp_managed_zone='zone',
            gcp_change_timeout=0,
        )
        with self.assertRaisesRegex(ValueError, 'GCP_CHANGE_TIMEOUT'):
            cfg.validate()


class ProviderSafetyTests(unittest.TestCase):
    @staticmethod
    def f5_provider(session):
        provider = dns.F5GTMProvider(make_f5_config())
        provider.session = session
        return provider

    def test_f5_uses_member_selflinks_and_propagates_each_patch_failure(self):
        session = mock.Mock()
        session.get.side_effect = [
            FakeHTTPResponse(),
            FakeHTTPResponse(payload={'items': f5_members('primary')}),
        ]
        session.patch.side_effect = [
            FakeHTTPResponse(),
            FakeHTTPResponse(500),
        ]
        provider = self.f5_provider(session)

        with self.assertRaisesRegex(RuntimeError, 'HTTP 500'):
            provider.set_records(provider.cfg.dr_ip, 'dr', 2_000)

        calls = session.patch.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn('~Common~primary-server:~Common~telemetry', calls[0].args[0])
        self.assertEqual(calls[0].kwargs['json'], {'disabled': True})
        self.assertIn('~Common~dr-server:~Common~telemetry', calls[1].args[0])
        self.assertEqual(calls[1].kwargs['json'], {'enabled': True})
        self.assertNotIn('../', calls[0].args[0])
        self.assertNotIn('../', calls[1].args[0])

    def test_f5_propagates_pool_get_and_lease_patch_failures(self):
        session = mock.Mock()
        session.get.side_effect = [FakeHTTPResponse(503)]
        provider = self.f5_provider(session)
        with self.assertRaisesRegex(RuntimeError, 'HTTP 503'):
            provider._site_members()

        session = mock.Mock()
        session.get.side_effect = [
            FakeHTTPResponse(),
            FakeHTTPResponse(payload={'items': f5_members('primary')}),
            FakeHTTPResponse(payload={'items': f5_members('dr')}),
        ]
        session.patch.side_effect = [
            FakeHTTPResponse(),
            FakeHTTPResponse(),
            FakeHTTPResponse(500),
        ]
        provider = self.f5_provider(session)
        with self.assertRaisesRegex(RuntimeError, 'HTTP 500'):
            provider.set_records(provider.cfg.dr_ip, 'dr', 2_000)
        self.assertEqual(session.patch.call_args_list[-1].args[0], provider.datagroup_url)

    def test_f5_get_records_requires_pool_and_lease_to_agree(self):
        lease_records = {
            'records': [
                {'name': 'owner', 'data': 'dr'},
                {'name': 'exp', 'data': '2000'},
                {'name': 'ip', 'data': '192.0.2.20'},
            ]
        }
        session = mock.Mock()
        session.get.side_effect = [
            FakeHTTPResponse(),
            FakeHTTPResponse(payload=lease_records),
            FakeHTTPResponse(payload={'items': f5_members('dr')}),
        ]
        provider = self.f5_provider(session)

        self.assertEqual(
            provider.get_records(),
            {'A': '192.0.2.20', 'TXT': 'owner=dr exp=2000'},
        )

        lease_records['records'][0]['data'] = 'primary'
        lease_records['records'][2]['data'] = '192.0.2.10'
        session = mock.Mock()
        session.get.side_effect = [
            FakeHTTPResponse(),
            FakeHTTPResponse(payload=lease_records),
            FakeHTTPResponse(payload={'items': f5_members('dr')}),
        ]
        provider = self.f5_provider(session)
        with self.assertRaisesRegex(RuntimeError, 'disagrees'):
            provider.get_records()

    def test_gcp_change_wait_times_out_and_rejects_error_status(self):
        cfg = make_config(
            provider='gcp-dns',
            gcp_project_id='project',
            gcp_managed_zone='zone',
            gcp_change_timeout=1,
        )
        changes = FakeGCPChanges()
        provider = object.__new__(dns.GCPDNSProvider)
        provider.cfg = cfg
        provider.zone = FakeGCPZone(changes)
        provider.record_name = 'failover.example.test.'

        with mock.patch(
            'failover.dns_failover.time.monotonic', side_effect=[0.0, 0.6, 1.1]
        ), mock.patch('failover.dns_failover.time.sleep'):
            with self.assertRaisesRegex(TimeoutError, 'did not complete'):
                provider.set_records(cfg.primary_ip, 'primary', 2_000)

        self.assertTrue(changes.created)
        self.assertEqual(changes.reload_calls, 1)

        changes = FakeGCPChanges(status='failed')
        provider.zone = FakeGCPZone(changes)
        with mock.patch('failover.dns_failover.time.monotonic', return_value=0.0):
            with self.assertRaisesRegex(RuntimeError, 'unexpected status'):
                provider.set_records(cfg.primary_ip, 'primary', 2_000)


class PrimaryIterationTests(unittest.TestCase):
    @mock.patch('failover.dns_failover.now_unix', return_value=1_000)
    def test_expired_dr_lease_is_reclaimed_after_health_stabilizes(self, _now):
        cfg = make_config()
        provider = FakeProvider(
            {'A': cfg.dr_ip, 'TXT': 'owner=dr exp=900'}
        )
        state = dns.PrimaryLoopState(healthy_streak=cfg.failback_threshold - 1)

        active_health = StaticHealth(False)
        recovery_health = StaticHealth()
        dns.run_primary_iteration(
            cfg, provider, active_health, state, make_status(), recovery_health
        )

        self.assertEqual(provider.writes[-1][0:2], (cfg.primary_ip, 'primary'))
        self.assertEqual(active_health.calls, 0)
        self.assertEqual(recovery_health.calls, 1)

    @mock.patch('failover.dns_failover.now_unix', return_value=1_000)
    def test_unexpired_dr_lease_is_not_overwritten(self, _now):
        cfg = make_config(auto_failback=False)
        provider = FakeProvider(
            {'A': cfg.dr_ip, 'TXT': 'owner=dr exp=2_000'}
        )
        state = dns.PrimaryLoopState(healthy_streak=cfg.failback_threshold)

        dns.run_primary_iteration(
            cfg, provider, StaticHealth(False), state, make_status(), StaticHealth()
        )

        self.assertEqual(provider.writes, [])

    @mock.patch('failover.dns_failover.now_unix', return_value=1_000)
    def test_primary_never_reclaims_an_unexpired_dr_lease(self, _now):
        cfg = make_config(auto_failback=True)
        provider = FakeProvider(
            {'A': cfg.dr_ip, 'TXT': 'owner=dr exp=2_000'}
        )
        state = dns.PrimaryLoopState(healthy_streak=cfg.failback_threshold)

        dns.run_primary_iteration(
            cfg, provider, StaticHealth(False), state, make_status(), StaticHealth()
        )

        self.assertEqual(provider.writes, [])

    @mock.patch('failover.dns_failover.now_unix', return_value=1_000)
    def test_missing_recovery_probe_blocks_expired_reclamation(self, _now):
        cfg = make_config()
        provider = FakeProvider({'A': cfg.dr_ip, 'TXT': 'owner=dr exp=900'})

        dns.run_primary_iteration(
            cfg, provider, StaticHealth(), dns.PrimaryLoopState(), make_status()
        )

        self.assertEqual(provider.writes, [])

    @mock.patch('failover.dns_failover.now_unix', return_value=1_000)
    def test_expired_self_owned_lease_is_not_renewed_into_a_dr_race(self, _now):
        cfg = make_config(failback_health_url='http://local/health')
        provider = FakeProvider(
            {'A': cfg.primary_ip, 'TXT': 'owner=primary exp=900'}
        )
        active_health = StaticHealth(True)
        recovery_health = StaticHealth(True)

        dns.run_primary_iteration(
            cfg,
            provider,
            active_health,
            dns.PrimaryLoopState(),
            make_status(),
            recovery_health,
        )

        self.assertEqual(provider.writes, [])
        self.assertEqual(active_health.calls, 0)
        self.assertEqual(recovery_health.calls, 0)

    @mock.patch('failover.dns_failover.now_unix', return_value=1_000)
    def test_near_expiry_self_lease_never_substitutes_recovery_for_active_health(self, _now):
        cfg = make_config(failback_health_url='http://local/health')
        provider = FakeProvider(
            {'A': cfg.primary_ip, 'TXT': 'owner=primary exp=1_004'}
        )
        active_health = StaticHealth(False)
        recovery_health = StaticHealth(True)

        dns.run_primary_iteration(
            cfg,
            provider,
            active_health,
            dns.PrimaryLoopState(),
            make_status(),
            recovery_health,
        )

        self.assertEqual(provider.writes, [])
        self.assertEqual(active_health.calls, 1)
        self.assertEqual(recovery_health.calls, 0)

    @mock.patch('failover.dns_failover.now_unix', return_value=1_000)
    def test_checker_phase_change_resets_failback_streak(self, _now):
        cfg = make_config()
        provider = FakeProvider({'A': cfg.dr_ip, 'TXT': 'owner=dr exp=900'})
        state = dns.PrimaryLoopState(
            healthy_streak=cfg.failback_threshold,
            observed_owner='primary',
            observed_health_phase='active',
        )

        dns.run_primary_iteration(
            cfg, provider, StaticHealth(), state, make_status(), StaticHealth()
        )

        self.assertEqual(provider.writes, [])
        self.assertEqual(state.healthy_streak, 1)

    def test_unknown_lease_fails_closed(self):
        cfg = make_config()
        provider = FakeProvider(error=TimeoutError('control plane unavailable'))
        state = dns.PrimaryLoopState(
            healthy_streak=cfg.failback_threshold,
            observed_owner='dr',
            observed_health_phase='recovery',
        )
        delay = dns.run_primary_iteration(
            cfg, provider, StaticHealth(), state, make_status()
        )
        self.assertEqual(provider.writes, [])
        self.assertGreaterEqual(delay, 1)
        self.assertEqual(state.healthy_streak, 0)
        self.assertIsNone(state.observed_owner)
        self.assertIsNone(state.observed_health_phase)

    def test_unhealthy_primary_does_not_touch_dns(self):
        cfg = make_config()
        provider = FakeProvider(
            {'A': cfg.primary_ip, 'TXT': 'owner=primary exp=2000'}
        )
        dns.run_primary_iteration(
            cfg, provider, StaticHealth(False), dns.PrimaryLoopState(), make_status()
        )
        self.assertEqual(provider.writes, [])


class DRIterationTests(unittest.TestCase):
    @mock.patch('failover.dns_failover.now_unix', return_value=1_000)
    def test_dr_promotes_only_after_failure_threshold_and_expiry(self, _now):
        cfg = make_config(role='dr')
        provider = FakeProvider(
            {'A': cfg.primary_ip, 'TXT': 'owner=primary exp=900'}
        )
        state = dns.DRLoopState(primary_failures=cfg.fail_threshold - 1)
        dns.run_dr_iteration(
            cfg, provider, StaticHealth(False), StaticHealth(True), state, make_status()
        )
        self.assertEqual(provider.writes[-1][0:2], (cfg.dr_ip, 'dr'))

    def test_dr_never_promotes_on_unknown_lease(self):
        cfg = make_config(role='dr')
        provider = FakeProvider(records={'A': None, 'TXT': None})
        state = dns.DRLoopState(primary_failures=cfg.fail_threshold)
        dns.run_dr_iteration(
            cfg, provider, StaticHealth(False), StaticHealth(True), state, make_status()
        )
        self.assertEqual(provider.writes, [])
        self.assertEqual(state.primary_failures, 0)

    @mock.patch('failover.dns_failover.now_unix', return_value=1_000)
    def test_dr_renews_its_lease_when_primary_recovers_without_auto_failback(self, _now):
        cfg = make_config(role='dr', auto_failback=False)
        provider = FakeProvider({'A': cfg.dr_ip, 'TXT': 'owner=dr exp=2_000'})

        dns.run_dr_iteration(
            cfg,
            provider,
            StaticHealth(True),
            StaticHealth(True),
            dns.DRLoopState(),
            make_status(),
        )

        self.assertEqual(provider.writes[-1][0:2], (cfg.dr_ip, 'dr'))

    @mock.patch('failover.dns_failover.now_unix', return_value=1_000)
    def test_expired_dr_self_owned_lease_is_not_renewed_into_primary_race(self, _now):
        cfg = make_config(role='dr', auto_failback=False)
        provider = FakeProvider({'A': cfg.dr_ip, 'TXT': 'owner=dr exp=900'})
        primary_health = StaticHealth(False)
        local_health = StaticHealth(True)

        dns.run_dr_iteration(
            cfg,
            provider,
            primary_health,
            local_health,
            dns.DRLoopState(),
            make_status(),
        )

        self.assertEqual(provider.writes, [])
        self.assertEqual(primary_health.calls, 0)
        self.assertEqual(local_health.calls, 0)

    @mock.patch('failover.dns_failover.now_unix', return_value=1_000)
    def test_auto_failback_makes_dr_yield_without_competing_write(self, _now):
        cfg = make_config(
            role='dr',
            auto_failback=True,
            failback_health_url='http://primary.example.test/health',
        )
        provider = FakeProvider({'A': cfg.dr_ip, 'TXT': 'owner=dr exp=2_000'})
        normal_health = StaticHealth(False)

        dns.run_dr_iteration(
            cfg,
            provider,
            normal_health,
            StaticHealth(True),
            dns.DRLoopState(),
            make_status(),
            StaticHealth(True),
        )

        self.assertEqual(provider.writes, [])
        self.assertEqual(normal_health.calls, 0)


class MetricsHealthTests(unittest.TestCase):
    def test_metric_name_match_is_exact(self):
        text = 'wanted_extra 99\nwanted{source="a"} 2\nwanted 3\n'
        self.assertEqual(dns.parse_metric_value(text, 'wanted'), 5)

    @mock.patch('failover.dns_failover.fetch_metrics')
    def test_counter_reset_is_healthy(self, fetch):
        fetch.side_effect = ['counter 10\n', 'counter 1\n']
        checker = dns.MetricsHealthChecker('http://example', 'counter', 2, 1)
        self.assertTrue(checker.check())
        self.assertTrue(checker.check())


if __name__ == '__main__':
    unittest.main()
