from http.client import RemoteDisconnected
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from safety_lab.mock_service import FixtureServer
from safety_lab.runner import (Round, instance_env, instrumentation_passed, load_matrix,
                               main, prepare_instance, sha256, validate_config, verify_versions)


def config_fixture(root):
    template = root / 'template'
    template.mkdir(exist_ok=True)
    (template / 'config.ini').write_text('hw.lcd.width=1920\nhw.cpu.arch=x86_64\n'
                                        'disk.dataPartition.path=/shared/userdata.img\n'
                                        'hw.sdCard.path=/shared/sdcard.img\n')
    (template / 'userdata-qemu.img').write_bytes(b'stale identity')
    (template / 'snapshots').mkdir(exist_ok=True)
    apk, test = root / 'app.apk', root / 'test.apk'
    apk.write_bytes(b'application fixture')
    test.write_bytes(b'instrumentation fixture')
    return validate_config({'emulator': str(root / 'emulator'), 'adb': str(root / 'adb'),
        'avd_template': str(template), 'system_image': str(root / 'image'),
        'apk': str(apk), 'test_apk': str(test), 'package': 'org.example.app',
        'test_package': 'org.example.app.test', 'test_runner': 'org.example.Runner',
        'expected': {'android_release': '9', 'android_sdk': '28', 'android_fingerprint': 'verified/fingerprint',
                     'version_name': '1.8.9', 'version_code': '108900',
                     'apk_sha256': sha256(apk), 'test_apk_sha256': sha256(test)},
        'routes': {'/fixture': {'status': 200, 'json': {'ok': True}}}})


class FakeProcess:
    def poll(self):
        return None


class SimulatedRound(Round):
    """Exercise orchestration outcomes without claiming to execute Android."""
    instrument_output = 'OK (1 test)\nINSTRUMENTATION_CODE: -1\n'
    fail_cleanup = False
    emit_request = True

    def command(self, argv, **kwargs):
        if kwargs.get('background'):
            return FakeProcess()
        if argv[:4] == ['ip', '-j', 'link', 'show']:
            return '[{"ifname":"lo"}]'
        if argv[:2] == ['ip', '-j']:
            return '[]'
        if argv[:3] == ['ip', 'netns', 'delete'] and self.fail_cleanup:
            raise RuntimeError('simulated deletion failure')
        return ''

    def adb(self, *args, **kwargs):
        if args[:2] == ('shell', 'getprop'):
            return {'sys.boot_completed': '1', 'ro.build.version.release': '9',
                    'ro.build.version.sdk': '28', 'ro.build.fingerprint': 'verified/fingerprint'}[args[2]]
        if args[0] == 'install':
            return 'Success\n'
        if args[:3] == ('shell', 'dumpsys', 'package'):
            return '  versionName=1.8.9\n  versionCode=108900 minSdk=21\n'
        if args[:3] == ('shell', 'pm', 'list'):
            return 'instrumentation:org.example.app.test/org.example.Runner (target=org.example.app)\n'
        if args[:3] == ('shell', 'am', 'instrument'):
            if self.emit_request:
                (self.archive / 'mock.jsonl').write_text('{"matched":true}\n')
            if isinstance(self.instrument_output, BaseException):
                raise self.instrument_output
            return self.instrument_output
        return ''


class IsolationTests(unittest.TestCase):
    def test_fresh_source_installation_and_cache_per_round(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = config_fixture(root)
            ids = []
            for number in range(2):
                work, name = prepare_instance(root / 'instances', config, str(number))
                avd = work / 'avd' / (name + '.avd')
                self.assertEqual({p.name for p in avd.iterdir()}, {'config.ini'})
                self.assertNotIn('/shared/', (avd / 'config.ini').read_text())
                self.assertEqual(list((work / 'cache').iterdir()), [])
                self.assertFalse((work / 'source' / '.private').exists())
                code = ('import json; from client_state import ClientState; '
                        's=ClientState(".private/client-state", "same-slot"); '
                        'print(json.dumps(s.load())); s.update_did("previous-round"); s.first_time(123)')
                proc = subprocess.run([sys.executable, '-B', '-c', code], cwd=work / 'source',
                                      capture_output=True, text=True, timeout=10, check=True)
                state = json.loads(proc.stdout)
                ids.append((state['installation_id'], state['buvid']))
                self.assertEqual(state['did'], '')
                self.assertIsNone(state['ftime'])
            self.assertNotEqual(ids[0][0], ids[1][0])
            self.assertNotEqual(ids[0][1], ids[1][1])

    def test_environment_does_not_inherit_proxy_or_shared_paths(self):
        with patch.dict(os.environ, {'HTTP_PROXY': 'http://external.invalid', 'PYTHONPATH': '/shared',
                                    'ADB_SERVER_SOCKET': 'tcp:host:5037'}):
            env = instance_env(Path('/test/round'))
        self.assertNotIn('HTTP_PROXY', env)
        self.assertNotIn('PYTHONPATH', env)
        self.assertEqual(env['ADB_SERVER_SOCKET'], 'tcp:127.0.0.1:5037')
        self.assertEqual(env['XDG_CACHE_HOME'], str(Path('/test/round/cache')))

    def test_matrix_does_not_merge_partial_version_expectations(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = config_fixture(root)
            config['targets'] = [{'name': 'android9'}, {'name': 'android11',
                                 'expected': {**config['expected'], 'android_release': '11', 'android_sdk': '30'}}]
            path = root / 'matrix.json'
            path.write_text(json.dumps(config))
            configs = load_matrix(path)
            self.assertEqual([c['expected']['android_sdk'] for c in configs], ['28', '30'])
            config['targets'][1]['expected'] = {'android_release': '11'}
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                load_matrix(path)

    def test_hash_change_between_preflight_and_copy_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = config_fixture(root)
            Path(config['apk']).write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'changed during copy'):
                prepare_instance(root / 'instances', config, 'round')

    def test_version_mismatch_and_nonempty_instrumentation_required(self):
        with tempfile.TemporaryDirectory() as folder:
            expected = config_fixture(Path(folder))['expected']
        actual = {key: expected[key] for key in ('android_release', 'android_sdk', 'android_fingerprint')}
        with self.assertRaisesRegex(RuntimeError, 'version_code mismatch'):
            verify_versions(expected, actual, 'versionName=1.8.9\nversionCode=108800 minSdk=21')
        actual['android_sdk'] = '30'
        with self.assertRaisesRegex(RuntimeError, 'android_sdk mismatch'):
            verify_versions(expected, actual, '')
        for output in ('', 'OK (0 tests)\nINSTRUMENTATION_CODE: -1',
                       'OK (1 test)\nINSTRUMENTATION_CODE: 0',
                       'OK (1 test)\nFAILURES!!!\nINSTRUMENTATION_CODE: -1'):
            self.assertFalse(instrumentation_passed(output))
        self.assertTrue(instrumentation_passed('OK (2 tests)\nINSTRUMENTATION_CODE: -1\n'))

    def test_success_failure_timeout_and_interrupt_archive_then_remove_instance(self):
        for outcome in ('OK (1 test)\nINSTRUMENTATION_CODE: -1\n', 'FAILURES!!!',
                        TimeoutError('instrumentation timeout'), KeyboardInterrupt()):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                run = SimulatedRound(config_fixture(root), root / 'instances', root / 'archive', 'normal')
                run.instrument_output = outcome
                if isinstance(outcome, KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        run.run()
                else:
                    run.run()
                report = json.loads((run.archive / 'manifest.json').read_text())
                self.assertTrue(report['cleanup_complete'])
                self.assertFalse(run.work.exists())
                self.assertTrue((run.archive / 'mock.jsonl').exists())
                expected = ('passed' if isinstance(outcome, str) and outcome.startswith('OK') else
                            'interrupted' if isinstance(outcome, KeyboardInterrupt) else 'failed')
                self.assertEqual(report['status'], expected)

    def test_cleanup_failure_is_visible_and_keeps_instance(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = SimulatedRound(config_fixture(root), root / 'instances', root / 'archive', 'normal')
            run.fail_cleanup = True
            report = run.run()
            self.assertEqual(report['status'], 'cleanup_failed')
            self.assertFalse(report['cleanup_complete'])
            self.assertTrue(run.work.exists())

    def test_successful_instrumentation_without_local_requests_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = SimulatedRound(config_fixture(root), root / 'instances', root / 'archive', 'normal')
            run.emit_request = False
            report = run.run()
            self.assertEqual(report['status'], 'failed')
            self.assertIn('No application test request', report['error'])
            self.assertTrue(report['cleanup_complete'])

    def test_interruption_during_namespace_creation_still_deletes_namespace(self):
        class InterruptedCreation(SimulatedRound):
            deleted = False

            def command(self, argv, **kwargs):
                if argv[:3] == ['ip', 'netns', 'add']:
                    raise KeyboardInterrupt()
                if argv == ['ip', 'netns', 'list']:
                    return self.namespace + '\n'
                if argv[:3] == ['ip', 'netns', 'delete']:
                    self.deleted = True
                return super().command(argv, **kwargs)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = InterruptedCreation(config_fixture(root), root / 'instances', root / 'archive', 'normal')
            with self.assertRaises(KeyboardInterrupt):
                run.run()
            self.assertTrue(run.deleted)
            self.assertTrue(run.report['cleanup_complete'])
            self.assertFalse(run.work.exists())

    def test_missing_evidence_cannot_be_reported_as_passed(self):
        class MissingEvidence(SimulatedRound):
            def adb(self, *args, **kwargs):
                if args[0] == 'logcat':
                    raise RuntimeError('logcat unavailable')
                return super().adb(*args, **kwargs)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = MissingEvidence(config_fixture(root), root / 'instances', root / 'archive', 'normal')
            report = run.run()
            self.assertEqual(report['status'], 'evidence_failed')
            self.assertTrue(report['cleanup_complete'])

    def test_matrix_summary_and_stop_after_cleanup_failure(self):
        for failed_cleanup in (False, True):
            with self.subTest(cleanup_failure=failed_cleanup), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                config = config_fixture(root)
                config['targets'] = [{'name': 'version-a'}, {'name': 'version-b'}]
                config['scenarios'] = ['normal', 'slow']
                path = root / 'matrix.json'
                path.write_text(json.dumps(config))
                with patch('safety_lab.runner.preflight'), patch('safety_lab.runner.Round', SimulatedRound), \
                        patch.object(SimulatedRound, 'fail_cleanup', failed_cleanup):
                    code = main(['--config', str(path), '--work-root', str(root / 'work'),
                                 '--archive-root', str(root / 'archive')])
                suite = json.loads(next((root / 'archive').glob('suite-*.json')).read_text())
                self.assertEqual(code, 1 if failed_cleanup else 0)
                self.assertEqual(suite['status'], 'cleanup_failed' if failed_cleanup else 'passed')
                self.assertEqual(len(suite['rounds']), 1 if failed_cleanup else 4)
                self.assertEqual(len({r['run_id'] for r in suite['rounds']}), len(suite['rounds']))

    def test_no_network_fallback_and_no_unowned_deletion(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = Round(config_fixture(root), root / 'instances', root / 'archive', 'normal')
            with self.assertRaisesRegex(RuntimeError, 'outside the network namespace'):
                run.command(['anything'])
            run.work, _ = prepare_instance(run.work_root, run.config, run.run_id)
            (run.work / 'owner.json').write_text('{}')
            with self.assertRaisesRegex(RuntimeError, 'ownership'):
                run.remove_owned_work()
            self.assertTrue(run.work.exists())


class MockNetworkTests(unittest.TestCase):
    def test_real_http_faults_and_redacted_request_log(self):
        opener = build_opener(ProxyHandler({}))
        for scenario in ('normal', 'slow', 'unavailable', 'disconnect', 'timeout'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as folder:
                log = Path(folder) / 'requests.jsonl'
                server = FixtureServer(('127.0.0.1', 0), scenario, log,
                                       {'/fixture': {'status': 200, 'json': {'ok': True}}}, delay=.1)
                thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02})
                thread.start()
                try:
                    url = f'http://127.0.0.1:{server.server_port}/fixture?token=private'
                    start = time.monotonic()
                    if scenario in ('normal', 'slow'):
                        with opener.open(url, timeout=2) as response:
                            self.assertEqual(json.load(response), {'ok': True})
                        if scenario == 'slow':
                            self.assertGreaterEqual(time.monotonic() - start, .09)
                    elif scenario == 'unavailable':
                        with self.assertRaises(HTTPError) as raised:
                            opener.open(url, timeout=1)
                        self.assertEqual(raised.exception.code, 503)
                        raised.exception.close()
                    else:
                        with self.assertRaises((TimeoutError, RemoteDisconnected, URLError, ConnectionError)):
                            opener.open(url, timeout=.1)
                    text = log.read_text()
                    self.assertNotIn('private', text)
                    self.assertTrue(json.loads(text)['matched'])
                    with opener.open(f'http://127.0.0.1:{server.server_port}/__health', timeout=1) as response:
                        self.assertEqual(response.read(), b'ok')
                    self.assertEqual(len(log.read_text().splitlines()), 1)
                finally:
                    server.stopping.set()
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)


@unittest.skipUnless(sys.platform == 'linux' and os.environ.get('TV_SAFETY_NETNS_TEST') == '1',
                     'Opt-in Linux root/iproute2 integration test; does not execute Android')
class RealNamespaceTests(unittest.TestCase):
    def test_host_loopback_and_public_network_unreachable_and_cleanup(self):
        with tempfile.TemporaryDirectory() as folder, socket.socket() as host_listener:
            root = Path(folder)
            host_listener.bind(('127.0.0.1', 0))
            host_listener.listen()
            run = Round(config_fixture(root), root / 'instances', root / 'archive', 'normal')
            run.work, _ = prepare_instance(run.work_root, run.config, run.run_id)
            try:
                run.command(['ip', 'netns', 'add', run.namespace], inside=False)
                run.namespace_created = True
                run.command(['ip', 'link', 'set', 'lo', 'up'])
                code = ('import socket\n'
                        f'for address in [("127.0.0.1", {host_listener.getsockname()[1]}), ("192.0.2.1", 80)]:\n'
                        ' s=socket.socket(); s.settimeout(.2)\n'
                        ' try: s.connect(address)\n'
                        ' except OSError: pass\n'
                        ' else: raise AssertionError("network escaped namespace")\n'
                        ' finally: s.close()\n')
                run.command([sys.executable, '-c', code])
                # A real local mock process is reachable only within this namespace.
                run.command([sys.executable, '-m', 'safety_lab.mock_service', '--scenario', 'normal',
                             '--config', run.work / 'inputs' / 'config.json',
                             '--log', run.archive / 'mock.jsonl'], background=True)
                probe = ('import time, urllib.request\n'
                         'for i in range(30):\n'
                         ' try:\n'
                         '  r=urllib.request.urlopen("http://127.0.0.1:18080/fixture",timeout=1); assert r.status==200; r.close(); break\n'
                         ' except OSError: time.sleep(.1)\n'
                         'else: raise AssertionError("local service unavailable")\n')
                run.command([sys.executable, '-c', probe])
            finally:
                run.cleanup()
            self.assertTrue(run.report['cleanup_complete'], run.report)
            self.assertFalse(run.work.exists())
            self.assertNotIn(run.namespace, subprocess.check_output(['ip', 'netns', 'list'], text=True))


if __name__ == '__main__':
    unittest.main()
