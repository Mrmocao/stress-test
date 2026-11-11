import ast
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tv_playback as tv
from client_state import ClientState
from tv_media import select_media, decoder_command
from device_profiles import resolve_profile


class TVProtocolTests(unittest.TestCase):
    def test_identity_survives_processes_and_separates_modes_and_slots(self):
        with tempfile.TemporaryDirectory() as folder:
            script = ('import json,sys; from client_state import ClientState; '
                      's=ClientState(sys.argv[1],"live:profile:lane-01"); '
                      'print(json.dumps(s.load()))')
            def launch(_):
                result = subprocess.run([sys.executable, '-c', script, folder],
                                        capture_output=True, check=True, timeout=15,
                                        cwd=Path(tv.__file__).parent)
                return json.loads(result.stdout)
            with ThreadPoolExecutor(max_workers=4) as pool:
                states = list(pool.map(launch, range(4)))
            self.assertTrue(all(s == states[0] for s in states))
            self.assertEqual(states[0]['did'], '')
            self.assertIsNone(states[0]['ftime'])
            store = ClientState(folder, 'live:profile:lane-01')
            self.assertEqual(store.first_time(1700000000), 1700000000)
            self.assertEqual(store.first_time(1700000010), 1700000000)
            store.update_did('server-identity')
            self.assertEqual(launch(0)['did'], 'server-identity')
            for namespace in ('demo:profile:lane-01', 'live:profile:lane-02'):
                other = ClientState(folder, namespace).load()
                self.assertNotEqual(other['buvid'], states[0]['buvid'])
                self.assertEqual(other['did'], '')

    def test_server_identity_is_saved_without_click_replay(self):
        for code in (0, 14013):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                store = ClientState(root / '.private/client-state', 'fixture')
                store.load()
                state = {'client_state_namespace': 'fixture'}
                item = {}
                client = Mock()
                client.request.return_value = {'code': code, 'data': {'rpdid': 'fixture-server-did'}}
                with patch.object(tv, 'ROOT', root):
                    if code:
                        with self.assertRaises(tv.PlaybackFailure):
                            tv.report_click(client, {}, state, root, item)
                    else:
                        tv.report_click(client, {}, state, root, item)
                self.assertEqual(client.request.call_count, 1)
                self.assertNotIn('did', client.request.call_args.args[1])
                self.assertEqual(store.load()['did'], 'fixture-server-did')
                self.assertEqual(item['click_retry_count'], 0)
                self.assertEqual(item['play_click_accepted'], code == 0)

    def test_extra_names_and_independent_session(self):
        extra = ast.literal_eval(tv.extra_string('request-session'))
        self.assertEqual(extra['session_id'], 'request-session')
        self.assertIn('internal_track_id', extra)
        self.assertNotIn('internal_trackid', extra)
        self.assertEqual(extra['resource_from'], '')

    def test_actual_returned_quality_avc_preference_and_dual_inputs(self):
        media = select_media({'quality': 64, 'dash': {
            'video': [{'id': 80, 'base_url': 'https://cdn/80'},
                      {'id': 64, 'codecid': 12, 'base_url': 'https://cdn/hevc'},
                      {'id': 64, 'codecid': 7, 'baseUrl': 'https://cdn/avc'}],
            'audio': [{'bandwidth': 10, 'base_url': 'https://cdn/audio'}]}})
        self.assertEqual(media['urls'], ['https://cdn/avc', 'https://cdn/audio'])
        command = decoder_command('ffmpeg', media, 10, 'test-UA', 'http://proxy:80')
        self.assertEqual(command.count('-re'), 2)
        self.assertEqual(command.count('-http_proxy'), 2)
        self.assertEqual(command.count('-user_agent'), 2)
        self.assertIn('1:a:0', command)

    def test_mp4_and_invalid_media(self):
        media = select_media({'durl': [{'url': 'https://cdn/mp4'}]})
        self.assertEqual(media['audio_input'], 0)
        for result in ({'is_drm': True}, {'is_preview': True},
                       {'durl': [{'url': 'file:///private'}]},
                       {'quality': 64, 'dash': {'video': [], 'audio': [{}]}}):
            with self.subTest(result=result), self.assertRaises(ValueError):
                select_media(result)

        fallback = select_media({'quality': 64, 'dash': {
            'video': [{'id': 32, 'codecid': 7, 'base_url': 'https://cdn/32'}],
            'audio': [{'bandwidth': 10, 'base_url': 'https://cdn/audio'}]}})
        self.assertEqual(fallback['selection']['selected_quality'], 32)
        self.assertEqual(fallback['selection']['reason'], 'lower-quality-fallback')
        self.assertEqual(fallback['quality'], 32)

    def test_build_serial_only_for_captured_version(self):
        self.assertEqual(resolve_profile('demo', 'offline-android9')['common']['build_sn'], '29938492')
        self.assertNotIn('build_sn', resolve_profile('demo', 'offline-tv08')['common'])

    def test_separate_web_tv_hosts_and_headers(self):
        class Response(io.BytesIO):
            status = 200
        seen = []
        def open_request(request, timeout):
            seen.append(request)
            return Response(b'{"code":0,"data":{}}')
        with tempfile.TemporaryDirectory() as folder, patch.object(tv, 'open_network', side_effect=open_request):
            client = tv.TVClient(Path(folder), Path(folder) / 'events.jsonl', buvid='fixture')
            tv.public_json('/x/web-interface/view')
            client.request('/x/tv/playurl', {})
        self.assertEqual(urlsplit(seen[0].full_url).hostname, 'api.bilibili.com')
        self.assertEqual(urlsplit(seen[1].full_url).hostname, 'api.snm0516.aisee.tv')
        self.assertEqual(seen[1].get_header('App-key'), 'android_tv_yst')
        self.assertEqual(seen[1].get_header('Session-id'), client.request_session)
        self.assertEqual(json.loads(parse_qs(urlsplit(seen[1].full_url).query)['statistics'][0])['appId'], '18')


if __name__ == '__main__':
    unittest.main()
