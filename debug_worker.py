"""Isolated instrumented playback. stdout is a JSON event transport."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import inspect
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import types
import urllib.parse
import urllib.request
from device_profiles import BASELINE, resolve_profile
from public_state import PRIVATE_FIELDS, public_copy, is_private_field

ROOT = Path(__file__).resolve().parent
FILES = {'tv_playback.py', 'local_signing.py', 'buvid_lab.py', 'did_lab.py'}
TRANSPORT = sys.stdout
WRITE_LOCK = threading.Lock()
RUN_GATE = threading.Event()
RUN_GATE.set()
STEP = False
EVENTS = 0
LIMIT = 200000
PRIVATE_NAMES = {'APP_SECRET', 'AES_KEY', 'AES_IV', 'CLICK_SALT', 'guest_access_key', 'access_key', 'rpdid'}


def emit(kind, **data):
    with WRITE_LOCK:
        TRANSPORT.write(json.dumps({'kind': kind, 'at': time.time(), **public_copy(data)}, ensure_ascii=True) + '\n')
        TRANSPORT.flush()


def value_snapshot(value, name='', depth=0):
    if name in PRIVATE_NAMES or is_private_field(name):
        return '<redacted constant>'
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        value = re.sub(r'(https?://[^\s?]+)\?[^\s\x27\"]+', r'\1?<signed query hidden>', value)
        value = public_copy(value)
        return value[:1500] + ('…' if len(value) > 1500 else '')
    if isinstance(value, bytes):
        return {'type': 'bytes', 'length': len(value), 'sha256': hashlib.sha256(value).hexdigest()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, argparse.Namespace):
        return value_snapshot(vars(value), name, depth)
    if depth >= 3:
        return f'<{type(value).__name__} len={len(value)}>' if isinstance(value, (dict, list, tuple)) else f'<{type(value).__name__}>'
    if isinstance(value, dict):
        items = list(value.items())[:35]
        result = {str(k): value_snapshot(v, str(k), depth + 1) for k, v in items}
        if len(value) > 35:
            result['…'] = f'{len(value) - 35} more keys'
        return result
    if isinstance(value, (tuple, list)):
        result = [value_snapshot(v, name, depth + 1) for v in value[:25]]
        if len(value) > 25:
            result.append(f'<{len(value) - 25} more items>')
        return result
    if type(value).__name__ == 'TVClient':
        return {k: value_snapshot(v, k, depth + 1) for k, v in vars(value).items()}
    return f'<{type(value).__name__}>'


def control_reader():
    global STEP
    for line in sys.stdin:
        try:
            command = json.loads(line)['command']
            if command == 'pause':
                STEP = False
                RUN_GATE.clear()
                emit('control', status='pausing')
            elif command == 'resume':
                STEP = False
                RUN_GATE.set()
                emit('control', status='running')
            elif command == 'step':
                STEP = True
                RUN_GATE.set()
        except (ValueError, KeyError):
            pass


class Tracer:
    def __init__(self):
        self.previous = {}
        self.lines = {}

    def trace(self, frame, event, arg):
        global EVENTS, STEP
        path = Path(frame.f_code.co_filename)
        if path.name not in FILES or path.parent != ROOT or event not in ('call', 'line', 'return', 'exception'):
            return None
        if EVENTS >= LIMIT:
            if EVENTS == LIMIT:
                emit('notice', message='Trace limit reached; playback continues without further line events.')
                EVENTS += 1
            return None
        fid = id(frame)
        scope = f'{path.name}:{frame.f_code.co_name}:{fid}'
        values = {name: value_snapshot(value, name) for name, value in list(frame.f_locals.items())
                  if not name.startswith('__') and not isinstance(value, (types.ModuleType, types.FunctionType, type))}
        old = self.previous.get(fid, {})
        changes = [{'name': name, 'before': old.get(name), 'after': value,
                    'op': 'changed' if name in old else 'created'}
                   for name, value in values.items() if name not in old or old[name] != value]
        changes.extend({'name': name, 'before': old[name], 'after': None, 'op': 'removed'}
                       for name in old.keys() - values.keys())
        payload = {'file': path.name, 'function': frame.f_code.co_name, 'scope': scope,
                   'line': frame.f_lineno, 'previous_line': self.lines.get(fid),
                   'event': event, 'changes': changes, 'locals': values,
                   'thread': threading.current_thread().name}
        if event == 'return':
            payload['result'] = value_snapshot(arg)
        elif event == 'exception':
            payload['exception'] = {'type': arg[0].__name__, 'message': value_snapshot(str(arg[1]))}
        EVENTS += 1
        emit('trace', **payload)
        if event == 'return':
            self.previous.pop(fid, None)
            self.lines.pop(fid, None)
        else:
            self.previous[fid] = values
            self.lines[fid] = frame.f_lineno
        if event == 'line':
            if not RUN_GATE.is_set():
                emit('control', status='paused', file=path.name, line=frame.f_lineno)
            RUN_GATE.wait()
            if STEP:
                STEP = False
                RUN_GATE.clear()
        return self.trace


class LogStream:
    def write(self, text):
        if text.strip():
            emit('console', message=value_snapshot(text))
        return len(text)

    def flush(self):
        pass


def install_demo(tv, run_dir, seconds):
    """Only replace urllib transport. Signing/reporting/FFmpeg code stays real."""
    from playback_runtime import ffmpeg_executable
    media = run_dir / 'demo-av.mp4'
    duration = max(3, math.ceil(seconds) + 2)
    command = [ffmpeg_executable(), '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
               '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=25',
               '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
               '-t', str(duration), '-c:v', 'libx264', '-threads', '2', '-preset', 'ultrafast',
               '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-movflags', '+faststart', '-shortest', str(media)]
    fixture = os.environ.get('TV_DEMO_MEDIA')
    if fixture:
        shutil.copyfile(fixture, media)
    else:
        subprocess.run(command, check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=120)
    video_media, audio_media = run_dir / 'demo-video.mp4', run_dir / 'demo-audio.m4a'
    use_dash = os.environ.get('TV_DEMO_FORMAT', 'dash') == 'dash'
    if use_dash:
        subprocess.run([ffmpeg_executable(), '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                        '-i', str(media), '-map', '0:v:0', '-c', 'copy', '-movflags', '+faststart', str(video_media),
                        '-map', '0:a:0', '-c', 'copy', '-movflags', '+faststart', str(audio_media)],
                       check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
    class MediaHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            selected = {'/demo.mp4': media, '/video.mp4': video_media, '/audio.m4a': audio_media}.get(self.path)
            if selected is None or not selected.exists():
                self.send_error(404)
                return
            body = selected.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass
    media_server = ThreadingHTTPServer(('127.0.0.1', 0), MediaHandler)
    threading.Thread(target=media_server.serve_forever, name='offline-media', daemon=True).start()
    media_url = f'http://127.0.0.1:{media_server.server_port}/demo.mp4'
    clicks = 0

    class Response(io.BytesIO):
        status = 200

    def urlopen(request, timeout=25):
        nonlocal clicks
        path = urllib.parse.urlsplit(request.full_url).path
        time.sleep(0.08)
        if path == '/x/web-interface/view':
            data = {'code': 0, 'data': {'aid': 100000001, 'cid': 100000002,
                    'bvid': 'BV0000000000', 'state': 0, 'title': 'Offline generated A/V fixture',
                    'stat': {'view': 0}}}
        elif path == '/x/tv/playurl':
            data = {'code': 0, 'data': {'is_drm': False, 'is_preview': False,
                    'durl': [{'url': media_url}], 'timelength': duration * 1000, 'quality': 16}}
            if use_dash:
                data['data'].update(durl=[], dash={
                    'video': [{'id': 16, 'codecid': 7, 'base_url': media_url.replace('/demo.mp4', '/video.mp4')}],
                    'audio': [{'id': 30216, 'base_url': media_url.replace('/demo.mp4', '/audio.m4a')}]})
        elif path == '/x/report/click/android/tv':
            clicks += 1
            # Observed first-install behavior: accept and return identity for next session.
            data = {'code': 0, 'message': 'offline fixture',
                    'data': {'rpdid': 'offline_server_did'}}
        elif path == '/x/click-interface/click/now':
            data = {'code': 0, 'data': {'now': int(time.time())}}
        elif path == '/x/report/heartbeat/mobile':
            data = {'code': 0, 'message': 'offline fixture', 'data': {}}
        elif path in ('/x/tv/playurl/online', '/x/tv/history/add'):
            data = {'code': 0, 'message': 'offline lifecycle fixture', 'data': {}}
        else:
            raise RuntimeError('Offline transport refuses unknown endpoint: ' + path)
        return Response(json.dumps(data).encode())

    urllib.request.urlopen = urlopen

    def deny_network(event, args):
        if event in ('socket.connect', 'socket.getaddrinfo'):
            raise RuntimeError('Network disabled in offline demo')
    sys.addaudithook(deny_network)
    tv.DEMO_INSTALLED = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('demo', 'live'), required=True)
    parser.add_argument('--video', default='BV0000000000')
    parser.add_argument('--seconds', type=float, default=45)
    parser.add_argument('--heartbeat-interval', type=int, choices=(0, 15), default=0)
    parser.add_argument('--context-file', type=Path)
    parser.add_argument('--tv-lifecycle', action='store_true')
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--paused', action='store_true')
    parser.add_argument('--new-ip', action='store_true')
    parser.add_argument('--ip-retries', type=int, default=0)
    parser.add_argument('--trace', action='store_true')
    parser.add_argument('--device-profile', default=BASELINE)
    parser.add_argument('--device-confirmed', action='store_true')
    parser.add_argument('--profile-seed')
    parser.add_argument('--client-slot', default='default')
    args = parser.parse_args()
    if args.context_file and (args.trace or args.paused):
        parser.error('携带私有播放上下文时不启用逐行追踪；请使用普通事件日志')
    try:
        profile = resolve_profile(args.mode, args.device_profile, args.device_confirmed, seed=args.profile_seed)
        from playback_duration import duration_range
        duration_range(args.seconds)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args.client_slot):
            raise ValueError('client-slot必须为1至64位字母数字下划线或连字符')
        if args.context_file:
            from playback_context import PlaybackContext
            context = PlaybackContext.from_file(args.context_file)
            context.validate_profile(profile)
            from client_state import ClientState
            namespace = f'{args.mode}:{profile["id"]}:{args.client_slot}:default'
            identity = ClientState(ROOT / '.private' / 'client-state', namespace).load()
            context.bind_runtime(profile, identity, args.client_slot, args.mode)
    except ValueError as error:
        parser.error(str(error))
    args.run_dir.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = LogStream()
    threading.Thread(target=control_reader, name='debug-control', daemon=True).start()
    if args.paused:
        RUN_GATE.clear()
    emit('lifecycle', status='preparing', mode=args.mode)
    # Import before tracing so protocol constants are never copied into snapshots.
    import tv_playback as tv
    # Both modes use the same complete, once-per-round selection. Transport is
    # selected separately; live mode must not silently fall back to the baseline.
    tv.DEVICE_PROFILE = profile
    tv.UA = profile['user_agent']
    tv.RUN_BUDGET = None
    tv.CLIENT_STATE_MODE = args.mode
    tv.CLIENT_STATE_SLOT = args.client_slot
    timeline = []
    timeline_lock = threading.Lock()
    timeline_file = args.run_dir / 'protocol-timeline.jsonl'
    timeline_seq = 0

    def observe_network(event, elapsed_ms):
        nonlocal timeline_seq
        with timeline_lock:
            timeline_seq += 1
            record = {'sequence': timeline_seq, 'at_epoch': time.time(),
                      'elapsed_ms': elapsed_ms, **public_copy(event)}
            timeline.append(record)
            with timeline_file.open('a', encoding='utf-8') as file:
                file.write(json.dumps(record, ensure_ascii=False) + '\n')
        emit('network', mode=args.mode, elapsed_ms=elapsed_ms, **event)

    tv.NETWORK_OBSERVER = observe_network
    tv.PROGRESS_OBSERVER = lambda progress: emit('progress', **progress)
    (args.run_dir / 'device-profile.json').write_text(
        json.dumps(public_copy(profile), ensure_ascii=False, indent=2), encoding='utf-8')
    emit('device_profile', profile=profile,
         message=f"设备配置：{profile['label']} · 选择方式：{profile['selection']}")
    if args.new_ip:
        from ip_proxy import acquire_verified_proxy, ProxyError
        from execution_budget import minimum_lease_seconds
        try:
            if args.mode != 'live':
                raise ProxyError('离线演示不提取代理 IP')
            lease = acquire_verified_proxy(args.ip_retries, publish=lambda **event: emit('proxy_check' if event.pop('diagnostic', False) else 'proxy', **event),
                                           minimum_seconds=minimum_lease_seconds(args.seconds, lifecycle=args.tv_lifecycle))
            tv.HTTP_OPENER = lease.opener()
            tv.HTTP_PROXY = lease.url
            from execution_budget import ExecutionBudget
            tv.RUN_BUDGET = ExecutionBudget(lease.expires_at, args.seconds, lifecycle=args.tv_lifecycle)
            emit('budget', hard_deadline_epoch=time.time() + tv.RUN_BUDGET.remaining(),
                 message='代理时间预算已启用；不足以完成目标时长时跳过本轮')
            info = {'enabled': True, 'status': 'ready', 'endpoint': lease.endpoint,
                    'exit_ip': lease.exit_ip, 'remaining': lease.remaining,
                    'source_ip': lease.source_ip,
                    'lifetime_seconds': lease.lifetime_seconds, 'lifetime_source': lease.lifetime_source,
                    'expires_at_epoch': time.time() + lease.seconds_left(),
                    'message': '本次接口请求和视频流使用此代理'}
            (args.run_dir / 'proxy.json').write_text(json.dumps(public_copy(info), ensure_ascii=False, indent=2), encoding='utf-8')
        except ProxyError as error:
            emit('failure', error_type='ProxyError', message=str(error))
            emit('lifecycle', status='failed', mode=args.mode)
            return 1
    if args.mode == 'demo':
        try:
            install_demo(tv, args.run_dir, args.seconds)
        except Exception as error:
            emit('failure', error_type=type(error).__name__, message=value_snapshot(str(error)))
            emit('lifecycle', status='failed', mode=args.mode)
            return 1
    emit('lifecycle', status='running', mode=args.mode)
    sys.argv = ['tv_playback.py', args.video if args.mode == 'live' else 'BV0000000000',
                '--mode', args.mode,
                '--seconds', str(args.seconds), '--run-dir', str(args.run_dir / 'playback'),
                '--heartbeat-interval', str(args.heartbeat_interval)]
    if args.context_file:
        sys.argv += ['--context-file', str(args.context_file)]
    if args.tv_lifecycle:
        sys.argv += ['--tv-lifecycle']
    tracer = Tracer()
    if args.trace or args.paused:
        threading.settrace(tracer.trace)
        sys.settrace(tracer.trace)
    outcome = 'failed'
    try:
        tv.main()
        outcome = 'completed'
    except BaseException as error:
        emit('failure', error_type=type(error).__name__, message=value_snapshot(str(error)))
    finally:
        sys.settrace(None)
        threading.settrace(None)
        paths = [event.get('path') for event in timeline if event.get('path')]
        (args.run_dir / 'protocol-timeline.json').write_text(json.dumps({
            'mode': args.mode, 'event_count': len(timeline),
            'observed_paths': paths,
            'events': timeline,
            'comparison_note': '按同一视频、设备方案、清晰度和启动方式与抓包逐项比较；此文件不包含 URL、请求正文或凭据。'
        }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        emit('lifecycle', status=outcome, mode=args.mode, trace_events=EVENTS)
    return 0 if outcome == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
