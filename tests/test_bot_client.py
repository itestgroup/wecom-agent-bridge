import base64
import hashlib
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from clients.wecom_bot import BotClient, AttachmentExpired
from clients.wecom_media import ServiceError
from clients.wecom_media import AttachmentExpired as SharedAttachmentExpired


class BotClientTests(unittest.TestCase):
    def test_expiry_exception_is_shared_and_old_import_still_catches(self):
        self.assertIs(AttachmentExpired, SharedAttachmentExpired)
        identifier = 'a' * 64
        with self.assertRaises(SharedAttachmentExpired) as caught:
            raise AttachmentExpired(identifier, 2)
        self.assertEqual(caught.exception.identifier, identifier)
        self.assertEqual(caught.exception.index, 2)
        self.assertEqual(caught.exception.status, 'needs_resend')
        self.assertFalse(caught.exception.retryable)

    def test_shared_base_accepts_local_message_only_subclasses(self):
        class LocalAttachmentExpired(SharedAttachmentExpired):
            pass
        with self.assertRaises(SharedAttachmentExpired) as caught:
            raise LocalAttachmentExpired('local expiry message')
        self.assertEqual(str(caught.exception), 'local expiry message')
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(str(SharedAttachmentExpired(message='custom message')), 'custom message')

    def test_attachment_uses_worker_and_validates_file(self):
        client = BotClient()
        identifier = 'a' * 64
        raw = b'%PDF-test bot file'
        calls = []
        def request(base, path, secret, header, data=None, **kwargs):
            calls.append((base, path, secret, header, data))
            return {'ok': True, 'media_id': identifier + ':0', 'offset': 0, 'next_offset': len(raw),
                    'size': len(raw), 'eof': True, 'content_base64': base64.b64encode(raw).decode(),
                    'chunk_sha256': hashlib.sha256(raw).hexdigest(), 'file_sha256': hashlib.sha256(raw).hexdigest()}
        client._request = request
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'test.pdf'
            client.download_attachment(identifier, 0, destination)
            self.assertEqual(destination.read_bytes(), raw)
            client.download_attachment(identifier, 0, destination)
            self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:4], ('/bot/media', 'INBOX_SECRET', 'Authorization'))
        self.assertEqual(calls[0][4]['chunk_size'], 512 * 1024)

    def test_explicit_reply_and_ack_routes(self):
        client = BotClient()
        calls = []
        client._bot = lambda *args: calls.append(args)
        client.reply_text('a' * 64, 'answer')
        client.acknowledge(['a' * 64])
        self.assertEqual(calls[0][0], '/bot/reply')
        self.assertEqual(calls[1][0], '/bot/ack')

    def test_invalid_attachment_is_not_requested(self):
        client = BotClient()
        for identifier, index in [('wrong', 0), ('a' * 64, -1), ('a' * 64, True)]:
            with self.assertRaises(ValueError):
                client.download_attachment(identifier, index, 'unused')

    def test_does_not_reuse_scf_upload_or_send(self):
        client = BotClient()
        with self.assertRaises(ValueError):
            client.send_media('media-id')

    def test_expiry_is_persisted_and_restart_does_not_request_again(self):
        identifier = 'a' * 64
        client = BotClient()
        requests = []
        def failed(*args, **kwargs):
            requests.append(args)
            raise ServiceError(410, 'bot_attachment_expired', 'expired')
        client._request = failed
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'expired.pdf'
            with self.assertRaises(AttachmentExpired) as caught:
                client.download_attachment(identifier, 0, destination)
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(caught.exception.status, 'needs_resend')
            self.assertTrue(Path(str(destination) + '.expired.json').exists())
            self.assertFalse(destination.exists())
            restarted = BotClient()
            restarted._request = failed
            with self.assertRaises(AttachmentExpired):
                restarted.download_attachment(identifier, 0, destination)
            self.assertEqual(len(requests), 1)

    def test_temporary_error_does_not_create_terminal_marker(self):
        client = BotClient()
        def unavailable(*args, **kwargs):
            raise ServiceError(502, 'bot_media_http_error', 'upstream unavailable')
        client._request = unavailable
        client._request_curl = unavailable
        with tempfile.TemporaryDirectory() as directory, patch('clients.wecom_bot.time.sleep'):
            destination = Path(directory) / 'retry.pdf'
            with self.assertRaises(ServiceError):
                client.download_attachment('b' * 64, 0, destination)
            self.assertFalse(Path(str(destination) + '.expired.json').exists())

    def test_failures_have_a_separate_operator_route(self):
        client = BotClient()
        calls = []
        client._bot = lambda *args: calls.append(args)
        client.failures()
        self.assertEqual(calls, [('/bot/failures',)])


if __name__ == '__main__':
    unittest.main()
