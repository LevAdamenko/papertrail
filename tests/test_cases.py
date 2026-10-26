"""Offline case memory views, command line, and local HTTP routes."""
import contextlib
from datetime import date, datetime, timezone
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen, Request

from papertrail import cases
from papertrail.store import Store
from papertrail.live import Access, token_hash
from papertrail.web.app import make_server

TODAY = date(2026, 10, 16)


class CasesTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'cases.sqlite3'
        self.memory = Store(self.path)
        self.addCleanup(self.memory.close)
        self.number = 'A/B 1'
        first = dict(case_number=self.number, authority='Office', procedure='Residence',
                     letter_type='request', letter_date='2026-10-01',
                     deadlines=[dict(kind='absolute', date='2026-10-17', what='Old')])
        self.memory.add_letter(first, 'private OCR first', now=datetime(2026, 10, 1, tzinfo=timezone.utc))
        other = dict(first, case_number='OTHER', deadlines=[dict(kind='relative', days=7, what='Reply')])
        self.memory.add_letter(other, 'private OCR other', now=datetime(2026, 10, 2, tzinfo=timezone.utc))
        latest = dict(first, letter_type='decision', letter_date='2026-10-03', deadlines=[
            dict(kind='absolute', date='2026-10-22', what='Later'),
            dict(kind='appointment', date='2026-10-16', time='10:30', place='Office', what='Visit'),
            dict(kind='absolute', date='2026-10-15', what='Past'),
            dict(kind='relative', days=7, what='Reply')])
        self.latest_id = self.memory.add_letter(latest, 'private OCR latest', now=datetime(2026, 10, 3, tzinfo=timezone.utc))['letter_id']
        self.verification = [dict(status='verified', claim_id='one'), dict(status='unverified', claim_id='two')]
        self.memory.set_verification(self.number, self.verification)

    def test_overview_and_detail(self):
        rows = cases.overview(self.memory, today=TODAY)
        self.assertEqual([self.number, 'OTHER'], [r['case_number'] for r in rows])
        row = rows[0]
        self.assertEqual(2, row['letters'])
        self.assertEqual('PL', row['country'])
        self.assertEqual('2026-10-16', row['next_due'])
        self.assertEqual(['2026-10-15'], row['overdue'])
        self.assertTrue(row['needs_delivery_date'])
        item = cases.detail(self.memory, 'a/b 1', today=TODAY)
        self.assertEqual(2, len(item['letters']))
        self.assertEqual(['request', 'decision'], [e['letter_type'] for e in item['timeline']])
        self.assertIn('- deadline 2026-10-17: Old', item['timeline'][1]['changes'])
        self.assertIn('+ deadline 2026-10-22: Later', item['timeline'][1]['changes'])
        self.assertEqual(self.verification, item['verification'])
        self.assertEqual(dict(what='Visit', date='2026-10-16', time='10:30', place='Office',
                              kind='appointment', needs_delivery_date=False), item['dues'][1])
        self.assertNotIn('ocr_text', json.dumps(item))
        self.assertNotIn('private OCR', json.dumps(item))
        self.assertNotIn('ics', item)
        self.assertIsNone(cases.detail(self.memory, 'unknown', today=TODAY))
        self.memory.set_delivered_on(self.latest_id, '2026-10-13')
        item = cases.detail(self.memory, self.number, today=TODAY)
        self.assertFalse(item['needs_delivery_date'])
        self.assertEqual('2026-10-20', item['dues'][-1]['date'])
        later = cases.overview(self.memory, today=date(2026, 10, 21))[0]
        self.assertEqual('2026-10-22', later['next_due'])
        self.assertIsNone(cases.overview(self.memory, today=date(2026, 11, 1))[0]['next_due'])

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cases.main(['--db', str(self.path), *args], today=TODAY)
        return code, out.getvalue(), err.getvalue()

    def test_cli(self):
        code, out, err = self.cli()
        self.assertEqual((0, ''), (code, err))
        self.assertIn('A/B 1 · Office · 2 letter(s) · next: 2026-10-16 (in 0 days)', out)
        self.assertIn('OVERDUE 2026-10-15', out)
        self.assertIn('needs delivery date', out)
        self.assertEqual(cases.overview(self.memory, today=TODAY), json.loads(self.cli('--json')[1]))
        self.assertEqual(cases.detail(self.memory, self.number, today=TODAY), json.loads(self.cli('--json', self.number)[1]))
        out = self.cli(self.number)[1]
        self.assertIn('decision', out)
        self.assertIn('- deadline 2026-10-17: Old', out)
        self.assertIn('verified: 1', out)
        self.assertIn('Dues:', out)
        self.assertEqual(1, self.cli('missing')[0])
        self.assertIn('Unknown case', self.cli('missing')[2])
        self.assertIn('in 5 days', self.cli('--today', '2026-10-10')[1])
        self.assertIn('No cases yet.', self.cli('--db', str(self.path.parent / 'absent.db'))[1])

    def start(self, path):
        data = self.path.parent / 'web-data'
        access = Access(data)
        token = access.create()
        self.cookie = 'live_session=' + token
        owner_path = data / 'cases' / (token_hash(token) + '.sqlite3')
        owner_path.parent.mkdir(exist_ok=True)
        if path.is_file():
            with sqlite3.connect(owner_path) as target:
                self.memory.conn.backup(target)
        server = make_server('127.0.0.1', 0, db_path=path, data_dir=data,
                             now=lambda: datetime(2026, 10, 16, tzinfo=timezone.utc))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(close)
        return 'http://127.0.0.1:' + str(server.server_port)

    def get(self, base, path):
        try:
            response = urlopen(Request(base + path, headers={"Cookie": self.cookie}), timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_web(self):
        base = self.start(self.path)
        self.assertEqual((200, cases.overview(self.memory, today=TODAY)), self.get(base, '/api/cases'))
        self.assertEqual((200, cases.detail(self.memory, self.number, today=TODAY)),
                         self.get(base, '/api/cases/' + quote(self.number, safe='')))
        self.assertEqual((404, {'error': 'Not found'}), self.get(base, '/api/cases/missing'))
        with Store(self.path, read_only=True) as memory:
            with self.assertRaises(sqlite3.OperationalError):
                memory.set_verification(self.number, [])

    def test_missing_database_is_not_created(self):
        path = self.path.parent / 'missing' / 'new.sqlite3'
        base = self.start(path)
        self.assertEqual((200, []), self.get(base, '/api/cases'))
        self.assertEqual(404, self.get(base, '/api/cases/unknown')[0])
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())


if __name__ == '__main__':
    unittest.main()
