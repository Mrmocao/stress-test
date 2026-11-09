"""Validate a playback interval while accepting legacy fixed durations."""
import math


def duration_range(seconds=45, minimum=None, maximum=None):
    if minimum is None and maximum is None:
        minimum = maximum = seconds
    elif minimum is None or maximum is None:
        raise ValueError('请同时填写最短和最长播放时长')
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or not 0 < value <= 600
           for value in (minimum, maximum)):
        raise ValueError('播放时长区间必须大于 0 且不超过 600 秒')
    if minimum > maximum:
        raise ValueError('最短播放时长不能大于最长播放时长')
    return float(minimum), float(maximum)
