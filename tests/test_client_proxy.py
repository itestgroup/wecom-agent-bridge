"""Proxy selection and real local HTTPS CONNECT; no external network needed."""
import base64
import os
import json
import ssl
import shutil
import subprocess
import tempfile
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
import unittest
from unittest.mock import patch
import urllib.request

from clients import wecom_media
from clients.wecom_kf import KfClient
from clients.wecom_bot import BotClient


class ProxyTests(unittest.TestCase):
    def test_environment_precedence_and_direct_default(self):
        for values, expected in [
            ({'HTTPS_PROXY': 'http://upper.invalid:3128', 'https_proxy': 'http://lower.invalid:3128'}, 'http://upper.invalid:3128'),
            ({'https_proxy': 'http://lower.invalid:3128'}, 'http://lower.invalid:3128'),
            ({'HTTPS_PROXY': '', 'https_proxy': 'http://lower.invalid:3128'}, 'http://lower.invalid:3128'),
            ({'HTTP_PROXY': 'http://unused.invalid:3128'}, ''),
            ({}, ''),
        ]:
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True):
                opener = wecom_media._build_opener()
                handlers = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
                self.assertEqual(handlers[0].proxies if handlers else {},
                                 {'http': expected, 'https': expected} if expected else {})
                self.assertTrue(any(isinstance(h, wecom_media.NoRedirect) for h in opener.handlers))

    def test_proxy_auth_reaches_connect_without_origin_secret_or_dns(self):
        observed = []
        class Proxy(BaseHTTPRequestHandler):
            def do_CONNECT(self):
                observed.append((self.path, dict(self.headers)))
                # No TLS server needed: verify CONNECT and reject deliberately.
                self.send_response(502)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = HTTPServer(('127.0.0.1', 0), Proxy)
        server.timeout = 2
        thread = Thread(target=server.handle_request, daemon=True)
        thread.start()
        try:
            proxy = 'http://muse%40test:p%40ss%3Aword@127.0.0.1:' + str(server.server_port)
            with patch.dict(os.environ, {'HTTPS_PROXY': proxy, 'INBOX_SECRET': 'origin-private-key'}, clear=True):
                with self.assertRaises(RuntimeError) as caught:
                    wecom_media.Client()._request('https://unresolvable-proxy-test.invalid', '/inbox',
                                                 'INBOX_SECRET', 'Authorization')
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0][0], 'unresolvable-proxy-test.invalid:443')
            headers = {k.lower(): v for k, v in observed[0][1].items()}
            self.assertEqual(headers['proxy-authorization'], 'Basic ' + base64.b64encode(b'muse@test:p@ss:word').decode())
            self.assertNotIn('authorization', headers)
            self.assertNotIn('p%40ss', str(caught.exception))
            self.assertNotIn('origin-private-key', str(caught.exception))
        finally:
            thread.join(timeout=3)
            server.server_close()

    def test_redirect_stays_blocked_when_proxy_enabled(self):
        observed = []
        class Proxy(BaseHTTPRequestHandler):
            def do_GET(self):
                observed.append(self.path)
                self.send_response(302)
                self.send_header('Location', 'http://must-not-follow.invalid/secret')
                self.end_headers()
            def log_message(self, *args):
                pass
        server = HTTPServer(('127.0.0.1', 0), Proxy)
        server.timeout = 2
        thread = Thread(target=server.handle_request, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:' + str(server.server_port)}, clear=True):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    wecom_media._build_opener().open('http://proxy-redirect-test.invalid/start', timeout=2)
            self.assertEqual(caught.exception.code, 302)
            self.assertEqual(observed, ['http://proxy-redirect-test.invalid/start'])
        finally:
            thread.join(timeout=3)
            server.server_close()

    def test_kf_and_bot_share_the_patched_transport(self):
        self.assertIs(KfClient._request, wecom_media.Client._request)
        self.assertIs(BotClient._request, wecom_media.Client._request)

    @unittest.skipUnless(shutil.which('curl') and shutil.which('openssl'), 'curl/openssl not installed')
    def test_real_curl_authenticated_connect_tls_and_json(self):
        observed = []
        with tempfile.TemporaryDirectory() as directory:
            cert, key = Path(directory) / 'cert.pem', Path(directory) / 'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                            '-subj', '/CN=worker-proxy-test.invalid', '-addext', 'subjectAltName=DNS:worker-proxy-test.invalid',
                            '-keyout', str(key), '-out', str(cert)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(cert), str(key))
            class Proxy(BaseHTTPRequestHandler):
                def do_CONNECT(self):
                    observed.append(('connect', self.path, dict(self.headers)))
                    self.send_response(200); self.end_headers()
                    self.connection = context.wrap_socket(self.connection, server_side=True)
                    self.rfile = self.connection.makefile('rb')
                    self.wfile = self.connection.makefile('wb')
                    try:
                        self.handle_one_request()
                    finally:
                        self.connection.close()
                def do_POST(self):
                    body = self.rfile.read(int(self.headers['Content-Length']))
                    observed.append(('post', self.path, dict(self.headers), json.loads(body)))
                    result = b'{"ok":true,"via":"curl"}'
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(result)))
                    self.end_headers(); self.wfile.write(result)
                def log_message(self, *args):
                    pass
            server = HTTPServer(('127.0.0.1', 0), Proxy)
            server.timeout = 3
            thread = Thread(target=server.handle_request, daemon=True); thread.start()
            try:
                proxy = 'http://muse%40test:p%40ss%3Aword@127.0.0.1:' + str(server.server_port)
                with patch.dict(os.environ, {'HTTPS_PROXY': proxy, 'INBOX_SECRET': 'origin-private-key',
                                              'CURL_CA_BUNDLE': str(cert)}, clear=True):
                    result = wecom_media.Client()._request_curl('https://worker-proxy-test.invalid', '/bot/media',
                                              'INBOX_SECRET', 'Authorization', {'id': 'test', 'index': 0}, timeout=3)
                self.assertEqual(result, {'ok': True, 'via': 'curl'})
                self.assertEqual(observed[0][1], 'worker-proxy-test.invalid:443')
                connect_headers = {k.lower(): v for k, v in observed[0][2].items()}
                post_headers = {k.lower(): v for k, v in observed[1][2].items()}
                self.assertIn('proxy-authorization', connect_headers)
                self.assertNotIn('authorization', connect_headers)
                self.assertEqual(post_headers['authorization'], 'Bearer origin-private-key')
                self.assertNotIn('proxy-authorization', post_headers)
                self.assertEqual(observed[1][3], {'id': 'test', 'index': 0})
            finally:
                thread.join(timeout=4)
                server.server_close()


if __name__ == '__main__':
    unittest.main()
