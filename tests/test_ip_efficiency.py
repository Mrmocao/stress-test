import argparse
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import ssl
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import batch_worker
from debug_server import Inspector
from dispatch_limits import DispatchLimits
from execution_budget import ExecutionBudget
import ip_proxy
from network_deadline import read_bounded
import tv_playback as tv


class NetworkEfficiencyTests(unittest.TestCase):
    def test_large_body_over_three_seconds_completes_within_lease(self):
        body = json.dumps({'code':0, 'data':{'padding':'x'*388000}}).encode()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                for i in range(0, len(body), 97000):
                    time.sleep(.72)
                    self.wfile.write(body[i:i+97000]); self.wfile.flush()
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        direct=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        observer=Mock()
        try:
            with patch.object(tv,'API_ROOT',f'http://127.0.0.1:{server.server_port}'), patch.object(tv,'HTTP_OPENER',direct), patch.object(tv,'RUN_BUDGET',ExecutionBudget(time.monotonic()+90,45)), patch.object(tv,'NETWORK_OBSERVER',observer):
                self.assertEqual(len(tv.public_json('/x/web-interface/view')['padding']),388000)
            self.assertEqual(observer.call_count,1)
            self.assertGreater(observer.call_args.args[1],3000)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_request_types_respect_remaining_budget(self):
        clock=Mock(return_value=0)
        budget=ExecutionBudget(90,45,clock=clock)
        with patch.object(tv,'RUN_BUDGET',budget):
            self.assertEqual(tv.request_timeout('/x/web-interface/view?x=1'),12)
            self.assertEqual(tv.request_timeout('/x/report/heartbeat/mobile'),8)
            clock.return_value=34
            self.assertEqual(tv.request_timeout('/x/web-interface/view'),1)

    def test_tls_want_read_recovers_and_eventually_times_out(self):
        response=Mock(headers={}); response.fp=None; response.length=0
        response.read1.side_effect=[ssl.SSLWantReadError(),b'ok',b'']
        self.assertEqual(read_bounded(response,{},time.monotonic()+1),b'ok')
        response.read1.side_effect=ssl.SSLWantReadError()
        started=time.monotonic()
        with self.assertRaises(TimeoutError): read_bounded(response,{},started+.05)
        self.assertLess(time.monotonic()-started,.2)

    def test_verification_logs_service_phase_and_redacts_error(self):
        class Reply(io.BytesIO):
            status=200
            headers={}
        opener=Mock()
        opener.open.side_effect=[urllib.error.URLError(TimeoutError('secret-signed-url')),
                                  Reply(b'{"origin":"8.8.8.8"}')]
        lease=ip_proxy.ProxyLease('8.8.8.8:80',expires_at=time.monotonic()+90)
        events=[]
        with patch.object(lease,'opener',return_value=opener):
            lease.verify(publish=lambda **e:events.append(e),minimum_seconds=55)
        self.assertEqual(lease.exit_ip,'8.8.8.8')
        self.assertEqual([e['status'] for e in events],['failed','ok'])
        self.assertEqual(events[0]['error_category'],'timeout')
        self.assertEqual(events[0]['phase'],'connecting')
        self.assertNotIn('secret',json.dumps(events))
        self.assertTrue(all(0<call.kwargs['timeout']<=3 for call in opener.open.call_args_list))

    def test_verification_body_is_bounded_by_total_six_seconds(self):
        clock=Mock(return_value=100.)
        class Reply(io.BytesIO):
            status=200
            headers={}
            def read1(self,n):
                clock.return_value+=.5
                return b'x'
        opener=Mock(); opener.open.side_effect=lambda *a,**k:Reply()
        lease=ip_proxy.ProxyLease('8.8.8.8:80',expires_at=190.)
        events=[]
        with patch('time.monotonic',clock),patch.object(lease,'opener',return_value=opener):
            with self.assertRaises(ip_proxy.ProxyError):
                lease.verify(publish=lambda **e:events.append(e),minimum_seconds=55)
        self.assertEqual(clock.return_value,106.)
        self.assertEqual([e['error_category'] for e in events],['timeout','timeout'])
        self.assertTrue(all(e['phase']=='response_body' for e in events))


class DispatchEfficiencyTests(unittest.TestCase):
    def test_real_worker_processes_share_cap_and_cancel_future_rounds(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'debug_worker.py').write_text(
                'import sys,json,time\n'
                'n=int(sys.argv[sys.argv.index("--ip-retries")+1])+1\n'
                'for i in range(n):\n'
                ' print(json.dumps({"kind":"proxy","status":"acquiring"}),flush=True)\n'
                ' time.sleep(.02)\n'
                'print(json.dumps({"kind":"lifecycle","status":"failed"}),flush=True)\n'
                'sys.exit(1)\n',encoding='utf-8')
            args=argparse.Namespace(mode='demo',video='BV0000000000',seconds=1,run_dir=root/'run',
                                    repeat=2,concurrency=4,new_ip=True,ip_retries=2,ip_limit=5,failure_limit=0)
            inspector=Inspector()
            with patch.object(batch_worker,'ROOT',root):
                batch_worker.run_batch(args,batch_worker.ControlRelay(),inspector.record,Mock())
            self.assertEqual(inspector.batch['ip_attempts'],5)
            self.assertEqual(inspector.batch['current'],2)
            self.assertEqual(inspector.batch['cancelled'],6)
            self.assertTrue(inspector.batch['quota_exhausted'])

    def test_concurrent_workers_never_exceed_attempt_cap(self):
        with tempfile.TemporaryDirectory() as folder:
            args=argparse.Namespace(mode='live',video='BV0000000000',seconds=1,run_dir=Path(folder),
                                    repeat=3,concurrency=10,new_ip=True,ip_retries=2,ip_limit=7,failure_limit=0)
            def launch(command,**kwargs):
                count=int(command[command.index('--ip-retries')+1])+1
                events=[{'kind':'proxy','status':'acquiring'}]*count+[{'kind':'lifecycle','status':'failed'}]
                proc=Mock(pid=None,stdout=io.StringIO('\n'.join(json.dumps(e) for e in events)+'\n'),stdin=io.StringIO())
                proc.wait.return_value=1
                return proc
            inspector=Inspector()
            with patch.object(batch_worker.subprocess,'Popen',side_effect=launch):
                batch_worker.run_batch(args,batch_worker.ControlRelay(),inspector.record,Mock())
            self.assertEqual(inspector.batch['ip_attempts'],7)
            self.assertTrue(inspector.batch['quota_exhausted'])
            self.assertEqual(inspector.batch['current']+inspector.batch['cancelled'],30)
            self.assertEqual(inspector.status,'completed_with_errors')

    def test_unused_reservations_return_but_crash_is_charged_conservatively(self):
        relay=batch_worker.ControlRelay(); relay.debug_controls=False
        limits=DispatchLimits(relay,4,0)
        self.assertEqual(limits.reserve(3),3)
        limits.finish(3,1,True,'completed')
        self.assertEqual(limits.reserve(3),3)
        limits.finish(3,0,False,'failed')
        self.assertEqual(limits.spent,4)
        self.assertIsNone(limits.reserve(3))

    def test_pause_waits_for_resume_without_pausing_active_worker(self):
        relay=batch_worker.ControlRelay(); relay.debug_controls=False
        relay.proc=Mock(); relay.limits=limits=DispatchLimits(relay,0,2)
        limits.finish(0,0,True,'failed'); self.assertFalse(relay.paused)
        limits.finish(0,0,True,'failed'); self.assertTrue(relay.paused)
        waiting=threading.Event()
        def reserve():
            waiting.set()
            return limits.reserve(1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(reserve)
            try:
                self.assertTrue(waiting.wait(1))
                self.assertFalse(future.done())
                relay.proc.stdin.write.assert_not_called()
            finally: relay.send('resume')
            self.assertEqual(future.result(timeout=1),1)
        self.assertEqual(limits.failures,0)

    def test_quota_exhaustion_wins_over_automatic_pause(self):
        relay=batch_worker.ControlRelay(); relay.debug_controls=False
        limits=DispatchLimits(relay,1,1)
        self.assertEqual(limits.reserve(3),1)
        limits.finish(1,1,True,'failed')
        self.assertTrue(relay.paused)
        self.assertIsNone(limits.reserve(3))

    def test_invalid_limits_rejected_before_any_launch(self):
        for key, value in [('ip_limit',-1),('ip_limit',400001),('ip_limit',True),
                            ('failure_limit',-1),('failure_limit',1.5),('failure_limit',1001)]:
            with self.subTest(key=key,value=value),patch('debug_server.subprocess.Popen') as launch:
                with self.assertRaises(ValueError):Inspector().start('demo','',1,**{key:value})
                launch.assert_not_called()


if __name__=='__main__': unittest.main()
