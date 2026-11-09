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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tv_playback as tv


class PlaybackFailureTests(unittest.TestCase):
    def test_real_decoder_503_is_public_and_never_sends_play_reports(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_error(503)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                private = root / 'private'
                private.mkdir()
                client = Mock(private_dir=private)
                client.request.return_value = {'data': {
                    'durl': [{'url': f'http://127.0.0.1:{server.server_port}/video?sign=secret-token'}],
                    'timelength': 60000, 'quality': 16}}
                view = {'aid': 1, 'cid': 2, 'state': 0, 'stat': {'view': 0}}
                with patch.object(tv, 'public_json', return_value=view), patch.object(tv, 'HTTP_PROXY', None), patch.object(sys, 'stdout', io.StringIO()), patch.dict(os.environ, {'no_proxy': '*', 'NO_PROXY': '*'}):
                    result = tv.play_one(client, {'bvid': 'BV0000000000', 'aid': 1},
                                         {'attempts': {}, 'ftime': 1}, root, 1)
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['media_http_status'], 503)
                self.assertEqual(result['decoded_seconds'], 0)
                self.assertIn('HTTP 503', result['failure_reason'])
                self.assertNotIn('secret-token', json.dumps(result))
                self.assertNotIn('http://', result['failure_reason'])
                self.assertEqual(client.request.call_count, 1)
                self.assertEqual(client.request.call_args.args[0], '/x/tv/playurl')
                with patch.object(sys, 'argv', ['tv_playback.py', 'BV0000000000']), patch.object(tv, 'play_window', return_value=result):
                    with self.assertRaisesRegex(SystemExit, 'HTTP 503'):
                        tv.main()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_decode_errors_are_classified_without_exposing_private_log(self):
        for log, expected in (
            ('HTTP error 403 Forbidden', 'HTTP 403'),
            ('Connection timed out', '超时'),
            ('Stream ends prematurely', '连接中断'),
            ('Error reading HTTP response: Error number -138 occurred', 'HTTP 响应读取失败'),
            ('Error opening input: I/O error', '媒体输入读写失败'),
            ('Error opening input: Invalid data found when processing input', '媒体响应无法解析'),
            ('Unknown decoder error', '播放要求'),
        ):
            with self.subTest(log=log):
                reason, _ = tv.decoder_failure(log + '\nhttps://cdn.invalid/?sign=secret', 1)
                self.assertIn(expected, reason)
                self.assertNotIn('secret', reason)
        self.assertNotIn('secret', tv.playback_failure_message(RuntimeError('secret')))

    def test_windows_gbk_worker_transport_preserves_chinese_and_unicode_paths(self):
        message = '视频流失败 · 中文路径 / 测试🧪'
        for module, expression in (
            ('batch_worker', 'emit({"kind":"failure","message":message})'),
            ('debug_worker', 'emit("failure", message=message)'),
        ):
            script = f'from {module} import emit\nmessage={ascii(message)}\n{expression}'
            with self.subTest(module=module):
                result = subprocess.run([sys.executable, '-c', script],
                                        cwd=Path(tv.__file__).parent, capture_output=True, timeout=10,
                                        env={**os.environ, 'PYTHONIOENCODING': 'gbk'})
                self.assertEqual(result.returncode, 0, result.stderr)
                event = json.loads(result.stdout.decode('utf-8'))
                self.assertEqual(event['message'], message)


if __name__ == '__main__':
    unittest.main()
