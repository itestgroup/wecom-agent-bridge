/** Dedicated URL-callback bot. No application/SCF secrets or response_url forwarding. */
import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto';
import { Buffer } from 'node:buffer';

const FILE_LIMIT = 8 * 1024 * 1024;
const CHUNK_LIMIT = 512 * 1024;
const list = value => String(value || '').split(',').map(x => x.trim()).filter(Boolean);

export async function handleBot(request, env, h) {
  const { reply, method, authorize, database, bodyText, bad, HttpError, hash, signedCipher, decrypt, checkD1 } = h;
  const path = new URL(request.url).pathname;
  if (env.BOT_ENABLED !== 'true') throw new HttpError(409, 'bot_not_enabled');
  const configuredKey = () => {
    if (typeof env.BOT_TOKEN !== 'string' || !env.BOT_TOKEN || !/^[A-Za-z0-9+/]{43}$/.test(env.BOT_AES_KEY || '')) {
      throw new Error('Bot secrets not configured');
    }
    return Buffer.from(env.BOT_AES_KEY + '=', 'base64');
  };
  const json = async () => {
    try { return JSON.parse(await bodyText(request)); }
    catch (e) { if (e instanceof HttpError) throw e; bad('invalid_json'); }
  };
  const str = (value, name, max = 1024, empty = false) => {
    if (typeof value !== 'string' || value.length > max || (!empty && !value)) bad('invalid_' + name);
    return value;
  };
  const identifier = value => {
    if (typeof value !== 'string' || !/^[0-9a-f]{64}$/.test(value)) bad('invalid_bot_id');
    return value;
  };
  const rows = async (sql, ...values) => (checkD1(await database(env).prepare(sql).bind(...values).all()).results || []);
  const allowed = () => list(env.BOT_ALLOWED_USER_IDS);
  const permitted = row => allowed().includes(row.from_user) && (!env.BOT_ID || row.bot_id === env.BOT_ID);
  const getMessage = async id => {
    const row = (await rows('SELECT * FROM bot_messages WHERE id = ?', identifier(id)))[0];
    if (!row) throw new HttpError(404, 'bot_message_not_found');
    if (!permitted(row)) throw new HttpError(403, 'bot_user_not_allowed');
    return row;
  };
  const failedMedia = row => JSON.parse(row.attachments).some(a => a.status === 'needs_resend');
  const expiredError = details => new HttpError(410, 'bot_attachment_expired', {
    ...details, attachment_status: 'needs_resend', terminal: true, retry_safe: false, action: 'resend',
  });

  if (path !== '/bot') {
    // Only the callback is public. Operator endpoints use existing inbox auth.
    authorize(request, env);
    if (path === '/bot/identities') {
      const rejected = method(request, 'GET'); if (rejected) return rejected;
      const identities = await rows('SELECT DISTINCT bot_id, from_user FROM bot_messages ORDER BY bot_id, from_user LIMIT 100');
      return reply({ ok: true, identities });
    }
    if (path === '/bot/inbox' || path === '/bot/failures') {
      const rejected = method(request, 'GET'); if (rejected) return rejected;
      const users = allowed();
      if (!users.length) return reply({ ok: true, messages: [], has_more: false, authorization_required: true });
      const failures = path === '/bot/failures';
      const expiredClause = "EXISTS (SELECT 1 FROM json_each(bot_messages.attachments) AS a WHERE json_extract(a.value, '$.status') = 'needs_resend')";
      const messages = await rows(`SELECT * FROM bot_messages WHERE ${failures ? expiredClause : 'acked_at IS NULL AND NOT ' + expiredClause} AND from_user IN (${users.map(() => '?').join(',')})`
        + (env.BOT_ID ? ' AND bot_id = ?' : '') + ' ORDER BY received_at, id LIMIT 51', ...users, ...(env.BOT_ID ? [env.BOT_ID] : []));
      return reply({ ok: true, has_more: messages.length > 50, messages: messages.slice(0, 50).map(row => ({
        id: row.id, source: 'wecom_bot', bot_id: row.bot_id, msgid: row.msgid, from: row.from_user,
        chat_id: row.chat_id, msgtype: row.msg_type, content: row.content, received_at: row.received_at,
        reply_deadline: row.expires_at, replied: row.reply_content !== null,
        status: failedMedia(row) ? 'needs_resend' : 'pending',
        attachments: JSON.parse(row.attachments).map((a, index) => ({ index, kind: a.kind, filename: a.filename,
          host: new URL(a.url).hostname, status: a.status || 'pending',
          ...(a.expired_at ? { expired_at: a.expired_at, reason: 'url_or_token_expired' } : {}) })),
      })) });
    }
    const rejected = method(request, 'POST'); if (rejected) return rejected;
    const data = await json();
    if (path === '/bot/ack') {
      const ids = data?.ids;
      if (!Array.isArray(ids) || !ids.length || ids.length > 50) bad('invalid_ack_ids');
      const unique = [...new Set(ids.map(identifier))];
      // Check every identity before the single atomic UPDATE.
      for (const id of unique) {
        if (failedMedia(await getMessage(id))) throw new HttpError(409, 'bot_attachment_requires_resend');
      }
      const result = await rows(`UPDATE bot_messages SET acked_at = COALESCE(acked_at, ?) WHERE id IN (${unique.map(() => '?').join(',')})`
        + ` AND NOT EXISTS (SELECT 1 FROM bot_messages AS b, json_each(b.attachments) AS a WHERE b.id IN (${unique.map(() => '?').join(',')})`
        + " AND json_extract(a.value, '$.status') = 'needs_resend') RETURNING id",
        new Date().toISOString(), ...unique, ...unique);
      if (result.length !== unique.length) throw new HttpError(409, 'bot_attachment_requires_resend');
      return reply({ ok: true, acknowledged: result.map(r => r.id) });
    }
    if (path === '/bot/reply') {
      const row = await getMessage(data?.id);
      const content = str(data?.text, 'reply_text', 16000);
      if (Buffer.byteLength(content) > 16000) bad('reply_too_large');
      if (row.reply_content !== null) {
        if (row.reply_content !== content) throw new HttpError(409, 'reply_already_final');
        return reply({ ok: true, stored: true, delivery_confirmed: false });
      }
      if (Date.now() > row.expires_at) throw new HttpError(409, 'bot_reply_window_expired');
      const changed = await rows('UPDATE bot_messages SET reply_content=?, replied_at=? WHERE id=? AND reply_content IS NULL RETURNING id',
        content, new Date().toISOString(), row.id);
      if (!changed.length) {
        const current = await getMessage(row.id);
        if (current.reply_content !== content) throw new HttpError(409, 'reply_already_final');
      }
      return reply({ ok: true, stored: true, delivery_confirmed: false });
    }
    if (path === '/bot/media') {
      const row = await getMessage(data?.id);
      const attachments = JSON.parse(row.attachments);
      if (!Number.isInteger(data.index) || data.index < 0 || data.index >= attachments.length) bad('invalid_attachment_index');
      const attachment = attachments[data.index];
      if (attachment.status === 'needs_resend') throw expiredError();
      const offset = data.offset ?? 0, count = data.chunk_size ?? CHUNK_LIMIT;
      if (!Number.isSafeInteger(offset) || offset < 0 || offset > FILE_LIMIT
        || !Number.isInteger(count) || count < 1 || count > CHUNK_LIMIT) bad('invalid_chunk_boundaries');
      const url = new URL(attachment.url);
      // Exact operator-configured Tencent CDN hosts only. No redirects or credentials.
      if (!list(env.BOT_MEDIA_HOSTS).includes(url.hostname)) throw new HttpError(403, 'bot_media_host_not_allowed');
      const response = await fetch(url, { redirect: 'manual', signal: AbortSignal.timeout(20000), headers: { 'Accept-Encoding': 'identity' } });
      if (response.status !== 200) {
        const details = await mediaFailure(response, url);
        if (response.status === 403 && details.upstream_reason === 'url_or_token_expired'
          && ['AccessDenied', 'ExpiredToken', 'RequestExpired'].includes(details.upstream_code)) {
          // JSON_SET updates only this attachment, preserving concurrent sibling changes.
          // Do not ACK/delete: this is a terminal failed receipt, not a successful download.
          checkD1(await database(env).prepare(
            "UPDATE bot_messages SET attachments = json_set(attachments, ?, 'needs_resend', ?, ?) WHERE id = ?"
          ).bind(`$[${data.index}].status`, `$[${data.index}].expired_at`, new Date().toISOString(), row.id).run());
          throw expiredError(details);
        }
        throw new HttpError(502, 'bot_media_http_error', details);
      }
      const encrypted = Buffer.from(await bodyTextBinary(response, FILE_LIMIT + 32, HttpError));
      const keyText = attachment.aeskey || env.BOT_AES_KEY;
      if (typeof keyText !== 'string' || !/^[A-Za-z0-9+/]{43}=?$/.test(keyText)) throw new HttpError(503, 'bot_media_key_not_configured');
      const key = Buffer.from(keyText.replace(/=$/, '') + '=', 'base64');
      if (!encrypted.length || encrypted.length % 16) throw new HttpError(502, 'invalid_media_ciphertext');
      const decipher = createDecipheriv('aes-256-cbc', key, key.subarray(0, 16));
      decipher.setAutoPadding(false);
      const padded = Buffer.concat([decipher.update(encrypted), decipher.final()]);
      const pad = padded.at(-1);
      if (pad < 1 || pad > 32 || pad > padded.length || !padded.subarray(-pad).every(b => b === pad)) throw new HttpError(502, 'invalid_media_padding');
      const file = padded.subarray(0, padded.length - pad);
      if (file.length > FILE_LIMIT) throw new HttpError(413, 'bot_media_too_large');
      if (offset > file.length) throw new HttpError(416, 'offset_out_of_range');
      const chunk = file.subarray(offset, offset + count);
      return reply({ ok: true, media_id: `${row.id}:${data.index}`, filename: attachment.filename,
        size: file.length, offset, next_offset: offset + chunk.length, eof: offset + chunk.length === file.length,
        content_base64: chunk.toString('base64'), chunk_sha256: hash('sha256', chunk), file_sha256: hash('sha256', file) });
    }
    return reply({ ok: false, err: 'not_found' }, 404);
  }

  const key = configuredKey();
  const url = new URL(request.url), receiveId = env.BOT_RECEIVE_ID || '';
  const encryptedReply = value => {
    const message = Buffer.from(JSON.stringify(value)), length = Buffer.alloc(4);
    length.writeUInt32BE(message.length);
    const raw = Buffer.concat([randomBytes(16), length, message, Buffer.from(receiveId)]);
    const pad = 32 - raw.length % 32;
    const cipher = createCipheriv('aes-256-cbc', key, key.subarray(0, 16)); cipher.setAutoPadding(false);
    const encrypt = Buffer.concat([cipher.update(Buffer.concat([raw, Buffer.alloc(pad, pad)])), cipher.final()]).toString('base64');
    const timestamp = String(Math.floor(Date.now() / 1000)), nonce = randomBytes(12).toString('hex');
    return reply(JSON.stringify({ encrypt, msgsignature: hash('sha1', [env.BOT_TOKEN, timestamp, nonce, encrypt].sort().join('')), timestamp, nonce }));
  };
  if (request.method === 'GET') {
    if (url.searchParams.getAll('echostr').length !== 1) bad('missing_echostr');
    const echo = (url.searchParams.get('echostr') || '').replace(/ /g, '+');
    signedCipher(url, env.BOT_TOKEN, echo);
    return reply(decrypt(echo, key, receiveId));
  }
  const rejected = method(request, 'POST'); if (rejected) return rejected;
  const outer = await json();
  if (outer?.encrypt && outer?.Encrypt) bad('ambiguous_ciphertext');
  const ciphertext = str(outer?.encrypt ?? outer?.Encrypt, 'ciphertext', 256 * 1024);
  signedCipher(url, env.BOT_TOKEN, ciphertext);
  let message;
  try { message = JSON.parse(decrypt(ciphertext, key, receiveId)); }
  catch (e) { if (e instanceof HttpError) throw e; bad('invalid_bot_json'); }
  const stream = row => ({ msgtype: 'stream', stream: { id: row.id, finish: row.reply_content !== null || Date.now() > row.expires_at,
    content: row.reply_content ?? (Date.now() > row.expires_at ? '本次回复窗口已结束，请重新发起查询。' : '已收到，等待 Muse 处理。') } });
  if (message?.msgtype === 'stream') {
    const id = identifier(message.stream?.id);
    const row = (await rows('SELECT * FROM bot_messages WHERE id = ?', id))[0];
    // Authenticated refreshes must not leak another bot's stream.
    if (!row || (message.aibotid && message.aibotid !== row.bot_id)) return encryptedReply({});
    if (!permitted(row)) return encryptedReply({ msgtype: 'stream', stream: { id, finish: true, content: '此用户尚未授权，请联系管理员。' } });
    return encryptedReply(stream(row));
  }
  if (!['text', 'image', 'file', 'mixed'].includes(message?.msgtype)) return encryptedReply({});
  // Initial release is personal / single-chat only; no implicit group ingestion.
  if (message.chattype !== 'single') return encryptedReply({});
  const botId = str(message.aibotid, 'aibotid', 256), user = str(message.from?.userid, 'userid', 256);
  if (env.BOT_ID && botId !== env.BOT_ID) throw new HttpError(403, 'bot_identity_mismatch');
  const msgid = str(message.msgid, 'msgid', 512);
  const attachments = [], text = [];
  const parseItem = item => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) bad('invalid_message_item');
    if (item.msgtype === 'text') text.push(str(item.text?.content, 'text', 16000, true));
    else if (['image', 'file'].includes(item.msgtype)) {
      const asset = item[item.msgtype], rawUrl = str(asset?.url, 'media_url', 8192);
      let u;
      try { u = new URL(rawUrl); } catch { bad('invalid_media_url'); }
      if (u.protocol !== 'https:' || u.username || u.password || u.hash || (u.port && u.port !== '443')) bad('invalid_media_url');
      let filename = asset.filename ?? asset.name ?? '';
      filename = str(filename, 'filename', 512, true).replace(/\\/g, '/').split('/').at(-1);
      const aeskey = asset.aeskey ?? '';
      if (aeskey && (typeof aeskey !== 'string' || !/^[A-Za-z0-9+/]{43}=?$/.test(aeskey))) bad('invalid_media_key');
      attachments.push({ kind: item.msgtype, url: u.href, filename, aeskey });
    } else bad('unsupported_mixed_item');
  };
  if (message.msgtype === 'mixed') {
    if (!Array.isArray(message.mixed?.msg_item) || !message.mixed.msg_item.length || message.mixed.msg_item.length > 20) bad('invalid_mixed_message');
    for (const item of message.mixed.msg_item) parseItem(item);
  } else parseItem(message);
  if (text.join('\n').length > 16000) bad('text_too_large');
  const id = hash('sha256', JSON.stringify(['bot', botId, msgid]));
  checkD1(await database(env).prepare('INSERT INTO bot_messages '
    + '(id, bot_id, msgid, from_user, chat_id, msg_type, content, attachments, received_at, expires_at) '
    + 'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING')
    .bind(id, botId, msgid, user, user, message.msgtype, text.join('\n'), JSON.stringify(attachments), new Date().toISOString(), Date.now() + 5 * 60 * 1000).run());
  const row = (await rows('SELECT * FROM bot_messages WHERE id = ?', id))[0];
  if (!permitted(row)) return encryptedReply({ msgtype: 'text', text: { content: '此用户尚未授权，请联系管理员。' } });
  return encryptedReply(stream(row));
}

// Never return/log raw COS XML: it can echo the signed URL and credential fields.
async function mediaFailure(response, url) {
  const details = { upstream_status: response.status, upstream_reason: 'http_error', host: url.hostname };
  const reader = response.body?.getReader();
  const chunks = []; let size = 0;
  if (reader) {
    try {
      while (size < 4096) {
        const { done, value } = await reader.read(); if (done) break;
        const part = value.subarray(0, 4096 - size);
        chunks.push(Buffer.from(part)); size += part.length;
      }
    } catch { /* Diagnostics must not replace the original upstream failure. */ }
    finally { try { await reader.cancel(); } catch { /* ignore */ } reader.releaseLock(); }
  }
  const body = Buffer.concat(chunks).toString('utf8');
  const code = /<Code>\s*([A-Za-z][A-Za-z0-9_.-]{0,63})\s*<\/Code>/.exec(body)?.[1];
  const known = new Set(['AccessDenied', 'SignatureDoesNotMatch', 'NoSuchKey', 'NoSuchBucket',
    'InvalidAccessKeyId', 'ExpiredToken', 'RequestExpired', 'InvalidToken', 'RequestTimeTooSkewed', 'SlowDown', 'InternalError', 'BadDigest']);
  if (code && known.has(code)) details.upstream_code = code;
  if (/Request has expired|RequestExpired|ExpiredToken/i.test(body)) details.upstream_reason = 'url_or_token_expired';
  else if (/SignatureDoesNotMatch|signature.*does not match/i.test(body)) details.upstream_reason = 'signature_mismatch';
  else if (/RequestTimeTooSkewed/i.test(body)) details.upstream_reason = 'clock_skew';
  else if (response.status >= 300 && response.status < 400) {
    details.upstream_reason = 'redirect_not_followed';
    try { details.redirect_host = new URL(response.headers.get('Location'), url).hostname; } catch { /* absent */ }
  } else if (response.status === 403) details.upstream_reason = 'access_denied';
  else if (response.status === 404) details.upstream_reason = 'object_not_found';
  else if (response.status === 429 || response.status === 503) details.upstream_reason = 'upstream_throttled_or_unavailable';
  // Expiry timestamps are safe to report; query keys, signatures and tokens are not.
  const period = url.searchParams.get('q-sign-time');
  if (period && /^\d{1,12};\d{1,12}$/.test(period)) details.signature_expires_at = Number(period.split(';')[1]);
  return details;
}

async function bodyTextBinary(response, limit, HttpError) {
  const length = response.headers.get('Content-Length');
  if (length && (!/^\d+$/.test(length) || Number(length) > limit)) {
    await response.body?.cancel(); throw new HttpError(413, 'bot_media_too_large');
  }
  const reader = response.body?.getReader();
  if (!reader) throw new HttpError(502, 'empty_media_response');
  const chunks = []; let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read(); if (done) break;
      size += value.length;
      if (size > limit) { await reader.cancel(); throw new HttpError(413, 'bot_media_too_large'); }
      chunks.push(Buffer.from(value));
    }
  } finally { reader.releaseLock(); }
  if (length && size !== Number(length)) throw new HttpError(502, 'truncated_media_response');
  return Buffer.concat(chunks);
}
