import os
import stat
import tempfile
import unittest
from pathlib import Path

from failover import setup


class SetupSecurityTests(unittest.TestCase):
    def test_credentials_are_externalized_and_files_are_private(self):
        with tempfile.TemporaryDirectory() as directory:
            result = setup.externalize_secrets(
                {'DNS_PROVIDER': 'cloudflare', 'CLOUDFLARE_API_TOKEN': 'super-secret'},
                directory,
            )
            secret_path = os.path.join(
                directory, 'secrets', 'failover', 'cloudflare_api_token'
            )
            self.assertNotIn('CLOUDFLARE_API_TOKEN', result)
            self.assertEqual(
                result['CLOUDFLARE_API_TOKEN_FILE'],
                '/secrets/cloudflare_api_token',
            )
            with open(secret_path, encoding='utf-8') as handle:
                self.assertEqual(handle.read(), 'super-secret')
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(os.stat(secret_path).st_mode), 0o600)

    def test_generated_env_has_no_plaintext_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            config = setup.externalize_secrets(
                {
                    'ROLE': 'primary',
                    'DNS_PROVIDER': 'cloudflare',
                    'CLOUDFLARE_API_TOKEN': 'do-not-write-me',
                },
                directory,
            )
            env_path = os.path.join(directory, '.env.primary')
            setup.write_env_file(config, env_path)
            with open(env_path, encoding='utf-8') as handle:
                env_text = handle.read()
            self.assertNotIn('do-not-write-me', env_text)
            self.assertIn('CLOUDFLARE_API_TOKEN_FILE=/secrets/cloudflare_api_token', env_text)

    def test_gcp_credentials_are_copied_into_mounted_secrets_directory(self):
        with tempfile.TemporaryDirectory() as source_directory, tempfile.TemporaryDirectory() as output_directory:
            source_path = Path(source_directory, 'arbitrary-name.json')
            source_contents = '{"type":"service_account","project_id":"example"}\n'
            source_path.write_text(source_contents, encoding='utf-8')

            config = setup.externalize_secrets(
                {
                    'DNS_PROVIDER': 'gcp-dns',
                    'GCP_CREDENTIALS_FILE': str(source_path),
                },
                output_directory,
            )

            copied_path = Path(
                output_directory, 'secrets', 'failover', 'gcp_credentials.json'
            )
            self.assertEqual(
                config['GCP_CREDENTIALS_FILE'],
                '/secrets/gcp_credentials.json',
            )
            self.assertEqual(copied_path.read_text(encoding='utf-8'), source_contents)
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(copied_path.stat().st_mode), 0o600)

            env_path = Path(output_directory, '.env.primary')
            setup.write_env_file(config, env_path)
            env_text = env_path.read_text(encoding='utf-8')
            self.assertIn(
                'GCP_CREDENTIALS_FILE=/secrets/gcp_credentials.json', env_text
            )
            self.assertNotIn(str(source_path), env_text)

    def test_gcp_credentials_must_be_a_readable_json_object(self):
        with tempfile.TemporaryDirectory() as directory:
            invalid_path = Path(directory, 'credentials.json')
            invalid_path.write_text('[]', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'must contain an object'):
                setup.externalize_secrets(
                    {'GCP_CREDENTIALS_FILE': str(invalid_path)}, directory
                )

            with self.assertRaisesRegex(ValueError, 'Cannot read GCP credentials'):
                setup.externalize_secrets(
                    {'GCP_CREDENTIALS_FILE': str(Path(directory, 'missing.json'))},
                    directory,
                )


if __name__ == '__main__':
    unittest.main()
