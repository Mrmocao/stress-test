import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import batch_worker
import ip_proxy
import tv_playback as tv
from execution_budget import ExecutionBudget, BudgetExceeded
from debug_server import Inspector
from playback_runtime import ffmpeg_executable


class BudgetTests(unittest.TestCase):
    def test_prepare_retry_and_playback_respect_same_monotonic_deadline(self):
        clock = Mock(return_value=0)
        budget = ExecutionBudget(60, 45, clock=clock)
        self.assertEqual(budget.timeout(8), 5)
        clock.return_value = 3
        self.assertEqual(budget.timeout(), 2)
        self.assertTrue(budget.can_retry(.5))
        clock.return_value = 4
        self.assertFalse(budget.can_retry(.5))
        clock.return_value = 8
        with self.assertRaises(BudgetExceeded):
            budget.begin_playback(45)
        clock.return_value = 4
        budget.begin_playback(45)
        clock.return_value = 57
        with self.assertRaises(BudgetExceeded):
            budget.timeout()
        budget.begin_finishing()
        self.assertEqual(budget.timeout(), 1)
        clock.return_value = 58
        with self.assertRaises(BudgetExceeded):
            budget.check()

    def test_provider_remaining_lifetime_is_parsed_and_invalid_values_rejected(self):
        def body(endpoint):
            return {'code': 200, 'data': {'proxy_list': [endpoint]}}
        lease = ip_proxy.parse_response(body('8.8.8.8:80,59'))
        self.assertEqual((lease.endpoint, lease.lifetime_seconds, lease.lifetime_source), ('8.8.8.8:80', 59, 'provider'))
        self.assertEqual(ip_proxy.parse_response(body('8.8.8.8:80')).lifetime_source, 'configured_60s')
        for seconds in ('0', '-1', 'NaN', '99999999'):
            with self.subTest(seconds=seconds), self.assertRaises(ip_proxy.ProxyError):
                ip_proxy.parse_response(body('8.8.8.8:80,' + seconds))

    def test_insufficient_lease_does_not_consume_more_proxies(self):
        lease = ip_proxy.ProxyLease('8.8.8.8:80', expires_at=time.monotonic() + 20)
        with patch.object(ip_proxy, 'acquire_proxy', return_value=lease) as acquire, patch.object(lease, 'verify'), patch.object(ip_proxy.time, 'sleep') as wait:
            with self.assertRaisesRegex(ip_proxy.ProxyError, '剩余寿命不足'):
                ip_proxy.acquire_verified_proxy(3, minimum_seconds=50)
        self.assertEqual(acquire.call_count, 1)
        wait.assert_not_called()

    def test_response_deadline_rejects_continuously_trickling_body(self):
        class SlowBody:
            def read(self, size):
                return self.read1(size)
            def read1(self, size):
                time.sleep(.03)
                return b'x'
        with self.assertRaises(TimeoutError):
            tv.read_response(SlowBody(), {}, time.monotonic() + .08)

    def test_logs_are_incremental_and_state_io_is_not_needed_for_progress(self):
        inspector = Inspector()
        inspector.record({'kind': 'notice', 'message': 'one'})
        first = inspector.snapshot(0, detail=False)
        inspector.record({'kind': 'notice', 'message': 'two'})
        second = inspector.snapshot(first['cursor'], detail=False)
        self.assertTrue(second['logs_incremental'])
        self.assertEqual([e['message'] for e in second['logs']], ['two'])


class SchedulerTests(unittest.TestCase):
    def test_pause_stops_new_dispatch_without_pausing_current_process(self):
        relay = batch_worker.ControlRelay()
        relay.debug_controls = False
        relay.proc = Mock()
        relay.send('pause')
        self.assertTrue(relay.paused)
        relay.proc.stdin.write.assert_not_called()
        relay.send('resume')
        self.assertFalse(relay.paused)

    def test_watchdog_kills_a_blocked_worker_and_its_child(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'debug_worker.py').write_text(
                'import subprocess,sys,time\nfrom pathlib import Path\n'
                'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\n'
                'Path(__file__).with_name("child.pid").write_text(str(child.pid))\n'
                'time.sleep(60)\n', encoding='utf-8')
            args = argparse.Namespace(mode='demo', video='BV0000000000', seconds=1, run_dir=root/'run',
                                      repeat=1, concurrency=1, new_ip=False, round_timeout=1.2)
            events = []
            with patch.object(batch_worker, 'ROOT', root):
                batch_worker.run_batch(args, batch_worker.ControlRelay(), events.append)
            self.assertEqual(events[-1]['failed'], 1)
            self.assertTrue(any('时间预算' in e.get('message', '') for e in events))
            pid = int((root / 'child.pid').read_text())
            if os.name == 'nt':
                import ctypes
                kernel = ctypes.windll.kernel32
                kernel.OpenProcess.restype = ctypes.c_void_p
                kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
                kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                handle = kernel.OpenProcess(0x1000, False, pid)
                if handle:
                    code = ctypes.c_ulong()
                    kernel.GetExitCodeProcess(handle, ctypes.byref(code))
                    kernel.CloseHandle(handle)
                    self.assertNotEqual(code.value, 259, 'child is still active')


class CleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.media = Path(cls.folder.name) / 'fixture.mp4'
        subprocess.run([ffmpeg_executable(), '-hide_banner', '-loglevel', 'error',
                        '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=25',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
                        '-t', '1', '-c:v', 'libx264', '-threads', '2', '-preset', 'ultrafast',
                        '-c:a', 'aac', '-movflags', '+faststart', str(cls.media)],
                       check=True, capture_output=True, timeout=20)
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                body = cls.media.read_bytes()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.folder.cleanup()

    def play(self, target, fail_start=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            private = root / 'private'
            private.mkdir()
            client = Mock(private_dir=private, appkey='fixture')
            heartbeats = []
            def request(path, *args):
                if path == '/x/click-interface/click/now':
                    return {'code': 0, 'data': {'now': 1700000000}}
                if path == '/x/tv/playurl':
                    return {'data': {'durl': [{'url': f'http://127.0.0.1:{self.server.server_port}/fixture.mp4'}],
                                     'timelength': target * 1000, 'quality': 16}}
                if path == '/x/report/heartbeat/mobile':
                    heartbeats.append(path)
                    if fail_start or len(heartbeats) == 2:
                        raise tv.RequestFailure('start failed' if fail_start else 'cleanup failed')
                return {'code': 0, 'data': {}}
            client.request.side_effect = request
            video = {'bvid': 'BV0000000000', 'aid': 1, 'cid': 2, 'state': 0, 'stat': {'view': 0}}
            with patch.object(tv, 'public_json') as public, patch.object(tv, 'HTTP_PROXY', None), patch.object(tv, 'RUN_BUDGET', None), patch.object(sys, 'stdout', io.StringIO()):
                result = tv.play_one(client, video, {'attempts': {}, 'ftime': 1}, root, target)
            public.assert_not_called()
            return result, len(heartbeats)

    def test_full_decode_is_kept_when_end_heartbeat_fails(self):
        result, count = self.play(1)
        self.assertEqual(count, 2)
        self.assertEqual(result['media_status'], 'completed')
        self.assertEqual(result['report_status'], 'unconfirmed')
        self.assertIn('播放完成，上报未确认', result['failure_reason'])

    def test_cleanup_does_not_replace_original_media_failure(self):
        result, _ = self.play(3)
        self.assertEqual(result['failure_stage'], 'media')
        self.assertIn('播放要求', result['failure_reason'])
        self.assertEqual(result['cleanup_error'], 'cleanup failed')

    def test_failed_start_heartbeat_never_sends_end_heartbeat(self):
        result, count = self.play(1, fail_start=True)
        self.assertEqual(count, 1)
        self.assertIn('start failed', result['failure_reason'])


if __name__ == '__main__':
    unittest.main()
