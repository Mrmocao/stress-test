import argparse
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import batch_worker
from debug_server import Inspector
from playback_duration import duration_range


class DurationRangeTests(unittest.TestCase):
    def test_invalid_ranges_rejected_before_launch(self):
        for low, high in ((46, 45), (0, 45), (-1, 45), (1, 601),
                          (float('nan'), 45), (1, float('inf')), (True, 45),
                          ('40', 45), (None, 45), (40, None)):
            with self.subTest(low=low, high=high), patch('debug_server.subprocess.Popen') as launch:
                with self.assertRaises(ValueError):
                    Inspector().start('demo', '', 45, seconds_min=low, seconds_max=high)
                launch.assert_not_called()
        self.assertEqual(duration_range(45), (45, 45))
        self.assertEqual(duration_range(45, .1, 600), (.1, 600))

    def test_each_lane_round_samples_once_and_passes_same_target_to_worker_and_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            args = argparse.Namespace(mode='demo', video='BV0000000000', seconds=45,
                                      seconds_min=40, seconds_max=44, concurrency=2, repeat=2,
                                      new_ip=False, run_dir=Path(folder))
            commands = []
            inspector = Inspector()
            def launch(command, **kwargs):
                commands.append(command)
                process = Mock()
                process.pid = None
                process.stdout = io.StringIO(json.dumps({'kind':'lifecycle', 'status':'failed'})+'\n')
                process.stdin = io.StringIO()
                process.wait.return_value = 1
                return process
            wait = Mock()
            with patch.object(batch_worker.random, 'uniform', side_effect=[40.1,41.2,42.3,43.4]) as draw, patch.object(batch_worker.subprocess, 'Popen', side_effect=launch):
                batch_worker.run_batch(args, batch_worker.ControlRelay(), inspector.record, wait)
            self.assertEqual(draw.call_count, 4)
            self.assertEqual(wait.call_count, 2)  # Failed first round in each lane waits five seconds.
            for command in commands:
                directory = command[command.index('--run-dir')+1]
                target = float(command[command.index('--seconds')+1])
                event = next(e for e in inspector.events if e['kind']=='round' and e.get('run_dir')==directory)
                self.assertEqual(event['target_seconds'], target)
                result = next(r for r in inspector.batch['rounds'] if r['lane']==event['lane'] and r['number']==event['round'])
                self.assertEqual(result['requested_seconds'], target)
                self.assertEqual(result['progress']['target_seconds'], target)
            self.assertEqual(sorted(r['requested_seconds'] for r in inspector.batch['rounds']), [40.1,41.2,42.3,43.4])

    def test_equal_bounds_keep_fixed_duration(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(batch_worker.random, 'uniform') as draw:
            args = argparse.Namespace(mode='demo', video='BV0000000000', seconds=45,
                                      seconds_min=3.2, seconds_max=3.2, concurrency=1, repeat=1,
                                      new_ip=False, run_dir=Path(folder))
            with patch.object(batch_worker.subprocess, 'Popen', side_effect=OSError):
                events = []
                batch_worker.run_batch(args, batch_worker.ControlRelay(), events.append)
            draw.assert_not_called()
            self.assertEqual(next(e['target_seconds'] for e in events if e.get('phase')=='start'), 3.2)


if __name__ == '__main__':
    unittest.main()
