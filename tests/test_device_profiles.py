import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import device_profiles as profiles
from debug_server import Inspector
import tv_playback as tv


class DeviceProfileTests(unittest.TestCase):
    def test_unknown_selection_and_unconfirmed_baseline_rejected_before_spawn(self):
        for selection, confirmed in [('missing', True), (None, True),
                                     ('original-tv', False), ('original-tv', 'true')]:
            with self.subTest(selection=selection, confirmed=confirmed), patch('debug_server.subprocess.Popen') as spawn:
                with self.assertRaises(ValueError):
                    Inspector().start('live', 'BV1S64q69EPN', 1,
                                      device_profile=selection, device_confirmed=confirmed)
                spawn.assert_not_called()
        self.assertEqual(profiles.resolve_profile('live', 'original-tv', True)['id'], 'original-tv')

    def test_random_draws_complete_test_profiles_in_both_modes_and_copies_are_isolated(self):
        fixtures = [p for p in profiles.catalog() if p['synthetic']]
        self.assertEqual(len(fixtures), 10)
        self.assertEqual(len({p['common']['model'] for p in fixtures}), 10)
        self.assertEqual({v:sum(p['app_version'] == v for p in fixtures) for v in profiles.SIMULATED_VERSIONS},
                         {'1.8.9':4, '1.8.8':3, '1.8.7':3})
        for fixture in fixtures:
            key = fixture['id']
            for mode in ('demo', 'live'):
                with patch.object(profiles.secrets, 'choice', return_value=key) as choose:
                    selected = profiles.resolve_profile(mode, profiles.RANDOM)
                choose.assert_called_once()
                self.assertEqual(set(choose.call_args.args[0]), {p['id'] for p in fixtures})
                self.assertFalse(selected['actual_client_confirmed'])
                self.assertFalse(selected['offline_only'])
            common = selected['common']
            self.assertIn(common['model'], selected['user_agent'])
            self.assertIn('build/' + common['build'], selected['user_agent'])
            self.assertIn('BiliTV/' + selected['app_version'], selected['user_agent'])
            self.assertEqual(common['sys_ver'], {'8.1':'27', '9':'28', '10':'29', '11':'30', '12':'31', '13':'33'}[selected['android_version']])
            self.assertEqual(common['build'], profiles.SIMULATED_VERSIONS[selected['app_version']])
            self.assertEqual(profiles.resolve_profile('live', key)['id'], key)
            self.assertFalse(profiles.resolve_profile('live', key, True)['actual_client_confirmed'])
            selected['common']['model'] = 'mutated'
            self.assertNotEqual(profiles.resolve_profile('demo', key)['common']['model'], 'mutated')

    def test_default_profile_preserves_existing_client_parameters(self):
        self.assertEqual(tv.UA, 'Mozilla/5.0 BiliTV/1.8.9 os/android model/MiTV-ASTP0 mobi_app/android_tv_yst build/108900 channel/master innerVer/108900 osVer/9 network/2')
        with tempfile.TemporaryDirectory() as folder:
            client = tv.TVClient(Path(folder), Path(folder) / 'events.jsonl', buvid='test')
            self.assertEqual(client.common, {'platform':'android', 'mobi_app':'android_tv_yst',
                'build':'108900', 'channel':'master', 'sys_ver':'28', 'brand':'xiaomi',
                'model':'MiTV-ASTP0', 'teenager_mode':'0', 'child_lock':'0', 'build_sn':'29938492',
                'statistics':'{"appId":"18","platform":"3","version":"108900"}'})

    def test_worker_rejects_unknown_profile_before_creating_run_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / 'must-not-exist'
            result = subprocess.run([sys.executable, 'debug_worker.py', '--mode', 'live',
                '--device-profile', 'missing-profile', '--run-dir', str(run)],
                cwd=Path(tv.__file__).parent, capture_output=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(run.exists())

    def test_profile_visible_without_trace_and_preserved_in_round_summary(self):
        inspector = Inspector()
        inspector.record({'kind':'round', 'phase':'start', 'lane':1, 'round':1, 'run_dir':'test'})
        profile = profiles.resolve_profile('demo', 'offline-android11')
        inspector.record({'kind':'device_profile', 'lane':1, 'profile':profile})
        self.assertEqual(inspector.snapshot(0, detail=False)['workers'][0]['device_profile'], profile)
        inspector.record({'kind':'round', 'phase':'finish', 'lane':1, 'round':1, 'status':'completed'})
        self.assertEqual(inspector.batch['rounds'][0]['device_profile'], profile)
        inspector.record({'kind':'round', 'phase':'start', 'lane':1, 'round':2, 'run_dir':'test2'})
        self.assertIsNone(inspector.worker(1)['device_profile'])

    def test_all_three_versions_reach_request_fields_in_both_mode_branches(self):
        cases = [(mode, key) for mode in ('demo', 'live')
                 for key in ('offline-android9', 'offline-tv05', 'offline-tv08')]
        for mode, key in cases:
            with self.subTest(mode=mode, profile=key), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                # Exercise the live branch too, with a network-denying local
                # transport installed BEFORE main. No public API traffic.
                script = ('import sys\nfrom pathlib import Path\nimport tv_playback\n'
                          'tv_playback.ROOT=Path(sys.argv[1])\n'
                          'import debug_worker\n'
                          'if sys.argv[3]=="live": debug_worker.install_demo(tv_playback, Path(sys.argv[1]), 1)\n'
                          'sys.argv=["debug_worker.py","--mode",sys.argv[3],"--seconds","1",'
                          '"--run-dir",sys.argv[1],"--device-profile",sys.argv[2]]\n'
                          'sys.exit(debug_worker.main())\n')
                result = subprocess.run([sys.executable, '-c', script, str(root), key, mode],
                                        cwd=Path(tv.__file__).parent, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
                state = json.loads((root / 'playback/state.json').read_text(encoding='utf-8'))
                selected = profiles.resolve_profile(mode, key)
                self.assertEqual(state['device_profile'], selected)
                private = Path(state['private_dir'])
                requests = [json.loads(p.read_text(encoding='utf-8')) for p in private.glob('request-*.json')]
                self.assertTrue(any(r['kind'] == 'CLICK' for r in requests))
                for r in requests:
                    self.assertEqual(r['headers']['User-Agent'], selected['user_agent'])
                    if r['kind'] == 'CLICK':
                        self.assertEqual(r['params']['build'], selected['common']['build'])
                from urllib.parse import parse_qs, urlsplit
                for p in private.glob('request-*.json'):
                    r = json.loads(p.read_text(encoding='utf-8'))
                    if r['kind'] == 'CLICK':
                        continue
                    body = urlsplit(r['url']).query if r['kind'] == 'GET' else p.with_suffix('.body').read_text()
                    self.assertEqual(parse_qs(body)['build'], [selected['common']['build']])

    def test_legacy_random_selection_is_compatible_without_claiming_device_verification(self):
        for mode in ('demo', 'live'):
            with patch.object(profiles.secrets, 'choice', return_value='offline-tv08') as choose:
                result = profiles.resolve_profile(mode, profiles.LEGACY_RANDOM, True)
            choose.assert_called_once()
            self.assertEqual(result['id'], 'offline-tv08')
            self.assertFalse(result['actual_client_confirmed'])


if __name__ == '__main__':
    unittest.main()
