"""Local test-client identity; atomic updates under a cross-process file lock."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from buvid_lab import generate_buvid, generate_mac
from playback_runtime import lock_experiment


class ClientState:
    def __init__(self, root, namespace):
        self.path = Path(root) / (hashlib.sha256(namespace.encode()).hexdigest() + '.json')

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix('.lock').open('a+', encoding='utf-8') as handle:
            deadline = time.monotonic() + 10
            while True:
                try:
                    lock_experiment(handle)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Client identity is busy') from None
                    time.sleep(.05)
            yield

    def write(self, state):
        tmp = self.path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
        try:
            tmp.write_text(json.dumps(state), encoding='utf-8')
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def load(self, buvid=None):
        with self.locked():
            if self.path.exists():
                state = json.loads(self.path.read_text(encoding='utf-8'))
                if 'installation_id' not in state:
                    state['installation_id'] = uuid.uuid4().hex
                    self.write(state)
                return state
            state = {'buvid': buvid or generate_buvid('mac', generate_mac()),
                     'did': '', 'ftime': None, 'installation_id': uuid.uuid4().hex}
            self.write(state)
            return state

    def update_did(self, did):
        with self.locked():
            state = json.loads(self.path.read_text(encoding='utf-8'))
            state['did'] = did
            self.write(state)

    def first_time(self, server_time):
        with self.locked():
            state = json.loads(self.path.read_text(encoding='utf-8'))
            if state['ftime'] is None:
                state['ftime'] = server_time
                self.write(state)
            return state['ftime']
