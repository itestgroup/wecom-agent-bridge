import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import unittest
from unittest.mock import patch
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("scf_bridge", ROOT / "scf/index.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class Response(io.BytesIO):
    def __init__(self, body, headers=None, code=200):
        super().__init__(body)
        self.headers = headers or {}
        self.code = code
        self.status = code

    def getcode(self):
        return self.code


def json_response(value):
    return Response(json.dumps(value).encode(), {"Content-Type": "application/json"})


class SCFMediaTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"PUSH_SECRET": "test-call-secret", "WECOM_CORPID": "wwtest",
                              "WECOM_AGENTID": "1000003", "WECOM_SECRET": "secret&+value",
                              "WECOM_TOUSER": "Charles", "MEDIA_ALLOWED_HOSTS": "assets.example.com"})
        self.env.start()
        bridge._TOKEN.clear()
        self.calls = []

    def tearDown(self):
        self.env.stop()

    def open(self, request, timeout=15):
        self.calls.append(request)
        if "/gettoken?" in request.full_url:
            return json_response({"access_token": "token-value", "expires_in": 7200})
        if "/message/send?" in request.full_url:
            return json_response({"errcode": 0, "errmsg": "ok", "msgid": "sent-id"})
        if "/media/upload?" in request.full_url:
            return json_response({"type": "file", "media_id": "uploaded-id", "created_at": 1792200000})
        raise AssertionError("Unexpected network request")

    def call(self, data=None, path="/", method="POST", headers=None, encoded=False):
        body = json.dumps({} if data is None else data)
        if encoded:
            body = base64.b64encode(body.encode()).decode()
        event = {"headers": {"X-Push-Secret": "test-call-secret"} if headers is None else headers,
                 "httpMethod": method, "path": path, "body": body, "isBase64Encoded": encoded}
        with patch.object(bridge, "_open", side_effect=self.open):
            result = bridge.main_handler(event, None)
        return result["statusCode"], json.loads(result["body"])

    def test_legacy_text_and_recipient_override_are_preserved(self):
        status, result = self.call({"text": "  hello  ", "touser": "other-user"})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        payload = json.loads(self.calls[-1].data)
        self.assertEqual(payload["touser"], "other-user")
        self.assertEqual(payload["text"], {"content": "hello"})
        self.assertEqual(payload["duplicate_check_interval"], 600)
        self.assertNotIn("enable_duplicate_check", payload)

    def test_configured_recipient_and_all_fallback_are_preserved(self):
        self.call({"text": "test"})
        self.assertEqual(json.loads(self.calls[-1].data)["touser"], "Charles")
        with patch.dict(os.environ, {"WECOM_TOUSER": "@all"}):
            self.call({"text": "test"})
            self.assertEqual(json.loads(self.calls[-1].data)["touser"], "@all")
        value = os.environ.pop("WECOM_TOUSER")
        try:
            self.call({"text": "test"})
            self.assertEqual(json.loads(self.calls[-1].data)["touser"], "@all")
        finally:
            os.environ["WECOM_TOUSER"] = value

    def test_cache_and_query_parameter_encoding(self):
        self.call({"text": "first"})
        self.call({"text": "second"})
        tokens = [r for r in self.calls if "/gettoken?" in r.full_url]
        self.assertEqual(len(tokens), 1)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(tokens[0].full_url).query)
        self.assertEqual(query["corpsecret"], ["secret&+value"])

    def test_explicit_token_expiry_refreshes_once(self):
        replies = iter([{"access_token": "old", "expires_in": 7200}, {"errcode": 42001},
                        {"access_token": "new", "expires_in": 7200}, {"errcode": 0}])
        with patch.object(bridge, "_open", side_effect=lambda *a, **kw: json_response(next(replies))) as network:
            result = bridge.main_handler({"headers": {"x-push-secret": "test-call-secret"}, "body": '{"text":"hello"}'}, None)
        self.assertEqual(result["statusCode"], 200)
        self.assertTrue(json.loads(result["body"])["ok"])
        self.assertEqual(network.call_count, 4)

    def test_send_timeout_is_unknown_and_never_auto_retried(self):
        def timeout(request, **kwargs):
            if "/gettoken?" in request.full_url:
                return json_response({"access_token": "token", "expires_in": 7200})
            raise TimeoutError("must not leak secret&+value")
        with patch.object(bridge, "_open", side_effect=timeout) as network:
            result = bridge.main_handler({"headers": {"x-push-secret": "test-call-secret"}, "body": '{"text":"hello"}'}, None)
        body = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 504)
        self.assertEqual(body["err"], "delivery_unknown")
        self.assertFalse(body["retry_safe"])
        self.assertEqual(network.call_count, 2)
        self.assertNotIn("secret&+value", result["body"])

    def test_send_business_failure_keeps_legacy_body_contract(self):
        def reject(request, **kwargs):
            return json_response({"access_token": "token", "expires_in": 7200}) if "/gettoken?" in request.full_url else json_response({"errcode": 81013})
        with patch.object(bridge, "_open", side_effect=reject):
            result = bridge.main_handler({"headers": {"x-push-secret": "test-call-secret"}, "body": '{"text":"hello"}'}, None)
        self.assertEqual(result["statusCode"], 200)
        self.assertFalse(json.loads(result["body"])["ok"])

    def test_image_and_file_send_use_media_ids(self):
        for kind in ("image", "file"):
            status, body = self.call({"msgtype": kind, "media_id": "media-id", "touser": "@all"})
            self.assertEqual(status, 200)
            payload = json.loads(self.calls[-1].data)
            self.assertEqual(payload[kind], {"media_id": "media-id"})
            self.assertEqual(payload["touser"], "@all")

    def test_upload_image_multipart_and_file_bytes(self):
        for kind, name, raw in [("image", "截图.png", b"\x89PNG\r\n\x1a\nimage-body"), ("file", "报告.pdf", b"%PDF-test-body")]:
            status, body = self.call({"type": kind, "filename": name, "content_base64": base64.b64encode(raw).decode()}, "/media/upload")
            self.assertEqual(status, 200)
            self.assertEqual(body["media_id"], "uploaded-id")
            self.assertEqual(body["sha256"], hashlib.sha256(raw).hexdigest())
            req = self.calls[-1]
            self.assertIn(raw, req.data)
            self.assertIn(name.encode(), req.data)
            self.assertIn('name="media"', req.data.decode("utf-8", errors="replace"))
            self.assertIn("multipart/form-data", req.get_header("Content-type"))

    def test_bad_upload_inputs_do_not_call_upstream(self):
        cases = [
            {"type": "image", "filename": "x.png", "content_base64": "!!!"},
            {"type": "image", "filename": "x.png", "content_base64": base64.b64encode(b"not-an-image").decode()},
            {"type": "file", "filename": "x\r\ninjected.txt", "content_base64": "AAAA"},
            {"type": "file", "filename": "x.txt"},
            {"type": "file", "filename": "x.txt", "content_base64": "AAAA", "source_url": "https://example.com/x"},
            {"type": "video", "filename": "x.mp4", "content_base64": "AAAA"},
        ]
        for case in cases:
            status, body = self.call(case, "/media/upload")
            self.assertEqual(status, 400)
            self.assertFalse(body["ok"])
        self.assertEqual(self.calls, [])

    def test_inline_limit_and_metadata_limit(self):
        status, _ = self.call({"type": "file", "filename": "x.txt", "content_base64": base64.b64encode(b"x" * (bridge.INLINE_LIMIT + 1)).decode()}, "/media/upload")
        self.assertIn(status, (400, 413))
        self.assertEqual(self.calls, [])

    def test_download_range_chunks_can_reassemble(self):
        raw = b"abcdefghijk" * 120000
        chunks = []
        offset = 0
        def range_open(request, **kwargs):
            if "/gettoken?" in request.full_url:
                return json_response({"access_token": "token", "expires_in": 7200})
            start, end = map(int, request.get_header("Range")[6:].split("-"))
            part = raw[start:end + 1]
            return Response(part, {"Content-Range": "bytes %d-%d/%d" % (start, start + len(part) - 1, len(raw)),
                                  "Content-Length": str(len(part)), "Content-Type": "application/pdf",
                                  "Content-Disposition": "attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A.pdf"}, 206)
        with patch.object(bridge, "_open", side_effect=range_open):
            while True:
                body = bridge.download_media({"media_id": "incoming", "offset": offset, "chunk_size": 500000})
                chunk = base64.b64decode(body["content_base64"])
                self.assertEqual(hashlib.sha256(chunk).hexdigest(), body["chunk_sha256"])
                self.assertEqual(body["filename"], "报告.pdf")
                chunks.append(chunk)
                offset = body["next_offset"]
                if body["eof"]:
                    break
        self.assertEqual(b"".join(chunks), raw)

    def test_download_ignoring_range_is_bounded_and_correct(self):
        raw = b"{\"errcode\":123,\"my-file\":true}"
        with patch.object(bridge, "_open", return_value=Response(raw, {"Content-Type": "application/json", "Content-Disposition": "attachment; filename=data.json"})):
            body = bridge._download("token", "media", 3, 7)
        self.assertEqual(base64.b64decode(body["content_base64"]), raw[3:10])
        self.assertEqual(body["file_sha256"], hashlib.sha256(raw).hexdigest())

    def test_download_error_json_is_not_saved_as_attachment(self):
        with patch.object(bridge, "_open", return_value=json_response({"errcode": 40007, "errmsg": "invalid media"})):
            with self.assertRaises(bridge.WeComError):
                bridge._download("token", "expired", 0, 100)

    def test_download_rejects_bad_ranges_oversize_and_truncation(self):
        responses = [
            Response(b"abc", {"Content-Range": "bytes 7-9/10"}, 206),
            Response(b"abc", {"Content-Range": "bytes 0-2/999999999"}, 206),
            Response(b"abc", {"Content-Length": "10"}),
            Response(b"", {"Content-Length": str(bridge.FILE_LIMIT + 1)}),
        ]
        for response in responses:
            with patch.object(bridge, "_open", return_value=response):
                with self.assertRaises((bridge.UpstreamError, bridge.InputError)):
                    bridge._download("token", "media", 0, 100)

    def test_download_input_validation(self):
        for data in [{"media_id": "x", "offset": -1}, {"media_id": "x", "chunk_size": 0}, {"media_id": "x", "offset": True},
                     {"media_id": "x", "chunk_size": bridge.CHUNK_LIMIT + 1}, {"media_id": []}]:
            status, _ = self.call(data, "/media/download")
            self.assertEqual(status, 400)
        self.assertEqual(self.calls, [])

    def test_source_requires_exact_https_host_and_public_dns(self):
        for url in ("http://assets.example.com/x", "https://other.example.com/x", "https://assets.example.com.evil/x",
                    "https://user:pass@assets.example.com/x", "https://assets.example.com:444/x", "https://assets.example.com/x#fragment"):
            with self.assertRaises(bridge.InputError):
                bridge._source_bytes(url, 100)
        for address in ("127.0.0.1", "169.254.169.254", "10.1.2.3", "224.0.0.1", "::1", "ff02::1"):
            with patch.object(socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]):
                with self.assertRaises(bridge.InputError):
                    bridge._source_bytes("https://assets.example.com/x", 100)

    def test_source_uses_validated_ip_and_never_forwards_credentials(self):
        with patch.object(socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]), \
             patch.object(bridge, "PinnedHTTPS") as connection:
            connection.return_value.getresponse.return_value = Response(b"source-file")
            self.assertEqual(bridge._source_bytes("https://assets.example.com/file?sig=private", 100), b"source-file")
            connection.assert_called_once_with("assets.example.com", "8.8.8.8")
            args, kwargs = connection.return_value.request.call_args
            self.assertEqual(args, ("GET", "/file?sig=private"))
            self.assertEqual(kwargs["headers"], {"Accept-Encoding": "identity"})

    def test_source_redirect_is_not_followed(self):
        with patch.object(socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]), \
             patch.object(bridge, "PinnedHTTPS") as connection:
            connection.return_value.getresponse.return_value = Response(b"", code=302)
            with self.assertRaises(bridge.InputError):
                bridge._source_bytes("https://assets.example.com/x", 100)
            connection.return_value.close.assert_called_once()

    def test_auth_headers_methods_and_health(self):
        self.assertEqual(self.call({}, headers={})[0], 403)
        self.assertEqual(self.call({}, path="/health", method="GET", headers={"X-PUSH-SECRET": "test-call-secret"})[1]["version"], "0.1.0-dev")
        self.assertEqual(self.call({}, method="GET")[0], 405)
        self.assertEqual(self.call({}, path="/unknown")[0], 404)
        self.assertEqual(self.call({"text": "hello"}, encoded=True)[0], 200)

    def test_wrong_json_types_and_fields_are_client_errors(self):
        for value in ([], 123, "string", {"text": 123}, {"text": "hi", "touser": []}, {"msgtype": "video"}):
            self.assertEqual(self.call(value)[0], 400)
        result = bridge.main_handler({"headers": {"x-push-secret": "test-call-secret"}, "body": "not-json"}, None)
        self.assertEqual(result["statusCode"], 400)

    def test_does_not_expose_secret_or_token_in_business_error(self):
        bridge._TOKEN.update(value="token-value")
        safe = bridge._safe_response({"errcode": 1, "errmsg": "secret&+value token-value access_token=xxx&corpsecret=yyy", "access_token": "do-not-return"})
        self.assertNotIn("access_token", safe)
        self.assertNotIn("secret&+value", safe["errmsg"])
        self.assertNotIn("token-value", safe["errmsg"])
        self.assertNotIn("=xxx", safe["errmsg"])


if __name__ == "__main__":
    unittest.main()
