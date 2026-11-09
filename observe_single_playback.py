"""Run one playback, with a fresh baseline and bounded read-only follow-up."""
from datetime import datetime
import json
from pathlib import Path
import re
import time
import urllib.request

from view_monitor.monitor import fetch_public_stats, iso, save_json, TZ

ROOT = Path(__file__).resolve().parent
VIDEO = 'BV1S64q69EPN'


class LocalAPI:
    def __init__(self, port, name):
        self.base = f'http://127.0.0.1:{port}'
        self.name = name
        with urllib.request.urlopen(self.base, timeout=5) as response:
            page = response.read().decode('utf-8')
        self.token = re.search(f'name="{name}-token" content="([^"]+)"', page).group(1)

    def call(self, path, data=None):
        headers = {f'X-{self.name.title()}-Token': self.token}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.base + path,
            data=json.dumps(data).encode() if data is not None else None, headers=headers)
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)


def main():
    monitor = LocalAPI(8766, 'monitor')
    inspector = LocalAPI(8765, 'inspector')
    if monitor.call('/api/status')['running'] or inspector.call('/api/snapshot')['running']:
        raise RuntimeError('An existing observation or playback is running; leave it untouched')
    directory = ROOT / 'test-results' / (datetime.now(TZ).strftime('%Y%m%d-%H%M%S') + '-' + VIDEO)
    directory.mkdir(parents=True, exist_ok=False)
    report = {'video': VIDEO, 'started_at': iso(), 'play_seconds': 60,
              'followup_seconds': 180, 'playback_launch_count': 0, 'samples': [],
              'causation_established': False, 'status': 'preparing'}
    monitor_id = None
    def persist():
        save_json(directory / 'result.json', report)
    def log(stage, **values):
        print(json.dumps({'stage': stage, 'at': iso(), **values}, ensure_ascii=True), flush=True)
    def public_sample(label):
        try:
            sample = fetch_public_stats('bvid', VIDEO)
            raw = sample.pop('raw_response')
            save_json(directory / f'response-{len(report["samples"]):02}.json', raw)
            sample['label'] = label
            sample['delta_baseline'] = sample['view'] - report['baseline']['view']
        except Exception as error:
            sample = {'label': label, 'observed_at': iso(), 'ok': False,
                      'error': f'{type(error).__name__}: {error}'}
        report['samples'].append(sample)
        persist()
        log(label, view=sample.get('view'), delta=sample.get('delta_baseline'), error=sample.get('error'))

    try:
        monitor_id = monitor.call('/api/start', {'video': VIDEO, 'interval': 30, 'minutes': 10})['id']
        report['monitor_id'] = monitor_id
        persist()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            observation = monitor.call('/api/status')
            if observation['session']['id'] != monitor_id:
                raise RuntimeError('Observation session changed before launch')
            valid = [r for r in observation['samples'] if r['ok']]
            if valid:
                report['baseline'] = valid[0]
                break
            time.sleep(1)
        else:
            raise RuntimeError('No successful fresh baseline; playback was not launched')
        log('baseline', view=report['baseline']['view'], observed_at=report['baseline']['observed_at'])
        # The launch is never retried, including if the local HTTP response is lost.
        report.update(status='launching', launch_requested_at=iso(), playback_launch_count=1)
        persist()
        launch = inspector.call('/api/start', {'mode': 'live', 'video': VIDEO, 'seconds': 60, 'paused': False})
        report['playback_run_dir'] = launch['run_dir']
        report['status'] = 'playing'
        persist()
        log('playback_started', run_dir=launch['run_dir'])
        timeout = time.monotonic() + 300
        next_log = time.monotonic() + 25
        while True:
            snapshot = inspector.call('/api/snapshot?after=999999999')
            if snapshot['run_dir'] != launch['run_dir']:
                raise RuntimeError('Playback session changed; original run is preserved on disk')
            if not snapshot['running']:
                report['playback_status'] = snapshot['status']
                report['playback_state'] = snapshot['state']
                report['playback_finished_observed_at'] = iso()
                break
            if time.monotonic() > timeout:
                raise TimeoutError('Playback still active after 300 seconds; no automatic relaunch')
            if time.monotonic() >= next_log:
                log('playback_progress', status=snapshot['status'], metrics=snapshot['metrics'])
                next_log = time.monotonic() + 25
            time.sleep(2)
        report['status'] = 'observing_after_playback'
        persist()
        log('playback_finished', status=report['playback_status'])
        public_sample('immediately_after')
        start = time.monotonic()
        for seconds in range(30, 181, 30):
            while time.monotonic() < start + seconds:
                time.sleep(min(1, start + seconds - time.monotonic()))
            public_sample(f'after_{seconds}s')
        report['status'] = 'completed'
    except Exception as error:
        report.update(status='error', error=f'{type(error).__name__}: {error}')
        log('error', error=report['error'])
    finally:
        if monitor_id:
            try:
                snapshot = monitor.call('/api/status')
                if snapshot['active_id'] == monitor_id:
                    monitor.call('/api/stop', {})
                report['monitor_final'] = monitor.call('/api/status?session=' + monitor_id)
            except Exception as error:
                report['monitor_cleanup_error'] = str(error)
        report['finished_at'] = iso()
        persist()
        log('saved', report=str(directory / 'result.json'), status=report['status'])


if __name__ == '__main__':
    main()
