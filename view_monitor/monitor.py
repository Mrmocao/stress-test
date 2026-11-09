"""Standalone, read-only Bilibili public view-count monitor (Python stdlib only)."""
import argparse
import csv
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
TZ = timezone(timedelta(hours=8))
ACTIVE = {'starting', 'watching', 'retrying', 'stopping'}
CSV_FIELDS = ['observed_at', 'bvid', 'view', 'delta_baseline', 'delta_previous', 'ok', 'latency_ms', 'error']


def iso(epoch=None):
    return datetime.fromtimestamp(time.time() if epoch is None else epoch, TZ).isoformat(timespec='seconds')


def save_json(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def normalize_video(value):
    value = str(value).strip()
    if value.startswith(('https://', 'http://')):
        url = urllib.parse.urlsplit(value)
        if url.hostname not in ('bilibili.com', 'www.bilibili.com', 'm.bilibili.com'):
            raise ValueError('请使用 bilibili.com 视频链接；短链接请先在浏览器打开')
        match = re.fullmatch(r'/video/([^/]+)/?', url.path)
        if not match:
            raise ValueError('链接需要包含 /video/BV… 或 /video/av…')
        value = match.group(1)
    if re.fullmatch(r'BV[0-9A-Za-z]{10}', value):
        return 'bvid', value
    if re.fullmatch(r'(?:av)?[1-9][0-9]*', value):
        return 'aid', value.removeprefix('av')
    raise ValueError('请输入 BV + 10 位字母数字、av 号、数字 AID 或完整视频链接')


def fetch_public_stats(key, value):
    """The sole external network operation: metadata GET, no playback/report APIs."""
    query = urllib.parse.urlencode({key: value, '_monitor_time': time.time_ns()})
    request = urllib.request.Request('https://api.bilibili.com/x/web-interface/view?' + query,
        headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json',
                 'Referer': 'https://www.bilibili.com/', 'Cache-Control': 'no-cache'})
    started = time.monotonic()
    requested_at = iso()
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError('API response exceeded 2 MB')
        metadata = {name: response.headers.get(name) for name in ('Date', 'Age', 'Cache-Control', 'X-Cache')}
        result = json.loads(raw)
    if result.get('code') != 0:
        raise ValueError(f'API {result.get("code")}: {result.get("message", "查询失败")}')
    data = result.get('data') or {}
    if data.get('state') != 0:
        raise ValueError(f'视频当前不可用，state={data.get("state")}')
    if key == 'bvid' and data.get('bvid') != value:
        raise ValueError('返回的视频 BV 号与请求不符')
    if key == 'aid' and data.get('aid') != int(value):
        raise ValueError('返回的视频 AID 与请求不符')
    view = (data.get('stat') or {}).get('view')
    if type(view) is not int or view < 0:
        raise ValueError('接口没有提供有效的公开播放量；不会将缺失值计为 0')
    return {'observed_at': iso(), 'requested_at': requested_at, 'ok': True,
            'bvid': data['bvid'], 'aid': data['aid'], 'title': data.get('title', ''),
            'owner': (data.get('owner') or {}).get('name', ''), 'view': view,
            'stats': data['stat'], 'latency_ms': round((time.monotonic() - started) * 1000),
            'response_headers': metadata, 'raw_response': result}


def apply_sample(summary, sample):
    """Pure accounting: failed samples never replace the baseline/latest count."""
    summary['sample_count'] += 1
    summary['last_attempt_at'] = sample['observed_at']
    if not sample['ok']:
        summary['error_count'] += 1
        summary['last_error'] = sample['error']
        sample.update(view=None, delta_baseline=None, delta_previous=None)
        return
    previous = summary.get('latest_view')
    if summary.get('baseline_view') is None:
        summary['baseline_view'] = sample['view']
        summary['baseline_at'] = sample['observed_at']
    sample['delta_baseline'] = sample['view'] - summary['baseline_view']
    sample['delta_previous'] = sample['view'] - previous if previous is not None else 0
    summary.update(latest_view=sample['view'], latest_at=sample['observed_at'],
                   delta_baseline=sample['delta_baseline'], last_error=None,
                   bvid=sample['bvid'], aid=sample['aid'], title=sample.get('title', ''),
                   owner=sample.get('owner', ''), latest_stats=sample.get('stats', {}))
    summary['success_count'] += 1
    if sample['delta_baseline'] > 0 and not summary.get('first_increase_at'):
        summary['first_increase_at'] = sample['observed_at']
    if sample['delta_previous'] < 0:
        summary['decrease_count'] += 1


class Monitor:
    def __init__(self, data_dir, fetcher=fetch_public_stats):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.fetcher = fetcher
        self.lock = threading.RLock()
        self.thread = None
        self.stop_event = None
        self.current = None
        self.samples = []
        # One service owns this directory (enforced by main's process lock).
        for path in sorted(self.data_dir.glob('*/session.json'), reverse=True):
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
                if record['status'] in ACTIVE:
                    record.update(status='interrupted', ended_at=iso(), next_sample_at=None)
                    save_json(path, record)
                if self.current is None:
                    self.current = record
                    self.samples = self.read_samples(record['id'])
            except (OSError, ValueError, KeyError):
                continue

    def read_samples(self, session_id):
        try:
            lines = (self.data_dir / session_id / 'samples.jsonl').read_text(encoding='utf-8').splitlines()
        except FileNotFoundError:
            return []
        rows = []
        for line in lines:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass  # A partial last line after a hard shutdown is not a zero count.
        return rows

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, video, interval=30, minutes=60):
        key, value = normalize_video(video)
        interval, minutes = float(interval), float(minutes)
        if not math.isfinite(interval) or not 15 <= interval <= 3600:
            raise ValueError('查询间隔应在 15～3600 秒之间')
        if not math.isfinite(minutes) or not 1 <= minutes <= 1440:
            raise ValueError('观测时长应在 1～1440 分钟之间')
        with self.lock:
            if self.running():
                raise ValueError('已有观测正在进行，请先停止它')
            identifier = datetime.now(TZ).strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)
            directory = self.data_dir / identifier
            (directory / 'responses').mkdir(parents=True)
            now = time.time()
            summary = {'id': identifier, 'query_key': key, 'query_value': value, 'input': str(video),
                       'bvid': value if key == 'bvid' else None, 'title': '', 'status': 'starting',
                       'started_at': iso(now), 'expected_end_at': iso(now + minutes * 60),
                       'ended_at': None, 'interval_seconds': interval, 'duration_minutes': minutes,
                       'baseline_view': None, 'baseline_at': None, 'latest_view': None,
                       'latest_at': None, 'delta_baseline': None, 'first_increase_at': None,
                       'sample_count': 0, 'success_count': 0, 'error_count': 0, 'decrease_count': 0,
                       'last_error': None, 'next_sample_at': iso(now), 'causation_established': False,
                       'source': 'GET /x/web-interface/view → data.stat.view'}
            save_json(directory / 'session.json', summary)
            self.current, self.samples = summary, []
            self.stop_event = threading.Event()
            self.thread = threading.Thread(target=self.loop, args=(summary, self.stop_event),
                                           name='public-view-monitor', daemon=True)
            self.thread.start()
            return identifier

    def loop(self, summary, stop_event):
        directory = self.data_dir / summary['id']
        deadline = time.monotonic() + summary['duration_minutes'] * 60
        failures = 0
        try:
            while not stop_event.is_set() and time.monotonic() < deadline:
                started = time.monotonic()
                raw = None
                try:
                    sample = dict(self.fetcher(summary['query_key'], summary['query_value']))
                    raw = sample.pop('raw_response', None)
                    failures = 0
                except Exception as error:
                    failures += 1
                    sample = {'observed_at': iso(), 'ok': False, 'bvid': summary.get('bvid'),
                              'latency_ms': round((time.monotonic() - started) * 1000),
                              'error': f'{type(error).__name__}: {error}'}
                with self.lock:
                    apply_sample(summary, sample)
                    self.samples.append(sample)
                    number = summary['sample_count']
                    if raw is not None:
                        save_json(directory / 'responses' / f'{number:04}.json', raw)
                    with (directory / 'samples.jsonl').open('a', encoding='utf-8') as out:
                        out.write(json.dumps(sample, ensure_ascii=False) + '\n')
                    new_csv = not (directory / 'samples.csv').exists()
                    with (directory / 'samples.csv').open('a', encoding='utf-8-sig', newline='') as out:
                        writer = csv.DictWriter(out, fieldnames=CSV_FIELDS, extrasaction='ignore')
                        if new_csv:
                            writer.writeheader()
                        writer.writerow(sample)
                    interval = summary['interval_seconds']
                    delay = max(interval, min(300, interval * 2 ** min(failures, 4)))
                    delay = max(0, min(delay, deadline - time.monotonic()))
                    summary.update(status='stopping' if stop_event.is_set() else ('retrying' if failures else 'watching'),
                                   next_sample_at=iso(time.time() + delay) if not stop_event.is_set() else None)
                    save_json(directory / 'session.json', summary)
                stop_event.wait(delay)
            with self.lock:
                summary.update(status='stopped' if stop_event.is_set() else 'completed',
                               ended_at=iso(), next_sample_at=None)
        except Exception as error:
            with self.lock:
                summary.update(status='error', ended_at=iso(), next_sample_at=None,
                               last_error=f'{type(error).__name__}: {error}')
        finally:
            with self.lock:
                save_json(directory / 'session.json', summary)

    def stop(self):
        with self.lock:
            if self.running():
                self.current.update(status='stopping', next_sample_at=None)
                self.stop_event.set()
                save_json(self.data_dir / self.current['id'] / 'session.json', self.current)

    def snapshot(self, session_id=None, all_samples=False):
        with self.lock:
            history = []
            for path in sorted(self.data_dir.glob('*/session.json'), reverse=True)[:50]:
                try:
                    row = json.loads(path.read_text(encoding='utf-8'))
                    history.append({k: row.get(k) for k in ('id', 'bvid', 'started_at', 'status', 'title')})
                except (OSError, ValueError):
                    continue
            summary, rows = self.current, self.samples
            if session_id and (not self.current or session_id != self.current['id']):
                if not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[a-f0-9]{6}', session_id):
                    raise ValueError('无效的观测编号')
                try:
                    summary = json.loads((self.data_dir / session_id / 'session.json').read_text(encoding='utf-8'))
                except FileNotFoundError as error:
                    raise ValueError('观测不存在') from error
                rows = self.read_samples(session_id)
            # Deep copy while holding lock: caller serializes after worker can mutate again.
            return json.loads(json.dumps({'session': summary, 'samples': rows if all_samples else rows[-500:],
                    'history': history, 'running': self.running(),
                    'active_id': self.current['id'] if self.running() else None,
                    'data_dir': str(self.data_dir), 'server_time': iso()}))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body, mime='application/json; charset=utf-8', filename=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def valid_host(self):
        return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

    def authorized(self):
        return self.valid_host() and secrets.compare_digest(self.headers.get('X-Monitor-Token', ''), self.server.token)

    def do_GET(self):
        if not self.valid_host():
            return self.reply(403, {'error': 'Local host required'})
        url = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(url.query)
        if url.path.startswith('/api/'):
            if not self.authorized():
                return self.reply(403, {'error': 'Invalid token; reload this page'})
            try:
                if url.path not in ('/api/status', '/api/export'):
                    return self.reply(404, {'error': 'Not found'})
                snapshot = self.server.monitor.snapshot(params.get('session', [None])[0], url.path == '/api/export')
                if url.path == '/api/status':
                    return self.reply(200, snapshot)
                session = snapshot['session']
                if not session:
                    raise ValueError('尚无观测记录')
                if params.get('format', ['json'])[0] == 'csv':
                    output = io.StringIO(newline='')
                    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, extrasaction='ignore')
                    writer.writeheader()
                    writer.writerows(snapshot['samples'])
                    return self.reply(200, output.getvalue().encode('utf-8-sig'), 'text/csv; charset=utf-8', session['id']+'.csv')
                return self.reply(200, {'session': session, 'samples': snapshot['samples']}, filename=session['id']+'.json')
            except (ValueError, OSError) as error:
                return self.reply(400, {'error': str(error)})
        files = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                 '/style.css': ('style.css', 'text/css; charset=utf-8')}
        if url.path not in files:
            return self.reply(404, {'error': 'Not found'})
        name, mime = files[url.path]
        body = (ROOT/'ui'/name).read_text(encoding='utf-8').replace('__MONITOR_TOKEN__', self.server.token)
        self.reply(200, body.encode('utf-8'), mime)

    def do_POST(self):
        if not self.authorized():
            return self.reply(403, {'error': 'Invalid token or host'})
        origin = self.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'):
            return self.reply(403, {'error': 'Invalid origin'})
        try:
            size = int(self.headers.get('Content-Length', 0))
            if not 0 < size <= 8192:
                raise ValueError('Invalid request size')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError('Expected JSON object')
            if self.path == '/api/start':
                identifier = self.server.monitor.start(data.get('video', ''), data.get('interval', 30), data.get('minutes', 60))
                return self.reply(200, {'id': identifier})
            if self.path == '/api/stop':
                self.server.monitor.stop()
                return self.reply(200, {'ok': True})
            return self.reply(404, {'error': 'Not found'})
        except (ValueError, TypeError, OSError) as error:
            return self.reply(400, {'error': str(error)})


def acquire_lock(path):
    handle = path.open('a+b')
    handle.seek(0, 2)
    if handle.tell() == 0:
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError('Another monitor service owns this data directory')
    return handle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--data-dir', type=Path, default=ROOT/'data')
    parser.add_argument('--video', help='Optionally start a metadata-only observation immediately')
    parser.add_argument('--interval', type=float, default=30)
    parser.add_argument('--minutes', type=float, default=60)
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    service_lock = acquire_lock(args.data_dir/'service.lock')
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.monitor = Monitor(args.data_dir)
    server.token = secrets.token_urlsafe(32)
    if args.video:
        server.monitor.start(args.video, args.interval, args.minutes)
    print(f'Public view monitor: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.monitor.stop()
        if server.monitor.thread:
            server.monitor.thread.join(timeout=25)
        server.server_close()
        service_lock.close()


if __name__ == '__main__':
    main()
