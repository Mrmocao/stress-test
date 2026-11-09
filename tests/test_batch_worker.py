import argparse
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import batch_worker
from debug_server import Inspector
import tv_playback as tv


class FakeProcess:
    def __init__(self, code=0):
        self.stdout = io.StringIO(json.dumps({'kind': 'lifecycle', 'status': 'completed' if code == 0 else 'failed'}) + '\n')
        self.stdin = io.StringIO()
        self.code = code

    def wait(self):
        return self.code

    def poll(self):
        return None


class BatchTests(unittest.TestCase):
    def test_progress_is_live_without_trace_isolated_and_reset_between_rounds(self):
        inspector = Inspector()
        for lane in (1, 2):
            inspector.record({'kind': 'round', 'phase': 'start', 'lane': lane, 'round': 1,
                              'run_dir': 'missing-test-directory'})
        inspector.record({'kind': 'progress', 'lane': 1, 'stage': 'playing',
                          'decoded_seconds': 3.2, 'target_seconds': 10, 'frames': 80})
        inspector.record({'kind': 'progress', 'lane': 2, 'stage': 'connecting',
                          'decoded_seconds': 0, 'target_seconds': 20, 'frames': 0})
        snapshot = inspector.snapshot(0, detail=False)
        self.assertEqual(snapshot['workers'][0]['progress']['decoded_seconds'], 3.2)
        self.assertEqual(snapshot['workers'][1]['progress']['decoded_seconds'], 0)
        self.assertFalse(any(e['kind'] == 'progress' for e in snapshot['logs']))
        inspector.record({'kind': 'round', 'phase': 'finish', 'lane': 1, 'round': 1, 'status': 'failed'})
        self.assertEqual(inspector.batch['rounds'][0]['progress']['decoded_seconds'], 3.2)
        inspector.record({'kind': 'round', 'phase': 'start', 'lane': 1, 'round': 2,
                          'run_dir': 'missing-test-directory-2'})
        self.assertEqual(inspector.worker(1)['progress'], {})
        self.assertEqual(inspector.worker(2)['progress']['target_seconds'], 20)

    def args(self, directory, count=3):
        return argparse.Namespace(mode='live', video='BV1Gq486CEK4', seconds=1,
                                  run_dir=Path(directory), repeat=count, new_ip=True)

    def test_repeat_validation_happens_before_launch(self):
        for value in (0, -1, 1001, 1.5, True, '3', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Inspector().start('demo', '', 1, repeat=value)

    def test_each_round_is_a_separate_worker_and_requests_new_ip(self):
        events = []
        commands = []
        def launch(command, **kwargs):
            commands.append(command)
            return FakeProcess()
        with tempfile.TemporaryDirectory() as directory, patch.object(batch_worker.subprocess, 'Popen', side_effect=launch):
            self.assertEqual(batch_worker.run_batch(self.args(directory), batch_worker.ControlRelay(), events.append), 0)
        self.assertEqual(len(commands), 3)
        paths = [command[command.index('--run-dir') + 1] for command in commands]
        self.assertEqual(len(set(paths)), 3)
        self.assertTrue(all('--new-ip' in command for command in commands))
        self.assertEqual([e['round'] for e in events if e['kind'] == 'round' and e['phase'] == 'finish'], [1, 2, 3])
        self.assertEqual(sum(e['kind'] == 'lifecycle' and e['status'] == 'completed' for e in events), 1)

    def test_failed_round_waits_five_seconds_and_continues(self):
        events = []
        wait = Mock()
        with tempfile.TemporaryDirectory() as directory, patch.object(batch_worker.subprocess, 'Popen', side_effect=[FakeProcess(), FakeProcess(1), FakeProcess(), FakeProcess(), FakeProcess()]) as launch:
            self.assertEqual(batch_worker.run_batch(self.args(directory, 5), batch_worker.ControlRelay(), events.append, wait), 0)
        self.assertEqual(launch.call_count, 5)
        wait.assert_called_once_with(5)
        self.assertEqual(events[-1]['status'], 'completed_with_errors')
        self.assertEqual(events[-1]['failed'], 1)
        self.assertEqual([e['status'] for e in events if e['kind'] == 'round' and e['phase'] == 'finish'], ['completed', 'failed', 'completed', 'completed', 'completed'])

    def test_launch_failure_is_counted_and_next_round_starts(self):
        events = []
        wait = Mock()
        with tempfile.TemporaryDirectory() as directory, patch.object(batch_worker.subprocess, 'Popen', side_effect=[OSError(), FakeProcess(), FakeProcess()]) as launch:
            self.assertEqual(batch_worker.run_batch(self.args(directory), batch_worker.ControlRelay(), events.append, wait), 0)
        self.assertEqual(launch.call_count, 3)
        wait.assert_called_once_with(5)
        self.assertTrue(any(e['kind'] == 'round' and e['status'] == 'failed' for e in events))

    def test_concurrent_lanes_overlap_and_keep_separate_directories(self):
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        active, peak = 0, 0
        commands, events = [], []
        class Stream(io.StringIO):
            def __iter__(self):
                barrier.wait(timeout=5)
                return super().__iter__()
        class Process(FakeProcess):
            def wait(self):
                nonlocal active
                with lock:
                    active -= 1
                return 0
        def launch(command, **kwargs):
            nonlocal active, peak
            commands.append(command)
            with lock:
                active += 1
                peak = max(peak, active)
            proc = Process()
            proc.stdout = Stream(json.dumps({'kind': 'lifecycle', 'status': 'completed'}) + '\n')
            return proc
        with tempfile.TemporaryDirectory() as directory, patch.object(batch_worker.subprocess, 'Popen', side_effect=launch):
            args = self.args(directory, 2)
            args.concurrency = 2
            self.assertEqual(batch_worker.run_batch(args, batch_worker.ControlRelay(), events.append, Mock()), 0)
        self.assertEqual(peak, 2)
        self.assertEqual(len(commands), 4)
        paths = [command[command.index('--run-dir') + 1] for command in commands]
        self.assertEqual(len(set(paths)), 4)
        self.assertEqual({e['lane'] for e in events if e['kind'] == 'round'}, {1, 2})
        self.assertEqual(events[-1]['total'], 4)

    def test_lane_ip_and_call_counts_do_not_mix(self):
        inspector = Inspector()
        inspector.new_ip = True
        for lane in (1, 2):
            inspector.record({'kind': 'round', 'phase': 'start', 'lane': lane, 'round': 1, 'run_dir': f'lane-{lane}'})
            inspector.record({'kind': 'proxy', 'lane': lane, 'status': 'ready', 'exit_ip': f'8.8.8.{lane}'})
        inspector.record({'kind': 'network', 'lane': 1, 'path': '/one', 'http_status': 200, 'code': 0})
        for _ in range(2):
            inspector.record({'kind': 'network', 'lane': 2, 'path': '/two', 'http_status': 200, 'code': 0})
        for lane in (1, 2):
            inspector.record({'kind': 'round', 'phase': 'finish', 'lane': lane, 'round': 1, 'status': 'completed'})
        rows = inspector.batch['rounds']
        self.assertEqual([(r['exit_ip'], r['requests']) for r in rows], [('8.8.8.1', 1), ('8.8.8.2', 2)])
        self.assertEqual(inspector.batch['requests'], 3)
        self.assertEqual(inspector.snapshot(0, lane=1)['proxy']['exit_ip'], '8.8.8.1')

    def test_concurrency_and_retry_inputs_are_validated(self):
        for value in (0, 101, True, 1.5, '2'):
            with self.subTest(concurrency=value), self.assertRaises(ValueError):
                Inspector().start('demo', '', 1, concurrency=value)
        for value in (-1, 4, True, 1.5, '1'):
            with self.subTest(ip_retries=value), self.assertRaises(ValueError):
                Inspector().start('demo', '', 1, ip_retries=value)

    def test_real_failed_workers_wait_and_finish_all_lanes_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = subprocess.run([sys.executable, str(batch_worker.ROOT / 'batch_worker.py'),
                                     '--mode', 'demo', '--video', 'BV0000000000', '--seconds', '1',
                                     '--run-dir', str(root), '--repeat', '2', '--concurrency', '2'],
                                    env={**os.environ, 'TV_FFMPEG': str(root / 'missing-ffmpeg.exe'),
                                         'PYTHONIOENCODING': 'utf-8'}, capture_output=True,
                                    text=True, encoding='utf-8', timeout=25)
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        finished = [e for e in events if e['kind'] == 'round' and e['phase'] == 'finish']
        self.assertEqual(len(finished), 4)
        self.assertTrue(all(e['status'] == 'failed' for e in finished))
        self.assertEqual(events[-1]['failed'], 4)
        self.assertEqual(events[-1]['status'], 'completed_with_errors')
        self.assertFalse(any(e['kind'] == 'network' for e in events))
        for lane in (1, 2):
            first = next(e for e in finished if e['lane'] == lane and e['round'] == 1)
            second = next(e for e in events if e['kind'] == 'round' and e['phase'] == 'start' and e['lane'] == lane and e['round'] == 2)
            self.assertGreaterEqual(second['at'] - first['at'], 4.9)

    def test_resume_updates_state_for_the_next_round(self):
        relay = batch_worker.ControlRelay(True)
        proc = FakeProcess()
        relay.proc = proc
        relay.send('resume')
        self.assertFalse(relay.paused)
        self.assertIn('resume', proc.stdin.getvalue())

    def test_stop_discards_buffered_worker_events(self):
        inspector = Inspector()
        inspector.stopped = True
        inspector.status = 'stopped'
        inspector.batch['current'] = 1
        proc = FakeProcess()
        proc.stdout = io.StringIO(json.dumps({'kind': 'round', 'phase': 'start', 'round': 2,
                                             'run_dir': 'should-not-start'}) + '\n')
        inspector.collect(proc)
        self.assertEqual(inspector.batch['current'], 1)
        self.assertEqual(inspector.status, 'stopped')

    def test_ip_and_request_summary_survive_round_reset(self):
        inspector = Inspector()
        inspector.new_ip = True
        for number in (1, 2):
            inspector.record({'kind': 'round', 'phase': 'start', 'round': number, 'run_dir': f'round-{number}'})
            inspector.record({'kind': 'proxy', 'status': 'acquiring'})
            if number == 1:
                inspector.record({'kind': 'proxy', 'status': 'verifying', 'endpoint': '1.1.1.1:80'})
                inspector.record({'kind': 'proxy', 'status': 'retrying'})
                inspector.record({'kind': 'proxy', 'status': 'acquiring'})
            else:
                # Extraction failure without a returned proxy does not count as used.
                inspector.record({'kind': 'proxy', 'status': 'retrying'})
                inspector.record({'kind': 'proxy', 'status': 'acquiring'})
            inspector.record({'kind': 'proxy', 'status': 'verifying', 'endpoint': '1.1.1.1:80'})
            inspector.record({'kind': 'proxy', 'status': 'ready', 'exit_ip': f'8.8.8.{number}', 'endpoint': '1.1.1.1:80'})
            inspector.record({'kind': 'network', 'path': '/test', 'http_status': 200, 'code': 0})
            inspector.record({'kind': 'round', 'phase': 'finish', 'round': number, 'status': 'completed'})
        self.assertEqual(inspector.batch['requests'], 2)
        self.assertEqual(inspector.batch['ip_used'], 3)
        self.assertEqual(inspector.batch['ip_attempts'], 4)
        self.assertEqual(inspector.metrics['ip_used'], 1)
        self.assertEqual([row['ip_used'] for row in inspector.batch['rounds']], [2, 1])
        self.assertEqual(inspector.metrics['request_count'], 1)
        self.assertEqual([row['exit_ip'] for row in inspector.batch['rounds']], ['8.8.8.1', '8.8.8.2'])
        result = inspector.snapshot(0, detail=False)
        self.assertEqual(result['batch']['ip_used'], 3)
        self.assertTrue(all(event['kind'] != 'trace' for event in result['events']))
        self.assertIsNone(result['current'])

    def test_network_logs_strip_query_and_include_failure_status(self):
        calls = []
        class Response(io.BytesIO):
            status = 200
        with patch.object(tv, 'NETWORK_OBSERVER', lambda event, elapsed: calls.append((event, elapsed))), patch.object(tv, 'open_network', return_value=Response(b'{"code":-1,"message":"fixture"}')):
            with self.assertRaises(RuntimeError):
                tv.public_json('/fixture?secret=private')
        self.assertEqual(calls[0][0]['path'], '/fixture')
        self.assertEqual(calls[0][0]['http_status'], 200)
        self.assertEqual(calls[0][0]['code'], -1)
        self.assertNotIn('private', json.dumps(calls))

    def test_state_replace_retries_temporary_reader_lock(self):
        replace = Path.replace
        calls = []
        def locked_then_replace(source, target):
            calls.append(target)
            if len(calls) <= 2:
                raise PermissionError('temporary reader lock')
            return replace(source, target)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'state.json'
            target.write_text('{"status":"playing"}', encoding='utf-8')
            with patch.object(Path, 'replace', locked_then_replace), patch.object(tv.os, 'name', 'nt'), patch.object(tv.time, 'sleep'):
                tv.save(target, {'status': 'completed'})
            self.assertEqual(json.loads(target.read_text())['status'], 'completed')
            self.assertEqual(len(calls), 3)

    def test_permanent_save_error_keeps_previous_state(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'state.json'
            target.write_text('{"keep":true}', encoding='utf-8')
            with patch.object(Path, 'replace', side_effect=PermissionError('permanent')), patch.object(tv.time, 'sleep'):
                with self.assertRaises(PermissionError):
                    tv.save(target, {'keep': False})
            self.assertEqual(json.loads(target.read_text()), {'keep': True})


if __name__ == '__main__':
    unittest.main()
