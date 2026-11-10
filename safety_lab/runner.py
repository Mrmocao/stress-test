"""Linux runner: fresh Android data disks, local-only networking, durable evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

from .mock_service import SCENARIOS

SOURCE = Path(__file__).resolve().parents[1]
PATH_FIELDS = ('emulator', 'adb', 'avd_template', 'system_image', 'apk', 'test_apk')


def write_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load_config(path):
    config = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    return validate_config(config)


def load_matrix(path):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError('Config must be a JSON object')
    if 'targets' not in data:
        return [validate_config(data)]
    targets = data.pop('targets')
    if not isinstance(targets, list) or not 1 <= len(targets) <= 100:
        raise ValueError('targets must contain 1..100 explicit OS/application combinations')
    configs, names = [], set()
    for target in targets:
        if (not isinstance(target, dict) or not isinstance(target.get('name'), str)
                or target['name'] in names):
            raise ValueError('Every target requires a unique name')
        names.add(target['name'])
        # Expected versions/hashes are replaced as a whole, never inherited field by field.
        configs.append(validate_config({**data, **target}))
    return configs


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError('Config must be a JSON object')
    name = config.setdefault('name', 'single-target')
    if not isinstance(name, str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}', name):
        raise ValueError('Target name must contain 1..64 letters, digits, hyphens or underscores')
    for key in PATH_FIELDS:
        value = config.get(key)
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ValueError(f'{key}: supply an absolute path on the Linux runner')
    for key in ('package', 'test_package'):
        if not isinstance(config.get(key), str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+', config[key]):
            raise ValueError(f'Invalid {key}')
    if config['package'] == config['test_package']:
        raise ValueError('Application and instrumentation packages must be different')
    if not isinstance(config.get('test_runner'), str) or not re.fullmatch(r'[A-Za-z.][A-Za-z0-9_.$]*', config['test_runner']):
        raise ValueError('Invalid test_runner')
    expected = config.get('expected', {})
    if not isinstance(expected, dict):
        raise ValueError('expected must be an object')
    for key in ('android_release', 'android_sdk', 'android_fingerprint', 'version_name', 'version_code'):
        if not isinstance(expected.get(key), str) or not expected[key].strip():
            raise ValueError(f'expected.{key} is required; no unverified version fallback')
    for key in ('apk_sha256', 'test_apk_sha256'):
        if not isinstance(expected.get(key), str) or not re.fullmatch('[0-9a-f]{64}', expected[key]):
            raise ValueError(f'expected.{key} must pin the actual artifact')
    scenarios = config.setdefault('scenarios', list(SCENARIOS))
    if not isinstance(scenarios, list) or not scenarios or any(s not in SCENARIOS for s in scenarios):
        raise ValueError('Invalid scenarios')
    for key, default, lower, upper in (('round_timeout', 600, 30, 3600),
                                     ('slow_seconds', 2, .1, 30),
                                     ('python_demo_seconds', 0, 0, 30)):
        value = config.setdefault(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not lower <= value <= upper:
            raise ValueError(f'Invalid {key}')
    routes = config.get('routes')
    if not isinstance(routes, dict) or not routes:
        raise ValueError('At least one local fixture route is required')
    for route, fixture in routes.items():
        if not re.fullmatch(r'/[A-Za-z0-9_./-]*', route) or route.startswith('//') or route == '/__health':
            raise ValueError('Invalid fixture path')
        if (not isinstance(fixture, dict) or type(fixture.get('status')) is not int
                or not 200 <= fixture['status'] <= 599 or 'json' not in fixture):
            raise ValueError('Every fixture needs status (200..599) and json')
    return config


def preflight(config):
    if sys.platform != 'linux':
        raise RuntimeError('Full isolation requires Linux (or a Linux VM/WSL with /dev/kvm), not Windows native')
    if os.geteuid() != 0:
        raise RuntimeError('Run with sudo: ip netns requires CAP_SYS_ADMIN/CAP_NET_ADMIN')
    if not shutil.which('ip', path=os.defpath):
        raise RuntimeError('Install iproute2')
    if not os.access('/dev/kvm', os.R_OK | os.W_OK):
        raise RuntimeError('A usable /dev/kvm is required for the Android emulator')
    for key in PATH_FIELDS:
        path = Path(config[key])
        if not path.exists():
            raise ValueError(f'Missing {key}: {path}')
    for key in ('adb', 'emulator'):
        if not os.access(config[key], os.X_OK):
            raise ValueError(f'{key} is not executable')
    if not (Path(config['avd_template']) / 'config.ini').is_file():
        raise ValueError('avd_template must contain config.ini')
    if not (Path(config['system_image']) / 'system.img').is_file():
        raise ValueError('system_image must be an extracted Android SDK system image')
    for key in ('apk', 'test_apk'):
        if sha256(Path(config[key])) != config['expected'][key + '_sha256']:
            raise ValueError(f'{key} SHA-256 does not match expected artifact')
    if config['python_demo_seconds']:
        subprocess.run([sys.executable, '-c', 'import cryptography'], check=True, timeout=10)
        if not shutil.which('ffmpeg', path=os.defpath):
            raise RuntimeError('Install ffmpeg for python_demo_seconds')


def prepare_instance(work_root, config, run_id):
    """Copy code/config only. Never import app data, snapshots, identity or caches."""
    work_root.mkdir(parents=True, exist_ok=True)
    work = work_root / run_id
    work.mkdir(mode=0o700, exist_ok=False)
    for name in ('home', 'cache', 'tmp', 'android', 'avd', 'source', 'inputs'):
        (work / name).mkdir()
    write_json(work / 'owner.json', {'run_id': run_id, 'namespace': 'tvsl-' + run_id})
    for file in SOURCE.glob('*.py'):
        shutil.copyfile(file, work / 'source' / file.name)
    shutil.copytree(SOURCE / 'safety_lab', work / 'source' / 'safety_lab',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for key in ('apk', 'test_apk'):
        dest = work / 'inputs' / (key + '.apk')
        shutil.copyfile(config[key], dest)
        if sha256(dest) != config['expected'][key + '_sha256']:
            raise ValueError(f'{key} changed during copy')
    name = 'lab_' + run_id
    avd = work / 'avd' / (name + '.avd')
    avd.mkdir()
    # Whitelist hardware values; absolute userdata/sdcard/snapshot paths from a
    # template would defeat isolation even if the AVD directory itself is new.
    allowed = {'abi.type', 'hw.cpu.arch', 'hw.cpu.ncore', 'hw.ramSize', 'hw.lcd.width',
               'hw.lcd.height', 'hw.lcd.density', 'hw.keyboard', 'hw.mainKeys',
               'hw.dPad', 'hw.device.name', 'hw.device.manufacturer', 'tag.id', 'tag.display'}
    values = {}
    for line in (Path(config['avd_template']) / 'config.ini').read_text(encoding='utf-8').splitlines():
        key, sep, value = line.partition('=')
        if sep and key.strip() in allowed:
            values[key.strip()] = value.strip()
    values.update({'AvdId': name, 'avd.ini.displayname': name,
                   'image.sysdir.1': str(Path(config['system_image']).resolve()) + '/',
                   'hw.sdCard': 'no', 'hw.gpu.enabled': 'yes', 'hw.gpu.mode': 'swiftshader_indirect',
                   'disk.dataPartition.size': '4G', 'fastboot.forceColdBoot': 'yes',
                   'fastboot.forceFastBoot': 'no', 'snapshot.present': 'no'})
    (avd / 'config.ini').write_text(''.join(f'{k}={v}\n' for k, v in values.items()), encoding='utf-8')
    (work / 'avd' / (name + '.ini')).write_text(f'avd.ini.encoding=UTF-8\npath={avd}\n', encoding='utf-8')
    write_json(work / 'inputs' / 'config.json', config)
    return work, name


def instance_env(work):
    # Do not inherit proxy settings, ADB endpoints, credential paths or Python hooks.
    return {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'HOME': str(work / 'home'),
            'TMPDIR': str(work / 'tmp'), 'XDG_CACHE_HOME': str(work / 'cache'),
            'ANDROID_USER_HOME': str(work / 'android'), 'ANDROID_EMULATOR_HOME': str(work / 'android'),
            'ANDROID_AVD_HOME': str(work / 'avd'), 'ADB_SERVER_SOCKET': 'tcp:127.0.0.1:5037',
            'PYTHONIOENCODING': 'utf-8', 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1'}


def verify_versions(expected, actual, package_dump):
    for field in ('android_release', 'android_sdk', 'android_fingerprint'):
        if actual[field] != expected[field]:
            raise RuntimeError(f'{field} mismatch: expected {expected[field]!r}, got {actual[field]!r}')
    name = re.search(r'^\s*versionName=(\S+)', package_dump, re.M)
    code = re.search(r'^\s*versionCode=(\d+)\b', package_dump, re.M)
    actual.update(version_name=name.group(1) if name else None,
                  version_code=code.group(1) if code else None)
    for field in ('version_name', 'version_code'):
        if actual[field] != expected[field]:
            raise RuntimeError(f'{field} mismatch: expected {expected[field]!r}, got {actual[field]!r}')


def instrumentation_passed(output):
    # adb/am may return 0 even for failed tests or a missing instrumentation.
    return (bool(re.search(r'^OK \([1-9][0-9]* tests?\)\s*$', output, re.M))
            and 'INSTRUMENTATION_CODE: -1' in output
            and not re.search(r'FAILURES!!!|INSTRUMENTATION_FAILED|shortMsg=|Process crashed', output))


class Round:
    def __init__(self, config, work_root, archive_root, scenario):
        self.config, self.scenario = config, scenario
        self.run_id = uuid.uuid4().hex
        self.namespace = 'tvsl-' + self.run_id
        self.work_root = work_root.resolve()
        self.work = self.work_root / self.run_id
        self.archive = archive_root.resolve() / self.run_id
        self.archive.mkdir(parents=True, mode=0o700, exist_ok=False)
        self.processes, self.files = [], []
        self.namespace_created = False
        self.namespace_attempted = False
        self.counter = 0
        self.deadline = time.monotonic() + config['round_timeout']
        self.report = {'run_id': self.run_id, 'scenario': scenario, 'namespace': self.namespace,
                       'target': config['name'],
                       'work': str(self.work), 'started_at': time.time(), 'status': 'preparing',
                       'cleanup_complete': False, 'expected': config['expected'], 'actual': {}}
        self.save()

    def save(self):
        write_json(self.archive / 'manifest.json', self.report)

    def command(self, argv, *, inside=True, timeout=30, check=True, background=False, cleanup=False):
        if inside and not self.namespace_created:
            raise RuntimeError('Refusing to launch outside the network namespace')
        if not cleanup:
            timeout = min(timeout, self.deadline - time.monotonic())
            if timeout <= 0:
                raise TimeoutError('Round deadline exceeded')
        full = (['ip', 'netns', 'exec', self.namespace] if inside else []) + list(map(str, argv))
        self.counter += 1
        log = self.archive / f'{self.counter:04d}-{Path(str(argv[0])).name}.log'
        with (self.archive / 'commands.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'argv': full, 'output': log.name, 'at': time.time()}) + '\n')
        handle = log.open('wb')
        self.files.append(handle)
        cwd = self.work / 'source' if (self.work / 'source').exists() else self.archive
        proc = subprocess.Popen(full, cwd=cwd,
                                env=instance_env(self.work), stdin=subprocess.DEVNULL,
                                stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append(proc)
        if background:
            return proc
        try:
            code = proc.wait(timeout=timeout)
        finally:
            if proc.poll() is None:
                self.kill_group(proc)
            handle.close()
        output = log.read_text(encoding='utf-8', errors='replace')
        if check and code:
            raise RuntimeError(f'Command failed ({code}); see {log.name}')
        return output

    @staticmethod
    def kill_group(proc):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=10)

    def adb(self, *args, **kwargs):
        return self.command([self.config['adb'], '-s', 'emulator-5554', *args], **kwargs)

    def run(self):
        try:
            self.work, name = prepare_instance(self.work_root, self.config, self.run_id)
            self.report['source_sha256'] = {
                str(file.relative_to(self.work / 'source')): sha256(file)
                for file in sorted((self.work / 'source').rglob('*.py'))}
            self.report['avd_config_sha256'] = sha256(self.work / 'avd' / (name + '.avd') / 'config.ini')
            self.save()
            if (Path('/var/run/netns') / self.namespace).exists():
                raise RuntimeError('Namespace name already exists; refusing to reuse it')
            self.namespace_attempted = True
            self.command(['ip', 'netns', 'add', self.namespace], inside=False)
            self.namespace_created = True
            self.command(['ip', 'link', 'set', 'lo', 'up'])
            links = json.loads(self.command(['ip', '-j', 'link', 'show']))
            routes4 = json.loads(self.command(['ip', '-j', '-4', 'route', 'show', 'table', 'all']))
            routes6 = json.loads(self.command(['ip', '-j', '-6', 'route', 'show', 'table', 'all']))
            if {link['ifname'] for link in links} != {'lo'} or any(
                    route.get('dev') != 'lo' for route in routes4 + routes6):
                raise RuntimeError('Network isolation verification failed')
            self.report['network'] = {'interfaces': links, 'ipv4_routes': routes4, 'ipv6_routes': routes6}
            mock = self.command([sys.executable, '-m', 'safety_lab.mock_service',
                          '--scenario', self.scenario, '--config', self.work / 'inputs' / 'config.json',
                          '--log', self.archive / 'mock.jsonl'], background=True)
            # The service module lives in the per-round source checkout.
            adb = self.command([self.config['adb'], '-L', 'tcp:127.0.0.1:5037', 'nodaemon', 'server'], background=True)
            # Wait for the owned ADB server before any adb client can auto-start
            # another daemon and race its port binding.
            ready = ('import socket, time\n'
                     'for port in (18080, 5037):\n'
                     ' for attempt in range(100):\n'
                     '  try:\n'
                     '   socket.create_connection(("127.0.0.1",port),timeout=.1).close(); break\n'
                     '  except OSError: time.sleep(.1)\n'
                     ' else: raise RuntimeError("Local service did not start")\n')
            self.command([sys.executable, '-c', ready], timeout=25)
            emulator = self.command([self.config['emulator'], '-avd', name, '-sysdir', self.config['system_image'],
                          '-wipe-data', '-no-snapshot', '-no-window', '-no-audio', '-no-boot-anim',
                          '-gpu', 'swiftshader_indirect', '-port', '5554', '-no-metrics'], background=True)
            boot_deadline = min(self.deadline, time.monotonic() + 240)
            while True:
                if any(proc.poll() is not None for proc in (mock, adb, emulator)):
                    raise RuntimeError('Mock service, ADB or emulator exited before boot; see command logs')
                boot = self.adb('shell', 'getprop', 'sys.boot_completed', check=False, timeout=10).strip()
                if boot == '1':
                    break
                if time.monotonic() >= boot_deadline:
                    raise TimeoutError('Android boot timed out')
                time.sleep(1)
            actual = self.report['actual']
            for key, prop in (('android_release', 'ro.build.version.release'),
                              ('android_sdk', 'ro.build.version.sdk'),
                              ('android_fingerprint', 'ro.build.fingerprint')):
                actual[key] = self.adb('shell', 'getprop', prop).strip()
            # No shared ADB host server; all connections, keys and data are round-local.
            for package in (self.config['package'], self.config['test_package']):
                installed = self.adb('shell', 'pm', 'path', package, check=False)
                if 'package:' in installed:
                    raise RuntimeError('Baseline contains test package; use a clean system image')
            for key in ('apk', 'test_apk'):
                installed = self.adb('install', '--no-streaming', str(self.work / 'inputs' / (key + '.apk')), timeout=120)
                if not re.search(r'^Success\s*$', installed, re.M):
                    raise RuntimeError(f'{key} installation did not succeed')
            dump = self.adb('shell', 'dumpsys', 'package', self.config['package'])
            verify_versions(self.config['expected'], actual, dump)
            runner = self.config['test_runner']
            if runner.startswith('.'):
                runner = self.config['test_package'] + runner
            registered = self.adb('shell', 'pm', 'list', 'instrumentation')
            binding = f"instrumentation:{self.config['test_package']}/{runner} (target={self.config['package']})"
            if binding not in registered.splitlines():
                raise RuntimeError('Instrumentation does not target the configured application')
            actual['apk_sha256'] = sha256(self.work / 'inputs' / 'apk.apk')
            actual['test_apk_sha256'] = sha256(self.work / 'inputs' / 'test_apk.apk')
            self.report['status'] = 'testing'
            self.save()
            output = self.adb('shell', 'am', 'instrument', '-w', '-r',
                              '-e', 'mock_base_url', 'http://10.0.2.2:18080',
                              '-e', 'network_scenario', self.scenario,
                              '-e', 'installation_id', self.run_id,
                              self.config['test_package'] + '/' + self.config['test_runner'],
                              timeout=min(180, self.config['round_timeout']))
            if not instrumentation_passed(output):
                raise RuntimeError('Instrumentation did not report a nonempty successful test run')
            mock_log = self.archive / 'mock.jsonl'
            requests = [json.loads(line) for line in mock_log.read_text(encoding='utf-8').splitlines()] if mock_log.exists() else []
            if not any(item.get('matched') for item in requests):
                raise RuntimeError('No application test request reached a configured local fixture')
            self.report['matched_requests'] = sum(bool(item.get('matched')) for item in requests)
            if self.config['python_demo_seconds']:
                self.command([sys.executable, self.work / 'source' / 'tv_playback.py', 'BV0000000000',
                              '--mode', 'demo', '--device-profile', 'offline-android9', '--seconds',
                              str(self.config['python_demo_seconds'])], timeout=120)
            self.report['status'] = 'passed'
        except BaseException as error:
            self.report.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                               error=f'{type(error).__name__}: {error}')
            if not isinstance(error, Exception):
                raise
        finally:
            self.cleanup()
        return self.report

    def cleanup(self):
        errors = []
        # A signal can arrive after `ip netns add` created the namespace but
        # before command() returned. Recover that ownership before teardown.
        if self.namespace_attempted and not self.namespace_created:
            try:
                listing = self.command(['ip', 'netns', 'list'], inside=False, cleanup=True)
                self.namespace_created = any(line.split()[0] == self.namespace
                                             for line in listing.splitlines() if line.split())
            except Exception as error:
                errors.append(str(error))
        # Evidence is written outside the disposable tree before anything is removed.
        if self.namespace_created:
            for args in (('logcat', '-d', '-v', 'threadtime'), ('shell', 'dumpsys', 'activity', 'activities')):
                try:
                    self.adb(*args, timeout=10, cleanup=True)
                except Exception as error:
                    self.report.setdefault('log_collection_errors', []).append(str(error))
            try:
                # ADB/QEMU can fork out of their original process group. Enumerate
                # the owned namespace as well as the processes started by this runner.
                for _ in range(3):
                    pids = self.command(['ip', 'netns', 'pids', self.namespace], inside=False, cleanup=True).split()
                    if not pids:
                        break
                    for pid in pids:
                        try:
                            os.kill(int(pid), signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    time.sleep(.2)
            except Exception as error:
                errors.append(str(error))
        for proc in self.processes:
            try:
                if proc.poll() is None:
                    self.kill_group(proc)
                else:
                    proc.wait()
            except Exception as error:
                errors.append(str(error))
        for stream in self.files:
            stream.close()
        if self.namespace_created:
            try:
                if self.command(['ip', 'netns', 'pids', self.namespace], inside=False, cleanup=True).strip():
                    raise RuntimeError('Namespace still has processes; retaining instance for recovery')
                self.command(['ip', 'netns', 'delete', self.namespace], inside=False, cleanup=True)
                self.namespace_created = False
            except Exception as error:
                errors.append(str(error))
        try:
            results = self.work / 'source' / 'results'
            if results.exists():
                shutil.copytree(results, self.archive / 'python-results')
            if self.work.exists() and not errors:
                self.remove_owned_work()
        except Exception as error:
            errors.append(str(error))
        self.report.update(cleanup_complete=not errors, cleanup_errors=errors, finished_at=time.time())
        if errors:
            self.report['status'] = 'cleanup_failed'
        elif self.report.get('log_collection_errors') and self.report['status'] == 'passed':
            self.report['status'] = 'evidence_failed'
        self.save()

    def remove_owned_work(self):
        if self.work.is_symlink() or self.work.resolve().parent != self.work_root:
            raise RuntimeError('Refusing to remove a path outside this run root')
        owner = json.loads((self.work / 'owner.json').read_text(encoding='utf-8'))
        if owner != {'run_id': self.run_id, 'namespace': self.namespace}:
            raise RuntimeError('Instance ownership marker does not match')
        shutil.rmtree(self.work)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--work-root', type=Path, default=Path('.safety-lab/instances'))
    parser.add_argument('--archive-root', type=Path, default=Path('test-results/safety-lab'))
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--check', action='store_true', help='Check dependencies and artifact hashes without starting an instance')
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 100:
        parser.error('repeat must be 1..100')
    work, archive = args.work_root.resolve(), args.archive_root.resolve()
    if work == archive or work in archive.parents or archive in work.parents:
        parser.error('Instance and archive roots must not overlap')
    try:
        configs = load_matrix(args.config)
        for config in configs:
            preflight(config)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(2, f'Preflight failed: {error}\n')
    if args.check:
        print(f'Preflight passed for {len(configs)} targets; actual OS/app versions are checked after boot. No test has run.')
        return 0
    def interrupt(signum, frame):
        raise KeyboardInterrupt(f'Signal {signum}')
    previous = signal.signal(signal.SIGTERM, interrupt)
    failures = 0
    suite_path = archive / ('suite-' + uuid.uuid4().hex + '.json')
    archive.mkdir(parents=True, exist_ok=True)
    suite = {'status': 'running', 'targets': [c['name'] for c in configs], 'rounds': []}
    write_json(suite_path, suite)
    try:
        for config in configs:
            for _ in range(args.repeat):
                for scenario in config['scenarios']:
                    instance = Round(config, work, archive, scenario)
                    print(f"{config['name']}/{scenario}: {instance.run_id} -> {instance.archive}", flush=True)
                    try:
                        report = instance.run()
                    finally:
                        suite['rounds'].append(instance.report)
                        write_json(suite_path, suite)
                    failures += report['status'] != 'passed'
                    print(f"{report['status']}; cleanup_complete={report['cleanup_complete']}", flush=True)
                    if not report['cleanup_complete']:
                        suite['status'] = 'cleanup_failed'
                        return 1  # Never begin another round after incomplete cleanup.
        suite['status'] = 'failed' if failures else 'passed'
    except KeyboardInterrupt:
        suite['status'] = 'interrupted'
        return 130
    finally:
        write_json(suite_path, suite)
        signal.signal(signal.SIGTERM, previous)
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
