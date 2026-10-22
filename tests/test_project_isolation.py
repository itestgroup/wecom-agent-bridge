"""Check the open-source defaults without contacting any cloud service."""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from clients.wecom_media import Client, _proxy_url

ROOT = Path(__file__).resolve().parents[1]


class IsolationTests(unittest.TestCase):
    def test_client_has_no_default_service_or_private_proxy(self):
        with patch.dict(os.environ, {}, clear=True):
            client = Client()
            self.assertEqual((client.inbox, client.scf, _proxy_url()), ('', '', ''))
            with self.assertRaisesRegex(ValueError, 'HTTPS'):
                client.inbox_messages()

    def test_scf_deployment_requires_explicit_target_before_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            spec = importlib.util.spec_from_file_location('isolated_deployer', ROOT / 'scripts/deploy_scf.py')
            deployer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(deployer)
            with patch.object(deployer, 'create_client') as create:
                with self.assertRaisesRegex(ValueError, 'SCF_REGION'):
                    deployer.main()
                create.assert_not_called()

    def test_ci_contains_no_deployment_workflow(self):
        workflows = list((ROOT / '.github/workflows').glob('*.yml'))
        self.assertEqual([p.name for p in workflows], ['ci.yml'])
        text = workflows[0].read_text()
        self.assertNotIn('secrets.', text)
        self.assertNotIn('npm run deploy', text)
