"""Run independent lanes; each lane repeats isolated workers and skips failures."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import random
from pathlib import Path
import subprocess
import sys
import threading
import time
from playback_runtime import terminate_tree
from playback_duration import duration_range
from dispatch_limits import DispatchLimits, validate_limits

ROOT = Path(__file__).resolve().parent
EMIT_LOCK = threading.Lock()


def emit(event):
    with EMIT_LOCK:
        # ASCII JSON survives Windows console/code-page differences between processes.
        print(json.dumps({'at': time.time(), **event}, ensure_ascii=True), flush=True)


class ControlRelay:
    def __init__(self, paused=False):
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.proc = None
        self.procs = {}
        self.paused = paused
        self.debug_controls = True
        self.publish = lambda event: None
        self.limits = None

    def read(self):
        for line in sys.stdin:
            try:
                self.send(json.loads(line)['command'])
            except (ValueError, KeyError, TypeError):
                pass

    def send(self, command):
        if command not in ('pause', 'resume', 'step'):
            return
        with self.lock:
            if command in ('pause', 'resume'):
                self.paused = command == 'pause'
                if command == 'resume' and self.limits:
                    self.limits.resume()
                self.condition.notify_all()
                self.publish({'kind': 'dispatch', 'paused': self.paused, 'lane': 0,
                              'message': '暂停派发下一轮，当前轮继续完成' if self.paused else '继续派发后续轮次'})
            if not self.debug_controls:
                return
            targets = list(self.procs.values()) or ([self.proc] if self.proc is not None else [])
            for proc in targets:
                if proc.poll() is not None:
                    continue
                try:
                    proc.stdin.write(json.dumps({'command': command}) + '\n')
                    proc.stdin.flush()
                except (OSError, ValueError):
                    pass


def run_lane(args, relay, lane, publish, wait):
    failures = 0
    minimum, maximum = duration_range(args.seconds, getattr(args, 'seconds_min', None),
                                      getattr(args, 'seconds_max', None))
    stagger = getattr(args, 'stagger_seconds', 0)
    if stagger:
        wait((lane - 1) * stagger)
    for number in range(1, args.repeat + 1):
        concurrency = getattr(args, 'concurrency', 1)
        base = args.run_dir if concurrency == 1 else args.run_dir / f'lane-{lane:02d}'
        directory = base if args.repeat == 1 else base / f'round-{number:03d}'
        def send(event):
            publish({**event, 'lane': lane, 'round': number})
        wanted = getattr(args, 'ip_retries', 0) + 1 if args.new_ip else 0
        allocated = relay.limits.reserve(wanted)
        if allocated is None:
            send({'kind': 'cancelled_rounds', 'count': args.repeat-number+1,
                  'message': 'IP 提取上限已达，本路后续轮次取消'})
            break
        round_seconds = minimum if minimum == maximum else min(maximum, max(minimum, round(random.uniform(minimum, maximum), 1)))
        send({'kind': 'round', 'phase': 'start', 'total': args.repeat,
                 'run_dir': str(directory), 'status': 'preparing', 'target_seconds': round_seconds})
        command = [sys.executable, '-u', str(ROOT / 'debug_worker.py'), '--mode', args.mode,
                   '--video', args.video, '--seconds', str(round_seconds), '--run-dir', str(directory),
                   '--ip-retries', str(max(0, allocated-1) if args.new_ip else getattr(args, 'ip_retries', 0))]
        command.extend(['--device-profile', getattr(args, 'device_profile', 'original-tv')])
        command.extend(['--heartbeat-interval', str(getattr(args, 'heartbeat_interval', 0))])
        if getattr(args, 'device_confirmed', False):
            command.append('--device-confirmed')
        if args.new_ip:
            command.append('--new-ip')
        if getattr(args, 'trace', False):
            command.append('--trace')
        outcome, code, reason = 'failed', 1, ''
        proc = None
        done, expired = threading.Event(), threading.Event()
        watcher = None
        attempted, terminal_seen = 0, False
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with relay.lock:
                while relay.paused and (not relay.debug_controls or relay.limits.auto_paused):
                    relay.condition.wait()
                if relay.paused and relay.debug_controls:
                    command.append('--paused')
                proc = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                                        env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1'})
                relay.procs[lane] = proc
        except (OSError, ValueError):
            reason = '无法启动本轮工作进程'
            send({'kind': 'failure', 'message': reason})
        if proc is not None:
            limit = getattr(args, 'round_timeout', None) or (round_seconds + 60 + (getattr(args, 'ip_retries', 0) + 1) * 25)
            deadline = time.monotonic() + limit
            def watchdog():
                while not done.wait(.2):
                    if time.monotonic() >= deadline:
                        if proc.poll() is None:
                            expired.set()
                            try:
                                terminate_tree(proc.pid)
                            except (OSError, subprocess.SubprocessError):
                                proc.kill()
                        return
            if isinstance(getattr(proc, 'pid', None), int):
                watcher = threading.Thread(target=watchdog, daemon=True)
                watcher.start()
            try:
                for line in proc.stdout:
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict) or 'kind' not in event:
                            raise ValueError()
                    except (ValueError, TypeError):
                        from debug_worker import value_snapshot
                        event = {'kind': 'console', 'message': value_snapshot(line.rstrip())}
                    if event['kind'] == 'failure':
                        reason = event.get('message', '')
                    if event['kind'] == 'proxy' and event.get('status') == 'acquiring':
                        attempted += 1
                    if event['kind'] == 'budget':
                        deadline = min(deadline, time.monotonic() + max(0, event['hard_deadline_epoch'] - time.time()) + 1)
                    if event['kind'] == 'lifecycle':
                        if event.get('status') in ('completed', 'failed'):
                            outcome = event['status']
                            terminal_seen = True
                            continue
                        event['kind'] = 'worker_lifecycle'
                    elif event['kind'] == 'control':
                        event['kind'] = 'worker_control'
                    send(event)
                code = proc.wait()
            except Exception as error:
                reason = f'本轮工作进程异常（{type(error).__name__}）'
                send({'kind': 'failure', 'message': reason})
                if isinstance(getattr(proc, 'pid', None), int) and proc.poll() is None:
                    terminate_tree(proc.pid)
                    proc.wait(timeout=10)
            finally:
                done.set()
                if watcher:
                    watcher.join(timeout=12)
                with relay.lock:
                    relay.procs.pop(lane, None)
                proc.stdout.close()
                proc.stdin.close()
            if expired.is_set():
                reason, outcome, code = '本轮超过时间预算，已结束工作进程及播放器', 'failed', 1
                send({'kind': 'failure', 'message': reason})
        if code != 0:
            outcome = 'failed'
        send({'kind': 'round', 'phase': 'finish', 'status': outcome, 'returncode': code,
              'reason': reason if outcome == 'failed' else ''})
        relay.limits.finish(allocated, attempted, proc is None or (terminal_seen and not expired.is_set()), outcome)
        if outcome != 'completed':
            failures += 1
            if number < args.repeat:
                send({'kind': 'cooldown', 'seconds': 5, 'next_round': number + 1,
                      'message': '本轮失败已跳过，等待 5 秒后继续下一轮'})
                wait(5)
    return failures


def run_batch(args, relay, publish=emit, wait=time.sleep):
    concurrency = getattr(args, 'concurrency', 1)
    relay.debug_controls = bool(getattr(args, 'trace', False) and not args.new_ip and concurrency == 1)
    relay.publish = publish
    relay.limits = DispatchLimits(relay, getattr(args, 'ip_limit', 0), getattr(args, 'failure_limit', 0))
    publish({'kind': 'dispatch', 'paused': relay.paused, 'lane': 0,
             'message': '批量暂停仅暂停派发，当前轮继续完成' if not relay.debug_controls else '单路详细调试模式'})
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix='playback-lane') as pool:
        futures = [pool.submit(run_lane, args, relay, lane, publish, wait) for lane in range(1, concurrency + 1)]
        failures = sum(future.result() for future in futures)
    publish({'kind': 'lifecycle', 'status': 'completed_with_errors' if failures or relay.limits.exhausted else 'completed',
             'failed': failures, 'total': concurrency * args.repeat, 'lane': 0})
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('demo', 'live'), required=True)
    parser.add_argument('--video', required=True)
    parser.add_argument('--seconds', type=float, default=45)
    parser.add_argument('--heartbeat-interval', type=int, choices=(0, 15), default=0)
    parser.add_argument('--seconds-min', type=float)
    parser.add_argument('--seconds-max', type=float)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--concurrency', type=int, default=1)
    parser.add_argument('--ip-retries', type=int, default=0)
    parser.add_argument('--ip-limit', type=int, default=0)
    parser.add_argument('--failure-limit', type=int, default=5)
    parser.add_argument('--trace', action='store_true')
    parser.add_argument('--new-ip', action='store_true')
    parser.add_argument('--paused', action='store_true')
    parser.add_argument('--stagger-seconds', type=float, default=.3)
    parser.add_argument('--round-timeout', type=float)
    parser.add_argument('--device-profile', default='original-tv')
    parser.add_argument('--device-confirmed', action='store_true')
    args = parser.parse_args()
    try:
        validate_limits(args.ip_limit, args.failure_limit)
        args.seconds_min, args.seconds_max = duration_range(args.seconds, args.seconds_min, args.seconds_max)
    except ValueError as error:
        parser.error(str(error))
    from device_profiles import validate_selection
    try:
        validate_selection(args.mode, args.device_profile, args.device_confirmed)
    except ValueError as error:
        parser.error(str(error))
    if not 1 <= args.repeat <= 1000:
        parser.error('repeat must be 1..1000')
    if not 1 <= args.concurrency <= 100 or not 0 <= args.ip_retries <= 3:
        parser.error('concurrency must be 1..100; ip-retries must be 0..3')
    if not 0 <= args.stagger_seconds <= 5 or (args.round_timeout is not None and not 0 < args.round_timeout <= 3600):
        parser.error('invalid stagger or round timeout')
    if args.concurrency > 1:
        args.trace = False
    relay = ControlRelay(args.paused)
    threading.Thread(target=relay.read, name='batch-control', daemon=True).start()
    return run_batch(args, relay)


if __name__ == '__main__':
    raise SystemExit(main())
