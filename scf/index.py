"""WeCom Agent Bridge SCF development baseline (Python 3.9+, standard library only).

Existing POST {text, touser?} and recipient defaults are preserved.
POST /media/upload: {type: image|file, filename, content_base64 OR source_url}.
POST /media/download: {media_id, offset?, chunk_size?}; chunks are base64 JSON.
POST / or /send: {msgtype: image|file, media_id, touser?}.
All routes use X-Push-Secret. Enterprise secrets stay in SCF environment.
Optional MEDIA_ALLOWED_HOSTS permits exact public HTTPS source hosts, comma-separated.
Image cap: 2 MiB; file cap: 20 MiB; inline uploads: 2 MiB; download chunks: 1 MiB.
"""
import base64
import binascii
import hashlib
import hmac
import http.client
import ipaddress
import json
import mimetypes
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.message import Message

VERSION = "0.1.0-dev"
API = "https://qyapi.weixin.qq.com/cgi-bin/"
INLINE_LIMIT = 2 * 1024 * 1024
FILE_LIMIT = 20 * 1024 * 1024
IMAGE_LIMIT = 2 * 1024 * 1024
CHUNK_LIMIT = 1024 * 1024
BODY_LIMIT = 3 * 1024 * 1024
TOKEN_ERRORS = {40014, 42001}
_TOKEN = {}
_TOKEN_LOCK = threading.Lock()


class InputError(Exception):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.status = status


class UpstreamError(Exception):
    pass


class DeliveryUnknown(Exception):
    pass


class WeComError(Exception):
    def __init__(self, stage, response):
        self.stage = stage
        self.response = _safe_response(response)
        super().__init__("wecom_rejected")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(request, timeout=15):
    # A redirect must not send access tokens or payloads to an arbitrary host.
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)


def _read_limited(response, limit):
    length = response.headers.get("Content-Length")
    if length and (not str(length).isdigit() or int(length) > limit):
        raise InputError("media_too_large", 413)
    data = response.read(limit + 1)
    if len(data) > limit:
        raise InputError("media_too_large", 413)
    if length and len(data) != int(length):
        raise UpstreamError("truncated_upstream_response")
    return data


def _safe_response(response):
    allowed = ("errcode", "errmsg", "msgid", "invaliduser", "invalidparty", "invalidtag", "unlicenseduser")
    result = {key: response[key] for key in allowed if key in response}
    for key, value in list(result.items()):
        if isinstance(value, str):
            for secret in (os.environ.get("WECOM_SECRET", ""), _TOKEN.get("value", "")):
                if secret:
                    value = value.replace(secret, "[redacted]")
            value = re.sub(r"(?i)(access_token|corpsecret)=[^&\s]+", r"\1=[redacted]", value)
            result[key] = value[:512]
    return result


def _json_http(url, data=None, headers=None, sending=False):
    payload = None if data is None else (data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8"))
    req_headers = {"Content-Type": "application/json"}
    req_headers.update(headers or {})
    request = urllib.request.Request(url, data=payload, headers=req_headers, method="GET" if payload is None else "POST")
    try:
        with _open(request) as response:
            result = json.loads(_read_limited(response, 256 * 1024).decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("unexpected API response")
        return result
    except (OSError, ValueError, http.client.HTTPException, InputError, UpstreamError) as error:
        if sending:
            raise DeliveryUnknown("delivery_unknown") from error
        raise UpstreamError("upstream_unavailable") from error


def _get_token(force=False):
    corp, secret = os.environ["WECOM_CORPID"], os.environ["WECOM_SECRET"]
    scope = hashlib.sha256((corp + "\0" + secret).encode()).hexdigest()
    with _TOKEN_LOCK:
        if not force and _TOKEN.get("scope") == scope and time.monotonic() < _TOKEN.get("expires", 0):
            return _TOKEN["value"]
        query = urllib.parse.urlencode({"corpid": corp, "corpsecret": secret})
        response = _json_http(API + "gettoken?" + query)
        token = response.get("access_token")
        if not isinstance(token, str) or not token or response.get("errcode", 0) != 0:
            raise WeComError("gettoken", response)
        expires = response.get("expires_in", 7200)
        if not isinstance(expires, (int, float)) or expires <= 0:
            raise UpstreamError("invalid_token_expiry")
        _TOKEN.update(scope=scope, value=token, expires=time.monotonic() + expires - min(60, expires / 10))
        return token


def _with_token(operation):
    for attempt in range(2):
        try:
            return operation(_get_token(force=attempt == 1))
        except WeComError as error:
            # Only an explicit token rejection is safe to refresh and retry.
            if attempt or error.response.get("errcode") not in TOKEN_ERRORS:
                raise
    raise UpstreamError("token_refresh_failed")


def _api_json(path, token, data, stage, sending=False, query=None, headers=None):
    params = {"access_token": token}
    params.update(query or {})
    response = _json_http(API + path + "?" + urllib.parse.urlencode(params), data, headers, sending)
    if response.get("errcode", 0) != 0:
        raise WeComError(stage, response)
    return response


def _string(value, name, required=True, max_length=1024):
    if not isinstance(value, str) or len(value) > max_length or (required and not value.strip()):
        raise InputError("invalid_" + name)
    return value


def _media_id(value):
    value = _string(value, "media_id")
    if any(ord(c) < 33 for c in value):
        raise InputError("invalid_media_id")
    return value


def _recipient(touser):
    # Preserve caller-supplied recipient, WECOM_TOUSER and the original @all default.
    if touser is not None:
        touser = _string(touser, "touser", required=False, max_length=32768).strip()
    return touser or os.environ.get("WECOM_TOUSER", "@all")


def send_wecom(text=None, touser=None, msgtype="text", media_id=None):
    if msgtype not in ("text", "image", "file"):
        raise InputError("unsupported_msgtype")
    if msgtype == "text":
        text = _string(text, "text", max_length=BODY_LIMIT).strip()
        item = {"content": text}
    else:
        item = {"media_id": _media_id(media_id)}
    payload = {"touser": _recipient(touser), "agentid": int(os.environ["WECOM_AGENTID"]),
               "msgtype": msgtype, msgtype: item, "duplicate_check_interval": 600}
    try:
        response = _with_token(lambda token: _api_json("message/send", token, payload, "send", sending=True))
        # A send response without an explicit success code is not a confirmation.
        if response.get("errcode") != 0:
            raise DeliveryUnknown("delivery_unknown")
        return {"ok": True, "stage": "send", "resp": _safe_response(response)}
    except WeComError as error:
        return {"ok": False, "stage": error.stage, "resp": error.response}


def _filename(value):
    value = _string(value, "filename", max_length=255)
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    if not value or value in (".", "..") or any(ord(c) < 32 for c in value):
        raise InputError("invalid_filename")
    return value.replace('"', "_")


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=15, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def _source_bytes(url, limit):
    url = _string(url, "source_url", max_length=8192)
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
        allowed = {h.strip().lower() for h in os.environ.get("MEDIA_ALLOWED_HOSTS", "").split(",") if h.strip()}
        if parsed.scheme != "https" or not host or parsed.port not in (None, 443) or parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise InputError("invalid_source_url")
        if host not in allowed:
            raise InputError("source_host_not_allowed", 403)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise InputError("source_ip_not_allowed", 403)
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        def public(ip):
            address = ipaddress.ip_address(ip)
            return address.is_global and not (address.is_multicast or address.is_reserved
                                              or address.is_loopback or address.is_link_local or address.is_unspecified)
        if not addresses or not all(public(ip) for ip in addresses):
            raise InputError("source_address_not_public", 403)
        connection = PinnedHTTPS(host, sorted(addresses)[0])
        try:
            target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            connection.request("GET", target, headers={"Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status != 200:
                # Redirects are deliberately not followed, even to another allowed host.
                raise InputError("source_http_error_or_redirect", 502)
            return _read_limited(response, limit)
        finally:
            connection.close()
    except InputError:
        raise
    except (ValueError, UnicodeError):
        raise InputError("invalid_source_url")
    except (OSError, http.client.HTTPException) as error:
        raise UpstreamError("source_unavailable") from error


def upload_media(data):
    kind = data.get("type")
    if kind not in ("image", "file"):
        raise InputError("unsupported_media_type")
    name = _filename(data.get("filename"))
    inline, source = data.get("content_base64"), data.get("source_url")
    if (inline is None) == (source is None):
        raise InputError("provide_one_media_source")
    limit = IMAGE_LIMIT if kind == "image" else FILE_LIMIT
    if inline is not None:
        inline = _string(inline, "content_base64", max_length=4 * ((INLINE_LIMIT + 2) // 3))
        try:
            content = base64.b64decode(inline, validate=True)
        except (ValueError, binascii.Error):
            raise InputError("invalid_base64")
        if len(content) > INLINE_LIMIT:
            raise InputError("inline_too_large_use_source_url", 413)
    else:
        content = _source_bytes(source, limit)
    if not 5 < len(content) <= limit:
        raise InputError("invalid_media_size", 413)
    if kind == "image":
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif content.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        else:
            raise InputError("image_requires_png_or_jpeg")
    else:
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    boundary = "wecom-" + uuid.uuid4().hex
    head = ('--' + boundary + '\r\nContent-Disposition: form-data; name="media"; filename="' + name
            + '"\r\nContent-Type: ' + mime + '\r\n\r\n').encode("utf-8")
    payload = head + content + ("\r\n--" + boundary + "--\r\n").encode("ascii")
    response = _with_token(lambda token: _api_json("media/upload", token, payload, "upload", query={"type": kind},
                                                    headers={"Content-Type": "multipart/form-data; boundary=" + boundary}))
    identifier = response.get("media_id")
    if not isinstance(identifier, str) or not identifier:
        raise UpstreamError("invalid_upload_response")
    return {"ok": True, "type": kind, "media_id": identifier, "filename": name,
            "size": len(content), "sha256": hashlib.sha256(content).hexdigest(), "created_at": response.get("created_at")}


def _integer(value, name, maximum, default):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise InputError("invalid_" + name)
    return value


def _download(token, identifier, offset, count):
    query = urllib.parse.urlencode({"access_token": token, "media_id": identifier})
    request = urllib.request.Request(API + "media/get?" + query,
                                     headers={"Range": "bytes=%d-%d" % (offset, offset + count - 1), "Accept-Encoding": "identity"})
    try:
        with _open(request) as response:
            code = response.getcode()
            headers = response.headers
            raw = _read_limited(response, FILE_LIMIT if code == 200 else count)
            if "application/json" in headers.get("Content-Type", "").lower() and not headers.get("Content-Disposition"):
                try:
                    error = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError):
                    error = None
                if isinstance(error, dict) and error.get("errcode", 0) != 0:
                    raise WeComError("download", error)
            name_header = Message()
            name_header["Content-Disposition"] = headers.get("Content-Disposition", "")
            name = name_header.get_filename() or ""
            if name:
                name = _filename(name)
            mime = headers.get("Content-Type", "application/octet-stream").split(";", 1)[0]
            whole_hash = None
            if code == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", headers.get("Content-Range", ""))
                if not match:
                    raise UpstreamError("invalid_content_range")
                start, end, total = map(int, match.groups())
                if start != offset or end < start or end >= total or end - start + 1 != len(raw):
                    raise UpstreamError("invalid_content_range")
                chunk = raw
            elif code == 200:
                # Some upstream servers ignore Range; bounded full-read fallback stays correct.
                total = len(raw)
                whole_hash = hashlib.sha256(raw).hexdigest()
                chunk = raw[offset:offset + count]
            else:
                raise UpstreamError("unexpected_media_response")
            if total > FILE_LIMIT:
                raise InputError("media_too_large", 413)
            if offset > total:
                raise InputError("offset_out_of_range", 416)
            if not chunk and offset < total:
                raise UpstreamError("empty_media_chunk")
            next_offset = offset + len(chunk)
            return {"ok": True, "media_id": identifier, "filename": name, "content_type": mime,
                    "size": total, "offset": offset, "next_offset": next_offset, "eof": next_offset == total,
                    "content_base64": base64.b64encode(chunk).decode("ascii"),
                    "chunk_sha256": hashlib.sha256(chunk).hexdigest(), "file_sha256": whole_hash}
    except (OSError, http.client.HTTPException) as error:
        raise UpstreamError("media_download_unavailable") from error


def download_media(data):
    identifier = _media_id(data.get("media_id"))
    offset = _integer(data.get("offset"), "offset", FILE_LIMIT, 0)
    count = _integer(data.get("chunk_size"), "chunk_size", CHUNK_LIMIT, CHUNK_LIMIT)
    if count == 0:
        raise InputError("invalid_chunk_size")
    return _with_token(lambda token: _download(token, identifier, offset, count))


def _kf_account(value):
    account = _string(value, "open_kfid", max_length=256)
    expected = os.environ.get("KF_OPEN_KFID", "")
    if not expected:
        raise InputError("kf_account_not_configured", 503)
    if account != expected:
        raise InputError("kf_account_not_allowed", 403)
    return account


def kf_accounts(data):
    offset = _integer(data.get("offset"), "offset", 100000, 0)
    result = _with_token(lambda token: _api_json("kf/account/list", token,
                         {"offset": offset, "limit": 100}, "kf_accounts"))
    accounts = result.get("account_list")
    if not isinstance(accounts, list):
        raise UpstreamError("invalid_kf_accounts")
    return {"ok": True, "accounts": [{"open_kfid": a.get("open_kfid"), "name": a.get("name")}
            for a in accounts if isinstance(a, dict)], "offset": offset,
            "has_more": len(accounts) == 100}


def kf_sync(data):
    payload = {"open_kfid": _kf_account(data.get("open_kfid")),
               "limit": 10, "voice_format": 0}
    sync_token = _string(data.get("token", ""), "sync_token", required=False, max_length=128)
    if sync_token:
        payload["token"] = sync_token
    cursor = _string(data.get("cursor", ""), "cursor", required=False, max_length=4096)
    if cursor:
        payload["cursor"] = cursor
    result = _with_token(lambda token: _api_json("kf/sync_msg", token, payload, "kf_sync"))
    messages, following, more = result.get("msg_list"), result.get("next_cursor"), result.get("has_more")
    if (not isinstance(messages, list) or len(messages) > 10
            or not isinstance(following, str) or len(following) > 4096 or more not in (0, 1)):
        raise UpstreamError("invalid_kf_sync_response")
    if (messages or more) and (not following or following == cursor):
        raise UpstreamError("kf_cursor_not_advancing")
    return {"ok": True, "messages": messages, "next_cursor": following, "has_more": bool(more)}


def kf_send(data):
    account = _kf_account(data.get("open_kfid"))
    user = _string(data.get("external_userid"), "external_userid", max_length=256)
    allowed = {s.strip() for s in os.environ.get("KF_ALLOWED_USER_IDS", "").split(",") if s.strip()}
    if allowed and user not in allowed:
        raise InputError("kf_user_not_allowed", 403)
    kind = data.get("msgtype", "text")
    if kind == "text":
        text = _string(data.get("text"), "text", max_length=2048)
        if len(text.encode("utf-8")) > 2048:
            raise InputError("text_too_long")
        item = {"content": text}
    elif kind in ("image", "file"):
        item = {"media_id": _media_id(data.get("media_id"))}
    else:
        raise InputError("unsupported_msgtype")
    payload = {"open_kfid": account, "touser": user, "msgtype": kind, kind: item}
    # Stable IDs let a caller track an explicit send; never retry an unknown outcome blindly.
    if "msgid" in data:
        identifier = _string(data["msgid"], "msgid", max_length=32)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", identifier):
            raise InputError("invalid_msgid")
        payload["msgid"] = identifier
    result = _with_token(lambda token: _api_json("kf/send_msg", token, payload, "kf_send", sending=True))
    if result.get("errcode") != 0:
        raise DeliveryUnknown("delivery_unknown")
    return {"ok": True, "stage": "kf_send", "resp": _safe_response(result)}


def _resp(status, obj):
    return {"statusCode": status, "headers": {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"},
            "body": json.dumps(obj, ensure_ascii=False)}


def _body(event):
    body = event.get("body") or "{}"
    if isinstance(body, dict):
        data = body
    else:
        if not isinstance(body, (str, bytes)) or len(body) > BODY_LIMIT * 2:
            raise InputError("body_too_large", 413)
        try:
            if event.get("isBase64Encoded"):
                body = base64.b64decode(body, validate=True)
            if isinstance(body, bytes):
                if len(body) > BODY_LIMIT:
                    raise InputError("body_too_large", 413)
                body = body.decode("utf-8")
            if len(body.encode("utf-8")) > BODY_LIMIT:
                raise InputError("body_too_large", 413)
            data = json.loads(body)
        except (ValueError, UnicodeError, binascii.Error):
            raise InputError("invalid_json_or_encoding")
    if not isinstance(data, dict):
        raise InputError("body_must_be_object")
    return data


def main_handler(event, context):
    request_id = uuid.uuid4().hex
    try:
        if not isinstance(event, dict) or not isinstance(event.get("headers") or {}, dict):
            raise InputError("invalid_event")
        headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
        expected, supplied = os.environ.get("PUSH_SECRET", ""), headers.get("x-push-secret", "")
        if not expected or not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), expected.encode()):
            return _resp(403, {"ok": False, "err": "forbidden"})
        path, method = event.get("path") or "/", event.get("httpMethod") or "POST"
        if path == "/health" and method == "GET":
            return _resp(200, {"ok": True, "service": "wecom-push", "version": VERSION,
                               "kf_enabled": os.environ.get("KF_ENABLED") == "true"})
        if method != "POST":
            return _resp(405, {"ok": False, "err": "method_not_allowed"})
        data = _body(event)
        if path.startswith("/kf/"):
            if os.environ.get("KF_ENABLED") != "true":
                raise InputError("kf_not_enabled", 409)
            routes = {"/kf/accounts": kf_accounts, "/kf/sync": kf_sync, "/kf/send": kf_send}
            if path not in routes:
                return _resp(404, {"ok": False, "err": "not_found"})
            return _resp(200, routes[path](data))
        if path == "/media/upload":
            return _resp(200, upload_media(data))
        if path == "/media/download":
            return _resp(200, download_media(data))
        if path not in ("/", "/send"):
            return _resp(404, {"ok": False, "err": "not_found"})
        kind = data.get("msgtype", "text")
        if kind == "text" and isinstance(data.get("text", ""), str) and not data.get("text", "").strip():
            return _resp(400, {"ok": False, "err": "empty text"})
        result = send_wecom(data.get("text"), data.get("touser"), kind, data.get("media_id"))
        # Preserve the legacy text client's HTTP/body contract, including business failures.
        return _resp(200 if kind == "text" or result["ok"] else 502, result)
    except InputError as error:
        return _resp(error.status, {"ok": False, "err": str(error), "request_id": request_id})
    except WeComError as error:
        return _resp(502, {"ok": False, "stage": error.stage, "resp": error.response, "request_id": request_id})
    except DeliveryUnknown:
        return _resp(504, {"ok": False, "err": "delivery_unknown", "retry_safe": False, "request_id": request_id})
    except UpstreamError:
        return _resp(502, {"ok": False, "err": "upstream_unavailable", "request_id": request_id})
    except Exception:
        # Do not leak access_token, corpsecret, source URLs or attachment contents.
        print(json.dumps({"request_id": request_id, "event": "internal_error"}))
        return _resp(500, {"ok": False, "err": "internal_error", "request_id": request_id})
