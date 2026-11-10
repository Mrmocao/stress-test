"""Local HTTP fault fixtures. No forwarding, DNS lookup or upstream requests."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading
import time
from urllib.parse import urlsplit


SCENARIOS = ('normal', 'slow', 'unavailable', 'disconnect', 'timeout')


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, scenario, log_path, routes, delay=1.0):
        if scenario not in SCENARIOS:
            raise ValueError('Unknown network scenario')
        super().__init__(address, Handler)
        self.scenario, self.log_path = scenario, Path(log_path)
        self.routes, self.delay = routes, delay
        self.log_lock = threading.Lock()
        self.stopping = threading.Event()

    def record(self, **event):
        with self.log_lock, self.log_path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'at': time.time(), 'scenario': self.scenario, **event}) + '\n')

    def server_close(self):
        self.stopping.set()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_CONNECT(self):
        self.send_error(405, 'Proxying is disabled')

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        self.connection.settimeout(5)
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc:
            self.send_error(400, 'Only local origin-form paths are accepted')
            return
        path = parsed.path
        if path == '/__health':
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'ok')
            return
        # Query strings, headers and request bodies can contain credentials.
        # Retain route keys and sizes only, never arbitrary client strings.
        known = path in self.server.routes
        self.server.record(method=self.command, route=path if known else '<unknown>', matched=known)
        if self.headers.get('Transfer-Encoding'):
            self.send_error(411)
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            self.send_error(400)
            return
        if not 0 <= size <= 1024 * 1024:
            self.send_error(413)
            return
        try:
            self.rfile.read(size)
            if not known:
                self.send_error(404)
                return
            scenario = self.server.scenario
            if scenario == 'disconnect':
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
                return
            if scenario == 'timeout':
                self.server.stopping.wait(120)
                self.close_connection = True
                return
            if scenario == 'slow':
                self.server.stopping.wait(self.server.delay)
            fixture = self.server.routes[path]
            status = 503 if scenario == 'unavailable' else fixture['status']
            body = json.dumps({'error': 'unavailable'} if status == 503 else fixture['json']).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (OSError, TimeoutError):
            self.close_connection = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=SCENARIOS, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--log', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    with FixtureServer(('127.0.0.1', 18080), args.scenario, args.log,
                       config['routes'], config['slow_seconds']) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
