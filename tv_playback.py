#!/usr/bin/env python3
"""One real, paced playback per selected video; preserve every observation."""
import argparse
import csv
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import queue
import random
import re
import subprocess
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from local_signing import APP_KEY, sign_query
from buvid_lab import generate_buvid, generate_mac
from client_state import ClientState
from tv_media import select_media, decoder_command
from playback_runtime import ffmpeg_executable, lock_experiment
from execution_budget import BudgetExceeded
from device_profiles import baseline_profile
from playback_heartbeat import HeartbeatSchedule, heartbeat_interval
from playback_context import PlaybackContext
from playback_lifecycle import PlaybackLifecycle, ONLINE_PATH
from public_state import public_copy

ROOT = Path(__file__).resolve().parent
DEVICE_PROFILE = baseline_profile()
UA = DEVICE_PROFILE['user_agent']
API_ROOT = 'https://api.snm0516.aisee.tv'
WEB_API_ROOT = 'https://api.bilibili.com'
CLIENT_STATE_MODE = 'live'
CLIENT_STATE_SLOT = 'default'
HTTP_OPENER = None
HTTP_PROXY = None
NETWORK_OBSERVER = None
PROGRESS_OBSERVER = None
RUN_BUDGET = None
READ_ATTEMPTS = 2
READ_RETRY_DELAY = 5
HEARTBEAT_INTERVAL = 0
PLAYBACK_CONTEXT = PlaybackContext()
TV_LIFECYCLE = False
DEMO_INSTALLED = False


def playback_progress(**progress):
    if PROGRESS_OBSERVER is not None:
        PROGRESS_OBSERVER(progress)


class PlaybackFailure(RuntimeError):
    """A public failure summary constructed without private URLs or responses."""


class RequestFailure(PlaybackFailure):
    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def request_failure(event, error):
    """Summarize transport failures without exposing signed URLs or vendor text."""
    cause = error.reason if isinstance(error, urllib.error.URLError) else error
    status = event.get('http_status')
    if isinstance(error, urllib.error.HTTPError):
        status = event['http_status'] = error.code
    timeout = isinstance(cause, TimeoutError)
    disconnected = isinstance(cause, (ConnectionError, http.client.IncompleteRead, http.client.RemoteDisconnected))
    retryable = (timeout or disconnected or status in (502, 503, 504)) and not isinstance(cause, ssl.SSLCertVerificationError)
    if timeout:
        detail = '读取响应正文超时' if event.get('phase') == 'response_body' else '连接或等待响应头超时'
    elif status is not None and status >= 400:
        detail = f'HTTP {status}'
    elif disconnected:
        detail = '连接中断，响应内容未完整收到'
    elif isinstance(cause, ssl.SSLCertVerificationError):
        detail = 'TLS 证书校验失败'
    elif event.get('code') is not None:
        detail = f'业务接口返回 code={event["code"]}'
    else:
        detail = f'响应未成功处理（{type(error).__name__}）'
    if isinstance(error, BudgetExceeded):
        detail, retryable = str(error), False
    event.update(error_type=type(error).__name__, error_message=detail,
                 cause_type=type(cause).__name__)
    for field in ('errno', 'winerror'):
        value = getattr(cause, field, None)
        if isinstance(value, int):
            event[field] = value
    if retryable and event['method'] == 'GET' and event['attempt'] < event['max_attempts']:
        delay = 0.5 if RUN_BUDGET else READ_RETRY_DELAY
        if RUN_BUDGET is None or RUN_BUDGET.can_retry(delay):
            event['retry_in'] = delay
        else:
            retryable = False
            event['error_message'] += '；剩余寿命不足，取消重试'
    return RequestFailure(f'{event["method"]} {event["path"]}：{event["error_message"]}（第 {event["attempt"]}/{event["max_attempts"]} 次）',
                          retryable=retryable)


def read_attempts(operation, attempts=READ_ATTEMPTS):
    for attempt in range(1, attempts + 1):
        try:
            return operation(attempt, attempts)
        except RequestFailure as error:
            if not error.retryable or attempt == attempts:
                raise
        delay = 0.5 if RUN_BUDGET else READ_RETRY_DELAY
        if RUN_BUDGET and not RUN_BUDGET.can_retry(delay):
            raise BudgetExceeded('代理剩余寿命不足，取消重试')
        time.sleep(delay)


def decoder_failure(log, code):
    match = re.search(r'HTTP error ([45][0-9]{2})\b', log)
    if match:
        status = int(match.group(1))
        return f'视频流请求失败：HTTP {status}（代理或视频源返回，业务接口成功不代表视频流可用）', status
    if 'timed out' in log.lower():
        return '视频流读取超时', None
    if 'Error reading HTTP response' in log:
        return '媒体 HTTP 响应读取失败（代理或视频源连接异常；未取得完整响应，不能判定为设备参数错误）', None
    if 'Invalid data found when processing input' in log:
        return '媒体响应无法解析（可能为损坏、截断或非媒体内容；需检查私有解码日志）', None
    if 'Error opening input: I/O error' in log:
        return '媒体输入读写失败（I/O error；尚未取得可解码数据）', None
    if any(text in log for text in ('Stream ends prematurely', 'End of file', 'IO error:')):
        return '视频流连接中断或数据不完整', None
    return f'音视频解码未达到播放要求（FFmpeg 退出码 {code}）', None


def playback_failure_message(error):
    if isinstance(error, (PlaybackFailure, BudgetExceeded)):
        return str(error)
    if isinstance(error, urllib.error.HTTPError):
        return f'播放请求失败：HTTP {error.code}'
    if isinstance(error, TimeoutError):
        return '播放请求或解码超时'
    if isinstance(error, urllib.error.URLError):
        return '播放网络连接失败或超时'
    return f'播放失败（{type(error).__name__}）；详细信息已保存到本轮私有日志'


SERIES_PATH = '/x/series/archives?mid=525916013&series_id=673054&only_normal=true&sort=desc&pn=1&ps=100'
# PlayerStat static constants, from this APK; not an account credential.
AES_KEY = bytes([102,100,54,98,54,51,57,100,98,99,102,102,48,99,50,97,49,98,48,51,98,51,56,57,101,99,55,54,51,99,52,98])
AES_IV = bytes([55,55,98,48,55,97,54,55,50,100,53,55,100,54,52,99])
CLICK_SALT = bytes([57,99,97,102,97,54,52,54,54,97,48,50,56,98,102,98])


def now():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


def save(path, data):
    path = Path(path)
    if path.name == 'state.json':
        # Raw state needed for identity continuity belongs only in .private.
        save(private_state_path(path), data)
        data = public_copy(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    # Windows readers can briefly hold a handle without FILE_SHARE_DELETE.
    # Retry only the atomic file replacement, never replay network operations.
    for attempt in range(6):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if os.name != 'nt' or attempt == 5:
                raise
            time.sleep(0.02 * (attempt + 1))


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def private_state_path(path):
    key = hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()
    return ROOT / '.private' / 'state' / (key + '.json')


def read_runtime_state(path):
    private = private_state_path(path)
    return read(private if private.exists() else path)


def open_network(request, timeout=25):
    if HTTP_OPENER is not None:
        return HTTP_OPENER.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def request_timeout(path=''):
    maximum = 12 if urllib.parse.urlsplit(path).path == '/x/web-interface/view' else 8
    return RUN_BUDGET.timeout(maximum) if RUN_BUDGET else maximum


def read_response(response, event, deadline):
    from network_deadline import read_json_body, ResponseEncodingError
    try:
        return read_json_body(response, event, deadline)
    except ResponseEncodingError:
        raise PlaybackFailure('接口压缩响应损坏、不完整或编码不支持') from None
    except ValueError:
        raise PlaybackFailure('接口响应超过 2 MiB 上限') from None


def notify_network(event, started):
    safe = {key: event[key] for key in
            ('path', 'method', 'http_status', 'code', 'error_type', 'identity_update',
             'phase', 'attempt', 'max_attempts', 'retry_in', 'error_message',
             'cause_type', 'errno', 'winerror', 'bytes_read', 'content_length',
             'heartbeat_phase', 'played_time', 'content_encoding', 'decoded_bytes') if key in event}
    elapsed_ms = round((time.monotonic() - started) * 1000)
    if NETWORK_OBSERVER is not None:
        NETWORK_OBSERVER(safe, elapsed_ms)


def public_json(path):
    return read_attempts(lambda attempt, maximum: _public_json_once(path, attempt, maximum))


def _public_json_once(path, attempt, max_attempts):
    req = urllib.request.Request(WEB_API_ROOT + path, headers={'User-Agent': UA, 'Accept-Encoding': 'gzip'})
    started = time.monotonic()
    event = {'path': urllib.parse.urlsplit(path).path, 'method': 'GET',
             'attempt': attempt, 'max_attempts': max_attempts, 'phase': 'connecting'}
    try:
        timeout = request_timeout(path)
        with open_network(req, timeout=timeout) as resp:
            event['http_status'] = resp.status
            event['phase'] = 'response_body'
            data = json.loads(read_response(resp, event, started + timeout))
        event['phase'] = 'response_complete'
        event['code'] = data.get('code')
        if data.get('code') != 0:
            raise RuntimeError(f'Public API code={data.get("code")}: {data.get("message")}')
        return data['data']
    except Exception as error:
        raise request_failure(event, error) from None
    finally:
        notify_network(event, started)


def series_snapshot():
    start = now()
    data = public_json(SERIES_PATH)
    rows = data['archives']
    if len(rows) != data['page']['total'] or len({r['bvid'] for r in rows}) != len(rows):
        raise RuntimeError('Series response is incomplete or contains duplicate BVIDs')
    return {'started_at': start, 'finished_at': now(), 'archives': rows}


def observe(run_dir, label):
    snapshot = series_snapshot()
    path = run_dir / f'{label}.json'
    if path.exists():
        raise FileExistsError(path)
    save(path, snapshot)
    return snapshot


def click_payload(params):
    """PlayerStat.d: sorted raw pairs, SHA256(message || salt), AES-CBC."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7
    text = '&'.join(f'{k}={params[k]}' for k in sorted(params))
    signature = hashlib.sha256(text.encode() + CLICK_SALT).hexdigest()
    plaintext = (text + '&sign=' + signature).encode()
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    enc = Cipher(algorithms.AES(AES_KEY), modes.CBC(AES_IV)).encryptor()
    return enc.update(padded) + enc.finalize()


class TVClient:
    def __init__(self, private_dir, event_file, *, buvid=None, context=None):
        self.context = PLAYBACK_CONTEXT if context is None else context
        self.context.validate_profile(DEVICE_PROFILE)
        self.private_dir = private_dir
        private_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(private_dir, 0o700)
        self.event_file = event_file
        self.counter = len(list(private_dir.glob('request-*.json')))
        self.common = {**DEVICE_PROFILE['common'], **self.context.common()}
        self.user_agent = UA
        self.appkey = APP_KEY
        self.request_session = uuid.uuid4().hex
        # Playback entry points supply the persisted test-client Buvid.
        # An explicit empty string preserves legacy experiments without this header.
        self.buvid = generate_buvid('mac', generate_mac()) if buvid is None else buvid

    def sign(self, params):
        return sign_query(params)

    def request(self, path, params, kind='GET'):
        self.context.validate_runtime(CLIENT_STATE_MODE)
        # online is a state update despite its GET method. Never replay it.
        return read_attempts(lambda attempt, maximum: self._request_once(path, params, kind, attempt, maximum),
                             attempts=READ_ATTEMPTS if kind == 'GET' and path != ONLINE_PATH else 1)

    def _request_once(self, path, params, kind, attempt, max_attempts):
        self.counter += 1
        index = self.counter
        payload = None
        url = API_ROOT + path
        headers = {'User-Agent': self.user_agent, 'Accept-Encoding': 'gzip',
                   'App-Key': 'android_tv_yst', 'env': 'prod', 'session-id': self.request_session}
        if self.buvid:
            headers['Buvid'] = self.buvid
        if kind == 'CLICK':
            payload = click_payload(params)
            headers['Content-Type'] = 'application/octet-stream'
        else:
            signed = self.sign({**self.common, **{k: str(v) for k, v in params.items()}, 'ts': str(int(time.time()))})
            if kind == 'GET':
                url += '?' + signed
            else:
                payload = signed.encode()
                headers['Content-Type'] = 'application/x-www-form-urlencoded; charset=utf-8'
        save(self.private_dir / f'request-{index:03}.json',
             {'at': now(), 'url': url, 'params': params, 'kind': kind, 'headers': headers})
        if payload is not None:
            (self.private_dir / f'request-{index:03}.body').write_bytes(payload)
        started = time.monotonic()
        event = {'at': now(), 'path': path, 'kind': kind, 'method': 'GET' if kind == 'GET' else 'POST',
                 'attempt': attempt, 'max_attempts': max_attempts, 'phase': 'connecting',
                 'fields': sorted(params), 'aid': params.get('aid', params.get('object_id')),
                 'cid': params.get('cid'), 'played_time': params.get('played_time')}
        if path == '/x/report/heartbeat/mobile':
            event['heartbeat_phase'] = getattr(self, 'heartbeat_phase', None)
        try:
            req = urllib.request.Request(url, data=payload, headers=headers)
            timeout = request_timeout(path)
            try:
                with open_network(req, timeout=timeout) as response:
                    status = event['http_status'] = response.status
                    event['phase'] = 'response_body'
                    raw = read_response(response, event, started + timeout)
            except urllib.error.HTTPError as error:
                status = event['http_status'] = error.code
                event['phase'] = 'response_body'
                with error:
                    raw = read_response(error, event, started + timeout)
            event['phase'] = 'response_complete'
            (self.private_dir / f'response-{index:03}.body').write_bytes(raw)
            try:
                result = json.loads(raw)
            except ValueError:
                result = {}
            event.update(http_status=status, code=result.get('code') if type(result.get('code')) is int else None)
            data = result.get('data')
            replacement_did = data.get('rpdid') if isinstance(data, dict) else None
            # TV PlayerStat consumes a nonempty rpdid even from a nonzero API
            # response. Limit this compatibility branch to the observed 14013.
            identity_update = (status == 200 and kind == 'CLICK'
                               and path == '/x/report/click/android/tv'
                               and type(result.get('code')) is int and result.get('code') in (0, 14013)
                               and isinstance(replacement_did, str) and bool(replacement_did.strip()))
            if identity_update:
                event['identity_update'] = True
            if status != 200 or ((type(result.get('code')) is not int or result.get('code') != 0) and not identity_update):
                raise RuntimeError(f'{path}: HTTP {status}, API {result.get("code")}, {result.get("message")}')
            return result
        except Exception as error:
            raise request_failure(event, error) from None
        finally:
            notify_network(event, started)
            with self.event_file.open('a', encoding='utf-8') as file:
                file.write(json.dumps(event, ensure_ascii=False) + '\n')


def extra_string(session, *, context=None, common=None, endpoint='heartbeat'):
    return (context or PlaybackContext()).extra(
        session, DEVICE_PROFILE['common'] if common is None else common, endpoint)


def report_click(client, params, run_state, run_dir, item):
    """Store server identity for future sessions; never replay a click report."""
    payload = dict(params)
    item.update(play_click_accepted=False, click_retry_count=0, click_attempts=[])
    response = client.request('/x/report/click/android/tv', payload, 'CLICK')
    code = response.get('code')
    item['click_api_code'] = code
    item['click_attempts'].append({'code': code, 'did_present': bool(payload.get('did')),
                                   'did_sha256': hashlib.sha256(payload.get('did', '').encode()).hexdigest()})
    replacement = (response.get('data') or {}).get('rpdid')
    if isinstance(replacement, str) and replacement.strip():
        validate_did(replacement)
        item['returned_rpdid'] = replacement
        run_state['did'] = replacement
        if run_state.get('client_state_namespace'):
            ClientState(ROOT / '.private' / 'client-state', run_state['client_state_namespace']).update_did(replacement)
        save(run_dir / 'state.json', run_state)
    if type(code) is int and code == 0:
        item['play_click_accepted'] = True
        return response
    raise PlaybackFailure(f'Playback click was not accepted: code={code}; identity saved for next session; no replay')


def play_one(client, video, run_state, run_dir, target_seconds):
    bvid = video['bvid']
    item = {'bvid': bvid, 'started_at': now(), 'status': 'attempted',
            'session': hashlib.sha1(uuid.uuid4().bytes).hexdigest(),
            'tv_lifecycle_enabled': TV_LIFECYCLE, 'lifecycle_events': [],
            'lifecycle_status': 'not_started' if TV_LIFECYCLE else 'disabled'}
    context = getattr(client, 'context', None)
    if not isinstance(context, PlaybackContext):
        context = PlaybackContext()
    common = getattr(client, 'common', None)
    if not isinstance(common, dict):
        common = dict(DEVICE_PROFILE['common'])
    item['context'] = context.summary()
    label = f'{run_state["window_id"]} {bvid}' if 'window_id' in run_state else bvid
    if 'window_id' in run_state:
        item['window_id'] = run_state['window_id']
    # Persist before any playback/report; interrupted attempts are never auto-replayed.
    run_state['attempts'][bvid] = item
    save(run_dir / 'state.json', run_state)
    private = client.private_dir
    playback_progress(stage='resolving', decoded_seconds=0, frames=0, target_seconds=target_seconds)
    try:
        view = video if all(key in video for key in ('aid', 'cid', 'state', 'stat')) else public_json('/x/web-interface/view?bvid=' + bvid)
        if view['aid'] != video['aid'] or view['state'] != 0:
            raise RuntimeError('Video identity/state mismatch')
        aid, cid = view['aid'], view['cid']
        item.update(aid=aid, cid=cid, before_view=view['stat']['view'])
        result = client.request('/x/tv/playurl', {**context.fields('playurl'),
                                'object_id': aid, 'cid': cid, 'playurl_type': 1,
                                'qn': 80, 'fnver': 0, 'fnval': 131088, 'device_name': 'android',
                                'source_type': 1, 'force_host': 0, 'fourk': 1})['data']
        media = select_media(result)
        url = media['urls'][0]
        duration = float(result['timelength']) / 1000
        target = min(target_seconds, duration)
        if RUN_BUDGET:
            RUN_BUDGET.begin_playback(target)
        item.update(duration=duration, target_seconds=target,
                    requested_quality=result.get('quality'), quality=media.get('quality', result.get('quality')),
                    media_kind=media['kind'],
                    media_selection=media.get('selection'),
                    media_candidates=[{'quality': candidate.get('quality'),
                                       'reason': candidate.get('selection_reason'),
                                       'url_fingerprints': candidate.get('url_fingerprints', [])}
                                      for candidate in media.get('candidates', [])],
                    media_host=urllib.parse.urlsplit(url).hostname,
                    media_url_sha256=hashlib.sha256(url.encode()).hexdigest())
        selection = media.get('selection') or {}
        print(f'{label}: media selection requested={selection.get("requested_quality")} '
              f'selected={selection.get("selected_quality")} reason={selection.get("reason")} '
              f'candidates={selection.get("candidate_count", 1)}', flush=True)
        playback_progress(stage='connecting', decoded_seconds=0, frames=0, target_seconds=target)
        output = private / f'{bvid}.ffmpeg.log'
        progress_file = run_dir / f'{bvid}.progress.jsonl'
        command = decoder_command(ffmpeg_executable(), media, target, UA, HTTP_PROXY, bool(RUN_BUDGET))
        first_frame = False
        decoded = frames = 0
        start_clock = time.monotonic()
        cadence = HeartbeatSchedule(HEARTBEAT_INTERVAL, start_clock)
        item.update(heartbeat_interval=HEARTBEAT_INTERVAL, periodic_heartbeats=[])
        start_ts = int(time.time())
        events = []
        request_session = getattr(client, 'request_session', '')
        request_session = request_session if isinstance(request_session, str) else ''
        extra = extra_string(request_session, context=context, common=common)
        click_extra = extra_string(request_session, context=context, common=common, endpoint='click')
        def checkpoint_lifecycle():
            item['lifecycle_status'] = lifecycle.status
            save(run_dir / 'state.json', run_state)

        lifecycle = PlaybackLifecycle(client, context, aid, cid, item['lifecycle_events'],
                                      enabled=TV_LIFECYCLE, checkpoint=checkpoint_lifecycle, budget=RUN_BUDGET)
        identity = {'aid': str(aid), 'cid': str(cid), 'mid': '0', 'sid': '0', 'epid': '0',
                    'type': '3', 'sub_type': '0', 'play_mode': '1', 'session': item['session'],
                    'playlist_type': '', 'playlist_id': '', 'device': 'master', 'auto_play': '0'}

        def heartbeat(played, elapsed):
            # This decoder never pauses/seeks. Startup/buffering is not pause
            # time; played is a conservative decoded-time estimate, position
            # comes from media timestamps. This is not the TV lifecycle model.
            return {**{k: v for k, v in identity.items() if k != 'sid'}, **context.fields('heartbeat'),
                    'start_ts': start_ts, 'quality': result['quality'], 'fourk': 1,
                    'total_time': elapsed, 'paused_time': 0,
                    'played_time': played, 'video_duration': math.floor(duration), 'play_type': '1',
                    'network_type': '1', 'last_play_progress_time': math.floor(min(decoded, duration)) if played else 0,
                    'max_play_progress_time': math.floor(min(decoded, duration)) if played else 0, 'extra': extra}

        def send_heartbeat(phase, played, elapsed):
            client.heartbeat_phase = phase
            try:
                return client.request('/x/report/heartbeat/mobile', heartbeat(played, elapsed), 'FORM')
            finally:
                client.heartbeat_phase = None

        with output.open('w', encoding='utf-8') as stderr, progress_file.open('w', encoding='utf-8') as progress:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr, text=True,
                                    env={**os.environ, 'no_proxy': '', 'NO_PROXY': ''} if HTTP_PROXY else None)
            item.update(decoder_started_epoch=time.time(), decoder_pid=proc.pid)
            lines = queue.Queue()

            def reader():
                try:
                    for line in proc.stdout:
                        lines.put(line)
                finally:
                    lines.put(None)

            thread = threading.Thread(target=reader, daemon=True)
            thread.start()
            block = {}
            primary_error = cleanup_error = None
            try:
                while True:
                    if RUN_BUDGET:
                        RUN_BUDGET.check()
                    if time.monotonic() - start_clock > target + 90:
                        raise TimeoutError('Decoder exceeded playback budget')
                    try:
                        line = lines.get(timeout=1)
                    except queue.Empty:
                        continue
                    if line is None:
                        break
                    key, _, value = line.strip().partition('=')
                    block[key] = value
                    if key != 'progress':
                        continue
                    progress.write(json.dumps({'at': now(), **block}) + '\n')
                    progress.flush()
                    frames = max(frames, int(block.get('frame', 0)))
                    decoded = max(decoded, int(block.get('out_time_us', 0)) / 1_000_000)
                    playback_progress(stage='playing', decoded_seconds=decoded, frames=frames,
                                      target_seconds=target, wall_seconds=round(time.monotonic() - start_clock, 2))
                    if frames > 0 and decoded > 0 and not first_frame:
                        first_frame = True
                        item['first_frame_epoch'] = time.time()
                        start_ts = int(time.time() - decoded)
                        lifecycle.start(time.monotonic(), position=decoded)
                        item['failure_stage'] = 'start_heartbeat'
                        send_heartbeat('start', 0, 0)
                        events.append('start_heartbeat')
                        item['failure_stage'] = 'server_clock'
                        server_time = client.request('/x/click-interface/click/now', {})['data']['now']
                        if type(server_time) is not int or server_time <= 0:
                            raise PlaybackFailure('Server clock response is invalid')
                        if run_state.get('client_state_namespace'):
                            run_state['ftime'] = ClientState(ROOT / '.private' / 'client-state', run_state['client_state_namespace']).first_time(server_time)
                        elif run_state.get('ftime') is None:
                            run_state['ftime'] = server_time
                        click = {**identity, **context.fields('click'), 'appkey': client.appkey,
                                 **{k: common[k] for k in ('platform', 'mobi_app', 'build', 'statistics')},
                                 'part': str(next((p['page'] for p in view.get('pages', []) if p.get('cid') == cid), -1)), 'lv': '0',
                                 'ftime': str(run_state['ftime']), 'stime': str(server_time),
                                 'ogv_copyright_index': '',
                                 'extra': urllib.parse.quote_plus(click_extra, safe='*').replace('~', '%7E')}
                        if run_state.get('did'):
                            click['did'] = run_state['did']
                            item['sent_did'] = run_state['did']
                        item['failure_stage'] = 'play_click'
                        report_click(client, click, run_state, run_dir, item)
                        events.append('play_click')
                        item.pop('failure_stage', None)
                        print(f'{label}: first decoded frame; start reports accepted; no click replay', flush=True)
                    clock_now = time.monotonic()
                    if 'play_click' in events:
                        lifecycle.tick(clock_now, position=decoded, ended=block.get('progress') == 'end'
                                       or proc.poll() is not None or decoded >= target - .75)
                    played = cadence.take(decoded, clock_now - start_clock, clock_now, target,
                                          ended=block.get('progress') == 'end' or proc.poll() is not None)
                    if played is not None and 'play_click' in events:
                        observation = {'played_time': played, 'status': 'unconfirmed'}
                        item['periodic_heartbeats'].append(observation)
                        try:
                            send_heartbeat('periodic', played, math.floor(clock_now - start_clock))
                            observation['status'] = 'accepted'
                            events.append('periodic_heartbeat')
                        except Exception as error:
                            observation['error'] = playback_failure_message(error)
                        save(run_dir / 'state.json', run_state)
                    block = {}
                code = proc.wait(timeout=10)
            except Exception as error:
                primary_error = error
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                proc.stdout.close()
                thread.join(timeout=2)
                elapsed = time.monotonic() - start_clock
                item.update(decoded_seconds=decoded, frames=frames, wall_seconds=round(elapsed, 3),
                            ffmpeg_exit=proc.returncode, accepted_events=events,
                            decoder_finished_epoch=time.time())
                playback_progress(stage='finishing', decoded_seconds=decoded, frames=frames,
                                  target_seconds=target, wall_seconds=round(elapsed, 2))
                log = output.read_text(encoding='utf-8', errors='replace')
                item['audio_decoded'] = bool(re.search(r'audio:\s*[1-9][0-9]*(?:\.[0-9]+)?(?:KiB|kB|B)', log))
                media_complete = (proc.returncode == 0 and frames > 0 and decoded >= target - .75
                                  and elapsed >= decoded - 1 and item['audio_decoded'])
                item['media_status'] = 'completed' if media_complete else 'failed'
                if primary_error is None and not media_complete:
                    reason, status = decoder_failure(log, proc.returncode)
                    item.update(failure_stage='media', media_http_status=status)
                    primary_error = PlaybackFailure(reason)
                if 'start_heartbeat' in events:
                    if RUN_BUDGET:
                        RUN_BUDGET.begin_finishing('heartbeat')
                    played = math.floor(min(decoded, elapsed))
                    try:
                        if RUN_BUDGET:
                            RUN_BUDGET.timeout(8)
                        send_heartbeat('end', played, math.floor(elapsed))
                        events.append('end_heartbeat')
                    except Exception as error:
                        cleanup_error = error
                        item['cleanup_error'] = playback_failure_message(error)
                        item['end_heartbeat_status'] = 'skipped' if isinstance(error, BudgetExceeded) else 'unconfirmed'
                        if isinstance(error, BudgetExceeded):
                            item['end_heartbeat_skip_reason'] = 'insufficient_budget'
                if lifecycle.started:
                    if RUN_BUDGET:
                        RUN_BUDGET.begin_finishing('history')
                    item['termination_reason'] = 'completed' if media_complete and primary_error is None else 'abnormal'
                    lifecycle.finish(min(decoded, elapsed, target), min(decoded, duration), reason=item['termination_reason'])
                item['lifecycle_status'] = lifecycle.status
                lifecycle_failed = TV_LIFECYCLE and lifecycle.status != 'completed'
                periodic_failed = any(h['status'] != 'accepted' for h in item['periodic_heartbeats'])
                item['report_status'] = 'completed' if not periodic_failed and not lifecycle_failed and all(e in events for e in ('start_heartbeat', 'play_click', 'end_heartbeat')) else 'unconfirmed'
        if primary_error is not None:
            raise primary_error
        if cleanup_error is not None:
            item['failure_stage'] = 'end_heartbeat'
            raise cleanup_error
        if periodic_failed:
            item['failure_stage'] = 'periodic_heartbeat'
            raise PlaybackFailure('周期心跳存在未确认请求；未自动重发，详见心跳日志')
        if lifecycle_failed:
            item['failure_stage'] = 'tv_lifecycle'
            raise PlaybackFailure('online/history 存在未确认请求；未自动重发，详见生命周期日志')
        item['status'] = 'completed'
    except Exception as error:
        item['status'] = 'failed'
        # Error strings may contain signed CDN URLs. Keep details private.
        item['error_type'] = type(error).__name__
        item['failure_reason'] = playback_failure_message(error)
        selection = item.get('media_selection') or {}
        if selection:
            item['failure_reason'] += (f'；媒体选择 requested={selection.get("requested_quality")}，'
                                       f'selected={selection.get("selected_quality")}，'
                                       f'reason={selection.get("reason")}，'
                                       f'candidates={selection.get("candidate_count", 1)}')
        if item.get('media_status') == 'completed':
            item['failure_reason'] = '播放完成，上报未确认；' + item['failure_reason']
        (private / f'{bvid}.error.txt').write_text(str(error), encoding='utf-8')
    finally:
        item['finished_at'] = now()
        save(run_dir / 'state.json', run_state)
        playback_progress(stage=item['status'], decoded_seconds=item.get('decoded_seconds', 0),
                          frames=item.get('frames', 0), target_seconds=item.get('target_seconds', target_seconds),
                          wall_seconds=item.get('wall_seconds', 0), media_status=item.get('media_status', 'not_started'),
                          report_status=item.get('report_status', 'not_started'))
    print(f'{label}: {item["status"]}, decoded={item.get("decoded_seconds", 0):.2f}s', flush=True)
    return item


def resolve_video(video_id):
    if re.fullmatch(r'BV[0-9A-Za-z]{10}', video_id):
        query = 'bvid=' + video_id
    elif re.fullmatch(r'(?:av)?[1-9][0-9]*', video_id):
        query = 'aid=' + video_id.removeprefix('av')
    else:
        raise ValueError('video-id must be a BVID, av ID or positive numeric AID')
    data = public_json('/x/web-interface/view?' + query + '&_research_time=' + str(time.time_ns()))
    if data.get('state') != 0 or not data.get('bvid') or not data.get('aid'):
        raise ValueError('Video is unavailable or missing identity fields')
    return data


def play_window(video_id, window_id, buvid, run_dir, seconds=60, *, did=None, start_barrier=None):
    """One isolated playback instance. Existing output directories are never replayed."""
    run_dir = Path(run_dir).resolve()
    try:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', window_id):
            raise ValueError('window-id must contain 1..64 letters, digits, underscores or hyphens')
        if not 0 < seconds <= 600:
            raise ValueError('seconds must be >0 and <=600')
        if buvid is not None and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', buvid):
            raise ValueError('buvid must contain 1..64 letters, digits, underscores or hyphens')
        validate_did(did)
        # Atomic reservation prevents concurrent launches into the same output folder.
        run_dir.mkdir(parents=True, exist_ok=False)
        namespace = f'{CLIENT_STATE_MODE}:{DEVICE_PROFILE["id"]}:{CLIENT_STATE_SLOT}:{buvid or "default"}'
        identity = ClientState(ROOT / '.private' / 'client-state', namespace).load(buvid)
        PLAYBACK_CONTEXT.bind_runtime(DEVICE_PROFILE, identity, CLIENT_STATE_SLOT, CLIENT_STATE_MODE)
        did_source = 'explicit' if did is not None else ('server-persisted' if identity['did'] else 'absent')
        buvid = identity['buvid']
        if did is None:
            did = identity['did']
        private = ROOT / '.private' / 'windows' / hashlib.sha256(str(run_dir).encode()).hexdigest()[:24]
        client = TVClient(private, run_dir / 'events.jsonl', buvid=buvid)
        state = {'window_id': window_id, 'video_id': video_id, 'created_at': now(),
                 'buvid': client.buvid, 'ftime': identity['ftime'], 'seconds': seconds,
                 'client_state_namespace': namespace,
                 'initial_did': did, 'did': did or '', 'device_profile': DEVICE_PROFILE,
                 'initial_did_source': did_source,
                 'private_dir': str(private), 'attempts': {}, 'status': 'preparing'}
        save(run_dir / 'state.json', state)
        playback_progress(stage='resolving', decoded_seconds=0, frames=0, target_seconds=seconds)
        video = resolve_video(video_id)
        if start_barrier is not None:
            start_barrier.wait()
    except BaseException as error:
        # Persist setup failures too, so observers do not see a stuck preparing state.
        if 'state' in locals():
            state.update(status='failed', error_type=type(error).__name__,
                         failure_reason=playback_failure_message(error), finished_at=now())
            save(run_dir / 'state.json', state)
        if start_barrier is not None:
            start_barrier.abort()
        raise
    state.update(status='playing', released_at=now())
    save(run_dir / 'state.json', state)
    item = play_one(client, video, state, run_dir, seconds)
    state.update(status=item['status'], finished_at=now())
    save(run_dir / 'state.json', state)
    return item


def validate_did(did):
    # 实验输入约束，不是服务端校验规则。允许 URL-safe Base64 及常见编码字符，
    # 拒绝会破坏原客户端未转义 key=value&... 正文的字符。
    if did is not None and (not re.fullmatch(r'[A-Za-z0-9_+/=|~().\x27-]{1,1024}', did)):
        raise ValueError('did must be 1..1024 identifier characters without whitespace or &')


def make_report(run_dir):
    state = read(run_dir / 'state.json')
    snapshots = sorted(run_dir.glob('observation-*.json'))
    baseline = {v['bvid']: v['stat']['view'] for v in read(run_dir / 'baseline.json')['archives']}
    latest = read(snapshots[-1]) if snapshots else read(run_dir / 'baseline.json')
    current = {v['bvid']: v['stat']['view'] for v in latest['archives']}
    rows = []
    for group in ['videos', 'controls']:
        for v in state[group]:
            bvid = v['bvid']; item = state['attempts'].get(bvid, {})
            after = current.get(bvid)
            rows.append({'group': group, 'bvid': bvid, 'title': v['title'], 'before': baseline[bvid],
                         'after': after, 'delta': after - baseline[bvid] if after is not None else None,
                         'playback': item.get('status', 'not_played'), 'decoded_seconds': item.get('decoded_seconds', 0)})
    with (run_dir / 'comparison.csv').open('w', newline='', encoding='utf-8-sig') as file:
        writer = csv.DictWriter(file, fieldnames=rows[0].keys()); writer.writeheader(); writer.writerows(rows)
    save(run_dir / 'comparison.json', {'observed_at': latest['finished_at'], 'rows': rows,
                                      'causation_established': False})
    return rows


def main():
    global HEARTBEAT_INTERVAL, PLAYBACK_CONTEXT, TV_LIFECYCLE, CLIENT_STATE_MODE, DEVICE_PROFILE, UA
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='play', metavar='VIDEO_ID|ACTION',
                        help='Video ID for one playback, or run/observe/report/play')
    parser.add_argument('--run-dir', type=Path)
    parser.add_argument('--mode', choices=('demo', 'live'), help='Required for network/media actions; report stays local')
    parser.add_argument('--device-profile', default=None)
    parser.add_argument('--device-confirmed', action='store_true')
    parser.add_argument('--profile-seed', help='Reproducible 32-hex seed for demo dynamic profiles')
    parser.add_argument('--video-id', help='BVID, av ID or numeric AID for one playback window')
    parser.add_argument('--buvid', help='Explicit client identity; omitted reuses persisted test-client Buvid')
    parser.add_argument('--did', help='Explicit report did; otherwise reuse server rpdid or omit on first session')
    parser.add_argument('--limit', type=int, default=10, help='Maximum new attempts this invocation')
    parser.add_argument('--heartbeat-interval', type=int, choices=(0, 15), default=0,
                        help='Experimental real-playback heartbeat: 0=off, 15=seconds')
    parser.add_argument('--context-file', type=Path,
                        help='Private JSON with explicit device/scene/guest context bound to profile_id')
    parser.add_argument('--tv-lifecycle', action='store_true',
                        help='Opt in to online presence and final UGC history; no replay after failure')
    duration = parser.add_mutually_exclusive_group()
    duration.add_argument('--seconds', type=float, help='Fixed playback duration in seconds')
    duration.add_argument('--max-seconds', type=float,
                          help='Random playback upper bound for play (45..600, default: 60)')
    args = parser.parse_args()
    if args.action != 'report' and args.mode is None:
        parser.error('必须显式指定 --mode demo 或 --mode live；离线示例使用 demo')
    if args.mode:
        CLIENT_STATE_MODE = args.mode
        from device_profiles import resolve_profile
        try:
            # The worker has already selected one complete profile for this round.
            if args.device_profile is not None or args.profile_seed is not None or DEVICE_PROFILE.get('mode') != args.mode:
                DEVICE_PROFILE = resolve_profile(args.mode, args.device_profile or 'original-tv',
                                                 args.device_confirmed, seed=args.profile_seed)
                UA = DEVICE_PROFILE['user_agent']
        except ValueError as error:
            parser.error(str(error))
    HEARTBEAT_INTERVAL = heartbeat_interval(args.heartbeat_interval)
    try:
        PLAYBACK_CONTEXT = PlaybackContext.from_file(args.context_file) if args.context_file else PlaybackContext()
        PLAYBACK_CONTEXT.validate_profile(DEVICE_PROFILE)
    except ValueError as error:
        parser.error(str(error))
    TV_LIFECYCLE = args.tv_lifecycle
    if args.action not in ('run', 'observe', 'report', 'play'):
        if not re.fullmatch(r'(?:BV[0-9A-Za-z]{10}|(?:av)?[1-9][0-9]*)', args.action):
            parser.error('expected a BVID, av ID, numeric AID or run/observe/report/play')
        if args.video_id is not None:
            parser.error('specify the video ID either positionally or with --video-id')
        args.video_id, args.action = args.action, 'play'
    if args.action != 'play' and (args.context_file or args.tv_lifecycle):
        parser.error('--context-file and --tv-lifecycle apply only to one play invocation')
    if args.mode == 'demo' and args.action not in ('play', 'report'):
        parser.error('demo 仅支持单次play或本地report；不运行旧系列联网入口')
    if args.max_seconds is not None:
        if args.action != 'play':
            parser.error('--max-seconds applies only to play')
        if not 45 <= args.max_seconds <= 600:
            parser.error('max-seconds must be >=45 and <=600')
    if args.seconds is None:
        args.seconds = (random.uniform(45, args.max_seconds if args.max_seconds is not None else 60)
                        if args.action == 'play' else 60)
    if not 1 <= args.limit <= 10 or not 0 < args.seconds <= 600:
        parser.error('limit must be 1..10; seconds must be >0 and <=600')
    try:
        validate_did(args.did)
    except ValueError as error:
        parser.error(str(error))
    if args.action == 'play':
        if not args.video_id:
            parser.error('play requires --video-id')
        window_id = 'window-' + uuid.uuid4().hex
        directory = args.run_dir or ROOT / 'results' / window_id
        if args.mode == 'demo' and not DEMO_INSTALLED:
            from debug_worker import install_demo
            fixture_dir = ROOT / '.private' / 'demo-fixtures' / window_id
            fixture_dir.mkdir(parents=True, exist_ok=False)
            install_demo(sys.modules[__name__], fixture_dir, args.seconds)
            args.video_id = 'BV0000000000'
        try:
            result = play_window(args.video_id, window_id, args.buvid, directory, args.seconds, did=args.did)
        except (ValueError, FileExistsError) as error:
            parser.error(str(error))
        if result['status'] != 'completed':
            raise SystemExit(result.get('failure_reason') or '播放失败；请检查本轮输出目录')
        return
    if args.video_id or args.buvid is not None:
        parser.error('--video-id and --buvid apply only to play')
    if args.did is not None and args.action != 'run':
        parser.error('--did applies only to play or run')
    directory = (args.run_dir or ROOT / 'results/experiment-01').resolve()
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / 'state.json'
    if args.action == 'run':
        run_lock = (directory / 'playback.lock').open('a+', encoding='utf-8')
        try:
            lock_experiment(run_lock)
        except BlockingIOError:
            raise SystemExit('Another playback process owns this experiment')
        if state_path.exists():
            state = read_runtime_state(state_path)
            if state['seconds'] != args.seconds:
                parser.error('Cannot change duration in an existing experiment')
        else:
            videos = read(ROOT / 'evidence/candidates.json')['videos']
            if len(videos) != 10 or len({v['bvid'] for v in videos}) != 10:
                raise RuntimeError('Expected exactly 10 distinct candidates')
            baseline = observe(directory, 'baseline')
            selected = {v['bvid'] for v in videos}
            controls = sorted((v for v in baseline['archives'] if v['bvid'] not in selected and v['aid'] != 328686358),
                              key=lambda v: (v['stat']['view'], v['bvid']))[:5]
            state = {'created_at': now(), 'ftime': int(time.time()), 'seconds': args.seconds,
                     'videos': videos, 'controls': controls, 'attempts': {}}
            save(state_path, state)
        if args.did is not None:
            state['did'] = args.did
            state['initial_did'] = state['did']
            state['initial_did_source'] = 'explicit'
            save(state_path, state)
        namespace = f'{CLIENT_STATE_MODE}:{DEVICE_PROFILE["id"]}:{CLIENT_STATE_SLOT}:legacy-run'
        stored = ClientState(ROOT / '.private' / 'client-state', namespace).load()
        state.update(client_state_namespace=namespace, buvid=stored['buvid'], ftime=stored['ftime'])
        state['did'] = args.did if args.did is not None else stored['did']
        client = TVClient(ROOT / '.private' / directory.name, directory / 'events.jsonl', buvid=stored['buvid'])
        count = 0
        for video in state['videos']:
            if video['bvid'] in state['attempts']:
                continue
            item = play_one(client, video, state, directory, args.seconds)
            count += 1
            observe(directory, f'observation-{time.time_ns()}')
            make_report(directory)
            if item['status'] != 'completed':
                raise SystemExit('Stopped after failed attempt; inspect private error before continuing')
            if count >= args.limit:
                break
    elif args.action == 'observe':
        observe(directory, f'observation-{time.time_ns()}')
    rows = make_report(directory)
    for row in rows:
        print(f'{row["group"]} {row["bvid"]} {row["before"]} -> {row["after"]} delta={row["delta"]} {row["playback"]}')


if __name__ == '__main__':
    main()
