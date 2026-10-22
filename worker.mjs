/** WeCom Agent Bridge development inbox — D1 binding: DB. No external packages.
 * Requires Node.js compatibility. Keep schema.sql in sync.
 * Secrets: WECOM_TOKEN, WECOM_AES_KEY, INBOX_SECRET.
 * Text variables: WECOM_CORP_ID; optional WECOM_AGENT_ID, ALLOWED_USER_IDS.
 * MEDIA_ENABLED=true only after running migrations/0001_media_messages.sql.
 * Consumers opt in with GET /inbox?include_media=1; old reads remain text-only.
 * GET /inbox never deletes. POST /inbox/ack only after durable local receipt.
 */
import { createDecipheriv, createHash, timingSafeEqual, randomUUID } from 'node:crypto';
import { Buffer } from 'node:buffer';
import { handleBot } from './bot.mjs';

const VERSION = '0.1.0-dev';
const MAX_BODY = 256 * 1024;
const BATCH_SIZE = 50;

class HttpError extends Error {
  constructor(status, code, details) { super(code); this.status = status; this.details = details; }
}
const bad = (code = 'invalid_message') => { throw new HttpError(400, code); };
const hash = (algorithm, data) => createHash(algorithm).update(data).digest('hex');
function equal(a, b) {
  const x = Buffer.from(a), y = Buffer.from(b);
  return x.length === y.length && timingSafeEqual(x, y);
}
function reply(value, status = 200, extra = {}) {
  const isText = typeof value === 'string';
  return new Response(isText ? value : JSON.stringify(value), {
    status,
    headers: {
      'Content-Type': isText ? 'text/plain; charset=utf-8' : 'application/json; charset=utf-8',
      'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', ...extra,
    },
  });
}
function method(request, expected) {
  return request.method === expected ? null
    : reply({ ok: false, err: 'method_not_allowed' }, 405, { Allow: expected });
}
function database(env) {
  if (!env.DB || typeof env.DB.prepare !== 'function') throw new Error('DB not bound');
  return env.DB;
}
function authorize(request, env) {
  if (typeof env.INBOX_SECRET !== 'string' || env.INBOX_SECRET.length < 32) {
    throw new Error('INBOX_SECRET must have at least 32 characters');
  }
  if (!equal(request.headers.get('Authorization') || '', `Bearer ${env.INBOX_SECRET}`)) {
    throw new HttpError(401, 'unauthorized');
  }
}
async function bodyText(request, maxBytes = MAX_BODY) {
  const declared = request.headers.get('Content-Length');
  if (declared && Number(declared) > maxBytes) throw new HttpError(413, 'body_too_large');
  if (!request.body) return '';
  const reader = request.body.getReader();
  let size = 0;
  const chunks = [];
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maxBytes) {
        await reader.cancel();
        throw new HttpError(413, 'body_too_large');
      }
      chunks.push(Buffer.from(value));
    }
    try { return new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks)); }
    catch { bad('invalid_utf8'); }
  } finally { reader.releaseLock(); }
}

// Restricted XML reader for WeCom's element/CDATA payloads, not general XML.
// Rejects DTDs, external entities, attributes and duplicate singleton fields.
// Nested elements are supported for ignored non-text events. No dependencies.
function xmlParse(input) {
  let xml = input.replace(/^\uFEFF/, '').trim();
  xml = xml.replace(/^<\?xml\s[^?]*\?>\s*/, '');
  let pos = 0, nodes = 0;
  const ws = () => { while (/\s/.test(xml[pos] || '') && pos < xml.length) pos++; };
  function entities(s) {
    return s.replace(/&([^;]*);|&/g, (full, entity) => {
      const named = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" };
      if (Object.hasOwn(named, entity)) return named[entity];
      if (!entity || !/^#(?:[0-9]+|x[0-9a-fA-F]+)$/.test(entity)) bad('invalid_xml_entity');
      const n = entity[1] === 'x' ? parseInt(entity.slice(2), 16) : Number(entity.slice(1));
      if (!(n === 9 || n === 10 || n === 13 || (n >= 32 && n <= 0xD7FF)
        || (n >= 0xE000 && n <= 0xFFFD) || (n >= 0x10000 && n <= 0x10FFFF))) bad('invalid_xml_entity');
      return String.fromCodePoint(n);
    });
  }
  function node(depth) {
    if (depth > 16 || ++nodes > 1024) bad('xml_too_complex');
    const open = /^<([A-Za-z_][A-Za-z0-9_.:-]*)(\s*\/?)>/.exec(xml.slice(pos));
    if (!open) bad('invalid_xml');
    pos += open[0].length;
    const result = { name: open[1], text: '', children: [] };
    if (open[2].trim() === '/') return result;
    for (;;) {
      const close = `</${result.name}>`;
      if (xml.startsWith(close, pos)) { pos += close.length; return result; }
      if (xml.startsWith('<![CDATA[', pos)) {
        const end = xml.indexOf(']]>', pos + 9);
        if (end < 0) bad('invalid_xml');
        result.text += xml.slice(pos + 9, end); pos = end + 3;
      } else if (xml[pos] === '<') {
        result.children.push(node(depth + 1));
      } else {
        const end = xml.indexOf('<', pos);
        if (end < 0) bad('invalid_xml');
        const text = xml.slice(pos, end);
        if (text.includes(']]>')) bad('invalid_xml');
        result.text += entities(text); pos = end;
      }
    }
  }
  const root = node(0); ws();
  if (root.name !== 'xml' || pos !== xml.length || root.text.trim()) bad('invalid_xml');
  return root;
}
function field(root, name, required = true) {
  const matches = root.children.filter(x => x.name === name);
  if (matches.length > 1 || matches[0]?.children.length) bad('invalid_xml_field');
  if (!matches.length) { if (required) bad(`missing_${name}`); return ''; }
  if (required && !matches[0].text) bad(`missing_${name}`);
  return matches[0].text;
}

function config(env) {
  const token = env.WECOM_TOKEN, aes = env.WECOM_AES_KEY, corp = env.WECOM_CORP_ID;
  if (typeof token !== 'string' || !token || typeof corp !== 'string' || !corp
    || typeof aes !== 'string' || !/^[A-Za-z0-9+/]{43}$/.test(aes)) {
    throw new Error('Invalid WeCom configuration');
  }
  const key = Buffer.from(aes + '=', 'base64');
  if (key.length !== 32) throw new Error('Invalid AES key');
  return { token, key, corp };
}
function signedCipher(url, token, ciphertext) {
  const one = name => {
    const all = url.searchParams.getAll(name);
    if (all.length !== 1 || !all[0]) bad('missing_or_duplicate_signature_parameter');
    return all[0];
  };
  const timestamp = one('timestamp'), nonce = one('nonce'), signature = one('msg_signature');
  if (!/^\d{1,12}$/.test(timestamp) || nonce.length > 256 || !/^[0-9a-f]{40}$/.test(signature)) {
    bad('invalid_signature_parameters');
  }
  const expected = hash('sha1', [token, timestamp, nonce, ciphertext].sort().join(''));
  if (!equal(signature, expected)) throw new HttpError(403, 'signature_mismatch');
}
function decrypt(ciphertext, key, corp) {
  if (!ciphertext || ciphertext.length % 4 !== 0
    || !/^[A-Za-z0-9+/]+={0,2}$/.test(ciphertext)) bad('invalid_ciphertext');
  const cipher = Buffer.from(ciphertext, 'base64');
  if (!cipher.length || cipher.length % 16 !== 0) bad('invalid_ciphertext');
  let padded;
  try {
    const decipher = createDecipheriv('aes-256-cbc', key, key.subarray(0, 16));
    decipher.setAutoPadding(false);
    padded = Buffer.concat([decipher.update(cipher), decipher.final()]);
  } catch { bad('decryption_failed'); }
  const pad = padded.at(-1);
  if (padded.length % 32 !== 0 || pad < 1 || pad > 32
    || !padded.subarray(-pad).every(x => x === pad)) bad('invalid_padding');
  const plain = padded.subarray(0, padded.length - pad);
  if (plain.length < 20) bad('invalid_plaintext');
  const length = plain.readUInt32BE(16);
  if (length > plain.length - 20 || !plain.subarray(20 + length).equals(Buffer.from(corp))) {
    bad('invalid_receiver_or_length');
  }
  try { return new TextDecoder('utf-8', { fatal: true }).decode(plain.subarray(20, 20 + length)); }
  catch { bad('invalid_utf8'); }
}
function checkD1(result) { if (!result || result.success === false) throw new Error('D1 failure'); return result; }
const mediaEnabled = env => env.MEDIA_ENABLED === 'true';
const kfEnabled = env => env.KF_ENABLED === 'true';

export default {
  async fetch(request, env) {
    const requestId = randomUUID();
    let stage = 'route';
    try {
      const url = new URL(request.url), path = url.pathname;
      if (path === '/bot' || path.startsWith('/bot/')) {
        stage = 'bot';
        return await handleBot(request, env, { reply, method, authorize, database, bodyText, bad,
          HttpError, hash, signedCipher, decrypt, checkD1 });
      }
      if (path === '/health' || (path === '/' && request.method === 'GET' && !url.searchParams.has('echostr'))) {
        const rejected = method(request, 'GET');
        return rejected || reply({ ok: true, service: 'wecom-inbox', version: VERSION, media_enabled: mediaEnabled(env), kf_enabled: kfEnabled(env), bot_enabled: env.BOT_ENABLED === 'true' });
      }
      // Dedicated opt-in queue; existing Muse pollers must not consume KF notices.
      if (path === '/kf/events' || path === '/kf/events/ack') {
        const rejected = method(request, path.endsWith('/ack') ? 'POST' : 'GET');
        if (rejected) return rejected;
        stage = 'kf_auth'; authorize(request, env);
        if (!kfEnabled(env)) throw new HttpError(409, 'kf_not_enabled');
        if (!env.KF_OPEN_KFID) throw new Error('KF_OPEN_KFID not configured');
        if (path === '/kf/events') {
          const result = checkD1(await database(env).prepare(
            'SELECT id, open_kfid, sync_token, event_time, received_at FROM kf_notifications '
            + 'WHERE acked_at IS NULL AND open_kfid = ? ORDER BY event_time DESC, received_at DESC, id LIMIT 51'
          ).bind(env.KF_OPEN_KFID).all());
          const rows = result.results || [];
          return reply({ ok: true, events: rows.slice(0, 50), has_more: rows.length > 50 });
        }
        let data;
        try { data = JSON.parse(await bodyText(request, 16 * 1024)); }
        catch (e) { if (e instanceof HttpError) throw e; bad('invalid_json'); }
        if (!Array.isArray(data?.ids) || !data.ids.length || data.ids.length > 50
          || !data.ids.every(x => typeof x === 'string' && /^[0-9a-f]{64}$/.test(x))) bad('invalid_ack_ids');
        const ids = [...new Set(data.ids)];
        const result = checkD1(await database(env).prepare(
          `UPDATE kf_notifications SET acked_at = COALESCE(acked_at, ?) WHERE open_kfid = ? AND id IN (${ids.map(() => '?').join(',')}) RETURNING id`
        ).bind(new Date().toISOString(), env.KF_OPEN_KFID, ...ids).all());
        const acknowledged = (result.results || []).map(r => r.id);
        return reply({ ok: true, acknowledged, missing: ids.filter(id => !acknowledged.includes(id)) });
      }
      if (path === '/inbox') {
        const rejected = method(request, 'GET'); if (rejected) return rejected;
        stage = 'inbox_auth'; authorize(request, env);
        if (url.searchParams.has('secret')) bad('use_authorization_header');
        const limitText = url.searchParams.get('limit') || '50';
        if (!/^[1-9]\d?$/.test(limitText) || Number(limitText) > BATCH_SIZE) bad('limit_must_be_1_to_50');
        const limit = Number(limitText);
        const includeMediaText = url.searchParams.get('include_media') || '0';
        if (!['0', '1'].includes(includeMediaText)) bad('include_media_must_be_0_or_1');
        const includeMedia = includeMediaText === '1';
        if (includeMedia && !mediaEnabled(env)) throw new HttpError(409, 'media_not_enabled');
        stage = 'inbox_read';
        const columns = 'id, msgid, from_user, msg_type, content, msg_time, received_at, agent_id';
        const query = includeMedia
          ? `SELECT ${columns}, NULL AS media_id, NULL AS filename, NULL AS file_size FROM inbox_messages WHERE acked_at IS NULL `
            + 'UNION ALL SELECT id, msgid, from_user, msg_type, \'\' AS content, msg_time, received_at, agent_id, '
            + 'media_id, filename, file_size FROM inbox_media_messages WHERE acked_at IS NULL '
            + 'ORDER BY received_at, id LIMIT ?'
          : `SELECT ${columns} FROM inbox_messages WHERE acked_at IS NULL ORDER BY received_at, id LIMIT ?`;
        const result = checkD1(await database(env).prepare(query).bind(limit + 1).all());
        const rows = result.results || [];
        const messages = rows.slice(0, limit).map(r => ({
          id: r.id, msgid: r.msgid, from: r.from_user, msgtype: r.msg_type,
          content: r.content, time: r.msg_time, received_at: r.received_at, agentid: r.agent_id,
          ...(r.media_id ? { media: { media_id: r.media_id, filename: r.filename || '', size: r.file_size ?? null } } : {}),
        }));
        return reply({ ok: true, messages, has_more: rows.length > limit, ack_required: true });
      }
      if (path === '/inbox/ack') {
        const rejected = method(request, 'POST'); if (rejected) return rejected;
        stage = 'ack_auth'; authorize(request, env);
        let data;
        try { data = JSON.parse(await bodyText(request, 16 * 1024)); }
        catch (e) { if (e instanceof HttpError) throw e; bad('invalid_json'); }
        if (!Array.isArray(data?.ids) || data.ids.length < 1 || data.ids.length > BATCH_SIZE
          || !data.ids.every(x => typeof x === 'string' && /^[0-9a-f]{64}$/.test(x))) bad('invalid_ack_ids');
        const ids = [...new Set(data.ids)];
        stage = 'ack_write';
        const db = database(env), now = new Date().toISOString();
        const statements = ['inbox_messages', ...(mediaEnabled(env) ? ['inbox_media_messages'] : [])].map(table => db.prepare(
          `UPDATE ${table} SET acked_at = COALESCE(acked_at, ?) WHERE id IN (${ids.map(() => '?').join(',')}) RETURNING id`
        ).bind(now, ...ids));
        const results = statements.length === 1 ? [await statements[0].all()] : await db.batch(statements);
        const acknowledged = [...new Set(results.flatMap(r => (checkD1(r).results || []).map(row => row.id)))];
        return reply({ ok: true, acknowledged, missing: ids.filter(id => !acknowledged.includes(id)) });
      }
      if (path !== '/' && path !== '/wecom') return reply({ ok: false, err: 'not_found' }, 404);
      if (request.method !== 'GET' && request.method !== 'POST') {
        return reply({ ok: false, err: 'method_not_allowed' }, 405, { Allow: 'GET, POST' });
      }
      stage = 'wecom_config'; const { token, key, corp } = config(env);
      if (request.method === 'GET') {
        stage = 'verify';
        if (url.searchParams.getAll('echostr').length !== 1) bad('missing_echostr');
        const echo = (url.searchParams.get('echostr') || '').replace(/ /g, '+');
        signedCipher(url, token, echo);
        return reply(decrypt(echo, key, corp));
      }
      stage = 'callback_signature';
      const outer = xmlParse(await bodyText(request));
      const ciphertext = field(outer, 'Encrypt');
      signedCipher(url, token, ciphertext);
      stage = 'callback_decrypt';
      const root = xmlParse(decrypt(ciphertext, key, corp));
      if (field(root, 'ToUserName') !== corp) bad('invalid_recipient');
      const agent = field(root, 'AgentID', false);
      const type = field(root, 'MsgType');
      // KF notifications from an authorized internal app need not contain AgentID.
      // Signature + receiver + explicit account allowlist authenticate this branch.
      if (type === 'event' && field(root, 'Event', false) === 'kf_msg_or_event') {
        stage = 'kf_notification';
        if (!kfEnabled(env)) throw new HttpError(503, 'kf_not_enabled');
        if (!env.KF_OPEN_KFID) throw new Error('KF_OPEN_KFID not configured');
        if (agent && env.WECOM_AGENT_ID && agent !== String(env.WECOM_AGENT_ID)) throw new HttpError(403, 'agent_not_allowed');
        const account = field(root, 'OpenKfId');
        if (account !== env.KF_OPEN_KFID) return reply('success');
        const token = field(root, 'Token'), timestamp = field(root, 'CreateTime');
        if (token.length > 4096 || !/^\d{1,12}$/.test(timestamp) || Number(timestamp) <= 0) bad('invalid_kf_event');
        const id = hash('sha256', JSON.stringify([corp, account, token, timestamp]));
        checkD1(await database(env).prepare(
          'INSERT INTO kf_notifications (id, open_kfid, sync_token, event_time, received_at) '
          + 'VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING'
        ).bind(id, account, token, Number(timestamp), new Date().toISOString()).run());
        console.log(JSON.stringify({ request_id: requestId, event: 'kf_notification_stored' }));
        return reply('success');
      }
      if (env.WECOM_AGENT_ID && agent !== String(env.WECOM_AGENT_ID)) throw new HttpError(403, 'agent_not_allowed');
      // Events and unsupported message types are deliberately ignored.
      if (!['text', 'image', 'file'].includes(type)) {
        console.log(JSON.stringify({ request_id: requestId, event: 'ignored_non_text' }));
        return reply('success');
      }
      // Never ACK supported media before storage has been explicitly enabled.
      if (type !== 'text' && !mediaEnabled(env)) throw new HttpError(503, 'media_not_enabled');
      const from = field(root, 'FromUserName');
      const allowed = String(env.ALLOWED_USER_IDS || '').split(',').map(s => s.trim()).filter(Boolean);
      if (allowed.length && !allowed.includes(from)) {
        console.log(JSON.stringify({ request_id: requestId, event: 'ignored_user' }));
        return reply('success');
      }
      const msgid = field(root, 'MsgId'), timeText = field(root, 'CreateTime');
      const time = Number(timeText);
      if (!/^\d{1,12}$/.test(timeText) || !Number.isSafeInteger(time) || time <= 0
        || msgid.length > 256 || from.length > 256 || agent.length > 128) bad('invalid_message_fields');
      const content = field(root, 'Content', false);
      const id = hash('sha256', JSON.stringify([corp, agent, msgid]));
      stage = 'callback_store';
      if (type !== 'text') {
        const mediaId = field(root, 'MediaId');
        const filename = field(root, 'FileName', false);
        const sizeText = field(root, 'FileSize', false);
        if (mediaId.length > 1024 || filename.length > 512 || (sizeText && !/^\d{1,10}$/.test(sizeText))) bad('invalid_media_fields');
        const result = checkD1(await database(env).prepare(
          'INSERT INTO inbox_media_messages (id, msgid, corp_id, agent_id, from_user, msg_type, media_id, filename, file_size, msg_time, received_at) '
          + 'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING'
        ).bind(id, msgid, corp, agent, from, type, mediaId, filename, sizeText ? Number(sizeText) : null, time, new Date().toISOString()).run());
        console.log(JSON.stringify({ request_id: requestId, event: result.meta?.changes ? 'media_stored' : 'duplicate' }));
        return reply('success');
      }
      // Keep acknowledged rows: duplicate delivery cannot resurrect a message.
      const result = checkD1(await database(env).prepare(
        'INSERT INTO inbox_messages (id, msgid, corp_id, agent_id, from_user, msg_type, content, msg_time, received_at) '
        + 'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING'
      ).bind(id, msgid, corp, agent, from, type, content, time, new Date().toISOString()).run());
      console.log(JSON.stringify({ request_id: requestId, event: result.meta?.changes ? 'stored' : 'duplicate' }));
      return reply('success');
    } catch (error) {
      const status = error instanceof HttpError ? error.status : 503;
      const details = error instanceof HttpError ? error.details : undefined;
      console.error(JSON.stringify({ request_id: requestId, stage, status, error: error instanceof HttpError ? error.message : 'internal_error', details }));
      return reply({ ok: false, err: error instanceof HttpError ? error.message : 'service_unavailable', request_id: requestId,
        ...(details ? { details } : {}) },
        status, status === 503 ? { 'Retry-After': '3' } : {});
    }
  },
};
