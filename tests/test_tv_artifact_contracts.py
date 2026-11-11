import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from client_state import ClientState
from device_parameter_rules import tv_apk_contract, tv_apk_catalog, SHAFA_TV_APK
from device_profiles import generate_profile, resolve_profile
from public_state import public_copy
from debug_worker import emit, value_snapshot
import game_device_library as library
from test_game_device_library import row


class ArtifactContractTests(unittest.TestCase):
    def test_same_version_different_artifacts_keep_channel_and_serial_together(self):
        official = tv_apk_contract('1.8.9')
        shafa = tv_apk_contract(artifact_sha256=SHAFA_TV_APK)
        self.assertEqual(official['version_code'], shafa['version_code'])
        self.assertEqual((official['channel'], official['build_sn']), ('master', '29938492'))
        self.assertEqual((shafa['channel'], shafa['build_sn']), ('shafa', '29938502'))
        self.assertEqual(tv_apk_contract('1.6.6')['version_code'], 106600)
        self.assertEqual(len({c['artifact_sha256'] for c in tv_apk_catalog()}), 5)
        self.assertEqual(tv_apk_contract('1.6.9')['build_sn'], '14500739')
        with self.assertRaises(ValueError):
            tv_apk_contract('1.6.6', artifact_sha256=SHAFA_TV_APK)
        shafa['native_abis'].clear()
        self.assertEqual(tv_apk_contract(artifact_sha256=SHAFA_TV_APK)['native_abis'], ['armeabi-v7a'])

    def test_unobserved_fields_remain_null_and_unverified_protocols_cannot_start_live(self):
        for version in ('1.8.8', '1.8.7'):
            contract = tv_apk_contract(version)
            self.assertIsNone(contract['version_code'])
            self.assertIsNone(contract['build_sn'])
            self.assertFalse(contract['artifact_verified'])
        for selection in ('offline-tv05', 'offline-tv08', 'apk-shafa-189', 'apk-master-166', 'apk-master-169', 'apk-dangbei-189'):
            self.assertTrue(resolve_profile('demo', selection)['synthetic'])
            with self.assertRaises(ValueError):
                resolve_profile('live', selection, True)

    def test_distinct_launches_separate_identity_even_when_model_matches(self):
        profiles = [generate_profile(f'{i:032x}') for i in range(5)]
        first = profiles[0]
        second = next(p for p in profiles[1:] if p['environment'] == first['environment'])
        with tempfile.TemporaryDirectory() as root:
            a = ClientState(root, 'demo:' + first['id']).load()
            b = ClientState(root, 'demo:' + second['id']).load()
            self.assertNotEqual(a['installation_id'], b['installation_id'])
            self.assertNotEqual(a['buvid'], b['buvid'])
            ClientState(root, 'demo:' + first['id']).update_did('private-server-did')
            self.assertEqual(ClientState(root, 'demo:' + first['id']).load()['did'], 'private-server-did')
            self.assertEqual(ClientState(root, 'demo:' + second['id']).load()['did'], '')
            self.assertIsNone(b['ftime'])

    def test_public_boundaries_hide_aliases_seed_and_addresses(self):
        profile = generate_profile('a' * 32)
        payload = {'profile': profile, '#user_id': 'private-user', 'androidId': 'private-device',
                   'nested': [{'client-ip': '192.0.2.77', 'exit_ip': '2001:db8::1'}],
                   'message': 'connect 192.0.2.77 and [2001:db8::1]',
                   'url': 'https://example.org/api?uid=private-user'}
        stream = io.StringIO()
        with patch('debug_worker.TRANSPORT', stream):
            emit('fixture', **payload)
        for result in (json.dumps(public_copy(payload)), json.dumps(value_snapshot(payload)), stream.getvalue()):
            for secret in ('private-user', 'private-device', '192.0.2.77', '2001:db8::1', 'a' * 32):
                self.assertNotIn(secret, result)
        self.assertEqual(public_copy(profile)['application']['version_code'], 108900)
        self.assertEqual(profile['generation_seed'], 'a' * 32)

    def test_game_fixture_does_not_publish_user_supplied_seed(self):
        data = library.build_library([row()], [{'事件名': 'app_step', '显示名': '启动'}])
        fixture = library.sample_fixture(data, '192.0.2.99-secret-seed')
        self.assertNotIn('192.0.2.99-secret-seed', json.dumps(fixture))


if __name__ == '__main__':
    unittest.main()
