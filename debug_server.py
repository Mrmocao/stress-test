"""Loopback-only runtime inspector, no web framework or frontend build required."""
import argparse
from collections import deque
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from playback_duration import duration_range
from playback_heartbeat import heartbeat_interval as validate_heartbeat_interval
from dispatch_limits import validate_limits
from device_profiles import BASELINE, catalog, generation_rules, validate_selection
from device_parameter_rules import tv_apk_catalog
from platform_contracts import platform_catalog, validate_playback_platform
from public_state import public_copy
from protocol_capabilities import CAPABILITIES
from offline_options import validate_offline_options
from ip_proxy import config_status, save_config

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / 'debug_ui'
SOURCES = {'tv_playback.py', 'local_signing.py', 'buvid_lab.py', 'did_lab.py'}


def batch_state(repeat=1, concurrency=1):
    return {'total': repeat * concurrency, 'repeat': repeat, 'concurrency': concurrency,
            'current': 0, 'completed': 0, 'failed': 0, 'stopped': 0, 'cancelled': 0,
            'requests': 0, 'errors': 0, 'ip_attempts': 0, 'ip_failures': 0, 'ip_used': 0, 'rounds': []}


def normalize_video(value):
    value = value.strip()
    if value.startswith(('https://', 'http://')):
        parsed = urllib.parse.urlsplit(value)
        if parsed.hostname not in ('www.bilibili.com', 'bilibili.com', 'm.bilibili.com'):
            raise ValueError('请输入 bilibili.com 视频链接或 BV/av 号')
        value = parsed.path.rstrip('/').split('/')[-1]
    if not re.fullmatch(r'(?:BV[0-9A-Za-z]{10}|(?:av)?[1-9][0-9]*)', value):
        raise ValueError('BV 格式：BV + 10 位字母数字；也支持 av 号或纯数字 AID')
    return value


class Inspector:
    def __init__(self):
        self.lock = threading.RLock()
        self.events = deque(maxlen=6000)
        self.seq = 0
        self.proc = None
        self.status = 'idle'
        self.mode = 'demo'
        self.run_dir = None
        self.trace_file = None
        self.current = None
        self.scopes = {}
        self.metrics = {}
        self.started = None
        self.stopped = False
        self.collector = None
        self.proxy = {'status': 'disabled', 'enabled': False}
        self.logs = deque(maxlen=2000)
        self.key_file = None
        self.active_dir = None
        self.new_ip = False
        self.batch = batch_state()
        self.workers = {}
        self.trace_enabled = False
        self.dispatch_paused = False
        self.trace_capped = False

    def worker(self, lane):
        if lane not in self.workers:
            self.workers[lane] = {'lane': lane, 'round': 0, 'status': 'idle', 'run_dir': None,
                                  'proxy': {'enabled': self.new_ip, 'status': 'disabled'},
                                  'metrics': {}, 'scopes': {}, 'current': None, 'reason': '', 'progress': {}, 'device_profile': None}
        return self.workers[lane]

    def focus(self, worker):
        self.proxy, self.metrics = worker['proxy'], worker['metrics']
        self.current, self.scopes = worker['current'], worker['scopes']
        self.active_dir = Path(worker['run_dir']) if worker['run_dir'] else self.run_dir

    def record(self, event):
        event = public_copy(event)
        with self.lock:
            kind = event['kind']
            lane = event.setdefault('lane', 0 if kind in ('launch', 'lifecycle', 'control', 'process_exit') else 1)
            worker = self.worker(lane) if lane else None
            event.setdefault('round', worker['round'] if worker else 0)
            self.seq += 1
            event['id'] = self.seq
            event.setdefault('at', time.time())
            self.events.append(event)
            if self.trace_file and not self.trace_capped:
                if self.trace_file.tell() >= 32 * 1024 * 1024:
                    self.trace_capped = True
                else:
                    self.trace_file.write(json.dumps(event, ensure_ascii=False) + '\n')
                    self.trace_file.flush()
            if kind not in ('trace', 'progress'):
                self.logs.append(event)
                if self.key_file:
                    self.key_file.write(json.dumps(event, ensure_ascii=False) + '\n')
                    self.key_file.flush()
            if kind == 'round':
                if event['phase'] == 'start':
                    self.batch['current'] += 1
                    worker.update(round=event['round'], run_dir=event['run_dir'], status='preparing',
                                  scopes={}, current=None, reason='', progress={}, device_profile=None,
                                  metrics={'request_count': 0, 'request_errors': 0, 'ip_attempts': 0, 'ip_failures': 0, 'ip_used': 0},
                                  proxy={'enabled': self.new_ip, 'status': 'acquiring' if self.new_ip else 'disabled'})
                    worker['requested_seconds'] = event.get('target_seconds')
                    if event.get('target_seconds') is not None:
                        worker['progress'] = {'target_seconds': event['target_seconds'], 'decoded_seconds': 0}
                else:
                    outcome = event['status']
                    worker['status'] = outcome
                    if outcome in ('completed', 'failed', 'stopped'):
                        self.batch[outcome] += 1
                    self.batch['rounds'].append({'lane': lane, 'number': event['round'], 'status': outcome,
                                                'exit_ip': worker['proxy'].get('exit_ip'),
                                                'endpoint': worker['proxy'].get('endpoint'),
                                                'requests': worker['metrics'].get('request_count', 0),
                                                'errors': worker['metrics'].get('request_errors', 0),
                                                'ip_attempts': worker['metrics'].get('ip_attempts', 0),
                                                'ip_used': worker['metrics'].get('ip_used', 0),
                                                'requested_seconds': worker.get('requested_seconds'),
                                                'progress': dict(worker['progress']),
                                                'device_profile': worker['device_profile'],
                                                'reason': event.get('reason') or worker['reason']})
            if kind == 'device_profile':
                worker['device_profile'] = event['profile']
            if kind in ('worker_lifecycle', 'worker_control'):
                worker['status'] = event['status']
            if kind == 'cooldown':
                worker['status'] = 'waiting'
            if kind == 'failure':
                worker['reason'] = event.get('message', '')
            if kind == 'dispatch':
                self.dispatch_paused = event['paused']
                self.batch['dispatch_message'] = event.get('message', '')
            if kind == 'quota':
                self.batch['quota_exhausted'] = event['exhausted']
                self.batch['quota_charged_attempts'] = event['charged_attempts']
            if kind == 'cancelled_rounds':
                self.batch['cancelled'] += event['count']
            if kind == 'progress':
                worker['progress'] = {key: event[key] for key in
                                      ('stage', 'decoded_seconds', 'target_seconds', 'frames', 'wall_seconds', 'at',
                                       'media_status', 'report_status')
                                      if key in event}
            if kind == 'network':
                error = bool(event.get('error_type') or event.get('http_status') != 200
                             or event.get('code') != 0)
                self.batch['requests'] += 1
                self.batch['errors'] += int(error)
                metrics = worker['metrics']
                metrics['request_count'] = metrics.get('request_count', 0) + 1
                metrics['request_errors'] = metrics.get('request_errors', 0) + int(error)
            if kind == 'proxy':
                worker['proxy'] = {k: v for k, v in event.items() if k not in ('kind', 'id', 'at', 'lane', 'round')}
                # Emitted once after extraction succeeds, before exit verification.
                # Count retries even if verification/playback subsequently fails.
                if event['status'] == 'verifying':
                    self.batch['ip_used'] += 1
                    worker['metrics']['ip_used'] = worker['metrics'].get('ip_used', 0) + 1
                if event['status'] in ('acquiring', 'retrying', 'failed'):
                    field = 'ip_attempts' if event['status'] == 'acquiring' else 'ip_failures'
                    self.batch[field] += 1
                    worker['metrics'][field] = worker['metrics'].get(field, 0) + 1
            if kind == 'trace':
                worker['current'] = event
                worker['scopes'][event['scope']] = event
                values = event['locals']
                metrics = worker['metrics']
                for key in ('decoded', 'frames', 'elapsed', 'target', 'start_ts', 'first_frame'):
                    if key in values:
                        metrics[key] = values[key]
                item = values.get('item')
                if isinstance(item, dict):
                    for key in ('decoded_seconds', 'frames', 'wall_seconds', 'quality', 'click_retry_count'):
                        if key in item:
                            metrics[key] = item[key]
                if event['function'] == 'request' and 'index' in values:
                    metrics['api_count'] = values['index']
            elif kind in ('lifecycle', 'control') and not self.stopped:
                self.status = event['status']
            if worker:
                self.focus(worker)
                if not self.stopped:
                    active = [w['status'] for w in self.workers.values() if w['status'] not in ('idle', 'completed', 'failed', 'stopped')]
                    self.status = 'paused' if active and all(s == 'paused' for s in active) else 'running'
            if kind == 'process_exit' and self.run_dir:
                try:
                    (self.run_dir / 'summary.json').write_text(json.dumps({'status': self.status, **self.batch}, ensure_ascii=False, indent=2), encoding='utf-8')
                except OSError:
                    pass

    def start(self, mode, video, seconds, paused=False, new_ip=False, repeat=1, concurrency=1, ip_retries=0, debug=False,
              device_profile=BASELINE, device_confirmed=False, seconds_min=None, seconds_max=None,
              ip_limit=0, failure_limit=5, heartbeat_interval=0, context_preset='none', tv_lifecycle=False,
              platform='tv'):
        validate_playback_platform(platform)
        if mode not in ('demo', 'live'):
            raise ValueError('Unknown mode')
        seconds_min, seconds_max = duration_range(seconds, seconds_min, seconds_max)
        validate_limits(ip_limit, failure_limit)
        heartbeat_interval = validate_heartbeat_interval(heartbeat_interval)
        if type(repeat) is not int or not 1 <= repeat <= 1000:
            raise ValueError('重复执行次数必须为 1～1000 的整数')
        if type(concurrency) is not int or not 1 <= concurrency <= 100:
            raise ValueError('并发数必须为 1～100 的整数')
        if type(ip_retries) is not int or not 0 <= ip_retries <= 3:
            raise ValueError('IP 额外重试次数必须为 0～3 的整数')
        if type(debug) is not bool:
            raise ValueError('debug 必须为布尔值')
        validate_selection(mode, device_profile, device_confirmed)
        validate_offline_options(mode, device_profile, context_preset, tv_lifecycle,
                                 repeat, concurrency, debug, paused)
        video = normalize_video(video) if mode == 'live' else 'BV0000000000'
        if not isinstance(new_ip, bool):
            raise ValueError('new_ip 必须为布尔值')
        if new_ip and mode != 'live':
            raise ValueError('离线演示不提取代理 IP；请切换到真实播放模式')
        if new_ip:
            from ip_proxy import load_config
            load_config()
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                raise RuntimeError('已有运行实例，请先停止或等待结束')
            if self.collector is not None and self.collector.is_alive():
                raise RuntimeError('上次运行正在写入最后的日志，请稍后再启动')
            if self.trace_file:
                self.trace_file.close()
            if self.key_file:
                self.key_file.close()
            self.events.clear()
            self.logs.clear()
            self.scopes.clear()
            self.current = None
            self.metrics = {}
            self.proxy = {'status': 'acquiring' if new_ip else 'disabled', 'enabled': new_ip}
            self.status, self.mode, self.stopped = 'preparing', mode, False
            self.started = time.time()
            self.new_ip = new_ip
            self.trace_enabled = concurrency == 1 and (debug or (paused and not new_ip))
            self.dispatch_paused, self.trace_capped = paused, False
            self.batch = batch_state(repeat, concurrency)
            self.batch.update(seconds_min=seconds_min, seconds_max=seconds_max)
            self.batch.update(ip_limit=ip_limit, failure_limit=failure_limit)
            self.batch['heartbeat_interval'] = heartbeat_interval
            self.batch.update(context_preset=context_preset, tv_lifecycle=tv_lifecycle)
            self.workers.clear()
            for lane in range(1, concurrency + 1):
                self.worker(lane)
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)
            self.run_dir = ROOT / '.debug-runs' / stamp
            self.run_dir.mkdir(parents=True)
            self.active_dir = self.run_dir
            self.trace_file = (self.run_dir / 'trace.jsonl').open('a', encoding='utf-8')
            self.key_file = (self.run_dir / 'key-events.jsonl').open('a', encoding='utf-8')
            command = [sys.executable, '-u', str(ROOT / 'batch_worker.py'), '--mode', mode,
                       '--video', video, '--seconds', str(seconds), '--run-dir', str(self.run_dir),
                       '--repeat', str(repeat), '--concurrency', str(concurrency), '--ip-retries', str(ip_retries)]
            command.extend(['--seconds-min', str(seconds_min), '--seconds-max', str(seconds_max)])
            command.extend(['--ip-limit', str(ip_limit), '--failure-limit', str(failure_limit)])
            command.extend(['--device-profile', device_profile])
            command.extend(['--heartbeat-interval', str(heartbeat_interval)])
            command.extend(['--context-preset', context_preset])
            if tv_lifecycle:
                command.append('--tv-lifecycle')
            if device_confirmed is True:
                command.append('--device-confirmed')
            if paused:
                command.append('--paused')
            if new_ip:
                command.append('--new-ip')
            if self.trace_enabled:
                command.append('--trace')
            env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1'}
            try:
                self.proc = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                             text=True, encoding='utf-8', errors='replace', env=env,
                                             start_new_session=os.name != 'nt')
            except OSError:
                self.status = 'failed'
                raise
            self.record({'kind': 'launch', 'mode': mode, 'video': video, 'seconds': seconds,
                         'seconds_min': seconds_min, 'seconds_max': seconds_max,
                         'ip_limit': ip_limit, 'failure_limit': failure_limit,
                         'heartbeat_interval': heartbeat_interval,
                         'device_profile_selection': device_profile,
                         'repeat': repeat, 'concurrency': concurrency, 'ip_retries': ip_retries,
                         'python': sys.executable, 'pid': self.proc.pid})
            self.collector = threading.Thread(target=self.collect, args=(self.proc,), daemon=True)
            self.collector.start()
            return str(self.run_dir)

    def collect(self, proc):
        for line in proc.stdout:
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or 'kind' not in event:
                    raise ValueError()
            except (ValueError, TypeError):
                event = {'kind': 'console', 'message': line.rstrip()}
            with self.lock:
                # Stop owns the final state; discard already-buffered child output.
                if not self.stopped:
                    self.record(event)
        code = proc.wait()
        with self.lock:
            if not self.stopped and self.status not in ('completed', 'completed_with_errors', 'failed'):
                self.status = 'failed' if code else 'completed'
            self.record({'kind': 'process_exit', 'returncode': code, 'status': self.status})
            proc.stdout.close()
            proc.stdin.close()

    def control(self, command):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                raise ValueError('没有正在运行的实例')
            if command == 'stop':
                self.stopped = True
                self.status = 'stopped'
                # Emergency stop includes FFmpeg descendants; never leave a decoder behind.
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(self.proc.pid), '/T', '/F'],
                                   capture_output=True, timeout=10)
                else:
                    import signal
                    # Worker descendants are found through the process group on POSIX.
                    os.killpg(self.proc.pid, signal.SIGKILL)
                self.record({'kind': 'control', 'status': 'stopped'})
                for lane, worker in self.workers.items():
                    if worker['proxy'].get('status') in ('acquiring', 'verifying', 'retrying'):
                        worker['proxy'].update(status='cancelled', message='本路任务已停止')
                    if worker['round'] and not any(row['lane'] == lane and row['number'] == worker['round'] for row in self.batch['rounds']):
                        self.record({'kind': 'round', 'phase': 'finish', 'lane': lane,
                                     'round': worker['round'], 'status': 'stopped'})
                    worker['status'] = 'stopped'
            elif command in ('pause', 'resume', 'step'):
                try:
                    self.proc.stdin.write(json.dumps({'command': command}) + '\n')
                    self.proc.stdin.flush()
                except (OSError, ValueError) as exc:
                    raise ValueError('进程已经结束') from exc
            else:
                raise ValueError('Unknown command')

    def snapshot(self, after, detail=True, lane=1):
        with self.lock:
            events = [e for e in (self.events if detail else self.logs)
                      if e['id'] > after and (e['kind'] != 'trace' or e.get('lane', 1) == lane)][:200]
            state = None
            worker = self.workers.get(lane) or (next(iter(self.workers.values())) if self.workers else None)
            if worker:
                self.focus(worker)
            if self.active_dir and detail:
                try:
                    state = json.loads((self.active_dir / 'playback' / 'state.json').read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    pass
            return {'status': self.status, 'mode': self.mode, 'run_dir': str(self.run_dir or ''),
                    'started': self.started, 'pid': self.proc.pid if self.proc else None,
                    'running': self.proc is not None and self.proc.poll() is None,
                    'cursor': events[-1]['id'] if events else max(after, self.seq),
                    'latest_id': self.seq, 'events': events, 'current': self.current if detail else None,
                    'scopes': [{'scope': k, 'file': v['file'], 'function': v['function'],
                                'line': v['line']} for k, v in self.scopes.items()] if detail else [],
                    'metrics': self.metrics, 'state': public_copy(state), 'proxy': self.proxy,
                    'batch': {**self.batch, 'rounds': [dict(row) for row in self.batch['rounds']]},
                    'workers': [{'lane': w['lane'], 'round': w['round'], 'status': w['status'],
                                 'proxy': dict(w['proxy']), 'metrics': dict(w['metrics']), 'reason': w['reason'],
                                 'progress': dict(w['progress']), 'device_profile': w['device_profile']}
                                for w in self.workers.values()],
                    'trace_enabled': self.trace_enabled,
                    'dispatch_paused': self.dispatch_paused, 'trace_capped': self.trace_capped,
                    'logs_incremental': True, 'duration_range_supported': True, 'efficiency_controls_supported': True,
                    'periodic_heartbeat_supported': True, 'offline_context_supported': True,
                    'protocol_capabilities': CAPABILITIES,
                    'logs': [e for e in self.logs if after < e['id'] <= (events[-1]['id'] if events else max(after, self.seq))],
                    'history_truncated': bool(self.events and after and after < self.events[0]['id'] - 1)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body, content_type='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        return secrets.compare_digest(self.headers.get('X-Inspector-Token', ''), self.server.token)

    def valid_host(self):
        return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

    def do_GET(self):
        if not self.valid_host():
            return self.reply(403, {'error': 'Loopback host required'})
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path.startswith('/api/'):
            if not self.authorized():
                return self.reply(403, {'error': 'Invalid inspector token'})
            params = urllib.parse.parse_qs(parsed.query)
            if parsed.path == '/api/device-profiles':
                return self.reply(200, {'profiles': catalog(), 'generation_rules': generation_rules(),
                                       'apk_catalog': tv_apk_catalog(), 'platforms': platform_catalog()})
            if parsed.path == '/api/juliang-config':
                return self.reply(200, config_status())
            if parsed.path == '/api/snapshot':
                try:
                    after = max(0, int(params.get('after', ['0'])[0]))
                except ValueError:
                    return self.reply(400, {'error': 'Invalid cursor'})
                try:
                    lane = int(params.get('lane', ['1'])[0])
                except ValueError:
                    return self.reply(400, {'error': 'Invalid lane'})
                return self.reply(200, self.server.inspector.snapshot(after, params.get('detail', ['1'])[0] == '1', lane))
            if parsed.path == '/api/source':
                name = params.get('file', ['tv_playback.py'])[0]
                if name not in SOURCES:
                    return self.reply(404, {'error': 'Source is not allowlisted'})
                return self.reply(200, {'file': name, 'text': (ROOT / name).read_text(encoding='utf-8')})
            return self.reply(404, {'error': 'Unknown endpoint'})
        files = {'/': ('index.html', 'text/html; charset=utf-8'),
                 '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                 '/style.css': ('style.css', 'text/css; charset=utf-8')}
        if parsed.path not in files:
            return self.reply(404, b'Not found', 'text/plain')
        name, mime = files[parsed.path]
        body = (STATIC / name).read_text(encoding='utf-8').replace('__INSPECTOR_TOKEN__', self.server.token)
        self.reply(200, body.encode('utf-8'), mime)

    def do_POST(self):
        if not self.valid_host() or not self.authorized():
            return self.reply(403, {'error': 'Invalid host or token'})
        origin = self.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'):
            return self.reply(403, {'error': 'Invalid origin'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 8192:
                raise ValueError('Invalid request size')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError('Expected JSON object')
            if self.path == '/api/start':
                directory = self.server.inspector.start(data.get('mode', 'demo'), str(data.get('video', '')),
                                                       float(data.get('seconds', 45)), bool(data.get('paused', False)),
                                                       data.get('new_ip', False), data.get('repeat', 1),
                                                       data.get('concurrency', 1), data.get('ip_retries', 0),
                                                       data.get('debug', False), data.get('device_profile', BASELINE),
                                                       data.get('device_confirmed', False),
                                                       seconds_min=data.get('seconds_min'), seconds_max=data.get('seconds_max'),
                                                       ip_limit=data.get('ip_limit', 0), failure_limit=data.get('failure_limit', 5),
                                                       heartbeat_interval=data.get('heartbeat_interval', 0),
                                                       context_preset=data.get('context_preset', 'none'),
                                                       tv_lifecycle=data.get('tv_lifecycle', False),
                                                       platform=data.get('platform', 'tv'))
                return self.reply(200, {'run_dir': directory})
            if self.path == '/api/control':
                self.server.inspector.control(data.get('command'))
                return self.reply(200, {'ok': True})
            if self.path == '/api/juliang-config':
                return self.reply(200, save_config(data.get('trade_no', ''), data.get('api_key', '')))
            return self.reply(404, {'error': 'Unknown endpoint'})
        except (ValueError, TypeError, OSError, RuntimeError) as error:
            return self.reply(400, {'error': str(error)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.inspector = Inspector()
    server.token = secrets.token_urlsafe(32)
    print(f'Runtime inspector: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server.inspector.proc and server.inspector.proc.poll() is None:
            server.inspector.control('stop')
        server.server_close()


if __name__ == '__main__':
    main()
