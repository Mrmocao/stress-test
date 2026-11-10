"""Pure offline state model. Never sends requests or controls a media process."""
import math


class PlaybackTimeline:
    def __init__(self):
        self.state = 'idle'
        self.at = None
        self.position = self.maximum_position = 0.0
        self.played = self.paused = self.buffered = self.background = 0.0
        self.transitions = []

    def event(self, name, at, *, position=None):
        if type(at) not in (int, float) or not math.isfinite(at) or at < 0 or (self.at is not None and at < self.at):
            raise ValueError('event time must be finite and monotonic')
        if position is not None and (type(position) not in (int, float) or not math.isfinite(position) or position < 0):
            raise ValueError('position must be finite and nonnegative')
        allowed = {
            'idle': {'start': 'playing'},
            'playing': {'progress': 'playing', 'pause': 'paused', 'buffer': 'buffering', 'background': 'background', 'seek': 'playing', 'end': 'ended', 'abort': 'aborted'},
            'paused': {'resume': 'playing', 'seek': 'paused', 'background': 'background', 'end': 'ended', 'abort': 'aborted'},
            'buffering': {'resume': 'playing', 'pause': 'paused', 'background': 'background', 'end': 'ended', 'abort': 'aborted'},
            'background': {'resume': 'playing', 'end': 'ended', 'abort': 'aborted'},
            'ended': {}, 'aborted': {},
        }
        if name not in allowed[self.state] or (name in ('seek', 'progress') and position is None):
            raise ValueError('invalid offline player transition')
        if name == 'progress' and position < self.position:
            raise ValueError('backward progress requires an explicit seek')
        delta = 0 if self.at is None else at - self.at
        if self.state == 'playing':
            # Count only observed forward progress, bounded by wall time.
            # Seek changes position without crediting the jump as playback.
            if name != 'seek' and position is not None:
                self.played += min(delta, max(0, position - self.position))
        elif self.state == 'paused': self.paused += delta
        elif self.state == 'buffering': self.buffered += delta
        elif self.state == 'background': self.background += delta
        if position is not None:
            self.position = float(position)
            self.maximum_position = max(self.maximum_position, self.position)
        self.state = allowed[self.state][name]
        self.at = at
        self.transitions.append({'event': name, 'at': at, 'state': self.state})
        return self.snapshot()

    def snapshot(self):
        return {'scope': 'offline_model', 'state': self.state, 'played_seconds': self.played,
                'paused_seconds': self.paused, 'buffering_seconds': self.buffered,
                'background_seconds': self.background, 'position_seconds': self.position,
                'max_position_seconds': self.maximum_position}


def main():
    import argparse
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', type=Path, required=True)
    args = parser.parse_args()
    try:
        with args.scenario.open('rb') as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise ValueError('scenario exceeds 64 KiB')
        events = json.loads(raw)
        if not isinstance(events, list) or not events:
            raise ValueError('scenario must be a nonempty event array')
        timeline = PlaybackTimeline()
        snapshots = []
        for event in events:
            if not isinstance(event, dict) or set(event) - {'event', 'at', 'position'} or not {'event', 'at'} <= set(event):
                raise ValueError('invalid scenario event')
            snapshots.append(timeline.event(event['event'], event['at'], position=event.get('position')))
        print(json.dumps({'snapshots': snapshots, 'final': timeline.snapshot()}, indent=2))
    except (ValueError, TypeError, OSError):
        parser.error('Invalid offline scenario; expected event, monotonic at, and optional finite position')


if __name__ == '__main__':
    main()
