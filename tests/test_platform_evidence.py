import base64
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from debug_server import Inspector
from platform_contracts import platform_catalog
from tools.extract_platform_evidence import add, body_fields, endpoint, finalize, extract_web


class PlatformEvidenceTests(unittest.TestCase):
    def test_form_values_and_signed_media_paths_never_leave_projection(self):
        rows = {}
        raw = 'mid=private-user-123&buvid=private-device-456&ip=192.0.2.18&csrf=private-token'
        add(rows, {'method': 'POST', 'url': 'https://api.bilibili.com/x/test?session=private-session',
                   'header_names': ['Cookie', 'Authorization']}, 'ev_fixture', {'request_event_sequence': 1},
            200, {'state': 'captured', 'encoding': 'base64', 'content': base64.b64encode(raw.encode()).decode(),
                  'media_type': 'application/x-www-form-urlencoded;charset=UTF-8'})
        add(rows, {'method': 'GET', 'url': 'https://upos-secret.bilivideo.com/private-media.m4s?upsig=private-sign'},
            'ev_fixture', {'request_event_sequence': 2}, 206)
        result = finalize(rows)
        encoded = json.dumps(result)
        for value in ('private-', '192.0.2.18', 'upos-secret', raw):
            self.assertNotIn(value, encoded)
        post = next(row for row in result if row['method'] == 'POST')
        self.assertEqual(post['body_parameters'], ['buvid', 'csrf', 'ip', 'mid'])
        self.assertEqual(post['query_parameters'], ['session'])

    def test_legacy_telemetry_does_not_turn_nested_urls_into_parameters(self):
        rows = {}
        add(rows, {'method': 'GET', 'url': 'https://data.bilibili.com/log/web?payload,https://cdn/media?buvid=secret&mid=private'},
            'ev_fixture', {'request_event_sequence': 1}, 200)
        row = finalize(rows)[0]
        self.assertEqual(row['query_parameters'], [])
        self.assertEqual(row['query_shape'], 'opaque_telemetry_not_decoded')

    def test_opaque_and_missing_body_do_not_mean_empty_schema(self):
        self.assertEqual(body_fields({'state': 'not_exposed'}), ([], 'not_exposed'))
        self.assertEqual(body_fields({'state': 'captured', 'media_type': 'text/plain', 'content': 'opaque'}),
                         ([], 'opaque_not_decoded'))
        self.assertIsNone(endpoint('http://192.0.2.1/x/test'))
        self.assertIsNone(endpoint('https://unrelated.example/x/test'))

    def test_transactions_join_by_id_not_receipt_order(self):
        record = {'evidence_id': 'ev_fixture', 'authority': 'controlled-replay', 'normalized_result': {
            'browser': {'product': 'Chromium', 'version': 'test'}, 'completeness': {'status': 'incomplete'},
            'events': {'retained': 3, 'dropped': 0, 'items': [
                {'kind': 'request', 'transaction_id': 'one', 'sequence': 1, 'method': 'GET', 'url': {'url': 'https://api.bilibili.com/x/one'}, 'header_names': []},
                {'kind': 'request', 'transaction_id': 'two', 'sequence': 2, 'method': 'GET', 'url': {'url': 'https://api.bilibili.com/x/two'}, 'header_names': []},
                {'kind': 'response', 'transaction_id': 'two', 'sequence': 3, 'status': 403},
            ]}}}
        rows = {r['path']: r for r in extract_web(record)['interfaces']}
        self.assertEqual(rows['/x/one']['response_count'], 0)
        self.assertEqual(rows['/x/two']['http_status_counts'], {'403': 1})

    def test_non_tv_platform_cannot_launch_tv_worker_in_either_mode(self):
        inspector = Inspector()
        before = inspector.snapshot(0)
        with patch('debug_server.subprocess.Popen') as launch:
            for platform in ('apk', 'web', '', None, 'unknown'):
                for mode in ('demo', 'live'):
                    with self.subTest(platform=platform, mode=mode), self.assertRaises(ValueError):
                        inspector.start(mode, 'BV1Gq486CEK4', 1, platform=platform)
            launch.assert_not_called()
        self.assertEqual(inspector.snapshot(0), before)

    def test_catalog_marks_missing_apk_and_partial_web_honestly(self):
        catalog = {r['id']: r for r in platform_catalog()}
        self.assertEqual(set(catalog), {'tv', 'apk', 'web'})
        self.assertGreaterEqual(len(catalog['apk']['interfaces']), 1)
        self.assertEqual(catalog['apk']['application']['package_name'], 'tv.danmaku.bili')
        self.assertEqual(catalog['apk']['application']['version_code'], 9180300)
        self.assertEqual(catalog['apk']['application']['build_sn'], 30591258)
        self.assertIsNone(catalog['apk']['application']['channel'])
        self.assertEqual(catalog['web']['coverage']['status'], 'incomplete')
        self.assertFalse(catalog['web']['playback_supported'])
        self.assertFalse(any(r['path'] == '/x/player/wbi/playurl' for r in catalog['web']['interfaces']))


if __name__ == '__main__':
    unittest.main()
