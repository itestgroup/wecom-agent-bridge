import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from clients.wecom_kf import KfClient

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('kf_scf', ROOT / 'scf/index.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def message(identifier='m1', origin=3, kind='file'):
    return {'msgid': identifier, 'open_kfid': 'wk_test', 'external_userid': 'wm_charles',
            'origin': origin, 'msgtype': kind, 'send_time': 1792200000,
            kind: {'media_id': 'media-test', 'content': 'hello'}}


def page(messages=None, cursor='c1', more=False):
    return {'ok': True, 'messages': [message()] if messages is None else messages,
            'next_cursor': cursor, 'has_more': more}


class KfClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = KfClient(self.temp.name)
        self.events = [{'id': 'event1', 'open_kfid': 'wk_test', 'sync_token': 'sensitive-token', 'event_time': int(time.time())}]
        self.acks = []
        self.client._worker = self.worker

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def worker(self, path, data=None):
        if data is None:
            return {'ok': True, 'events': self.events, 'has_more': False}
        self.acks.extend(data['ids'])
        return {'ok': True, 'missing': []}

    def test_sync_durable_receipt_restart_and_dedup(self):
        self.client._scf = lambda *args: page()
        self.client.sync_once()
        self.assertEqual(self.acks, ['event1'])
        self.assertEqual(len(self.client.pending(['wm_charles'])), 1)
        self.assertEqual(self.client.pending([]), [])
        self.client.close()
        self.client = KfClient(self.temp.name)
        self.client._worker = self.worker
        self.client._scf = lambda *args: self.fail('completed callback must not refetch')
        self.client.sync_once()
        item = self.client.pending(['wm_charles'])[0]
        self.assertEqual(item['source'], 'wecom_kf')
        self.client.complete(item['id'])
        self.assertEqual(self.client.pending(['wm_charles']), [])

    def test_failure_after_first_page_keeps_position_and_no_ack(self):
        results = iter([page(cursor='c1', more=True), RuntimeError('upstream down')])
        def remote(*args):
            result = next(results)
            if isinstance(result, Exception):
                raise result
            return result
        self.client._scf = remote
        with self.assertRaises(RuntimeError):
            self.client.sync_once()
        self.assertEqual(self.acks, [])
        self.assertEqual(self.client.db.execute('SELECT cursor FROM cursors').fetchone()[0], 'c1')
        self.assertEqual(len(self.client.pending(['wm_charles'])), 1)
        def resume(path, data):
            self.assertEqual(data['cursor'], 'c1')
            return page([message('m2')], 'c2')
        self.client._scf = resume
        self.client.sync_once()
        self.assertEqual(len(self.client.pending(['wm_charles'])), 2)

    def test_bad_page_rolls_back_messages_and_cursor(self):
        self.client._scf = lambda *args: page([message(), {'msgid': 'invalid'}])
        with self.assertRaises(ValueError):
            self.client.sync_once()
        self.assertEqual(self.client.db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)
        self.assertEqual(self.client.db.execute('SELECT count(*) FROM cursors').fetchone()[0], 0)
        self.assertFalse(self.acks)

    def test_no_cursor_advance_rejected(self):
        self.client._scf = lambda *args: page(cursor='', more=True)
        with self.assertRaises(ValueError):
            self.client.sync_once()

    def test_page_budget_leaves_event_unacked(self):
        self.client._scf = lambda *args: page(cursor='c1', more=True)
        self.assertTrue(self.client.sync_once(max_pages=1)['has_more'])
        self.assertFalse(self.acks)

    def test_other_origins_and_types_retained_but_not_dispatched(self):
        self.client._scf = lambda *args: page([message(origin=5), message('m2', kind='voice')])
        self.client.sync_once()
        self.assertEqual(self.client.pending(['wm_charles']), [])
        self.assertEqual(self.client.db.execute('SELECT count(*) FROM messages').fetchone()[0], 2)

    def test_account_mismatch_rolls_back(self):
        wrong = message(); wrong['open_kfid'] = 'another'
        self.client._scf = lambda *args: page([wrong])
        with self.assertRaises(ValueError):
            self.client.sync_once()
        self.assertFalse(self.acks)

    def test_ack_failure_does_not_refetch_saved_page(self):
        self.client._scf = lambda *args: page()
        def failed(path, data=None):
            if data is not None:
                raise RuntimeError('ack timeout')
            return self.worker(path)
        self.client._worker = failed
        with self.assertRaises(RuntimeError):
            self.client.sync_once()
        self.client._worker = self.worker
        self.client._scf = lambda *args: self.fail('must retry ACK only')
        self.client.sync_once()

    def test_customer_reply_uses_separate_route(self):
        calls = []
        self.client._scf = lambda *args: calls.append(args)
        self.client.reply_customer('wk_test', 'wm_charles', 'hello')
        self.assertEqual(calls[0][0], '/kf/send')
        self.assertNotIn('touser', calls[0][1])

    def test_expired_token_omitted_and_recovery_throttled(self):
        self.events[0]['event_time'] = 1
        calls = []
        def remote(path, data):
            calls.append(data)
            return page(cursor='c1', more=True)
        self.client._scf = remote
        result = self.client.sync_once()
        self.assertEqual(result['retry_after'], 60)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['token'], '')
        self.assertFalse(self.acks)

    def test_coalesced_notifications_use_newest_token(self):
        self.events.append({'id': 'event2', 'open_kfid': 'wk_test', 'sync_token': 'new-token', 'event_time': int(time.time()) + 1})
        self.events[0]['event_time'] -= 5
        calls = []
        def remote(path, data):
            calls.append(data)
            return page()
        self.client._scf = remote
        with patch('clients.wecom_kf.time.time', return_value=time.time() + 2):
            self.client.sync_once()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['token'], 'new-token')
        self.assertEqual(set(self.acks), {'event1', 'event2'})


class KfScfTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'KF_ENABLED': 'true', 'KF_OPEN_KFID': 'wk_test',
                         'KF_ALLOWED_USER_IDS': 'wm_charles', 'PUSH_SECRET': 'test-secret'})
        self.env.start()
        self.api = patch.object(bridge, '_with_token', side_effect=lambda op: op('access-token'))
        self.api.start()

    def tearDown(self):
        self.api.stop(); self.env.stop()

    def call(self, path, data):
        result = bridge.main_handler({'path': path, 'httpMethod': 'POST',
                        'headers': {'X-Push-Secret': 'test-secret'}, 'body': json.dumps(data)}, None)
        return result['statusCode'], json.loads(result['body'])

    def test_sync_uses_authorized_account_and_passes_token(self):
        with patch.object(bridge, '_api_json', return_value={'errcode': 0, 'msg_list': [message()],
                                    'next_cursor': 'next', 'has_more': 0}) as api:
            status, result = self.call('/kf/sync', {'open_kfid': 'wk_test', 'token': 'callback-token'})
        self.assertEqual(status, 200)
        self.assertEqual(api.call_args.args[2]['token'], 'callback-token')
        self.assertEqual(api.call_args.args[0], 'kf/sync_msg')
        self.assertNotIn('token', result)

    def test_wrong_account_and_invalid_token_rejected(self):
        self.assertEqual(self.call('/kf/sync', {'open_kfid': 'other', 'token': 't'})[0], 403)
        self.assertEqual(self.call('/kf/sync', {'open_kfid': 'wk_test', 'token': 123})[0], 400)

    def test_tokenless_recovery(self):
        with patch.object(bridge, '_api_json', return_value={'msg_list': [], 'next_cursor': '', 'has_more': 0}) as api:
            self.assertEqual(self.call('/kf/sync', {'open_kfid': 'wk_test'})[0], 200)
        self.assertNotIn('token', api.call_args.args[2])

    def test_disabled_gate(self):
        with patch.dict(os.environ, {'KF_ENABLED': 'false'}):
            self.assertEqual(self.call('/kf/accounts', {})[0], 409)

    def test_accounts_returns_only_public_fields(self):
        with patch.object(bridge, '_api_json', return_value={'account_list': [
                {'open_kfid': 'wk_test', 'name': '客服', 'secret': 'not-public'}]}):
            status, result = self.call('/kf/accounts', {})
        self.assertEqual(status, 200)
        self.assertNotIn('secret', result['accounts'][0])

    def test_bad_sync_does_not_confirm(self):
        with patch.object(bridge, '_api_json', return_value={'msg_list': [message()], 'next_cursor': 'old', 'has_more': 1}):
            self.assertEqual(self.call('/kf/sync', {'open_kfid': 'wk_test', 'token': 't', 'cursor': 'old'})[0], 502)

    def test_send_requires_explicit_customer_and_uses_kf_api(self):
        data = {'open_kfid': 'wk_test', 'external_userid': 'wm_charles', 'text': '你好'}
        with patch.object(bridge, '_api_json', return_value={'errcode': 0, 'msgid': 'out'}) as api:
            self.assertEqual(self.call('/kf/send', data)[0], 200)
            self.assertEqual(api.call_args.args[0], 'kf/send_msg')
            self.assertNotIn('agentid', api.call_args.args[2])
        data['external_userid'] = 'other'
        self.assertEqual(self.call('/kf/send', data)[0], 403)
        del data['external_userid']
        self.assertEqual(self.call('/kf/send', data)[0], 400)

    def test_customer_send_timeout_remains_unknown(self):
        with patch.object(bridge, '_api_json', side_effect=bridge.DeliveryUnknown()):
            status, result = self.call('/kf/send', {'open_kfid': 'wk_test', 'external_userid': 'wm_charles', 'text': 'hello'})
        self.assertEqual(status, 504)
        self.assertFalse(result['retry_safe'])


if __name__ == '__main__':
    unittest.main()
