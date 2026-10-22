import base64
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from clients.wecom_bot import BotClient, AttachmentExpired, DownloadInProgress, _attachment_lock
from clients.wecom_media import Client, ServiceError, TransportError


IDENTIFIER = 'a' * 64


def chunk(raw, offset=0, count=512 * 1024):
    part = raw[offset:offset + count]
    return {'ok': True, 'media_id': IDENTIFIER + ':0', 'offset': offset, 'next_offset': offset + len(part),
            'size': len(raw), 'eof': offset + len(part) == len(raw), 'content_base64': base64.b64encode(part).decode(),
            'chunk_sha256': hashlib.sha256(part).hexdigest(), 'file_sha256': hashlib.sha256(raw).hexdigest()}


class DownloadRetryTests(unittest.TestCase):
    def test_overlapping_cron_skips_active_writer_and_can_resume_after_unlock(self):
        client = BotClient()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'file'
            with patch.object(client, '_request', return_value=chunk(b'data')) as request:
                with _attachment_lock(destination):
                    with self.assertRaises(DownloadInProgress):
                        client.download_attachment(IDENTIFIER, 0, destination)
                    self.assertEqual(request.call_count, 0)
                client.download_attachment(IDENTIFIER, 0, destination)
                self.assertEqual(destination.read_bytes(), b'data')

    def test_abrupt_process_exit_releases_attachment_lock(self):
        import sys
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'file'
            code = ('from pathlib import Path; import os; '
                    'from clients.wecom_bot import _attachment_lock; '
                    'lock=_attachment_lock(Path(' + repr(str(destination)) + ')); '
                    'lock.__enter__(); os._exit(0)')
            subprocess.run([sys.executable, '-c', code], check=True, timeout=5)
            with _attachment_lock(destination):
                self.assertTrue(Path(str(destination) + '.download.lock').exists())

    def test_small_file_recovers_via_curl_and_keeps_sha256(self):
        raw = b'fixture-' * 1449 + b'abcdef'  # 11,598 bytes, same size as reported XLSX; not the actual user file.
        self.assertEqual(len(raw), 11598)
        client = BotClient()
        with tempfile.TemporaryDirectory() as directory, patch('clients.wecom_bot.time.sleep') as sleep, \
                patch('clients.wecom_bot.shutil.which', return_value='/usr/bin/curl'), \
                patch.object(client, '_request', side_effect=TransportError('transient')) as primary, \
                patch.object(client, '_request_curl', return_value=chunk(raw)) as fallback:
            path = Path(directory) / 'report.xlsx'
            result = client.download_attachment(IDENTIFIER, 0, path)
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(result['sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(primary.call_count, 1)
            self.assertEqual(fallback.call_count, 1)
            self.assertLessEqual(primary.call_args.kwargs['timeout'], 12)
            self.assertEqual(fallback.call_args.args[4]['offset'], 0)
            self.assertEqual(sleep.call_args.args, (0.25,))
            client.download_attachment(IDENTIFIER, 0, path)
            self.assertEqual(fallback.call_count, 1)

    def test_later_chunk_retry_does_not_duplicate_saved_bytes(self):
        raw = b'abcdefghijkl'
        offsets = []
        client = BotClient()
        def primary(*args, **kwargs):
            offset = args[4]['offset']; offsets.append(offset)
            if offset == 4:
                raise TransportError('connection reset')
            return chunk(raw, offset, 4)
        def fallback(*args, **kwargs):
            offset = args[4]['offset']; offsets.append(offset)
            return chunk(raw, offset, 4)
        client._request, client._request_curl = primary, fallback
        with tempfile.TemporaryDirectory() as directory, patch('clients.wecom_bot.time.sleep'), \
                patch('clients.wecom_bot.shutil.which', return_value='curl'):
            path = Path(directory) / 'file.bin'
            client.download_attachment(IDENTIFIER, 0, path)
            self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(offsets, [0, 4, 4, 8])

    def test_curl_absent_retries_standard_transport(self):
        client = BotClient()
        with tempfile.TemporaryDirectory() as directory, patch('clients.wecom_bot.time.sleep'), \
                patch('clients.wecom_bot.shutil.which', return_value=None), \
                patch.object(client, '_request', side_effect=[TransportError('reset'), chunk(b'data')]) as primary:
            client.download_attachment(IDENTIFIER, 0, Path(directory) / 'file')
            self.assertEqual(primary.call_count, 2)

    def test_expiry_after_transient_failure_stops_and_survives_restart(self):
        client = BotClient()
        with tempfile.TemporaryDirectory() as directory, patch('clients.wecom_bot.time.sleep'), \
                patch('clients.wecom_bot.shutil.which', return_value='curl'), \
                patch.object(client, '_request', side_effect=TransportError('reset')) as primary, \
                patch.object(client, '_request_curl', side_effect=ServiceError(410, 'bot_attachment_expired', 'expired')) as fallback:
            path = Path(directory) / 'file'
            with self.assertRaises(AttachmentExpired) as caught:
                client.download_attachment(IDENTIFIER, 0, path)
            self.assertFalse(caught.exception.retryable)
            self.assertTrue(Path(str(path) + '.expired.json').exists())
            self.assertEqual((primary.call_count, fallback.call_count), (1, 1))
            fresh = BotClient()
            with patch.object(fresh, '_request', side_effect=AssertionError('must not request again')):
                with self.assertRaises(AttachmentExpired):
                    fresh.download_attachment(IDENTIFIER, 0, path)

    def test_non_transient_failures_are_not_retried(self):
        errors = [ServiceError(401, 'unauthorized', 'unauthorized'),
                  ServiceError(403, 'bot_media_host_not_allowed', 'host'),
                  ServiceError(502, 'invalid_media_padding', 'padding'),
                  ServiceError(502, 'bot_media_http_error', 'signature', 'signature_mismatch')]
        for error in errors:
            with self.subTest(error=error.code), tempfile.TemporaryDirectory() as directory:
                client = BotClient()
                with patch.object(client, '_request', side_effect=error) as primary:
                    with self.assertRaises(ServiceError):
                        client.download_attachment(IDENTIFIER, 0, Path(directory) / 'file')
                self.assertEqual(primary.call_count, 1)

    def test_retry_cap_keeps_checkpoint_without_false_expiry(self):
        client = BotClient()
        with tempfile.TemporaryDirectory() as directory, patch('clients.wecom_bot.time.sleep') as sleep, \
                patch('clients.wecom_bot.shutil.which', return_value=None), \
                patch.object(client, '_request', side_effect=TransportError('reset')) as primary:
            path = Path(directory) / 'file'
            with self.assertRaises(TransportError):
                client.download_attachment(IDENTIFIER, 0, path)
            self.assertEqual(primary.call_count, 4)
            self.assertEqual(sleep.call_count, 3)
            self.assertFalse(path.exists())
            self.assertFalse(Path(str(path) + '.expired.json').exists())

    def test_total_budget_shared_between_chunks(self):
        client = BotClient()
        clock = [0]
        def primary(*args, **kwargs):
            clock[0] += 121
            return chunk(b'abcdefgh', 0, 4)
        with tempfile.TemporaryDirectory() as directory, patch('clients.wecom_bot.time.monotonic', side_effect=lambda: clock[0]), \
                patch.object(client, '_request', side_effect=primary) as request:
            path = Path(directory) / 'file'
            with self.assertRaises(TransportError):
                client.download_attachment(IDENTIFIER, 0, path)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(Path(str(path) + '.part').read_bytes(), b'abcd')
            self.assertFalse(Path(str(path) + '.expired.json').exists())

    def test_send_has_no_automatic_retry(self):
        client = Client()
        client.scf = 'https://scf.invalid'
        with patch.dict(os.environ, {'PUSH_SECRET': 'test-secret'}), \
                patch('clients.wecom_media._build_opener') as opener:
            opener.return_value.open.side_effect = ConnectionResetError('reset')
            with self.assertRaises(TransportError):
                client._scf('/send', {'text': 'only once'})
            self.assertEqual(opener.return_value.open.call_count, 1)

    def test_incomplete_http_body_is_a_transport_failure(self):
        client = Client()
        with patch.dict(os.environ, {'INBOX_SECRET': 'test-secret'}), \
                patch('clients.wecom_media._build_opener') as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.read.side_effect = http.client.IncompleteRead(b'partial', 99)
            with self.assertRaises(TransportError):
                client._request('https://example.invalid', '/bot/media', 'INBOX_SECRET', 'Authorization', {})


class CurlTransportTests(unittest.TestCase):
    def test_credentials_use_stdin_and_safe_response_decoding(self):
        client = Client()
        response = subprocess.CompletedProcess([], 0, b'{"ok":true}\n200', b'')
        with patch.dict(os.environ, {'INBOX_SECRET': 'origin-secret', 'HTTPS_PROXY': 'http://user:proxy-secret@proxy.invalid:3128'}), \
                patch('clients.wecom_media.subprocess.run', return_value=response) as run:
            result = client._request_curl('https://worker.invalid', '/bot/media', 'INBOX_SECRET', 'Authorization', {}, timeout=12)
        self.assertTrue(result['ok'])
        argv = run.call_args.args[0]
        self.assertEqual(argv, ['curl', '--disable', '--config', '-'])
        config = run.call_args.kwargs['input'].decode()
        self.assertIn('proxy-secret', config)
        self.assertIn('origin-secret', config)
        self.assertNotIn('location', config)
        self.assertNotIn('insecure', config)
        self.assertIn('max-time = 12', config)

    def test_curl_does_not_hide_expired_service_response(self):
        response = subprocess.CompletedProcess([], 0, b'{"ok":false,"err":"bot_attachment_expired"}\n410', b'')
        with patch.dict(os.environ, {'INBOX_SECRET': 'test-secret'}), \
                patch('clients.wecom_media.subprocess.run', return_value=response):
            with self.assertRaises(ServiceError) as caught:
                Client()._request_curl('https://worker.invalid', '/bot/media', 'INBOX_SECRET', 'Authorization', {}, timeout=12)
        self.assertEqual((caught.exception.status, caught.exception.code), (410, 'bot_attachment_expired'))

    def test_curl_stderr_never_leaks_proxy_or_origin_secrets(self):
        response = subprocess.CompletedProcess([], 7, b'', b'http://user:proxy-secret@host origin-secret')
        with patch.dict(os.environ, {'INBOX_SECRET': 'origin-secret'}), \
                patch('clients.wecom_media.subprocess.run', return_value=response):
            with self.assertRaises(TransportError) as caught:
                Client()._request_curl('https://worker.invalid', '/bot/media', 'INBOX_SECRET', 'Authorization', {}, timeout=12)
        self.assertNotIn('proxy-secret', str(caught.exception))
        self.assertNotIn('origin-secret', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
