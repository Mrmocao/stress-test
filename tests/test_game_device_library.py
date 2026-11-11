import csv
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import game_device_library as library
from device_parameter_rules import android_sdk_reference, tv_apk_contract
from device_profiles import generate_profile


def row(device='private-device-A', **changes):
    result = {key: '' for key in library.REQUIRED}
    result.update({
        '#device_id': device, '#user_id': 'private-user', '#ip': '192.0.2.77',
        '#uuid': 'private-event-uuid', 'phone_device': 'private-phone-id',
        'extend_data': 'private-payload', '#event_time': '2026-11-11 00:01:00',
        '$part_event': 'app_step', '#os': 'Android', '#os_version': '14',
        '#manufacturer': 'TestVendor', '#device_model': 'TestPhone',
        '#device_type': 'Phone', '#screen_width': '1080', '#screen_height': '2400',
        '#simulator': 'false', '#bundle_id': 'com.example.game', '#app_version': '2.0.0',
        'versionname': '2.0.0', 'platformid': '7', 'channel_id': '7',
        '#ram': '2.0/8.0', '#disk': '32.0/128.0', '#network_type': 'WIFI',
        '#lib': 'Unity', '#lib_version': '2.6.1',
    })
    result.update(changes)
    return result


class GameDeviceLibraryTests(unittest.TestCase):
    def build(self, rows):
        return library.build_library(rows, [{'事件名': 'app_step', '显示名': '前端设备启动'}])

    def test_event_repetition_does_not_increase_device_sampling_weight(self):
        data = self.build([row()] * 100 + [row('private-device-B', **{'#device_model': 'PhoneB'})])
        self.assertEqual(data['summary']['event_rows'], 101)
        self.assertEqual(data['summary']['device_ids'], 2)
        self.assertEqual(data['summary']['device_weight_sum'], 2)
        self.assertEqual(sorted(o['device_weight'] for o in data['observations']), [1, 1])
        counts = {}
        for i in range(600):
            model = library.sample_fixture(data, str(i))['hardware']['model']
            counts[model] = counts.get(model, 0) + 1
        self.assertTrue(all(230 < n < 370 for n in counts.values()), counts)

    def test_joint_sampling_never_crosses_hardware_app_runtime(self):
        a = row()
        b = row('B', **{'#device_model': 'PhoneB', '#bundle_id': 'com.example.other',
                        '#network_type': '5G', '#screen_width': '1440'})
        data = self.build([a, b])
        for i in range(40):
            fixture = library.sample_fixture(data, str(i))
            if fixture['hardware']['model'] == 'TestPhone':
                self.assertEqual(fixture['application']['bundle_id'], 'com.example.game')
                self.assertEqual(fixture['runtime']['network_type'], 'WIFI')
                self.assertEqual(fixture['hardware']['screen_width_reported'], '1080')
            else:
                self.assertEqual(fixture['application']['bundle_id'], 'com.example.other')
                self.assertEqual(fixture['runtime']['network_type'], '5G')

    def test_latest_snapshot_not_csv_order_and_conflicting_model_is_quarantined(self):
        older = row(**{'#network_type': '4G'})
        newer = row(**{'#event_time': '2026-11-11 00:02:00', '#network_type': 'WIFI'})
        data = self.build([newer, older])
        self.assertEqual(library.sample_fixture(data, 'seed')['runtime']['network_type'], 'WIFI')
        newer['#device_model'] = 'OtherPhone'
        data = self.build([newer, older])
        self.assertEqual(data['summary']['hardware_conflict_device_ids'], 1)
        with self.assertRaisesRegex(ValueError, 'No eligible'):
            library.sample_fixture(data, 'seed')
        self.assertIn('device_id_hardware_conflict', library.sample_fixture(data, 'seed', allow_review=True)['review_flags'])

    def test_rotation_is_preserved_but_not_called_an_id_collision(self):
        data = self.build([row(), row(**{'#event_time': '2026-11-11 00:02:00',
                                         '#screen_width': '2400', '#screen_height': '1080'})])
        fixture = library.sample_fixture(data, 'seed')
        self.assertEqual(data['summary']['hardware_conflict_device_ids'], 0)
        self.assertEqual(fixture['hardware']['screen_width_reported'], '2400')
        self.assertEqual(fixture['hardware']['screen_unit'], 'unverified')

    def test_identifiers_and_payloads_never_exported(self):
        data = self.build([row()])
        fixture = library.sample_fixture(data, 'local-seed')
        exported = json.dumps([data, fixture])
        for secret in ('private-device-A', 'private-user', '192.0.2.77',
                       'private-event-uuid', 'private-phone-id', 'private-payload'):
            self.assertNotIn(secret, exported)
        self.assertTrue(fixture['installation_id'].startswith('test-install-'))
        self.assertEqual(fixture, library.sample_fixture(data, 'local-seed'))
        fixture['hardware']['model'] = 'changed'
        self.assertEqual(library.sample_fixture(data, 'local-seed')['hardware']['model'], 'TestPhone')

    def test_unknown_sdk_and_apk_fields_are_not_invented(self):
        self.assertEqual(android_sdk_reference('Android', '8.1.0'), 27)
        self.assertEqual(android_sdk_reference('Android', '12L'), 32)
        self.assertEqual(android_sdk_reference('Android', '16'), 36)
        for family, release in [('HarmonyOS', '4.2.0'), ('iOS', '18.5'), ('other', 'API-26'), ('Android', '17')]:
            self.assertIsNone(android_sdk_reference(family, release))
        f = library.sample_fixture(self.build([row()]), 'seed')
        self.assertIsNone(f['hardware']['android_sdk_observed'])
        self.assertEqual(f['hardware']['android_sdk_reference'], 34)
        self.assertIsNone(f['application']['version_code'])
        self.assertIsNone(f['application']['build_sn'])
        self.assertIsNone(f['hardware']['brand'])

    def test_harmony_and_ios_never_inherit_android_sdk(self):
        for family, version in [('HarmonyOS', '4.2.0'), ('iOS', '18.5')]:
            data = self.build([row(**{'#os': family, '#os_version': version})])
            fixture = library.sample_fixture(data, 'seed', os_family=family)
            self.assertIsNone(fixture['hardware']['android_sdk_reference'])
            with self.assertRaisesRegex(ValueError, 'No eligible'):
                library.sample_fixture(data, 'seed', os_family='Android')

    def test_anomalies_and_simulators_are_explicit_filters(self):
        for changes in ({'#ram': '9/8'}, {'#ram': '1/501.8'}, {'#os_version': '17'},
                        {'versionname': '7.0'}, {'#screen_width': '0'}):
            data = self.build([row(**changes)])
            with self.assertRaisesRegex(ValueError, 'No eligible'):
                library.sample_fixture(data, 'seed')
            self.assertTrue(library.sample_fixture(data, 'seed', allow_review=True)['review_flags'])
        data = self.build([row(**{'#simulator': 'true'})])
        with self.assertRaises(ValueError):
            library.sample_fixture(data, 'seed')
        self.assertTrue(library.sample_fixture(data, 'seed', include_simulators=True)['hardware']['simulator'])

    def test_missing_id_does_not_become_an_independent_device(self):
        data = self.build([row(''), row('NULL'), row('real')])
        self.assertEqual(data['summary']['missing_device_id_rows'], 2)
        self.assertEqual(data['summary']['device_weight_sum'], 1)

    def test_capacity_units_missing_and_invalid_numbers(self):
        self.assertEqual(library.capacity_pair(''), (None, False))
        pair, bad = library.capacity_pair('1.8/5.5')
        self.assertFalse(bad)
        self.assertEqual(pair['denominator'], 5.5)
        self.assertEqual(pair['unit'], 'unverified')
        for value in ('NaN/8', '1/Infinity', 'broken', '-1/8', '1/0', '9/8'):
            pair, bad = library.capacity_pair(value)
            self.assertTrue(bad)
            json.dumps(pair, allow_nan=False)

    def test_csv_bom_multiline_and_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input.csv'
            with path.open('w', encoding='utf-8-sig', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=['事件名', '显示名'])
                writer.writeheader()
                writer.writerow({'事件名': 'app_step', '显示名': '设备启动,第一步\n第二行'})
            rows, source = library.read_csv(path, {'事件名', '显示名'})
            self.assertEqual(rows[0]['显示名'], '设备启动,第一步\n第二行')
            self.assertEqual(source['rows'], 1)
            with self.assertRaises(ValueError):
                library.read_csv(path, library.REQUIRED)
            path.write_text('a,a\n1,2\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                library.read_csv(path, {'a'})

    def test_tv_contract_remains_evidence_bound_and_seed_compatible(self):
        a = tv_apk_contract()
        a['build'] = 'mutated'
        self.assertEqual(tv_apk_contract()['build'], '108900')
        self.assertEqual(tv_apk_contract('1.8.8')['evidence'], 'legacy-synthetic-fixture')
        with self.assertRaises(ValueError):
            tv_apk_contract('1.6.0')
        p = generate_profile('0123456789abcdef0123456789abcdef')
        self.assertEqual(p['common']['model'], p['environment']['model'])
        self.assertIn(p['android_version'], ('9', '11'))
        self.assertEqual(p['common']['build_sn'], '29938492')


if __name__ == '__main__':
    unittest.main()
