"""Hosted-live security checks invoke handlers in process; no sockets or network."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import Message
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from papertrail import llm, store
from papertrail.live import Access, token_hash
from papertrail.web.app import make_server
from test_cli import fake_search, fake_chat
from test_web import multipart, ROOT, FIXTURES, PL


class HostedLiveTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        env = patch.dict(os.environ, {'JUDGE_CODE': 'private-judge-code',
                         'NEBIUS_API_KEY': 'private-model-key', 'TAVILY_API_KEY': 'private-search-key',
                         'LIVE_DAILY_CAP': '300'})
        env.start()
        self.addCleanup(env.stop)
        for target in ('papertrail.llm.http_transport', 'papertrail.verify.http_transport'):
            guard = patch(target, side_effect=AssertionError('network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)
        self.cookie = None
        self.start()

    def start(self, demo=False):
        replay = llm.replay_transport([FIXTURES / f'{PL}.ocr.json', FIXTURES / f'{PL}.extract.json'])
        with patch('papertrail.web.app.ThreadingHTTPServer', side_effect=lambda addr, handler: handler):
            self.handler = make_server('', 0, db_path=self.data / 'legacy.sqlite3', data_dir=self.data,
                                       transport=replay, search=fake_search, chat=fake_chat, demo=demo)

    def request(self, method, path, body=b'', headers=None, cookie=True):
        handler = self.handler.__new__(self.handler)
        handler.path = path
        handler.client_address = ('192.0.2.1', 1234)
        handler.headers = Message()
        for key, value in {'Content-Length': str(len(body)), **(headers or {})}.items():
            handler.headers[key] = value
        if cookie and self.cookie:
            handler.headers['Cookie'] = self.cookie
        handler.rfile = BytesIO(body)
        handler.wfile = BytesIO()
        handler.request_version = 'HTTP/1.1'
        handler.requestline = method + ' ' + path
        handler.command = method
        getattr(handler, 'do_' + method)()
        raw_headers, raw = handler.wfile.getvalue().split(b'\r\n\r\n', 1)
        lines = raw_headers.decode().split('\r\n')
        code = int(lines[0].split()[1])
        response_headers = dict(line.split(': ', 1) for line in lines[1:])
        if 'Set-Cookie' in response_headers:
            self.cookie = response_headers['Set-Cookie'].split(';')[0]
        try:
            data = json.loads(raw)
        except ValueError:
            data = raw
        return code, data, response_headers

    def unlock(self, code='private-judge-code', headers=None):
        return self.request('POST', '/api/unlock', json.dumps({'code': code}).encode(), headers)

    def upload(self):
        body, headers = multipart((ROOT / f'samples/pl/{PL}.jpg').read_bytes(), draft='0')
        return self.request('POST', '/api/letters', body, headers)

    def poll(self, job_id):
        for _ in range(300):
            status, job, _ = self.request('GET', '/api/jobs/' + job_id)
            self.assertEqual(200, status)
            if job['status'] != 'running':
                self.assertEqual('done', job['status'], job)
                return job
            time.sleep(.01)
        self.fail('worker did not finish')

    def test_locked_wrong_missing_and_config(self):
        self.assertEqual((401, {'error': 'live_locked'}), self.upload()[:2])
        self.assertEqual(401, self.unlock('wrong')[0])
        self.assertEqual(401, self.unlock(None)[0])
        status, data, _ = self.request('GET', '/api/config')
        self.assertEqual((200, {'live_available': True, 'live_unlocked': False}), (status, data))
        for name in ('JUDGE_CODE', 'NEBIUS_API_KEY', 'TAVILY_API_KEY'):
            self.assertNotIn(name, json.dumps(data))
            self.assertNotIn(os.environ[name], json.dumps(data))

    def test_rate_limit_last_forwarded_ip_and_restart(self):
        for _ in range(10):
            self.assertEqual(401, self.unlock('bad', {'X-Forwarded-For': '198.51.100.1, 10.0.0.1'})[0])
        self.start()
        # Rotating the client-controlled first hop does not evade the limit.
        self.assertEqual(429, self.unlock('bad', {'X-Forwarded-For': '198.51.100.9, 10.0.0.1'})[0])
        self.assertEqual(200, self.unlock(headers={'X-Forwarded-For': '198.51.100.1, 10.0.0.2'})[0])
        with patch.object(Access, 'timestamp', return_value=Access.timestamp() + 3601):
            self.assertEqual(401, self.unlock('bad', {'X-Forwarded-For': '10.0.0.1'})[0])

    def test_cookie_live_clients_restart_and_private_memory(self):
        code, _, headers = self.unlock(headers={'X-Forwarded-Proto': 'https'})
        self.assertEqual(200, code)
        for flag in ('HttpOnly', 'SameSite=Lax', 'Secure', 'Max-Age=2592000', 'Path=/'):
            self.assertIn(flag, headers['Set-Cookie'])
        self.assertNotIn(os.environ['JUDGE_CODE'], self.cookie)
        first_cookie = self.cookie
        status, data, _ = self.upload()
        self.assertEqual(202, status)
        job = self.poll(data['job_id'])
        number = job['result']['case']['case_number']
        self.start()
        self.assertTrue(self.request('GET', '/api/config')[1]['live_unlocked'])
        self.assertEqual(1, len(self.request('GET', '/api/cases')[1]))
        self.assertEqual(200, self.request('GET', '/api/cases/' + number)[0])
        self.assertEqual(200, self.unlock()[0])
        self.assertEqual([], self.request('GET', '/api/cases')[1])
        self.assertEqual(404, self.request('GET', '/api/cases/' + number)[0])
        second = self.poll(self.upload()[1]['job_id'])
        self.assertTrue(second['result']['store']['created'])
        self.assertEqual(200, self.request('DELETE', '/api/cases')[0])
        self.assertEqual([], self.request('GET', '/api/cases')[1])
        self.cookie = first_cookie
        self.assertEqual(1, len(self.request('GET', '/api/cases')[1]))
        with store.Store(next((self.data / 'cases').glob('*.sqlite3'))) as memory:
            rows = memory.conn.execute('SELECT ocr_text FROM letters').fetchall()
            for row in rows:
                self.assertRegex(row[0], r'^[a-f0-9]{64}$')
        self.assertEqual({'live.sqlite3', 'cases'}, {p.name for p in self.data.iterdir()})

    def test_jobs_and_calendar_require_owner(self):
        self.unlock()
        job_id = self.upload()[1]['job_id']
        self.poll(job_id)
        self.assertEqual(404, self.request('GET', '/api/jobs/' + job_id, cookie=False)[0])
        self.unlock()
        for suffix in ('', '/ics'):
            self.assertEqual(404, self.request('GET', '/api/jobs/' + job_id + suffix)[0])
        self.assertEqual(401, self.request('DELETE', '/api/cases', cookie=False)[0])

    def test_daily_cap_total_persisted_and_replay_open(self):
        with patch.dict(os.environ, {'LIVE_DAILY_CAP': '1'}):
            self.start()
            self.unlock()
            self.poll(self.upload()[1]['job_id'])
            self.start()
            self.unlock()
            self.assertEqual((429, {'error': 'live_daily_cap'}), self.upload()[:2])
            self.cookie = None
            status, data, _ = self.request('POST', '/api/demo/samples/' + PL)
            self.assertEqual(202, status)
            self.poll(data['job_id'])
            self.assertFalse(self.request('GET', '/api/config')[1]['live_unlocked'])
            self.assertEqual(401, self.upload()[0])

    def test_requires_all_env_settings_and_demo_stays_locked(self):
        for missing in ('JUDGE_CODE', 'NEBIUS_API_KEY', 'TAVILY_API_KEY'):
            with patch.dict(os.environ, {missing: ''}):
                self.start()
                self.assertFalse(self.request('GET', '/api/config')[1]['live_available'])
                self.assertEqual(401, self.unlock()[0])
                self.assertEqual(401, self.upload()[0])
        self.start(demo=True)
        self.assertEqual(401, self.unlock()[0])

    def test_expiry_and_startup_purge(self):
        self.unlock()
        owner = token_hash(self.cookie.split('=', 1)[1])
        path = self.data / 'cases' / (owner + '.sqlite3')
        old = datetime.now(timezone.utc) - timedelta(days=31)
        with store.Store(path) as memory:
            memory.add_letter({'case_number': 'OLD'}, 'hash', now=old)
            memory.add_letter({'case_number': 'NEW'}, 'hash')
        self.start()
        self.assertEqual(['NEW'], [r['case_number'] for r in self.request('GET', '/api/cases')[1]])
        with patch.object(Access, 'timestamp', return_value=Access.timestamp() + 31 * 86400):
            self.assertEqual(401, self.upload()[0])
            self.assertEqual([], self.request('GET', '/api/cases')[1])

    def test_atomic_cap_and_utc_day_rollover(self):
        access = Access(self.data)
        with patch('papertrail.live.datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 21, 23, 59, tzinfo=timezone.utc)
            with ThreadPoolExecutor(max_workers=8) as workers:
                accepted = list(workers.map(lambda _: access.reserve(3), range(20)))
            self.assertEqual(3, sum(accepted))
            clock.now.return_value = datetime(2026, 10, 22, tzinfo=timezone.utc)
            self.assertTrue(access.reserve(3))

    def test_model_error_does_not_echo_inputs_or_secrets(self):
        self.unlock()
        with patch('papertrail.ocr.ocr_image', side_effect=RuntimeError('private-model-key PHOTO_CONTENT')):
            job_id = self.upload()[1]['job_id']
            for _ in range(100):
                job = self.request('GET', '/api/jobs/' + job_id)[1]
                if job['status'] != 'running':
                    break
                time.sleep(.01)
        self.assertEqual('error', job['status'])
        self.assertEqual('Letter processing unavailable', job['error'])
        self.assertNotIn('PHOTO_CONTENT', json.dumps(job))
        self.assertNotIn('private-model-key', json.dumps(job))
