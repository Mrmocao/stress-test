"""Opt-in online presence and final history for the sequential UGC decoder."""
import math
from execution_budget import BudgetExceeded

ONLINE_PATH = '/x/tv/playurl/online'
HISTORY_PATH = '/x/tv/history/add'


class PlaybackLifecycle:
    def __init__(self, client, context, aid, cid, observations, *, enabled=False, checkpoint=None, budget=None):
        self.client, self.context = client, context
        self.aid, self.cid = str(aid), str(cid)
        self.observations = observations
        self.enabled = enabled
        self.started = self.finished = False
        self.next_online = None
        self.last_position = None
        self.budget = budget
        self.checkpoint = checkpoint or (lambda: None)

    def submit(self, path, params, kind, phase):
        observation = {'path': path, 'phase': phase, 'status': 'unconfirmed'}
        self.observations.append(observation)
        self.checkpoint()
        try:
            if self.budget:
                self.budget.timeout(8)  # Preflight before transport or request logging.
            response = self.client.request(path, params, kind)
            code = response.get('code')
            if type(code) is int:
                observation['code'] = code
            if type(code) is int and code == 0:
                observation['status'] = 'accepted'
                return True
        except BudgetExceeded:
            observation.update(status='skipped', reason='insufficient_budget')
        except Exception as error:
            # Never copy response text/URLs/credentials into the public state.
            observation['error_type'] = type(error).__name__
        finally:
            self.checkpoint()
        return False

    def online(self, phase):
        return self.submit(ONLINE_PATH, {'aid': self.aid, 'cid': self.cid,
                                        'playurl_type': '1', 'fourk': '1'}, 'GET', phase)

    def start(self, now, *, position=None):
        if not self.enabled or self.started or self.finished:
            return
        self.started = True
        self.last_position = position
        if self.online('start'):
            self.next_online = now + 30

    def tick(self, now, *, ended=False, position=None, state='playing'):
        if (not self.enabled or not self.started or self.finished or ended
                or state != 'playing' or self.next_online is None or now < self.next_online):
            return
        if self.last_position is not None:
            if position is None or position <= self.last_position:
                return
        self.last_position = position
        # A late tick sends one observation, never a burst of missed intervals.
        self.next_online = now + 30 if self.online('periodic') else None

    def finish(self, played, position, *, reason='completed'):
        if not self.enabled or not self.started or self.finished:
            return
        self.finished = True
        self.next_online = None
        if reason != 'completed':
            self.observations.append({'path': HISTORY_PATH, 'phase': 'end', 'status': 'skipped',
                                      'reason': 'abnormal_exit_unverified'})
            self.checkpoint()
            return
        if any(type(v) not in (float, int) or not math.isfinite(v) or v < 0 for v in (played, position)):
            self.observations.append({'path': HISTORY_PATH, 'phase': 'end', 'status': 'skipped',
                                      'reason': 'invalid_media_time'})
            self.checkpoint()
            return
        self.submit(HISTORY_PATH, {
            **self.context.fields('history'),
            'aid': self.aid, 'cid': self.cid, 'epid': '0', 'sid': '0',
            'business': 'archive', 'tp': '3', 'fourk': '1',
            'play_duration': str(max(0, math.floor(played))),
            'pro': str(max(0, math.floor(position))),
        }, 'FORM', 'end')

    @property
    def status(self):
        if not self.enabled:
            return 'disabled'
        if not self.started:
            return 'not_started'
        if not self.finished or any(o['status'] != 'accepted' for o in self.observations):
            return 'unconfirmed'
        return 'completed'
