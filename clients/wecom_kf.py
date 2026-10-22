"""Muse WeChat Customer Service adapter. SQLite is durable local receipt, not a cache.

Environment is identical to wecom_media.Client. One persistent state directory per
deployment. Sync advances the cursor in the SAME transaction as message storage.
Notifications are ACKed only after all pages have been durably saved.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

try:
    from .wecom_media import Client
except ImportError:
    from wecom_media import Client


class KfClient(Client):
    def __init__(self, state_directory):
        super().__init__()
        directory = Path(state_directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / 'kf.sqlite3'
        self.db = sqlite3.connect(str(path), timeout=5)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS cursors (account TEXT PRIMARY KEY, cursor TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS completed_events (id TEXT PRIMARY KEY);
          CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY, account TEXT NOT NULL, raw TEXT NOT NULL,
            actionable INTEGER NOT NULL, done INTEGER NOT NULL DEFAULT 0
          );
        ''')
        scope = json.dumps([self.inbox, self.scf])
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('scope', ?)", (scope,))
            if self.db.execute("SELECT value FROM metadata WHERE key='scope'").fetchone()[0] != scope:
                raise ValueError('State directory belongs to a different deployment')

    def close(self):
        self.db.close()

    def _worker(self, path, data=None):
        return self._request(self.inbox, path, 'INBOX_SECRET', 'Authorization', data)

    def _commit_page(self, account, previous, result):
        messages, following, more = result.get('messages'), result.get('next_cursor'), result.get('has_more')
        if (not isinstance(messages, list) or len(messages) > 10 or not isinstance(following, str)
                or type(more) is not bool or ((messages or more) and (not following or following == previous))):
            raise ValueError('Invalid customer service page')
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError('Invalid customer service message')
            msgid, kind, origin = message.get('msgid'), message.get('msgtype'), message.get('origin')
            if not isinstance(msgid, str) or not msgid or not isinstance(kind, str) or type(origin) is not int:
                raise ValueError('Missing customer service message fields')
            message_account = message.get('open_kfid') or (message.get('event') or {}).get('open_kfid')
            if message_account and message_account != account:
                raise ValueError('Customer service account mismatch')
            actionable = origin == 3 and kind in ('text', 'image', 'file')
            if actionable:
                if not isinstance(message.get('external_userid'), str) or not message['external_userid']:
                    raise ValueError('Missing customer identity')
                item = message.get(kind)
                if not isinstance(item, dict) or not isinstance(item.get('content' if kind == 'text' else 'media_id'), str):
                    raise ValueError('Missing customer message content')
                if kind != 'text' and not item['media_id']:
                    raise ValueError('Missing customer media id')
            identifier = hashlib.sha256(json.dumps(['kf', account, msgid], separators=(',', ':')).encode()).hexdigest()
            # Keep events and unsupported types too: advancing the cursor does not erase them.
            self.db.execute('INSERT OR IGNORE INTO messages (id, account, raw, actionable) VALUES (?, ?, ?, ?)',
                            (identifier, account, json.dumps(message, ensure_ascii=False), int(actionable)))
        # Some empty final responses omit a new position; never rewind a saved cursor.
        self.db.execute('INSERT INTO cursors VALUES (?, ?) ON CONFLICT(account) DO UPDATE SET cursor=excluded.cursor',
                        (account, following or previous))

    def sync_once(self, max_pages=20):
        if type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise ValueError('max_pages must be 1–100')
        notices = self._worker('/kf/events')
        pages = 0
        for event in notices['events']:
            siblings = [e for e in notices['events'] if e['open_kfid'] == event['open_kfid']]
            newest = max(siblings, key=lambda e: e.get('event_time', 0))
            # Callback tokens expire after ten minutes. Stale notifications remain
            # recoverable via the documented tokenless, more strictly rate-limited API.
            fresh = 0 <= time.time() - newest.get('event_time', 0) < 540
            sync_token = newest['sync_token'] if fresh else ''
            # A transaction lock serializes cursor access across local processes.
            while pages < max_pages:
                try:
                    self.db.execute('BEGIN IMMEDIATE')
                    if self.db.execute('SELECT 1 FROM completed_events WHERE id=?', (event['id'],)).fetchone():
                        self.db.commit()
                        break
                    if not fresh:
                        throttle_key = 'tokenless:' + event['open_kfid']
                        last = self.db.execute('SELECT value FROM metadata WHERE key=?', (throttle_key,)).fetchone()
                        if last and time.time() - float(last[0]) < 60:
                            self.db.commit()
                            return {'ok': True, 'pages': pages, 'has_more': True, 'retry_after': 60}
                    row = self.db.execute('SELECT cursor FROM cursors WHERE account=?', (event['open_kfid'],)).fetchone()
                    previous = row[0] if row else ''
                    result = self._scf('/kf/sync', {'open_kfid': event['open_kfid'],
                                       'token': sync_token, 'cursor': previous})
                    self._commit_page(event['open_kfid'], previous, result)
                    if not result['has_more']:
                        for sibling in siblings:
                            self.db.execute('INSERT OR IGNORE INTO completed_events VALUES (?)', (sibling['id'],))
                    if not fresh:
                        self.db.execute('INSERT INTO metadata VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                        (throttle_key, str(time.time())))
                    self.db.commit()
                    pages += 1
                    if not result['has_more']:
                        break
                except Exception:
                    self.db.rollback()
                    raise
            done = self.db.execute('SELECT 1 FROM completed_events WHERE id=?', (event['id'],)).fetchone()
            if done:
                ack = self._worker('/kf/events/ack', {'ids': [event['id']]})
                if ack.get('missing'):
                    raise RuntimeError('Notification ACK missing')
            else:
                return {'ok': True, 'pages': pages, 'has_more': True}
        return {'ok': True, 'pages': pages, 'has_more': notices.get('has_more', False)}

    def pending(self, allowed_users, limit=50):
        """Explicitly allow customer IDs before handing tasks to a personal agent.

        Empty allowlist exposes no tasks; inspect identities() during initial setup.
        Filtered messages stay on disk for later authorization, never silently ACKed.
        """
        if not allowed_users:
            return []
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('limit must be 1–50')
        allowed = sorted(set(allowed_users))
        rows = self.db.execute('SELECT id, account, raw FROM messages WHERE actionable=1 AND done=0 ORDER BY rowid')
        result = []
        for row in rows:
            message = json.loads(row['raw'])
            if message['external_userid'] not in allowed:
                continue
            kind = message['msgtype']
            result.append({'id': row['id'], 'source': 'wecom_kf', 'open_kfid': row['account'],
                           'from': message['external_userid'], 'msgid': message['msgid'],
                           'msgtype': kind, 'time': message.get('send_time'),
                           'content': message['text']['content'] if kind == 'text' else '',
                           **({'media': message[kind]} if kind != 'text' else {})})
            if len(result) >= limit:
                break
        return result

    def identities(self):
        users = set()
        for row in self.db.execute('SELECT account, raw FROM messages WHERE actionable=1'):
            message = json.loads(row['raw'])
            users.add((row['account'], message['external_userid']))
        return [{'open_kfid': a, 'external_userid': u} for a, u in sorted(users)]

    def complete(self, identifier):
        """Only after attachment is saved and the downstream task is durably accepted."""
        with self.db:
            result = self.db.execute('UPDATE messages SET done=1 WHERE id=? AND actionable=1', (identifier,))
            if not result.rowcount:
                raise ValueError('Unknown customer message')

    def reply_customer(self, account, user, text):
        # Customer IDs never go to the internal /send route or WECOM_TOUSER.
        return self._scf('/kf/send', {'open_kfid': account, 'external_userid': user, 'text': text})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    parser.add_argument('command', choices=['sync', 'identities', 'pending'])
    args = parser.parse_args()
    client = KfClient(args.state)
    try:
        if args.command == 'sync':
            result = client.sync_once()
        elif args.command == 'identities':
            result = client.identities()
        else:
            allowed = [s.strip() for s in os.environ.get('KF_ALLOWED_USER_IDS', '').split(',') if s.strip()]
            result = client.pending(allowed)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        client.close()


if __name__ == '__main__':
    main()
