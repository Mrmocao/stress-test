import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import debug_worker
from debug_server import Inspector
import ip_proxy
import tv_playback as tv
from playback_runtime import ffmpeg_executable


class ProxyTests(unittest.TestCase):
    def test_proxy_retry_replaces_failed_lease_after_five_seconds(self):
        first, second = Mock(), Mock()
        for lease, endpoint in ((first, '8.8.8.8:80'), (second, '1.1.1.1:81')):
            lease.endpoint, lease.remaining, lease.exit_ip = endpoint, 10, '9.9.9.9'
            lease.source_ip = '4.2.2.2'
            lease.seconds_left.return_value = 55
            lease.lifetime_seconds, lease.lifetime_source = 60, 'provider'
        first.verify.side_effect = ip_proxy.ProxyError('verification failed')
        events, wait = [], Mock()
        with patch.object(ip_proxy, 'acquire_proxy', side_effect=[first, second]) as acquire:
            lease = ip_proxy.acquire_verified_proxy(1, lambda **event: events.append(event), wait)
        self.assertIs(lease, second)
        self.assertEqual(acquire.call_count, 2)
        wait.assert_called_once_with(5)
        self.assertEqual([e['status'] for e in events], ['acquiring', 'verifying', 'retrying', 'acquiring', 'verifying', 'ready'])

    def test_zero_retry_fails_once_and_permanent_errors_are_not_retried(self):
        for retries, error in ((0, ip_proxy.ProxyError('temporary')), (3, ip_proxy.ProxyError('bad key', code=401, retryable=False))):
            wait = Mock()
            with self.subTest(retries=retries), patch.object(ip_proxy, 'acquire_proxy', side_effect=error) as acquire:
                with self.assertRaises(ip_proxy.ProxyError):
                    ip_proxy.acquire_verified_proxy(retries, wait=wait)
                self.assertEqual(acquire.call_count, 1)
                wait.assert_not_called()

    def test_signature_uses_sorted_parameters_and_keeps_key_out_of_url(self):
        # Expected digest generated independently from the documented raw string.
        import hashlib
        raw = 'auto_white=1&filter=1&ip_remain=1&num=1&pt=1&result_type=json&split=1&trade_no=123&key=test-key'
        url = ip_proxy.signed_url('123', 'test-key')
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(query['sign'], [hashlib.md5(raw.encode()).hexdigest()])
        self.assertEqual(query['num'], ['1'])
        self.assertNotIn('test-key', url)

    def test_provider_errors_and_invalid_addresses_do_not_expose_secrets(self):
        for body in ({'code': 401, 'msg': 'secret-key'}, {'code': 408},
                     {'code': 200, 'data': {'proxy_list': []}},
                     {'code': 200, 'data': {'proxy_list': ['127.0.0.1:80']}},
                     {'code': 200, 'data': {'proxy_list': ['8.8.8.8:65536']}},
                     {'code': 200, 'data': {'proxy_list': ['8.8.8.8:80:user:secret-key']}},
                     {'code': 200, 'data': []}, []):
            with self.subTest(body=body), self.assertRaises(ip_proxy.ProxyError) as raised:
                ip_proxy.parse_response(body)
            self.assertNotIn('secret-key', str(raised.exception))

    def test_one_extraction_per_call_and_no_persistent_cache(self):
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(side_effect=[
            io.BytesIO(json.dumps({'code': 200, 'data': {'proxy_list': ['8.8.8.8:80']}}).encode()),
            io.BytesIO(json.dumps({'code': 200, 'data': {'proxy_list': ['1.1.1.1:81']}}).encode())])
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(ip_proxy, 'load_config', return_value=('123', 'test-key')), patch.object(ip_proxy, 'direct_source_ip', return_value='4.2.2.2'), patch.object(ip_proxy.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(ip_proxy.acquire_proxy().endpoint, '8.8.8.8:80')
            self.assertEqual(ip_proxy.acquire_proxy().endpoint, '1.1.1.1:81')
        self.assertEqual(opener.open.call_count, 2)

    def test_offline_rejects_proxy_before_worker_or_quota_use(self):
        with patch.object(ip_proxy, 'acquire_proxy') as acquire:
            inspector = Inspector()
            with self.assertRaisesRegex(ValueError, '离线'):
                inspector.start('demo', '', 1, new_ip=True)
            self.assertIsNone(inspector.proc)
            acquire.assert_not_called()

    def test_failed_verification_never_enters_playback(self):
        lease = Mock()
        lease.endpoint, lease.remaining = '8.8.8.8:80', 10
        lease.source_ip = '4.2.2.2'
        lease.verify.side_effect = ip_proxy.ProxyError('verification failed')
        with tempfile.TemporaryDirectory() as folder:
            args = ['debug_worker.py', '--mode', 'live', '--device-confirmed', '--new-ip', '--run-dir', folder]
            with patch.object(sys, 'argv', args), patch.object(sys, 'stdout', io.StringIO()), patch.object(sys, 'stderr', io.StringIO()), patch.object(debug_worker, 'emit') as emit, patch.object(debug_worker.threading, 'Thread'), patch.object(ip_proxy, 'acquire_proxy', return_value=lease), patch.object(tv, 'NETWORK_OBSERVER'), patch.object(tv, 'PROGRESS_OBSERVER'), patch.object(tv, 'main') as playback:
                self.assertEqual(debug_worker.main(), 1)
                playback.assert_not_called()
                self.assertFalse((Path(folder) / 'proxy.json').exists())
                self.assertTrue(any(c.kwargs.get('status') == 'failed' for c in emit.call_args_list))

    def test_https_proxy_is_required_even_with_no_proxy_wildcard(self):
        handler = ip_proxy.RequiredProxyHandler({'https': 'http://8.8.8.8:80'})
        request = urllib.request.Request('https://api.example.invalid/video')
        with patch.dict(os.environ, {'no_proxy': '*', 'NO_PROXY': '*'}):
            handler.proxy_open(request, 'http://8.8.8.8:80', 'https')
        self.assertEqual(request.host, '8.8.8.8:80')
        self.assertEqual(request._tunnel_host, 'api.example.invalid')


class ProxyTransportTests(unittest.TestCase):
    def test_repeated_extraction_ignores_system_global_and_previous_proxy(self):
        hits = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                hits.append(self.path)
                body = ({'code': 0, 'data': {'ip': '4.2.2.2'}} if self.path == '/source'
                        else {'code': 200, 'data': {'proxy_list': ['8.8.8.8:80']}})
                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        poisoned = Mock()
        poisoned.open.side_effect = AssertionError('must not use previous/global proxy')
        env = {'HTTP_PROXY': 'http://127.0.0.1:1', 'HTTPS_PROXY': 'http://127.0.0.1:1',
               'http_proxy': 'http://127.0.0.1:1', 'https_proxy': 'http://127.0.0.1:1',
               'NO_PROXY': '', 'no_proxy': ''}
        try:
            with patch.dict(os.environ, env), patch.object(urllib.request, 'getproxies', return_value={'http': env['HTTP_PROXY']}), patch.object(urllib.request, '_opener', poisoned), patch.object(tv, 'HTTP_OPENER', poisoned), patch.object(tv, 'HTTP_PROXY', env['HTTP_PROXY']), patch.object(ip_proxy, 'load_config', return_value=('123', 'test-key')), patch.object(ip_proxy, 'signed_url', return_value=base + '/extract'), patch.object(ip_proxy, 'SOURCE_IP_URL', base + '/source'):
                for _ in range(2):
                    lease = ip_proxy.acquire_proxy()
                    self.assertEqual(lease.source_ip, '4.2.2.2')
                    self.assertEqual(lease.endpoint, '8.8.8.8:80')
            self.assertEqual(hits, ['/source', '/extract', '/source', '/extract'])
            poisoned.open.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_source_lookup_failure_does_not_block_direct_extraction(self):
        opener = Mock()
        response = Mock()
        response.__enter__ = Mock(return_value=io.BytesIO(b'{"code":200,"data":{"proxy_list":["8.8.8.8:80"]}}'))
        response.__exit__ = Mock(return_value=False)
        opener.open.side_effect = [TimeoutError(), response]
        with patch.object(ip_proxy, 'load_config', return_value=('123', 'test-key')), patch.object(ip_proxy, 'direct_opener', return_value=opener):
            lease = ip_proxy.acquire_proxy()
        self.assertEqual(lease.source_ip, '')
        self.assertEqual(lease.endpoint, '8.8.8.8:80')
        self.assertEqual(opener.open.call_count, 2)

    def test_extraction_error_logs_direct_source_without_credentials(self):
        opener = Mock()
        response = Mock()
        response.__enter__ = Mock(return_value=io.BytesIO(b'{"code":410,"msg":"secret-key"}'))
        response.__exit__ = Mock(return_value=False)
        opener.open.return_value = response
        events = []
        with patch.object(ip_proxy, 'load_config', return_value=('123', 'secret-key')), patch.object(ip_proxy, 'direct_opener', return_value=opener), patch.object(ip_proxy, 'direct_source_ip', return_value='4.2.2.2'):
            with self.assertRaises(ip_proxy.ProxyError):
                ip_proxy.acquire_verified_proxy(0, publish=lambda **event: events.append(event))
        self.assertEqual(events[-1]['source_ip'], '4.2.2.2')
        self.assertIn('4.2.2.2', events[-1]['message'])
        self.assertNotIn('secret-key', json.dumps(events))

    def test_api_reports_and_real_ffmpeg_use_the_same_proxy(self):
        """Use unresolvable origin hosts: success requires the local forward proxy."""
        hits = []
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            media = root / 'fixture.mp4'
            subprocess.run([ffmpeg_executable(), '-hide_banner', '-loglevel', 'error',
                            '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=25',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
                            '-t', '3', '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac',
                            '-movflags', '+faststart', str(media)], check=True, capture_output=True, timeout=30)
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_GET(self):
                    hits.append((self.command, self.path))
                    path = urllib.parse.urlsplit(self.path).path
                    if path == '/fixture.mp4':
                        body = media.read_bytes()
                    elif path == '/x/web-interface/view':
                        body = json.dumps({'code': 0, 'data': {'aid': 1, 'cid': 2, 'state': 0, 'stat': {'view': 0}}}).encode()
                    elif path == '/x/tv/playurl':
                        body = json.dumps({'code': 0, 'data': {'durl': [{'url': 'http://media.example.invalid/fixture.mp4'}], 'timelength': 3000, 'quality': 16}}).encode()
                    else:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

                def do_POST(self):
                    hits.append((self.command, self.path))
                    self.rfile.read(int(self.headers.get('Content-Length', 0)))
                    body = b'{"code":0,"data":{}}'
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            lease = ip_proxy.ProxyLease(f'127.0.0.1:{server.server_port}')
            try:
                client = tv.TVClient(root / 'private', root / 'events.jsonl', buvid='fixture')
                state = {'attempts': {}, 'ftime': 1}
                with patch.dict(os.environ, {'no_proxy': '*', 'NO_PROXY': '*'}), patch.object(tv, 'API_ROOT', 'http://api.example.invalid'), patch.object(tv, 'HTTP_OPENER', lease.opener()), patch.object(tv, 'HTTP_PROXY', lease.url), patch.object(sys, 'stdout', io.StringIO()):
                    result = tv.play_one(client, {'bvid': 'BV0000000000', 'aid': 1}, state, root, 1)
                self.assertEqual(result['status'], 'completed', result)
                self.assertTrue(result['audio_decoded'])
                paths = [urllib.parse.urlsplit(url).path for _, url in hits]
                for path in ('/x/web-interface/view', '/x/tv/playurl', '/fixture.mp4', '/x/report/click/android/tv', '/x/report/heartbeat/mobile'):
                    self.assertIn(path, paths)
                self.assertTrue(all(urllib.parse.urlsplit(url).hostname.endswith('.example.invalid') for _, url in hits))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
