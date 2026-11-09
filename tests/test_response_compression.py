import gzip
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from network_deadline import read_json_body, ResponseEncodingError
from execution_budget import ExecutionBudget, BudgetExceeded
import tv_playback as tv


class Reply(io.BytesIO):
    def __init__(self, body, encoding='gzip'):
        super().__init__(body)
        self.headers = {'Content-Encoding': encoding}


class CompressionTests(unittest.TestCase):
    def test_gzip_and_plain_have_same_json_with_separate_byte_counts(self):
        data = json.dumps({'data': 'x' * 388000}).encode()
        for encoding, wire in [('gzip', gzip.compress(data)), ('identity', data)]:
            event = {}
            self.assertEqual(read_json_body(Reply(wire, encoding), event, time.monotonic()+1), data)
            self.assertEqual(event['bytes_read'], len(wire))
            self.assertEqual(event['decoded_bytes'], len(data))

    def test_invalid_truncated_trailing_and_unknown_encoding_rejected(self):
        wire = gzip.compress(b'{}')
        for body, encoding in [(wire[:-1], 'gzip'), (b'bad', 'gzip'),
                               (wire+b'extra', 'gzip'), (wire, 'br')]:
            with self.subTest(encoding=encoding, body=body), self.assertRaises(ResponseEncodingError):
                read_json_body(Reply(body, encoding), {}, time.monotonic()+1)

    def test_wire_and_decompressed_limits(self):
        for data in [Reply(b'x'*101, 'identity'), Reply(gzip.compress(b'x'*101))]:
            with self.assertRaises(ValueError):
                read_json_body(data, {}, time.monotonic()+1, max_bytes=100)
        self.assertEqual(read_json_body(Reply(gzip.compress(b'x'*100)), {}, time.monotonic()+1, 100), b'x'*100)

    def test_playback_preserves_finish_reserve_and_end_request_uses_it(self):
        clock = Mock(return_value=0)
        budget = ExecutionBudget(90, 45, clock=clock)
        budget.begin_playback(45)
        clock.return_value=80
        self.assertEqual(budget.timeout(8), 3)
        clock.return_value=83
        with self.assertRaises(BudgetExceeded): budget.timeout(8)
        budget.begin_finishing()
        self.assertEqual(budget.timeout(8), 5)
        clock.return_value=88
        with self.assertRaises(BudgetExceeded): budget.timeout(8)

    def test_actual_get_and_post_negotiate_gzip_and_save_decoded_response(self):
        received=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def serve(self):
                received.append((self.command, self.headers.get('Accept-Encoding')))
                if self.command=='POST': self.rfile.read(int(self.headers['Content-Length']))
                raw=gzip.compress(json.dumps({'code':0,'data':{'value':'ok'}}).encode())
                self.send_response(200)
                self.send_header('Content-Encoding','gzip')
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            do_GET=serve
            do_POST=serve
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder, \
                 patch.object(tv,'API_ROOT',f'http://127.0.0.1:{server.server_port}'), patch.object(tv,'WEB_API_ROOT',f'http://127.0.0.1:{server.server_port}'), \
                 patch.object(tv,'HTTP_OPENER',urllib.request.build_opener(urllib.request.ProxyHandler({}))), \
                 patch.object(tv,'RUN_BUDGET',ExecutionBudget(time.monotonic()+90,45)):
                self.assertEqual(tv.public_json('/x/web-interface/view'),{'value':'ok'})
                client=tv.TVClient(Path(folder),Path(folder)/'events.jsonl',buvid='test')
                self.assertEqual(client.request('/x/report/heartbeat/mobile',{},'FORM')['code'],0)
                self.assertEqual(json.loads((Path(folder)/'response-001.body').read_bytes())['code'],0)
                event=json.loads((Path(folder)/'events.jsonl').read_text(encoding='utf-8'))
                self.assertEqual(event['content_encoding'],'gzip')
            self.assertEqual(received,[('GET','gzip'),('POST','gzip')])
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)


if __name__=='__main__': unittest.main()
