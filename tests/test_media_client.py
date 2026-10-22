import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('media_client', ROOT / 'clients/wecom_media.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def chunk(raw, start, count=4):
    part = raw[start:start + count]
    return {'ok': True, 'media_id': 'media', 'content_base64': base64.b64encode(part).decode(),
            'offset': start, 'next_offset': start + len(part), 'size': len(raw), 'eof': start + len(part) == len(raw),
            'chunk_sha256': hashlib.sha256(part).hexdigest(), 'file_sha256': hashlib.sha256(raw).hexdigest()}


class ClientTests(unittest.TestCase):
    def test_http_error_keeps_safe_upstream_diagnostics(self):
        client = module.Client()
        payload = {'ok': False, 'err': 'bot_media_http_error', 'details': {
            'upstream_status': 403, 'upstream_code': 'AccessDenied',
            'upstream_reason': 'url_or_token_expired', 'url': 'https://secret.example/signed?token=private',
            'message': 'private signature'}}
        error = module.urllib.error.HTTPError('https://service.invalid', 502, 'gateway', {}, io.BytesIO(json.dumps(payload).encode()))
        with patch.dict(os.environ, {'INBOX_SECRET': 'test-secret'}), patch.object(module.urllib.request, 'build_opener') as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(RuntimeError) as caught:
                client._request('https://service.invalid', '/bot/media', 'INBOX_SECRET', 'Authorization', {})
        text = str(caught.exception)
        self.assertIn('upstream_status=403', text)
        self.assertIn('upstream_reason=url_or_token_expired', text)
        self.assertNotIn('private', text)
        self.assertNotIn('secret.example', text)

    def test_inbox_uses_bearer_auth(self):
        client = module.Client()
        client.inbox = 'https://worker.example.invalid'
        with patch.dict(os.environ, {'INBOX_SECRET': 'local-secret'}), patch.object(module.urllib.request, 'build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(b'{"ok":true,"messages":[]}')
            client.inbox_messages()
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header('Authorization'), 'Bearer local-secret')
            self.assertIn('include_media=1', request.full_url)

    def test_interrupted_download_resumes_without_duplicate_bytes(self):
        client = module.Client()
        raw = b'abcdefghijk'
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / 'message.pdf'
            with patch.object(client, '_scf', side_effect=[chunk(raw, 0), TimeoutError()]):
                with self.assertRaises(TimeoutError):
                    client.download_media('media', dest)
            self.assertFalse(dest.exists())
            self.assertEqual(Path(str(dest) + '.part').read_bytes(), raw[:4])
            # Simulate a crash after file write but before checkpoint publication.
            with Path(str(dest) + '.part').open('ab') as f:
                f.write(b'uncommitted-tail')
            with patch.object(client, '_scf', side_effect=lambda path, data: chunk(raw, data['offset'])):
                result = client.download_media('media', dest)
            self.assertEqual(dest.read_bytes(), raw)
            self.assertEqual(result['sha256'], hashlib.sha256(raw).hexdigest())
            with patch.object(client, '_scf', side_effect=AssertionError('Must reuse complete file')):
                self.assertEqual(client.download_media('media', dest)['sha256'], result['sha256'])

    def test_bad_chunk_never_publishes_a_complete_file(self):
        client = module.Client()
        result = chunk(b'abcdefgh', 0)
        result['chunk_sha256'] = 'wrong'
        with tempfile.TemporaryDirectory() as folder, patch.object(client, '_scf', return_value=result):
            dest = Path(folder) / 'message.bin'
            with self.assertRaises(ValueError):
                client.download_media('media', dest)
            self.assertFalse(dest.exists())

    def test_complete_checkpoint_before_rename_recovers_without_network(self):
        client = module.Client()
        raw = b'complete-file'
        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / 'message.bin'
            Path(str(dest) + '.part').write_bytes(raw)
            Path(str(dest) + '.wecom.json').write_text(json.dumps({'media_id': 'media', 'offset': len(raw), 'size': len(raw),
                                                               'partial_sha256': digest, 'file_sha256': digest}))
            with patch.object(client, '_scf', side_effect=AssertionError('No extra EOF request')):
                client.download_media('media', dest)
            self.assertEqual(dest.read_bytes(), raw)

    def test_existing_file_and_mismatched_receipt_are_protected(self):
        client = module.Client()
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / 'existing.txt'
            dest.write_text('existing')
            with self.assertRaises(FileExistsError):
                client.download_media('media', dest)
            self.assertEqual(dest.read_text(), 'existing')
            Path(str(dest) + '.wecom.json').write_text('{"media_id":"someone-else"}')
            with self.assertRaises(ValueError):
                client.download_media('media', dest)


if __name__ == '__main__':
    unittest.main()
