import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tv_playback as tv
from ip_proxy import ProxyLease


class ReadRetryTests(unittest.TestCase):
    def setUp(self):
        self.replies = []
        self.hits = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.hits.append((self.command, self.path))
                status, stalled, code = owner.replies.pop(0)
                body = json.dumps({'code': code, 'data': {'aid': 1}}).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                if stalled:
                    self.wfile.write(body[:1])
                    self.wfile.flush()
                    threading.Event().wait(0.6)
                    return
                self.wfile.write(body)

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                self.do_GET()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        # Origin intentionally cannot resolve: both attempts must use this proxy.
        opener = ProxyLease(f'127.0.0.1:{self.server.server_port}').opener()
        self.patches = [
            patch.object(tv, 'API_ROOT', 'http://origin.invalid'),
            patch.object(tv, 'open_network', side_effect=lambda request, timeout: opener.open(request, timeout=0.2)),
            patch.object(tv.time, 'sleep'),
            patch.object(tv, 'NETWORK_OBSERVER'),
            patch.object(tv, 'request_timeout', return_value=.2),
        ]
        _, self.open, self.wait, self.observer, _ = [p.start() for p in self.patches]

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_http_200_body_timeout_reconnects_same_proxy_and_succeeds(self):
        self.replies = [(200, True, 0), (200, False, 0)]
        result = tv.public_json('/view?token=private')
        self.assertEqual(result['aid'], 1)
        self.assertEqual(len(self.hits), 2)
        self.wait.assert_called_once_with(5)
        first = self.observer.call_args_list[0].args[0]
        self.assertEqual(first['http_status'], 200)
        self.assertEqual(first['phase'], 'response_body')
        self.assertEqual(first['error_type'], 'TimeoutError')
        self.assertIn('正文超时', first['error_message'])
        self.assertEqual(first['retry_in'], 5)
        self.assertNotIn('private', json.dumps(first))
        self.assertEqual(self.observer.call_args_list[1].args[0]['code'], 0)

    def test_exhausted_timeout_stops_after_two_attempts(self):
        self.replies = [(200, True, 0), (200, True, 0)]
        with self.assertRaisesRegex(tv.RequestFailure, '正文超时.*2/2'):
            tv.public_json('/view')
        self.assertEqual(len(self.hits), 2)
        self.assertNotIn('retry_in', self.observer.call_args.args[0])

    def test_tv_read_503_retries_and_records_each_actual_request(self):
        self.replies = [(503, False, 0), (200, False, 0)]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            client = tv.TVClient(root / 'private', root / 'events.jsonl', buvid='fixture')
            self.assertEqual(client.request('/playurl', {})['code'], 0)
            self.assertEqual(client.counter, 2)
            events = [json.loads(line) for line in (root / 'events.jsonl').read_text().splitlines()]
            self.assertEqual([event['http_status'] for event in events], [503, 200])
            self.assertEqual([event['attempt'] for event in events], [1, 2])

    def test_post_report_is_never_automatically_replayed(self):
        self.replies = [(503, False, 0)]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            client = tv.TVClient(root / 'private', root / 'events.jsonl', buvid='fixture')
            with self.assertRaisesRegex(tv.RequestFailure, 'POST /report.*503'):
                client.request('/report', {}, 'FORM')
        self.assertEqual(len(self.hits), 1)
        self.wait.assert_not_called()

    def test_permission_and_business_errors_do_not_retry(self):
        for status, code in ((403, 0), (200, -412)):
            with self.subTest(status=status):
                self.replies = [(status, False, code)]
                self.hits.clear()
                with self.assertRaises(tv.RequestFailure):
                    tv.public_json('/view')
                self.assertEqual(len(self.hits), 1)
                self.wait.assert_not_called()


if __name__ == '__main__':
    unittest.main()
