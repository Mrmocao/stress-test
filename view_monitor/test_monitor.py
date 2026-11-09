import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from monitor import Monitor, apply_sample, fetch_public_stats, normalize_video


def summary():
    return {'sample_count': 0, 'error_count': 0, 'success_count': 0,
            'decrease_count': 0, 'baseline_view': None, 'latest_view': None}


def sample(count, at='2026-11-07T12:00:00+08:00'):
    return {'ok': True, 'observed_at': at, 'bvid': 'BV1S64q69EPN', 'aid': 123,
            'view': count, 'title': '测试视频', 'latency_ms': 3, 'stats': {'view': count}}


class MonitorTests(unittest.TestCase):
    def test_supported_video_inputs(self):
        for text in ('BV1S64q69EPN', 'https://www.bilibili.com/video/BV1S64q69EPN/?spm_id_from=x'):
            self.assertEqual(normalize_video(text), ('bvid', 'BV1S64q69EPN'))
        self.assertEqual(normalize_video('av123'), ('aid', '123'))
        for text in ('https://evil.example/video/BV1S64q69EPN', 'BVshort', '../state', 'https://www.bilibili.com/other/BV1S64q69EPN'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                normalize_video(text)

    def test_error_before_baseline_does_not_create_zero_baseline(self):
        state = summary()
        apply_sample(state, {'ok': False, 'observed_at': 'a', 'error': 'HTTP 412'})
        self.assertIsNone(state['baseline_view'])
        apply_sample(state, sample(137))
        self.assertEqual(state['baseline_view'], 137)
        self.assertEqual(state['delta_baseline'], 0)

    def test_increase_compares_to_baseline_and_previous_success(self):
        state = summary()
        apply_sample(state, sample(100))
        apply_sample(state, {'ok': False, 'observed_at': 'later', 'error': 'timeout'})
        self.assertEqual(state['latest_view'], 100)
        row = sample(104)
        apply_sample(state, row)
        self.assertEqual((row['delta_baseline'], row['delta_previous']), (4, 4))
        self.assertEqual(state['success_count'], 2)
        self.assertEqual(state['error_count'], 1)
        self.assertIsNotNone(state['first_increase_at'])

    def test_downward_correction_is_retained(self):
        state = summary()
        apply_sample(state, sample(100))
        apply_sample(state, sample(105))
        row = sample(98)
        apply_sample(state, row)
        self.assertEqual((row['delta_baseline'], row['delta_previous']), (-2, -7))
        self.assertEqual(state['decrease_count'], 1)

    def test_zero_is_a_valid_count(self):
        state = summary()
        apply_sample(state, sample(0))
        apply_sample(state, sample(1))
        self.assertEqual(state['baseline_view'], 0)
        self.assertEqual(state['delta_baseline'], 1)

    def test_remote_query_is_metadata_only_and_missing_stats_fail(self):
        class Response(io.BytesIO):
            headers = {}
        payload = {'code': 0, 'data': {'state': 0, 'bvid': 'BV1S64q69EPN', 'aid': 123, 'stat': {'view': 4}}}
        with patch('monitor.urllib.request.urlopen', return_value=Response(json.dumps(payload).encode())) as opener:
            result = fetch_public_stats('bvid', 'BV1S64q69EPN')
            req = opener.call_args.args[0]
            self.assertEqual(req.get_method(), 'GET')
            self.assertTrue(req.full_url.startswith('https://api.bilibili.com/x/web-interface/view?'))
            self.assertIsNone(req.data)
            self.assertEqual(result['view'], 4)
        payload['data']['stat']['view'] = '--'
        with patch('monitor.urllib.request.urlopen', return_value=Response(json.dumps(payload).encode())):
            with self.assertRaisesRegex(ValueError, '有效的公开播放量'):
                fetch_public_stats('bvid', 'BV1S64q69EPN')

    def test_video_identity_mismatch_rejected(self):
        class Response(io.BytesIO):
            headers = {}
        payload = {'code': 0, 'data': {'state': 0, 'bvid': 'BV0000000000', 'aid': 123, 'stat': {'view': 4}}}
        with patch('monitor.urllib.request.urlopen', return_value=Response(json.dumps(payload).encode())):
            with self.assertRaisesRegex(ValueError, '不符'):
                fetch_public_stats('bvid', 'BV1S64q69EPN')

    def test_invalid_configuration_does_not_start_thread(self):
        with tempfile.TemporaryDirectory() as folder:
            watcher = Monitor(folder)
            for interval, minutes in ((1, 60), (30, 0), (float('nan'), 60), (30, float('inf'))):
                with self.assertRaises(ValueError):
                    watcher.start('BV1S64q69EPN', interval, minutes)
            self.assertIsNone(watcher.thread)

    def test_stop_persists_and_history_survives_restart(self):
        entered = threading.Event()
        release = threading.Event()
        def fetcher(*args):
            entered.set()
            release.wait(3)
            return sample(42)
        with tempfile.TemporaryDirectory() as folder:
            watcher = Monitor(folder, fetcher)
            identifier = watcher.start('BV1S64q69EPN', 30, 1)
            self.assertTrue(entered.wait(2))
            with self.assertRaisesRegex(ValueError, '已有观测'):
                watcher.start('BV1S64q69EPN')
            watcher.stop()
            release.set()
            watcher.thread.join(3)
            self.assertFalse(watcher.running())
            self.assertEqual(watcher.current['status'], 'stopped')
            restored = Monitor(folder, fetcher).snapshot()
            self.assertEqual(restored['session']['baseline_view'], 42)
            self.assertEqual(restored['session']['id'], identifier)
            self.assertFalse(restored['running'])
            self.assertEqual(len(restored['samples']), 1)
            self.assertTrue((Path(folder)/identifier/'samples.csv').exists())

    def test_snapshot_cannot_mutate_monitor_state(self):
        with tempfile.TemporaryDirectory() as folder:
            watcher = Monitor(folder)
            watcher.current = {'id': 'test', 'latest_view': 42}
            exported = watcher.snapshot()
            exported['session']['latest_view'] = 0
            self.assertEqual(watcher.current['latest_view'], 42)


if __name__ == '__main__':
    unittest.main()
