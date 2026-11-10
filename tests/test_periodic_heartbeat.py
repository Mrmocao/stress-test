import io
import ast
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tv_playback as tv
from playback_heartbeat import HeartbeatSchedule
from debug_server import Inspector
from playback_context import PlaybackContext
from urllib.parse import unquote_plus
from test_playback_context import guest_contract


class PeriodicHeartbeatTests(unittest.TestCase):
    def test_stall_fast_decode_end_and_delayed_ticks(self):
        schedule = HeartbeatSchedule(15, 0)
        self.assertIsNone(schedule.take(20, 10, 10, 90))
        self.assertEqual(schedule.take(15, 15, 15, 90), 15)
        self.assertIsNone(schedule.take(15, 50, 50, 90))
        self.assertEqual(schedule.take(60, 60, 60, 90), 60)
        self.assertIsNone(schedule.take(75, 61, 61, 90))
        self.assertIsNone(schedule.take(90, 90, 90, 90))
        self.assertIsNone(schedule.take(80, 80, 80, 90, ended=True))

    def test_disabled(self):
        self.assertIsNone(HeartbeatSchedule(0, 0).take(30, 30, 30, 60))

    def test_invalid_setting_never_launches(self):
        for value in (True, '15', None, -1, 1, float('nan'), float('inf')):
            with self.subTest(value=value), patch('debug_server.subprocess.Popen') as launch:
                with self.assertRaises(ValueError):
                    Inspector().start('demo', '', 45, heartbeat_interval=value)
                launch.assert_not_called()

    def simulate(self, interval=15, fail_phase=None, *, context=None, lifecycle=False,
                 fail_path=None, captured=None):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            private = root / 'private'
            private.mkdir()
            client = Mock(private_dir=private, appkey='offline')
            client.context = context or PlaybackContext()
            client.common = {**tv.DEVICE_PROFILE['common'], **client.context.common()}
            client.request_session = 'fixture-session'
            requests = []
            clock = [0.0]
            view = dict(bvid='BV0000000000', aid=1, cid=2, state=0, stat={'view': 0})
            def request(path, params, kind='GET'):
                if captured is not None:
                    captured.append((path, dict(params), kind))
                if path == fail_path:
                    raise tv.RequestFailure('模拟辅助上报超时')
                if path == '/x/click-interface/click/now':
                    return {'code': 0, 'data': {'now': 1700000000}}
                if path == '/x/tv/playurl':
                    return {'data': {'durl': [{'url': 'http://127.0.0.1/fixture'}],
                                     'timelength': 60000, 'quality': 16}}
                if path.endswith('/heartbeat/mobile'):
                    phase = client.heartbeat_phase
                    requests.append((phase, dict(params)))
                    if phase == fail_phase:
                        raise tv.RequestFailure('模拟心跳超时')
                return {'code': 0, 'data': {}}
            client.request.side_effect = request
            blocks = []
            for second in ((1, 15, 30, 31, 45) if lifecycle else (1, 15, 30, 45)):
                for line in (f'frame={second * 25}\n', f'out_time_us={second * 1000000}\n',
                             'progress=end\n' if second == 45 else 'progress=continue\n'):
                    blocks.append((second, line))
            blocks.append((45, None))
            def get(timeout):
                clock[0], line = blocks.pop(0)
                return line
            lines = Mock()
            lines.get.side_effect = get
            proc = Mock(pid=1, returncode=0, stdout=io.StringIO(''))
            proc.poll.side_effect = lambda: 0 if clock[0] >= 45 else None
            proc.wait.return_value = 0
            def popen(command, **kwargs):
                kwargs['stderr'].write('video:10kB audio:1kB\n')
                kwargs['stderr'].flush()
                return proc
            with patch.object(tv, 'HEARTBEAT_INTERVAL', interval), patch.object(tv, 'TV_LIFECYCLE', lifecycle), patch.object(tv, 'RUN_BUDGET', None), \
                 patch.object(tv, 'PROGRESS_OBSERVER', None), patch.object(tv, 'ffmpeg_executable', return_value='fixture'), \
                 patch.object(tv.subprocess, 'Popen', side_effect=popen), patch.object(tv.queue, 'Queue', return_value=lines), \
                 patch.object(tv.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(sys, 'stdout', io.StringIO()):
                result = tv.play_one(client, view, {'attempts': {}, 'ftime': 1}, root, 45)
            return result, requests

    def test_real_progress_is_reported_in_one_session(self):
        result, requests = self.simulate()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([(p, b['played_time']) for p, b in requests],
                         [('start', 0), ('periodic', 15), ('periodic', 30), ('end', 45)])
        self.assertEqual(len({b['session'] for _, b in requests}), 1)
        self.assertEqual(len({b['start_ts'] for _, b in requests}), 1)
        self.assertTrue(all(b['video_duration'] == 60 and 'duration' not in b for _, b in requests))
        self.assertTrue(all(b['paused_time'] == 0 for _, b in requests))
        self.assertTrue(all('sid' not in b for _, b in requests))

    def test_periodic_failure_keeps_decoding_and_end_report_but_is_not_success(self):
        result, requests = self.simulate(fail_phase='periodic')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['media_status'], 'completed')
        self.assertEqual(result['report_status'], 'unconfirmed')
        self.assertEqual(result['failure_stage'], 'periodic_heartbeat')
        self.assertEqual(requests[-1][0], 'end')
        self.assertEqual(len(requests), 4)  # No transport replay after a timeout.

    def test_disabled_preserves_two_reports(self):
        result, requests = self.simulate(interval=0)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([p for p, _ in requests], ['start', 'end'])

    def test_failed_start_never_sends_periodic_or_end(self):
        result, requests = self.simulate(fail_phase='start')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual([p for p, _ in requests], ['start'])

    def test_explicit_context_and_lifecycle_are_used_by_real_playback_path(self):
        context = PlaybackContext({**guest_contract(), 'profile_id': tv.DEVICE_PROFILE['id'],
            'scene': {'teenager_mode': 3}, 'guest': {'guest_id': 'fixture-guest', 'guest_access_key': 'fixture-key'},
            'report': {'spmid': 'fixture.player', 'from_spmid': 'fixture.home', 'auto_play': 6},
            'extra': {'internal_track_id': 'fixture-track'}})
        captured = []
        result, requests = self.simulate(interval=0, context=context, lifecycle=True, captured=captured)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['lifecycle_status'], 'completed')
        paths = [p for p, _, _ in captured]
        self.assertLess(paths.index('/x/tv/playurl/online'), paths.index('/x/report/heartbeat/mobile'))
        self.assertEqual(paths.count('/x/tv/playurl/online'), 2)
        self.assertEqual(paths.count('/x/tv/history/add'), 1)
        click = next(fields for path, fields, _ in captured if path.endswith('/click/android/tv'))
        self.assertEqual(json.loads(click['statistics'])['version'], click['build'])
        self.assertEqual(click['auto_play'], '6')
        self.assertEqual(click['from_spmid'], 'fixture.home')
        self.assertEqual(ast.literal_eval(unquote_plus(click['extra']))['teenager_mode'], '3')
        self.assertNotIn('from_spmid', ast.literal_eval(unquote_plus(click['extra'])))
        heartbeat = requests[0][1]
        parsed = ast.literal_eval(heartbeat['extra'])
        self.assertEqual(parsed['from_spmid'], 'fixture.home')
        self.assertEqual(parsed['guest_id'], 'fixture-guest')
        self.assertEqual(parsed['session_id'], 'fixture-session')
        self.assertEqual(heartbeat['fourk'], 1)
        history = next(fields for path, fields, _ in captured if path == '/x/tv/history/add')
        self.assertEqual((history['play_duration'], history['pro']), ('45', '45'))
        self.assertNotIn('fixture-key', json.dumps(result))
        self.assertNotIn('fixture-track', json.dumps(result))

    def test_lifecycle_failure_does_not_hide_media_completion_or_replay(self):
        for endpoint in ('/x/tv/playurl/online', '/x/tv/history/add'):
            with self.subTest(endpoint=endpoint):
                captured = []
                result, requests = self.simulate(interval=0, lifecycle=True, fail_path=endpoint, captured=captured)
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['media_status'], 'completed')
                self.assertEqual(result['report_status'], 'unconfirmed')
                self.assertEqual(result['failure_stage'], 'tv_lifecycle')
                self.assertEqual(sum(path == endpoint for path, _, _ in captured), 1)
                self.assertEqual(requests[-1][0], 'end')

    def test_identity_update_is_not_counted_as_an_accepted_report(self):
        inspector = Inspector()
        inspector.record({'kind': 'network', 'http_status': 200, 'code': 14013, 'identity_update': True})
        self.assertEqual(inspector.batch['requests'], 1)
        self.assertEqual(inspector.batch['errors'], 1)


if __name__ == '__main__':
    unittest.main()
