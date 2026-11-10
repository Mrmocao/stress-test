import hashlib
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tv_playback as tv
from client_state import ClientState
from device_profiles import resolve_profile, RANDOM, LEGACY_RANDOM, catalog
from execution_budget import ExecutionBudget, BudgetExceeded, minimum_lease_seconds
from playback_context import PlaybackContext
from playback_lifecycle import PlaybackLifecycle, ONLINE_PATH, HISTORY_PATH
from playback_timeline import PlaybackTimeline
from public_state import public_copy
from debug_worker import value_snapshot
from debug_server import Inspector
from offline_options import validate_offline_options


class ReviewOptimizationTests(unittest.TestCase):
    def guest(self, **changes):
        data = {'profile_id': 'offline-android9',
                'binding': {'installation_id': 'a' * 32, 'client_slot': 'lane-01',
                            'buvid_sha256': hashlib.sha256(b'fixture').hexdigest()},
                'guest_contract': {'source': 'offline_fixture', 'issued_at': 100, 'expires_at': 200, 'refresh': 'manual'},
                'guest': {'guest_id': 'sensitive-guest', 'guest_access_key': 'sensitive-key'}}
        data.update(changes)
        return PlaybackContext(data)

    def bind(self, context, clock=lambda: 150, **changes):
        identity = {'installation_id': 'a' * 32, 'buvid': 'fixture'}
        identity.update(changes)
        context.bind_runtime(resolve_profile('demo', 'offline-android9'), identity, 'lane-01', 'demo', clock=clock)

    def test_guest_expiry_checked_again_before_transport(self):
        context = self.guest()
        clock = Mock(return_value=150)
        self.bind(context, clock)
        clock.return_value = 200
        with tempfile.TemporaryDirectory() as folder, patch.object(tv, 'CLIENT_STATE_MODE', 'demo'), patch.object(tv, 'DEVICE_PROFILE', resolve_profile('demo', 'offline-android9')):
            client = tv.TVClient(Path(folder), Path(folder)/'events.jsonl', context=context)
            with patch.object(tv, 'open_network') as network, self.assertRaisesRegex(ValueError, '过期'):
                client.request(ONLINE_PATH, {})
            network.assert_not_called()
            self.assertEqual(client.counter, 0)

    def test_binding_rejects_same_profile_other_installation_or_buvid_or_lane(self):
        for changes in ({'installation_id': 'b'*32}, {'buvid': 'other'}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, '绑定不匹配'):
                self.bind(self.guest(), **changes)
        with self.assertRaisesRegex(ValueError, '绑定不匹配'):
            self.guest().bind_runtime(resolve_profile('demo','offline-android9'),
                {'installation_id':'a'*32,'buvid':'fixture'}, 'lane-02','demo')

    def test_unverified_guest_refresh_and_live_use_are_rejected(self):
        with self.assertRaises(ValueError): self.guest(guest_contract={})
        context = self.guest()
        with self.assertRaisesRegex(ValueError, '仅允许离线'):
            context.bind_runtime(resolve_profile('demo','offline-android9'),
                {'installation_id':'a'*32,'buvid':'fixture'}, 'lane-01','live', clock=lambda:150)

    def test_installed_identity_migration_preserves_existing_values(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ClientState(folder,'fixture')
            store.path.write_text(json.dumps({'buvid':'old','did':'server','ftime':123}))
            first, second = store.load(), store.load()
            self.assertEqual(first,second)
            self.assertEqual((first['buvid'],first['did'],first['ftime']),('old','server',123))
            self.assertEqual(len(first['installation_id']),32)

    def test_known_representations_and_extra_grammar(self):
        for section,key,value in [('device','memory','１２'),('scene','mode_switch',1),
                                  ('history','is_interacted','1'),('report','auto_play',True),
                                  ('extra','session_more',"a'b"),('extra','session_more',r'a\b'),
                                  ('extra','internal_track_id','a&b')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                PlaybackContext({'profile_id':'offline-android9',section:{key:value}})
        context=PlaybackContext({'profile_id':'offline-android9','scene':{'teenager_mode':3,'mode_switch':True},'report':{'auto_play':6}})
        self.assertEqual(context.common()['mode_switch'],'true')

    def test_duplicate_json_is_not_last_value_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'context.json'
            path.write_text('{"profile_id":"one","profile_id":"two"}')
            with self.assertRaisesRegex(ValueError,'重复'): PlaybackContext.from_file(path)

    def test_live_nonempty_context_needs_installation_binding(self):
        context=PlaybackContext({'profile_id':'original-tv','scene':{'teenager_mode':0}})
        with self.assertRaisesRegex(ValueError,'必须绑定'):
            context.bind_runtime(resolve_profile('live','original-tv',True),{},'default','live')

    def test_abnormal_history_is_skipped_once_and_never_sent(self):
        client=Mock();client.request.return_value={'code':0};events=[]
        lifecycle=PlaybackLifecycle(client,PlaybackContext(),1,2,events,enabled=True)
        lifecycle.start(0,position=1)
        lifecycle.finish(3,3,reason='abnormal')
        lifecycle.finish(4,4)
        self.assertEqual(client.request.call_count,1)
        self.assertEqual(events[-1]['reason'],'abnormal_exit_unverified')
        self.assertEqual(lifecycle.status,'unconfirmed')

    def test_stalls_pause_background_and_end_do_not_send_periodic_online(self):
        client=Mock();client.request.return_value={'code':0}
        lifecycle=PlaybackLifecycle(client,PlaybackContext(),1,2,[],enabled=True)
        lifecycle.start(0,position=1)
        lifecycle.tick(30,position=1)
        lifecycle.tick(60,position=2,state='paused')
        lifecycle.tick(90,position=3,state='background')
        lifecycle.tick(120,position=4,ended=True)
        self.assertEqual(client.request.call_count,1)
        lifecycle.tick(150,position=5)
        lifecycle.tick(151,position=6)
        self.assertEqual(client.request.call_count,2)

    def test_history_budget_reserved_and_skips_before_network(self):
        clock=Mock(return_value=0)
        budget=ExecutionBudget(65,45,lifecycle=True,clock=clock)
        self.assertEqual(minimum_lease_seconds(45,lifecycle=True),60)
        self.assertEqual(budget.timeout(8),5)
        budget.begin_playback(45)
        clock.return_value=55
        budget.begin_finishing('heartbeat')
        self.assertEqual(budget.timeout(8),3)
        budget.begin_finishing('history')
        self.assertEqual(budget.timeout(8),5)
        clock.return_value=63
        client=Mock();events=[]
        lifecycle=PlaybackLifecycle(client,PlaybackContext(),1,2,events,enabled=True,budget=budget)
        lifecycle.start(0)
        lifecycle.finish(1,1)
        client.request.assert_not_called()
        self.assertTrue(all(e['status']=='skipped' and e['reason']=='insufficient_budget' for e in events))

    def test_offline_timeline_separates_seek_pause_buffer_and_played(self):
        timeline=PlaybackTimeline()
        for name,at,pos in [('start',0,0),('progress',10,10),('pause',10,None),('resume',15,None),
                            ('progress',20,15),('seek',20,40),('progress',25,45),('buffer',25,None),
                            ('resume',30,None),('background',30,None),('resume',35,None),('end',40,50)]:
            timeline.event(name,at,position=pos)
        result=timeline.snapshot()
        self.assertEqual((result['played_seconds'],result['position_seconds']), (25,50))
        self.assertEqual((result['paused_seconds'],result['buffering_seconds'],result['background_seconds']),(5,5,5))
        with self.assertRaises(ValueError):timeline.event('resume',41)

    def test_invalid_timeline_event_is_atomic(self):
        timeline=PlaybackTimeline();timeline.event('start',0,position=10)
        before=timeline.snapshot()
        for at,pos in [(1,5),(float('nan'),11),(-1,11)]:
            with self.assertRaises(ValueError):timeline.event('progress',at,position=pos)
            self.assertEqual(timeline.snapshot(),before)

    def test_synthetic_live_is_accepted_and_forwarded_before_spawn(self):
        class EmptyStream:
            def __iter__(self):
                return iter(())
            def close(self):
                pass

        for selection in [p['id'] for p in catalog() if p['synthetic']] + [RANDOM, LEGACY_RANDOM]:
            with self.subTest(selection=selection), patch('debug_server.subprocess.Popen') as spawn:
                process = spawn.return_value
                process.pid = 1234
                process.poll.return_value = 1
                process.wait.return_value = 0
                process.stdout = EmptyStream()
                Inspector().start('live','av1',1,device_profile=selection,device_confirmed=True)
                spawn.assert_called_once()
                command = spawn.call_args.args[0]
                index = command.index('--device-profile')
                self.assertEqual(command[index + 1], selection)

    def test_explicit_seed_reproduces_profile_only_in_demo(self):
        one=resolve_profile('demo',RANDOM,seed='a'*32)
        self.assertEqual(one,resolve_profile('demo',RANDOM,seed='a'*32))
        with self.assertRaises(ValueError):resolve_profile('live',RANDOM,seed='a'*32)

    def test_offline_page_options_cannot_cross_modes_or_lanes(self):
        validate_offline_options('demo','offline-android9','fixture',True,1,1)
        for mode,profile,repeat,lanes,debug in [('live','original-tv',1,1,False),('demo','offline-android9',2,1,False),
                ('demo','offline-android9',1,2,False),('demo','offline-android9',1,1,True),('demo','offline-tv03',1,1,False)]:
            with self.assertRaises(ValueError):validate_offline_options(mode,profile,'fixture',True,repeat,lanes,debug)

    def test_direct_entry_cannot_silently_launch_public_playback(self):
        with patch.object(sys,'argv',['tv_playback.py','av1']), patch.object(sys,'stderr',io.StringIO()), patch.object(tv,'play_window') as play:
            with self.assertRaises(SystemExit):tv.main()
            play.assert_not_called()

    def test_public_disk_snapshot_and_trace_hide_raw_identity(self):
        state={'did':'sensitive-id','buvid':'sensitive-buvid','private_dir':'private-path',
               'attempts':{'video':{'session':'sensitive-session','returned_rpdid':'sensitive-id','status':'completed'}}}
        with tempfile.TemporaryDirectory() as folder,patch.object(tv,'ROOT',Path(folder)):
            path=Path(folder)/'run'/'state.json';tv.save(path,state)
            self.assertNotIn('sensitive',path.read_text())
            self.assertEqual(tv.read_runtime_state(path),state)
            inspector=Inspector();inspector.active_dir=path.parent.parent
            legacy=Path(folder)/'playback';legacy.mkdir();(legacy/'state.json').write_text(json.dumps(state))
            self.assertNotIn('sensitive',json.dumps(inspector.snapshot(0)['state']))
        self.assertNotIn('sensitive',json.dumps(value_snapshot(state)))

    def test_boolean_business_code_cannot_accept_click(self):
        client=Mock();client.request.return_value={'code':False,'data':{}}
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(tv.PlaybackFailure):
            tv.report_click(client,{}, {'attempts':{}},Path(folder),{})

    def test_inspector_redacts_before_event_storage_without_mutating_source(self):
        event = {'kind': 'console', 'payload': {'guest_access_key': 'sensitive-key'},
                 'did': 'sensitive-id', 'message': 'https://example.invalid/video?token=sensitive'}
        inspector = Inspector()
        inspector.record(event)
        self.assertNotIn('sensitive', json.dumps(list(inspector.events)))
        self.assertNotIn('sensitive', json.dumps(list(inspector.logs)))
        self.assertEqual(event['did'], 'sensitive-id')

    def test_timeline_example_cli_and_invalid_scenario(self):
        root = Path(__file__).resolve().parents[1]
        command = [sys.executable, '-B', str(root / 'playback_timeline.py'), '--scenario']
        result = subprocess.run(command + [str(root / 'examples' / 'timeline-review.offline.json')],
                                capture_output=True, text=True, timeout=10, check=True)
        final = json.loads(result.stdout)['final']
        self.assertEqual(final['state'], 'ended')
        self.assertEqual(final['played_seconds'], 25)
        self.assertEqual(final['position_seconds'], 50)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'invalid.json'
            path.write_text('[{"event":"start","at":0},{"event":"progress","at":-1,"position":2}]')
            rejected = subprocess.run(command + [str(path)], capture_output=True, text=True, timeout=10)
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(rejected.stdout, '')

    def test_seed_cannot_be_ignored_when_worker_profile_already_selected(self):
        profile = resolve_profile('demo', 'offline-android9')
        with patch.object(tv, 'DEVICE_PROFILE', profile), patch.object(sys, 'stderr', io.StringIO()), \
                patch.object(sys, 'argv', ['tv_playback.py', 'av1', '--mode', 'demo', '--profile-seed', 'a'*32]), \
                patch.object(tv, 'play_window') as play:
            with self.assertRaises(SystemExit): tv.main()
            play.assert_not_called()


if __name__=='__main__':unittest.main()
