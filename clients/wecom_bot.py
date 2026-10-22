"""Personal WeCom URL-callback bot client. Existing Client download checkpoints apply.

All bot calls use INBOX_BASE_URL + INBOX_SECRET. No SCF or bot AES secrets in Muse.
Save messages/files/tasks durably before acknowledge(). Poll every 10–30 seconds.
"""
import argparse
from contextlib import contextmanager
import json
import os
import re
import shutil
import time
from pathlib import Path
try:
    from .wecom_media import Client, ServiceError, TransportError, AttachmentExpired, _write_json_atomic
except ImportError:
    from wecom_media import Client, ServiceError, TransportError, AttachmentExpired, _write_json_atomic

MEDIA_ATTEMPTS = 4
MEDIA_REQUEST_TIMEOUT = 12
MEDIA_DOWNLOAD_BUDGET = 120
MEDIA_BACKOFF = (0.25, 0.75, 1.5)


class DownloadInProgress(RuntimeError):
    """Another cron process owns this destination; try a later poll, not concurrently."""
    retryable = True


@contextmanager
def _attachment_lock(destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Keep the lock inode: unlinking after unlock races another process acquiring it.
    with Path(str(destination) + '.download.lock').open('a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            if not os.fstat(lock.fileno()).st_size:
                lock.write(b'0'); lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise DownloadInProgress('Attachment download already running; wait for next poll.') from None
            unlock = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DownloadInProgress('Attachment download already running; wait for next poll.') from None
            unlock = lambda: fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        try:
            yield
        finally:
            lock.seek(0)
            unlock()


def _retryable_download(error):
    if isinstance(error, TransportError):
        return True
    if not isinstance(error, ServiceError) or error.status not in (408, 429, 500, 502, 503, 504):
        return False
    return (error.code in ('http_error', 'service_unavailable', 'bot_media_http_error')
            and error.upstream_reason in (None, 'http_error', 'upstream_throttled_or_unavailable'))


class BotClient(Client):
    def _bot(self, path, data=None):
        return self._request(self.inbox, path, 'INBOX_SECRET', 'Authorization', data)

    def identities(self):
        return self._bot('/bot/identities')

    def failures(self):
        return self._bot('/bot/failures')

    def inbox_messages(self, limit=50):
        return self._bot('/bot/inbox')

    def acknowledge(self, ids):
        return self._bot('/bot/ack', {'ids': ids})

    def reply_text(self, identifier, text):
        """Save final stream text. stored=True does not prove WeCom displayed it."""
        return self._bot('/bot/reply', {'id': identifier, 'text': text})

    def _scf(self, path, data):
        # Reuse media downloader's verification + durable .part checkpoints while
        # routing bot media exclusively through the Worker, never the SCF media_id API.
        if path != '/media/download' or not re.fullmatch(r'[0-9a-f]{64}:[0-9]+', data.get('media_id', '')):
            raise ValueError('Use the existing media Client for application sends/uploads')
        identifier, index = data['media_id'].split(':')
        payload = {'id': identifier, 'index': int(index), 'offset': data['offset'],
                   'chunk_size': min(data['chunk_size'], 512 * 1024)}
        deadline = getattr(self, '_media_deadline', None)
        if deadline is None:
            deadline = time.monotonic() + MEDIA_DOWNLOAD_BUDGET
        for attempt in range(MEDIA_ATTEMPTS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportError('Media download time budget exhausted; checkpoint retained for next poll.')
            timeout = min(MEDIA_REQUEST_TIMEOUT, remaining)
            try:
                # Retry only this read operation, never sends/uploads/replies.
                transport = self._request_curl if attempt % 2 and shutil.which('curl') else self._request
                return transport(self.inbox, '/bot/media', 'INBOX_SECRET', 'Authorization', payload, timeout=timeout)
            except (TransportError, ServiceError) as error:
                if not _retryable_download(error) or attempt + 1 == MEDIA_ATTEMPTS:
                    raise
                delay = MEDIA_BACKOFF[attempt]
                if deadline - time.monotonic() <= delay:
                    raise TransportError('Media download time budget exhausted; checkpoint retained for next poll.') from None
                time.sleep(delay)

    def download_attachment(self, identifier, index, destination):
        if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{64}', identifier) or type(index) is not int or index < 0:
            raise ValueError('Invalid attachment identifier')
        destination = Path(destination)
        with _attachment_lock(destination):
            return self._download_attachment_locked(identifier, index, destination)

    def _download_attachment_locked(self, identifier, index, destination):
        marker = Path(str(destination) + '.expired.json')
        media_id = identifier + ':' + str(index)
        if marker.exists():
            state = json.loads(marker.read_text(encoding='utf-8'))
            if state.get('media_id') != media_id:
                raise ValueError('Expired receipt belongs to another attachment')
            if not destination.exists():
                raise AttachmentExpired(identifier, index)
        previous_deadline = getattr(self, '_media_deadline', None)
        self._media_deadline = time.monotonic() + MEDIA_DOWNLOAD_BUDGET
        try:
            try:
                return self.download_media(media_id, destination)
            except ServiceError as error:
                if error.status != 410 or error.code != 'bot_attachment_expired':
                    raise
                destination.parent.mkdir(parents=True, exist_ok=True)
                _write_json_atomic(marker, {'media_id': media_id, 'status': 'needs_resend',
                                           'reason': 'url_or_token_expired', 'retryable': False})
                raise AttachmentExpired(identifier, index) from None
        finally:
            self._media_deadline = previous_deadline


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['identities', 'inbox', 'failures'])
    args = parser.parse_args()
    client = BotClient()
    commands = {'identities': client.identities, 'inbox': client.inbox_messages, 'failures': client.failures}
    print(json.dumps(commands[args.command](), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
