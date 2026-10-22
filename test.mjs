// Local contract tests using real SQLite and synthetic WeCom ciphertext.
// Does not replace a Cloudflare + WeCom live acceptance test.
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import { createCipheriv, createHash, randomBytes } from 'node:crypto';
import worker from './worker.mjs';

const sqlite = new DatabaseSync(':memory:');
sqlite.exec(readFileSync(new URL('./schema.sql', import.meta.url), 'utf8'));
let fail = false;
const DB = {
  async batch(statements) {
    sqlite.exec('BEGIN');
    try {
      const results = [];
      for (const statement of statements) results.push(await statement.all());
      sqlite.exec('COMMIT'); return results;
    } catch (error) { sqlite.exec('ROLLBACK'); throw error; }
  },
  prepare(sql) {
    const statement = sqlite.prepare(sql);
    return { bind(...values) {
      return {
        async all() { if (fail) throw new Error('test storage failure'); return { success: true, results: statement.all(...values) }; },
        async run() { if (fail) throw new Error('test storage failure'); const r = statement.run(...values); return { success: true, meta: { changes: Number(r.changes) } }; },
      };
    } };
  },
};
const key = randomBytes(32), corp = 'ww_test_corporation';
const env = { DB, WECOM_TOKEN: 'testtoken', WECOM_AES_KEY: key.toString('base64').slice(0, -1),
  WECOM_CORP_ID: corp, INBOX_SECRET: randomBytes(32).toString('hex'), WECOM_AGENT_ID: '1000002' };
const auth = { Authorization: `Bearer ${env.INBOX_SECRET}` };
const call = (path, init = {}, config = env) => worker.fetch(new Request(`https://example.invalid${path}`, init), config);
const digest = (alg, s) => createHash(alg).update(s).digest('hex');
function encrypt(message, receiver = corp, corruptPad = false) {
  const text = Buffer.from(message), size = Buffer.alloc(4); size.writeUInt32BE(text.length);
  const raw = Buffer.concat([randomBytes(16), size, text, Buffer.from(receiver)]);
  const pad = 32 - raw.length % 32, padding = Buffer.alloc(pad, pad);
  if (corruptPad && pad > 1) padding[0] ^= 1;
  const c = createCipheriv('aes-256-cbc', key, key.subarray(0, 16)); c.setAutoPadding(false);
  return { ciphertext: Buffer.concat([c.update(Buffer.concat([raw, padding])), c.final()]).toString('base64'), pad };
}
function query(ciphertext) {
  const timestamp = String(Math.floor(Date.now() / 1000)), nonce = '123';
  return new URLSearchParams({ timestamp, nonce, msg_signature: digest('sha1', [env.WECOM_TOKEN, timestamp, nonce, ciphertext].sort().join('')) });
}
const xml = (id, content, type = 'text') => `<xml><ToUserName><![CDATA[${corp}]]></ToUserName><FromUserName><![CDATA[Charles]]></FromUserName><CreateTime>1792200000</CreateTime><MsgType>${type}</MsgType><Content>${content}</Content><MsgId>${id}</MsgId><AgentID>1000002</AgentID></xml>`;
async function callback(message, config = env, receiver = corp, corruptPad = false) {
  const { ciphertext } = encrypt(message, receiver, corruptPad);
  return call(`/wecom?${query(ciphertext)}`, { method: 'POST', body: `<xml><Encrypt><![CDATA[${ciphertext}]]></Encrypt></xml>` }, config);
}
const read = async (limit = 50) => {
  const response = await call(`/inbox?limit=${limit}`, { headers: auth });
  assert.equal(response.status, 200); return response.json();
};
let checks = 0;
function check(condition) { assert.ok(condition); checks++; }

// All 32 padding lengths for both URL validation and callback decryption.
const pads = new Set(), callbackPads = new Set();
for (let n = 0; n < 32; n++) {
  const message = `verify-${'x'.repeat(n)}`;
  const { ciphertext, pad } = encrypt(message); pads.add(pad);
  const q = query(ciphertext); q.set('echostr', ciphertext);
  const response = await call(`/wecom?${q}`);
  check(response.status === 200 && await response.text() === message);
  const incomingXml = xml(`padding-${String(n).padStart(2, '0')}`, `<![CDATA[${'x'.repeat(n)}]]>`);
  callbackPads.add(encrypt(incomingXml).pad);
  const incoming = await callback(incomingXml);
  check(incoming.status === 200 && await incoming.text() === 'success');
}
check(pads.size === 32);
check(callbackPads.size === 32);
// Retrieval is non-destructive and batched.
let first = await read(3), second = await read(3);
check(first.messages.length === 3 && first.has_more && first.ack_required);
check(JSON.stringify(first) === JSON.stringify(second));
const ids = first.messages.map(m => m.id);
const acknowledge = () => call('/inbox/ack', { method: 'POST', headers: auth, body: JSON.stringify({ ids }) });
check((await (await acknowledge()).json()).acknowledged.length === 3);
check((await (await acknowledge()).json()).acknowledged.length === 3);
check((await read()).messages.length === 29);
// Duplicate after ACK does not resurrect or overwrite an existing row.
const row = sqlite.prepare('SELECT * FROM inbox_messages WHERE id = ?').get(ids[0]);
check((await callback(xml(row.msgid, '<![CDATA[altered replay]]>'))).status === 200);
check(sqlite.prepare('SELECT acked_at, content FROM inbox_messages WHERE id = ?').get(row.id).acked_at === row.acked_at);
check(sqlite.prepare('SELECT content FROM inbox_messages WHERE id = ?').get(row.id).content === row.content);
// Entity and split CDATA handling, Unicode, and closing-tag-looking content.
const content = '<![CDATA[中文 </Content> & ]]]]><![CDATA[>]]>&amp;&#x1F600;';
check((await callback(xml('unicode', content))).status === 200);
check((await read()).messages.some(m => m.msgid === 'unicode' && m.content === '中文 </Content> & ]]>&😀'));
// Storage errors must not ACK the callback.
fail = true;
check((await callback(xml('failed-store', 'test'))).status === 503);
fail = false;
check((await callback(xml('failed-store', 'test'))).status === 200);
check((await callback(xml('missing-db', 'test'), { ...env, DB: undefined })).status === 503);
// Authentication, method guards, invalid IDs, wrong receiver, wrong app.
check((await call('/inbox')).status === 401);
check((await call('/inbox?secret=ignored', { headers: auth })).status === 400);
check((await call('/inbox', { method: 'POST', headers: auth })).status === 405);
check((await call('/inbox', { method: 'HEAD', headers: auth })).status === 405);
check((await call('/inbox/ack', { method: 'POST', headers: auth, body: '{"ids":["invalid"]}' })).status === 400);
check((await callback(xml('wrong-corp', 'test'), env, 'wrongcorp')).status === 400);
check((await callback(xml('wrong-agent', 'test'), { ...env, WECOM_AGENT_ID: 'other' })).status === 403);
check((await callback(xml('dup-field', 'test').replace('</xml>', '<MsgId>duplicate</MsgId></xml>'))).status === 400);
check((await callback('<!DOCTYPE xml [<!ENTITY x SYSTEM "file:///etc/passwd">]>' + xml('dtd', '&x;'))).status === 400);
check((await callback(xml('padding-corrupt', 'x'.repeat(8)), env, corp, true)).status === 400);
const before = sqlite.prepare('SELECT COUNT(*) AS n FROM inbox_messages').get().n;
check((await callback(xml('filtered', 'test'), { ...env, ALLOWED_USER_IDS: 'someone-else' })).status === 200);
check((await callback(xml('event', 'test', 'event'))).status === 200);
check(sqlite.prepare('SELECT COUNT(*) AS n FROM inbox_messages').get().n === before);
const e = encrypt(xml('bad-signature', 'test')).ciphertext, q = query(e); q.set('msg_signature', '0'.repeat(40));
check((await call(`/wecom?${q}`, { method: 'POST', body: `<xml><Encrypt>${e}</Encrypt></xml>` })).status === 403);
check((await call('/wecom', { method: 'POST', body: 'x'.repeat(256 * 1024 + 1) })).status === 413);
check((await call('/health')).headers.get('Cache-Control') === 'no-store');
check((await call('/not-a-route')).status === 404);
// Additive migration is repeatable and does not alter existing text/ACK history.
const textCount = sqlite.prepare('SELECT COUNT(*) AS n FROM inbox_messages').get().n;
const migration = readFileSync(new URL('./migrations/0001_media_messages.sql', import.meta.url), 'utf8');
sqlite.exec(migration); sqlite.exec(migration);
check(sqlite.prepare('SELECT COUNT(*) AS n FROM inbox_messages').get().n === textCount);
const mediaEnv = { ...env, MEDIA_ENABLED: 'true' };
const mediaXml = (id, type, extra = '') => xml(id, '', type).replace('</xml>', `<MediaId><![CDATA[media-${id}]]></MediaId>${extra}</xml>`);
check((await callback(mediaXml('disabled', 'image'))).status === 503);
check((await call('/inbox?include_media=1', { headers: auth })).status === 409);
check((await callback(mediaXml('picture', 'image'), mediaEnv)).status === 200);
check((await callback(mediaXml('document', 'file', '<FileName><![CDATA[报告.pdf]]></FileName><FileSize>1234</FileSize>'), mediaEnv)).status === 200);
check((await read()).messages.every(m => m.msgtype === 'text' && !('media' in m)));
const mixed = await (await call('/inbox?include_media=1', { headers: auth }, mediaEnv)).json();
check(mixed.messages.some(m => m.msgtype === 'image' && m.media.media_id === 'media-picture'));
check(mixed.messages.some(m => m.msgtype === 'file' && m.media.filename === '报告.pdf' && m.media.size === 1234));
const repeated = await (await call('/inbox?include_media=1', { headers: auth }, mediaEnv)).json();
check(JSON.stringify(mixed) === JSON.stringify(repeated));
const picture = mixed.messages.find(m => m.msgid === 'picture');
const document = mixed.messages.find(m => m.msgid === 'document');
const text = mixed.messages.find(m => m.msgtype === 'text');
const mixedIds = [picture.id, document.id, text.id];
const ackMedia = () => call('/inbox/ack', { method: 'POST', headers: auth, body: JSON.stringify({ ids: mixedIds }) }, mediaEnv);
check((await (await ackMedia()).json()).acknowledged.length === 3);
check((await (await ackMedia()).json()).acknowledged.length === 3);
check((await callback(mediaXml('picture', 'image'), mediaEnv)).status === 200);
const afterMediaAck = await (await call('/inbox?include_media=1', { headers: auth }, mediaEnv)).json();
check(!afterMediaAck.messages.some(m => mixedIds.includes(m.id)));
fail = true;
check((await callback(mediaXml('media-failed-store', 'file'), mediaEnv)).status === 503);
fail = false;
check((await callback(mediaXml('media-failed-store', 'file'), mediaEnv)).status === 200);
check((await callback(mediaXml('bad-size', 'file', '<FileSize>oops</FileSize>'), mediaEnv)).status === 400);
check((await callback(xml('no-media-id', '', 'image'), mediaEnv)).status === 400);
check((await call('/inbox?include_media=oops', { headers: auth }, mediaEnv)).status === 400);
check((await (await call('/health', {}, mediaEnv)).json()).media_enabled === true);
check((await (await call('/health')).json()).media_enabled === false);
// KF events do not contain AgentID: they require a separate signed allowlist branch.
const migrationKf = readFileSync(new URL('./migrations/0002_kf_notifications.sql', import.meta.url), 'utf8');
sqlite.exec(migrationKf); sqlite.exec(migrationKf);
const kfEnv = { ...mediaEnv, KF_ENABLED: 'true', KF_OPEN_KFID: 'wk_test' };
const kfXml = (token = 'private-sync-token', account = 'wk_test', agent = '') =>
  `<xml><ToUserName>${corp}</ToUserName><CreateTime>1792200000</CreateTime><MsgType>event</MsgType><Event>kf_msg_or_event</Event><Token>${token}</Token><OpenKfId>${account}</OpenKfId>${agent}</xml>`;
check((await callback(kfXml(), kfEnv)).status === 200);
check((await callback(kfXml(), kfEnv)).status === 200);
const kfEvents = async () => (await call('/kf/events', { headers: auth }, kfEnv)).json();
let notices = await kfEvents();
check(notices.events.length === 1 && notices.events[0].sync_token === 'private-sync-token');
check((await call('/kf/events', {}, kfEnv)).status === 401);
check((await call('/kf/events', { headers: auth }, mediaEnv)).status === 409);
check((await callback(kfXml(), mediaEnv)).status === 503);
check((await callback(kfXml('other-token', 'wk_other'), kfEnv)).status === 200);
check((await kfEvents()).events.length === 1);
check((await callback(kfXml('bad-agent', 'wk_test', '<AgentID>wrong</AgentID>'), kfEnv)).status === 403);
check((await callback(kfXml('no-account'), { ...kfEnv, KF_OPEN_KFID: '' })).status === 503);
fail = true;
check((await callback(kfXml('db-fails'), kfEnv)).status === 503);
fail = false;
check((await kfEvents()).events.length === 1);
const noticeId = notices.events[0].id;
const ackNotice = () => call('/kf/events/ack', { method: 'POST', headers: auth,
  body: JSON.stringify({ ids: [noticeId] }) }, kfEnv);
check((await (await ackNotice()).json()).acknowledged[0] === noticeId);
check((await (await ackNotice()).json()).acknowledged[0] === noticeId);
check((await callback(kfXml(), kfEnv)).status === 200);
check((await kfEvents()).events.length === 0);
check((await (await call('/health', {}, kfEnv)).json()).kf_enabled === true);
check((await (await call('/health')).json()).kf_enabled === false);
check(!(await read()).messages.some(m => m.msgtype === 'event'));
// Independent JSON bot callbacks use a different key/token and an empty receive ID.
const botMigration = readFileSync(new URL('./migrations/0003_bot_messages.sql', import.meta.url), 'utf8');
sqlite.exec(botMigration); sqlite.exec(botMigration);
const botKey = randomBytes(32);
const botEnv = { ...kfEnv, BOT_ENABLED: 'true', BOT_TOKEN: 'bot-test-token',
  BOT_AES_KEY: botKey.toString('base64').slice(0, -1), BOT_ID: 'bot-test', BOT_ALLOWED_USER_IDS: 'Charles' };
const botEncrypt = (text, receiver = '') => {
  const value = Buffer.from(text), len = Buffer.alloc(4); len.writeUInt32BE(value.length);
  const raw = Buffer.concat([randomBytes(16), len, value, Buffer.from(receiver)]);
  const pad = 32 - raw.length % 32;
  const cipher = createCipheriv('aes-256-cbc', botKey, botKey.subarray(0, 16)); cipher.setAutoPadding(false);
  return Buffer.concat([cipher.update(Buffer.concat([raw, Buffer.alloc(pad, pad)])), cipher.final()]).toString('base64');
};
const botQuery = encrypt => {
  const timestamp = '1792200000', nonce = 'bot-nonce';
  return new URLSearchParams({ timestamp, nonce, msg_signature: digest('sha1', [botEnv.BOT_TOKEN, timestamp, nonce, encrypt].sort().join('')) });
};
const botCallback = (message, config = botEnv, receiver = '') => {
  const encrypt = botEncrypt(JSON.stringify(message), receiver);
  return call(`/bot?${botQuery(encrypt)}`, { method: 'POST', body: JSON.stringify({ encrypt }) }, config);
};
const botMessage = (msgid = 'bot1', extra = {}) => ({ msgid, aibotid: 'bot-test', chattype: 'single', from: { userid: 'Charles' },
  msgtype: 'text', text: { content: '机器人测试' }, ...extra });
const botPost = (path, data, config = botEnv) => call(path, { method: 'POST', headers: auth, body: JSON.stringify(data) }, config);
const botInbox = async (config = botEnv) => (await call('/bot/inbox', { headers: auth }, config)).json();
const echoCipher = botEncrypt('bot-echo'), echoParams = botQuery(echoCipher); echoParams.set('echostr', echoCipher);
check(await (await call(`/bot?${echoParams}`, {}, botEnv)).text() === 'bot-echo');
check((await call('/bot/inbox', {}, botEnv)).status === 401);
check((await call('/bot', {}, env)).status === 409);
check((await botCallback(botMessage())).status === 200);
const b1 = (await botInbox()).messages[0];
check(b1.content === '机器人测试' && b1.source === 'wecom_bot' && b1.from === 'Charles');
check((await botCallback(botMessage())).status === 200);
check((await botInbox()).messages.length === 1);
check((await botCallback(botMessage('group', { chattype: 'group' }))).status === 200);
check((await botInbox()).messages.length === 1);
check((await botCallback(botMessage('wrong-bot', { aibotid: 'wrong' }))).status === 403);
check((await botCallback(botMessage('bad-receiver'), botEnv, 'wrong')).status === 400);
const quarantined = { ...botEnv, BOT_ALLOWED_USER_IDS: '' };
check((await botCallback(botMessage('quarantine'), quarantined)).status === 200);
check((await botInbox(quarantined)).authorization_required === true);
const identities = await (await call('/bot/identities', { headers: auth }, quarantined)).json();
check(identities.identities.some(x => x.bot_id === 'bot-test' && x.from_user === 'Charles'));
fail = true;
check((await botCallback(botMessage('bot-store-fail'))).status === 503);
fail = false;
check((await botPost('/bot/reply', { id: b1.id, text: '答复' })).status === 200);
check((await botPost('/bot/reply', { id: b1.id, text: '答复' })).status === 200);
check((await botPost('/bot/reply', { id: b1.id, text: 'changed' })).status === 409);
check((await botPost('/bot/ack', { ids: [b1.id] })).status === 200);
check(!(await botInbox()).messages.some(x => x.id === b1.id));
check((await botCallback(botMessage())).status === 200);
check(!(await botInbox()).messages.some(x => x.id === b1.id));
// Verify encrypted stream response end-to-end, after ACK too.
const streamRefresh = await botCallback({ msgtype: 'stream', stream: { id: b1.id }, aibotid: 'bot-test' });
const envelope = await streamRefresh.json();
check(envelope.msgsignature === digest('sha1', [botEnv.BOT_TOKEN, envelope.timestamp, envelope.nonce, envelope.encrypt].sort().join('')));
const { createDecipheriv } = await import('node:crypto');
const decodeBot = envelope => {
  const d = createDecipheriv('aes-256-cbc', botKey, botKey.subarray(0, 16)); d.setAutoPadding(false);
  const plain = Buffer.concat([d.update(Buffer.from(envelope.encrypt, 'base64')), d.final()]);
  return JSON.parse(plain.subarray(20, 20 + plain.readUInt32BE(16)).toString());
};
check(decodeBot(envelope).stream.content === '答复' && decodeBot(envelope).stream.finish === true);
sqlite.prepare('UPDATE bot_messages SET expires_at=0 WHERE msgid=?').run('quarantine');
const expired = (await botInbox()).messages.find(x => x.msgid === 'quarantine');
check((await botPost('/bot/reply', { id: expired.id, text: 'too late' })).status === 409);
// Files have signed HTTPS URLs, never exposed as credentials to a caller-selected host.
const fileMessage = botMessage('bot-file', { msgtype: 'file', file: { url: 'https://cdn.example.com/file?secret=signed', filename: '../test.pdf' } });
check((await botCallback(fileMessage)).status === 200);
const bf = (await botInbox()).messages.find(x => x.msgid === 'bot-file');
check(bf.attachments[0].host === 'cdn.example.com' && bf.attachments[0].filename === 'test.pdf' && !JSON.stringify(bf).includes('signed'));
check((await botPost('/bot/media', { id: bf.id, index: 0 })).status === 403);
check((await botPost('/bot/media', { id: bf.id, index: -1 })).status === 400);
check((await botPost('/bot/ack', { ids: [bf.id] }, quarantined)).status === 403);
const binary = Buffer.from('%PDF-test media'), pad = 32 - binary.length % 32;
const encFile = createCipheriv('aes-256-cbc', botKey, botKey.subarray(0, 16)); encFile.setAutoPadding(false);
const cipherFile = Buffer.concat([encFile.update(Buffer.concat([binary, Buffer.alloc(pad, pad)])), encFile.final()]);
const originalFetch = globalThis.fetch;
try {
  globalThis.fetch = async (url, options) => {
    check(options.redirect === 'manual' && !options.headers.Authorization);
    return new Response(cipherFile);
  };
  const result = await (await botPost('/bot/media', { id: bf.id, index: 0, offset: 0, chunk_size: 7 }, { ...botEnv, BOT_MEDIA_HOSTS: 'cdn.example.com' })).json();
  check(result.ok && Buffer.from(result.content_base64, 'base64').equals(binary.subarray(0, 7)) && result.next_offset === 7 && !result.eof);
  check(result.file_sha256 === digest('sha256', binary));
  globalThis.fetch = async () => new Response('', { status: 302, headers: { Location: 'http://127.0.0.1/' } });
  const redirected = await botPost('/bot/media', { id: bf.id, index: 0 }, { ...botEnv, BOT_MEDIA_HOSTS: 'cdn.example.com' });
  check(redirected.status === 502);
  const redirectInfo = await redirected.json();
  check(redirectInfo.details.upstream_status === 302 && redirectInfo.details.upstream_reason === 'redirect_not_followed');
  globalThis.fetch = async () => new Response('<Error><Code>AccessDenied</Code><Message>Request has expired</Message><Resource>private-signed-url-secret</Resource></Error>', { status: 403 });
  const expiredFileMessage = { ...fileMessage, msgid: 'expired-file' };
  check((await botCallback(expiredFileMessage)).status === 200);
  const expiredFile = (await botInbox()).messages.find(x => x.msgid === 'expired-file');
  const expiryResponse = await botPost('/bot/media', { id: expiredFile.id, index: 0 }, { ...botEnv, BOT_MEDIA_HOSTS: 'cdn.example.com' });
  check(expiryResponse.status === 410);
  const expiryInfo = await expiryResponse.json();
  check(expiryInfo.err === 'bot_attachment_expired' && expiryInfo.details.upstream_status === 403);
  check(expiryInfo.details.upstream_reason === 'url_or_token_expired' && expiryInfo.details.upstream_code === 'AccessDenied');
  check(expiryInfo.details.terminal === true && expiryInfo.details.retry_safe === false && expiryInfo.details.attachment_status === 'needs_resend');
  check(!JSON.stringify(expiryInfo).includes('private-signed-url-secret'));
  check(!(await botInbox()).messages.some(x => x.id === expiredFile.id));
  const failureList = await (await call('/bot/failures', { headers: auth }, botEnv)).json();
  check(failureList.messages.some(x => x.id === expiredFile.id && x.status === 'needs_resend' && x.attachments[0].status === 'needs_resend'));
  check(sqlite.prepare('SELECT acked_at FROM bot_messages WHERE id=?').get(expiredFile.id).acked_at === null);
  globalThis.fetch = async () => { throw new Error('must not fetch an expired attachment again'); };
  check((await botPost('/bot/media', { id: expiredFile.id, index: 0 }, { ...botEnv, BOT_MEDIA_HOSTS: 'cdn.example.com' })).status === 410);
  check((await botPost('/bot/ack', { ids: [expiredFile.id] })).status === 409);
  check((await botPost('/bot/ack', { ids: [bf.id, expiredFile.id] })).status === 409);
  check(sqlite.prepare('SELECT acked_at FROM bot_messages WHERE id=?').get(bf.id).acked_at === null);
  check((await botCallback(expiredFileMessage)).status === 200);
  check(!(await botInbox()).messages.some(x => x.id === expiredFile.id));
  globalThis.fetch = async () => new Response('<Error><Code>SignatureDoesNotMatch</Code></Error>', { status: 403 });
  const signatureInfo = await (await botPost('/bot/media', { id: bf.id, index: 0 }, { ...botEnv, BOT_MEDIA_HOSTS: 'cdn.example.com' })).json();
  check(signatureInfo.details.upstream_reason === 'signature_mismatch');
  check((await botInbox()).messages.some(x => x.id === bf.id));
  globalThis.fetch = async () => new Response('<Error><Code>NoSuchKey</Code></Error>', { status: 404 });
  const missingInfo = await (await botPost('/bot/media', { id: bf.id, index: 0 }, { ...botEnv, BOT_MEDIA_HOSTS: 'cdn.example.com' })).json();
  check(missingInfo.details.upstream_reason === 'object_not_found');
  globalThis.fetch = async () => new Response(Buffer.alloc(32));
  check((await botPost('/bot/media', { id: bf.id, index: 0 }, { ...botEnv, BOT_MEDIA_HOSTS: 'cdn.example.com' })).status === 502);
} finally { globalThis.fetch = originalFetch; }
const botMixed = botMessage('mixed', { msgtype: 'mixed', mixed: { msg_item: [
  { msgtype: 'text', text: { content: '看图' } }, { msgtype: 'image', image: { url: 'https://cdn.example.com/image' } },
] } });
check((await botCallback(botMixed)).status === 200);
check((await botInbox()).messages.find(x => x.msgid === 'mixed').attachments.length === 1);
check((await botCallback(botMessage('bad-url', { msgtype: 'file', file: { url: 'http://127.0.0.1/' } }))).status === 400);
check((await (await call('/health', {}, botEnv)).json()).bot_enabled === true);
console.log(`PASS: ${checks} checks; AES padding; text/media/KF compatibility; bot callbacks and attachments; real SQLite semantics.`);
sqlite.close();
