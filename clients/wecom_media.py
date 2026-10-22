"""Standard-library reference client for Muse. Never ACK an unfinished download.

Environment: SCF_BASE_URL, PUSH_SECRET; INBOX_BASE_URL, INBOX_SECRET.
HTTPS_PROXY / https_proxy selects the egress proxy (credentials may be in the URL).
Without HTTPS_PROXY / https_proxy, requests connect directly.
Import Client in the existing poller, or run this file with --help.
Send operations are never automatically retried.
"""
import argparse
import base64
import hashlib
import http.client
import json
import os
import subprocess
from pathlib import Path
import urllib.error
import urllib.request

INLINE_LIMIT = 2 * 1024 * 1024


class TransportError(RuntimeError):
    """Transient transfer/protocol failure; callers decide whether replay is safe."""


class ServiceError(RuntimeError):
    """Structured service failure, compatible with existing RuntimeError handlers."""
    def __init__(self, status, code, message, upstream_reason=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.upstream_reason = upstream_reason


class AttachmentExpired(RuntimeError):
    """Shared terminal attachment failure; usable as a base class by local pollers.

    Accept a normal exception message, or the existing (identifier, index) form.
    Both media and bot imports refer to this same class.
    """
    status = 'needs_resend'
    retryable = False

    def __init__(self, identifier=None, index=None, *, message=None):
        self.identifier = identifier if index is not None else None
        self.index = index
        if message is None:
            message = (str(identifier) if identifier is not None and index is None
                       else '附件已过期，需要重新发送；原消息和下载记录已保留')
        super().__init__(message)


def _write_json_atomic(path, value):
    temp = Path(str(path) + '.tmp')
    with temp.open('w', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, path)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _proxy_url():
    return (os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
            or '')


def _build_opener():
    # Configure both schemes explicitly instead of relying on auto-discovery.
    # urllib handles URL-encoded proxy credentials; never log the proxy URL.
    proxy = _proxy_url()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({'http': proxy, 'https': proxy} if proxy else {}), NoRedirect())


def _service_error(status, raw):
    code, reason, fields = 'http_error', None, []
    try:
        payload = json.loads(raw.decode('utf-8'))
        value = payload.get('err')
        if isinstance(value, str):
            code = value
        details = payload.get('details')
        if isinstance(details, dict):
            upstream = details.get('upstream_status')
            if type(upstream) is int and 100 <= upstream <= 599:
                fields.append('upstream_status=' + str(upstream))
            for name in ('upstream_code', 'upstream_reason'):
                value = details.get(name)
                if isinstance(value, str) and value and len(value) <= 64 and all(c.isascii() and (c.isalnum() or c in '_.-') for c in value):
                    fields.append(name + '=' + value)
                    if name == 'upstream_reason':
                        reason = value
    except (ValueError, UnicodeError, AttributeError):
        pass
    display = code + ('; ' + ', '.join(fields) if fields else '')
    return ServiceError(status, code, 'HTTP %s: %s; do not blindly retry sends' % (status, display), reason)


def _decode_payload(raw):
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        raise TransportError('Invalid or interrupted service response. Sending outcome may be unknown.') from None
    if not isinstance(payload, dict) or not payload.get('ok'):
        raise RuntimeError('Service reported failure; inspect the response and do not blindly retry sends')
    return payload


class Client:
    def __init__(self):
        self.scf = os.environ.get('SCF_BASE_URL', '').rstrip('/')
        self.inbox = os.environ.get('INBOX_BASE_URL', '').rstrip('/')

    def _request(self, base, path, secret_name, header, data=None, *, timeout=90):
        if not base.startswith('https://'):
            raise ValueError('Configure an HTTPS service URL')
        secret = os.environ.get(secret_name)
        if not secret:
            raise ValueError('Missing environment variable: ' + secret_name)
        if header == 'Authorization':
            secret = 'Bearer ' + secret
        body = None if data is None else json.dumps(data, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(base + path, data=body, method='GET' if body is None else 'POST',
                                         headers={header: secret, 'Content-Type': 'application/json'})
        try:
            with _build_opener().open(request, timeout=timeout) as response:
                raw = response.read(5 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as error:
            try:
                raw = error.read(8192)
            except (OSError, http.client.HTTPException):
                raw = b''
            finally:
                error.close()
            raise _service_error(error.code, raw) from None
        except (OSError, http.client.HTTPException):
            raise TransportError('Request/response failed; sending outcome may be unknown. No automatic retry.') from None
        except ValueError:
            raise RuntimeError('Invalid transport configuration; credentials omitted.') from None
        if len(raw) > 5 * 1024 * 1024:
            raise ValueError('Service response exceeds size limit')
        return _decode_payload(raw)

    def _request_curl(self, base, path, secret_name, header, data, *, timeout):
        """Single bounded request, used ONLY by the bot media read retry policy.

        URL, proxy credentials, bearer header and JSON go through stdin, not argv.
        No .curlrc, redirects, curl retries, insecure TLS or direct-network fallback.
        """
        if not base.startswith('https://'):
            raise ValueError('Configure an HTTPS service URL')
        secret = os.environ.get(secret_name)
        if not secret:
            raise ValueError('Missing environment variable: ' + secret_name)
        if header == 'Authorization':
            secret = 'Bearer ' + secret
        def quote(value):
            if any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError('Invalid curl configuration value')
            return json.dumps(value, ensure_ascii=False)
        config = '\n'.join([
            'silent', 'show-error', 'proto = "=https"',
            'max-time = ' + str(timeout), 'connect-timeout = ' + str(min(5, timeout)),
            'max-filesize = 5242880', 'request = "POST"',
            'url = ' + quote(base + path), 'proxy = ' + quote(_proxy_url()),
            'header = ' + quote(header + ': ' + secret),
            'header = "Content-Type: application/json"',
            'data-binary = ' + quote(json.dumps(data, ensure_ascii=False)),
            'write-out = "\\n%{http_code}"',
        ]) + '\n'
        try:
            completed = subprocess.run(['curl', '--disable', '--config', '-'], input=config.encode('utf-8'),
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout + 1, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise TransportError('Backup media transfer failed; credentials omitted.') from None
        if completed.returncode:
            # stderr can contain signed URLs/proxy credentials: never include it.
            raise TransportError('Backup media transfer failed; credentials omitted.')
        try:
            raw, status = completed.stdout.rsplit(b'\n', 1)
            status = int(status)
        except (ValueError, TypeError):
            raise TransportError('Invalid backup transfer response.') from None
        if len(raw) > 5 * 1024 * 1024:
            raise ValueError('Service response exceeds size limit')
        if not 200 <= status < 300:
            raise _service_error(status, raw[:8192])
        return _decode_payload(raw)

    def _scf(self, path, data):
        return self._request(self.scf, path, 'PUSH_SECRET', 'X-Push-Secret', data)

    def inbox_messages(self, limit=50):
        if not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError('limit must be 1–50')
        # The worker needs the Bearer prefix; keep both service secrets separate.
        return self._request(self.inbox, '/inbox?include_media=1&limit=' + str(limit),
                             'INBOX_SECRET', 'Authorization')

    def acknowledge(self, ids):
        result = self._request(self.inbox, '/inbox/ack', 'INBOX_SECRET', 'Authorization', {'ids': ids})
        if result.get('missing'):
            raise RuntimeError('Some ACK IDs are missing; receipt was not fully confirmed')
        return result

    def upload_file(self, filename, kind='file'):
        path = Path(filename)
        if path.stat().st_size > INLINE_LIMIT:
            raise ValueError('Inline limit is 2 MiB; use upload_url with an allowed HTTPS source')
        return self._scf('/media/upload', {'type': kind, 'filename': path.name,
                         'content_base64': base64.b64encode(path.read_bytes()).decode('ascii')})

    def upload_url(self, url, filename, kind='file'):
        return self._scf('/media/upload', {'type': kind, 'filename': filename, 'source_url': url})

    def send_media(self, media_id, kind='file', touser=None):
        data = {'msgtype': kind, 'media_id': media_id}
        if touser is not None:
            data['touser'] = touser
        return self._scf('/send', data)

    def download_media(self, media_id, destination):
        """Resume using a durable part file; verify chunks and publish with atomic rename.

        Use a distinct destination per inbox message id. Only one process may write
        a given destination. Existing unrelated files are never overwritten.
        """
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = Path(str(destination) + '.part')
        state_path = Path(str(destination) + '.wecom.json')
        state = {'media_id': media_id, 'offset': 0, 'size': None, 'file_sha256': None}
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding='utf-8'))
            if state.get('media_id') != media_id:
                raise ValueError('Destination belongs to another media id')
        if destination.exists():
            if not state_path.exists() or state.get('offset') != state.get('size'):
                raise FileExistsError('Refusing to overwrite an existing file')
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            if digest != state.get('partial_sha256'):
                raise ValueError('Existing destination checksum does not match the receipt')
            return {'path': str(destination), 'size': state['size'], 'sha256': digest}
        offset = state.get('offset', 0)
        if not isinstance(offset, int) or not 0 <= offset <= 20 * 1024 * 1024:
            raise ValueError('Invalid local download checkpoint')
        if offset and (not partial.exists() or partial.stat().st_size < offset):
            raise ValueError('Partial file is missing or truncated')
        digest = hashlib.sha256()
        with partial.open('r+b' if partial.exists() else 'w+b') as output:
            output.truncate(offset)
            output.seek(0)
            for block in iter(lambda: output.read(1024 * 1024), b''):
                digest.update(block)
            if offset and digest.hexdigest() != state.get('partial_sha256'):
                raise ValueError('Partial file checksum mismatch')
            output.seek(offset)
            while state['size'] is None or offset != state['size']:
                result = self._scf('/media/download', {'media_id': media_id, 'offset': offset, 'chunk_size': 1024 * 1024})
                raw = base64.b64decode(result['content_base64'], validate=True)
                total, following = result['size'], result['next_offset']
                if not isinstance(total, int) or not 0 <= total <= 20 * 1024 * 1024:
                    raise ValueError('Invalid remote file size')
                if (result.get('media_id') != media_id or result['offset'] != offset or following != offset + len(raw)
                        or following > total or len(raw) > 1024 * 1024 or (not raw and offset < total)
                        or result['eof'] is not (following == total)):
                    raise ValueError('Invalid download chunk boundaries')
                if state['size'] is not None and state['size'] != total:
                    raise ValueError('Remote file size changed')
                if hashlib.sha256(raw).hexdigest() != result['chunk_sha256']:
                    raise ValueError('Download chunk checksum mismatch')
                whole_hash = result.get('file_sha256')
                if state['file_sha256'] and whole_hash and whole_hash != state['file_sha256']:
                    raise ValueError('Remote file checksum changed')
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
                digest.update(raw)
                offset = following
                state.update(offset=offset, size=total, file_sha256=whole_hash or state['file_sha256'],
                             partial_sha256=digest.hexdigest())
                if result['eof'] and state['file_sha256'] and state['file_sha256'] != digest.hexdigest():
                    raise ValueError('Whole file checksum mismatch')
                _write_json_atomic(state_path, state)
                if result['eof']:
                    break
            if state['file_sha256'] and state['file_sha256'] != digest.hexdigest():
                raise ValueError('Whole file checksum mismatch')
        os.replace(partial, destination)
        return {'path': str(destination), 'size': state['size'], 'sha256': digest.hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    upload = sub.add_parser('upload'); upload.add_argument('path'); upload.add_argument('--type', choices=['image', 'file'], default='file')
    download = sub.add_parser('download'); download.add_argument('media_id'); download.add_argument('output')
    send = sub.add_parser('send'); send.add_argument('media_id'); send.add_argument('--type', choices=['image', 'file'], default='file'); send.add_argument('--touser')
    sub.add_parser('inbox')
    args = parser.parse_args()
    client = Client()
    if args.command == 'upload': result = client.upload_file(args.path, args.type)
    elif args.command == 'download': result = client.download_media(args.media_id, args.output)
    elif args.command == 'send': result = client.send_media(args.media_id, args.type, args.touser)
    else: result = client.inbox_messages()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
