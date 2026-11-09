"""Experimental cadence, driven by actual decoder progress and monotonic time."""
import math


def heartbeat_interval(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value not in (0, 15):
        raise ValueError('周期心跳只能设为 0（关闭）或 15 秒（试验）')
    return int(value)


class HeartbeatSchedule:
    def __init__(self, interval, started):
        self.interval = heartbeat_interval(interval)
        self.next_at = started + self.interval
        self.last_played = 0

    def take(self, decoded, elapsed, now, target, ended=False):
        # No catch-up bursts, frozen-progress reports or duplicate terminal report.
        played = math.floor(min(decoded, elapsed, target))
        if (not self.interval or ended or decoded >= target - .75
                or now < self.next_at or played < self.last_played + self.interval):
            return None
        self.next_at = now + self.interval
        self.last_played = played
        return played
