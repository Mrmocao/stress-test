import ast
import io
import json
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tv_playback as tv
from playback_context import PlaybackContext
from playback_lifecycle import PlaybackLifecycle, ONLINE_PATH, HISTORY_PATH
from device_profiles import generate_profile


def guest_contract():
    return {'binding': {'installation_id': 'a' * 32, 'client_slot': 'default',
                        'buvid_sha256': hashlib.sha256(b'fixture').hexdigest()},
            'guest_contract': {'source': 'offline_fixture', 'issued_at': 1,
                               'expires_at': 4102444800, 'refresh': 'manual'}}


class ContextTests(unittest.TestCase):
    def test_invalid_schema_cannot_override_protocol_or_client_identity(self):
        for data in (
            {'report': {'auto_play': 0}},
            {'profile_id': tv.DEVICE_PROFILE['id'], 'device': {'build': '1'}},
            {'profile_id': tv.DEVICE_PROFILE['id'], 'report': {'aid': '1'}},
            {'profile_id': tv.DEVICE_PROFILE['id'], 'extra': {'session_id': 'other'}},
            {'profile_id': tv.DEVICE_PROFILE['id'], 'guest': {'guest_id': 'partial'}},
            {'profile_id': tv.DEVICE_PROFILE['id'], 'extra': {'internal_track_id': 'x&aid=2'}},
            {'profile_id': tv.DEVICE_PROFILE['id'], 'extra': {'session_more': "can't"}},
            {'profile_id': tv.DEVICE_PROFILE['id'], 'device': {'memory': float('nan')}},
            {'profile_id': tv.DEVICE_PROFILE['id'], 'device': {'memory': '0'}},
            {'profile_id': tv.DEVICE_PROFILE['id'], 'scene': {'resource_id': 'a'}, 'extra': {'resource_id': 'b'}},
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                PlaybackContext(data)

    def test_context_is_copied_and_cannot_cross_profile_or_leak_in_summary(self):
        data = {**guest_contract(), 'profile_id': tv.DEVICE_PROFILE['id'],
                'guest': {'guest_id': 'private-guest', 'guest_access_key': 'private-key'}}
        context = PlaybackContext(data)
        data['guest']['guest_access_key'] = 'changed'
        copy = context.common()
        copy['guest_access_key'] = 'also-changed'
        self.assertEqual(context.common()['guest_access_key'], 'private-key')
        with self.assertRaises(ValueError):
            context.validate_profile(generate_profile('0' * 32))
        summary = json.dumps(context.summary())
        self.assertNotIn('private-key', summary)
        self.assertNotIn('private-guest', summary)

    def test_effective_context_reaches_transport_and_is_not_added_to_empty_client(self):
        class Response(io.BytesIO):
            status = 200
        seen = []
        def open_request(request, timeout):
            seen.append(request)
            return Response(b'{"code":0,"data":{}}')
        with tempfile.TemporaryDirectory() as folder, patch.object(tv, 'CLIENT_STATE_MODE', 'demo'), patch.object(tv, 'open_network', side_effect=open_request):
            root = Path(folder)
            context = PlaybackContext({**guest_contract(), 'profile_id': tv.DEVICE_PROFILE['id'],
                'device': {'cpu': 'fixture-cpu', 'memory': 2048},
                'scene': {'teenager_mode': 3},
                'guest': {'guest_id': 'private-guest', 'guest_access_key': 'private-key'}})
            context.bind_runtime(tv.DEVICE_PROFILE, {'installation_id': 'a' * 32, 'buvid': 'fixture'}, 'default', 'demo')
            first = tv.TVClient(root / 'one', root / 'events1.jsonl', buvid='fixture', context=context)
            first.request(ONLINE_PATH, {'aid': 1, 'cid': 2})
            second = tv.TVClient(root / 'two', root / 'events2.jsonl', buvid='fixture', context=PlaybackContext())
            second.request(ONLINE_PATH, {'aid': 1, 'cid': 2})
            query = parse_qs(urlsplit(seen[0].full_url).query)
            self.assertEqual(query['guest_access_key'], ['private-key'])
            self.assertEqual(query['memory'], ['2048'])
            self.assertEqual(ast.literal_eval(tv.extra_string(first.request_session, context=context, common=first.common))['teenager_mode'], '3')
            self.assertNotIn('guest_access_key', parse_qs(urlsplit(seen[1].full_url).query))
            self.assertNotIn('private-key', (root / 'events1.jsonl').read_text())
            self.assertNotIn('guest_access_key', tv.DEVICE_PROFILE['common'])

    def test_unknown_profile_rejected_before_request_files_are_created(self):
        context = PlaybackContext({'profile_id': 'other-device'})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(tv, 'open_network') as transport, self.assertRaises(ValueError):
                tv.TVClient(root / 'private', root / 'events.jsonl', context=context)
            self.assertFalse((root / 'private').exists())
            transport.assert_not_called()

    def test_invalid_file_errors_do_not_include_secret_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'context.json'
            for raw in ('{"guest_access_key":"private-value"', 'x' * 65537):
                path.write_text(raw, encoding='utf8')
                with self.assertRaises(ValueError) as error:
                    PlaybackContext.from_file(path)
                self.assertNotIn('private-value', str(error.exception))


class LifecycleTests(unittest.TestCase):
    def make(self, *, enabled=True, failure=None):
        client = Mock()
        client.request.side_effect = failure
        if failure is None:
            client.request.return_value = {'code': 0}
        events, checkpoints = [], []
        lifecycle = PlaybackLifecycle(client, PlaybackContext(), 1, 2, events,
            enabled=enabled, checkpoint=lambda: checkpoints.append(json.loads(json.dumps(events))))
        return lifecycle, client, events, checkpoints

    def test_disabled_and_never_started_do_not_submit_anything(self):
        lifecycle, client, _, _ = self.make(enabled=False)
        lifecycle.start(0)
        lifecycle.tick(60)
        lifecycle.finish(60, 60)
        client.request.assert_not_called()
        lifecycle, client, _, _ = self.make()
        lifecycle.tick(60)
        lifecycle.finish(60, 60)
        client.request.assert_not_called()

    def test_delays_do_not_burst_and_history_uses_distinct_time_and_position(self):
        lifecycle, client, events, checkpoints = self.make()
        lifecycle.start(0)
        lifecycle.start(10)
        lifecycle.tick(100)
        lifecycle.tick(101)
        lifecycle.tick(200, ended=True)
        lifecycle.finish(72.8, 43.2)
        lifecycle.finish(80, 50)
        lifecycle.tick(300)
        self.assertEqual([c.args[0] for c in client.request.call_args_list], [ONLINE_PATH, ONLINE_PATH, HISTORY_PATH])
        self.assertEqual(client.request.call_args.args[1]['play_duration'], '72')
        self.assertEqual(client.request.call_args.args[1]['pro'], '43')
        self.assertEqual(lifecycle.status, 'completed')
        self.assertEqual(checkpoints[0][0]['status'], 'unconfirmed')
        self.assertTrue(all(e['status'] == 'accepted' for e in events))

    def test_unconfirmed_online_stops_presence_but_does_not_replay_history(self):
        def request(path, params, kind):
            raise TimeoutError('https://private.invalid/?secret=private-value')
        lifecycle, client, events, _ = self.make(failure=request)
        lifecycle.start(0)
        lifecycle.tick(30)
        lifecycle.tick(60)
        lifecycle.finish(60, 50)
        lifecycle.finish(60, 50)
        self.assertEqual(client.request.call_count, 2)
        self.assertEqual(lifecycle.status, 'unconfirmed')
        self.assertNotIn('private-value', json.dumps(events))
        self.assertEqual(events[0]['error_type'], 'TimeoutError')

    def test_nonzero_business_response_is_not_accepted(self):
        lifecycle, client, events, _ = self.make()
        client.request.return_value = {'code': 14013}
        lifecycle.start(0)
        lifecycle.finish(1, 1)
        self.assertEqual(lifecycle.status, 'unconfirmed')
        self.assertTrue(all(e['status'] == 'unconfirmed' for e in events))

    def test_missing_or_boolean_code_is_not_a_confirmed_response(self):
        for reply in ({}, {'code': False}):
            with self.subTest(reply=reply):
                lifecycle, client, _, _ = self.make()
                client.request.return_value = reply
                lifecycle.start(0)
                lifecycle.finish(1, 1)
                self.assertEqual(lifecycle.status, 'unconfirmed')


if __name__ == '__main__':
    unittest.main()
