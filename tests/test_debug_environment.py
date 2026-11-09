import json
import argparse
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from debug_server import Inspector, normalize_video
from debug_worker import value_snapshot
import tv_playback as tv
from playback_runtime import lock_experiment


class EnvironmentTests(unittest.TestCase):
    def test_video_link_extracts_only_id(self):
        self.assertEqual(normalize_video('https://www.bilibili.com/video/BV1Gq486CEK4/?spm_id_from=333'), 'BV1Gq486CEK4')
        self.assertEqual(normalize_video('av12345'), 'av12345')
        for value in ('BVshort', 'bv1Gq486CEK4', 'https://example.com/video/BV1Gq486CEK4', '../state.json'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_video(value)

    def test_snapshots_do_not_alias_mutating_variables(self):
        data = {'attempts': {'video': {'status': 'preparing'}}}
        first = value_snapshot(data)
        data['attempts']['video']['status'] = 'playing'
        self.assertEqual(first['attempts']['video']['status'], 'preparing')
        self.assertEqual(value_snapshot(data)['attempts']['video']['status'], 'playing')

    def test_large_cyclic_objects_and_nonfinite_floats_are_json_safe(self):
        data = {'many': list(range(100)), 'nan': float('nan')}
        data['cycle'] = data
        output = value_snapshot(data)
        encoded = json.dumps(output, allow_nan=False)
        self.assertLess(len(encoded), 4000)
        self.assertEqual(len(output['many']), 26)

    def test_protocol_secrets_and_signed_urls_are_redacted(self):
        result = value_snapshot({'APP_SECRET': 'secret', 'url': 'https://cdn.example/video.mp4?token=private&sign=private'})
        text = json.dumps(result)
        self.assertNotIn('private', text)
        self.assertNotIn('"secret"', text)
        self.assertIn('signed query hidden', text)

    def test_startup_namespace_exposes_effective_arguments(self):
        args = argparse.Namespace(video_id='BV1Gq486CEK4', seconds=8, run_dir=Path('example'))
        result = value_snapshot(args)
        self.assertEqual(result['video_id'], 'BV1Gq486CEK4')
        self.assertEqual(result['seconds'], 8)

    def test_invalid_duration_never_creates_a_worker(self):
        inspector = Inspector()
        for seconds in (0, -1, 601, float('nan'), float('inf')):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                inspector.start('demo', '', seconds)
        self.assertIsNone(inspector.proc)

    def test_setup_failure_is_persisted_as_failed(self):
        with tempfile.TemporaryDirectory() as folder:
            run_dir = Path(folder) / 'run'
            with patch.object(tv, 'ROOT', Path(folder)), patch.object(tv, 'resolve_video', side_effect=RuntimeError('offline setup failure')):
                with self.assertRaisesRegex(RuntimeError, 'offline setup failure'):
                    tv.play_window('BV1Gq486CEK4', 'test', None, run_dir, 2)
            state = json.loads((run_dir / 'state.json').read_text(encoding='utf-8'))
            self.assertEqual(state['status'], 'failed')
            self.assertEqual(state['error_type'], 'RuntimeError')
            self.assertIn('finished_at', state)

    def test_existing_run_directory_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            run_dir = Path(folder)
            marker = run_dir / 'state.json'
            marker.write_text('{"keep": true}', encoding='utf-8')
            with self.assertRaises(FileExistsError):
                tv.play_window('BV1Gq486CEK4', 'test', None, run_dir, 2)
            self.assertEqual(json.loads(marker.read_text()), {'keep': True})

    def test_experiment_lock_excludes_a_second_process(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'playback.lock'
            with target.open('a+', encoding='utf-8') as file:
                lock_experiment(file)
                code = ('import sys\nfrom playback_runtime import lock_experiment\n'
                        'file=open(sys.argv[1], "a+", encoding="utf-8")\n'
                        'try:\n lock_experiment(file)\nexcept BlockingIOError:\n sys.exit(0)\n'
                        'sys.exit(1)\n')
                result = subprocess.run([sys.executable, '-c', code, str(target)],
                                        cwd=Path(tv.__file__).parent, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))


if __name__ == '__main__':
    unittest.main()
