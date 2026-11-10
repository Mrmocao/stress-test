"""Shared UI/batch admission rules for offline protocol exercises."""
from pathlib import Path
from playback_context import PlaybackContext
from device_profiles import resolve_profile

PRESET = Path(__file__).resolve().parent / 'examples' / 'playback-context.offline.json'


def validate_offline_options(mode, profile, preset, lifecycle, repeat, concurrency, debug=False, paused=False):
    if preset not in ('none', 'fixture') or type(lifecycle) is not bool:
        raise ValueError('离线上下文或生命周期选项无效')
    if preset == 'none' and not lifecycle:
        return
    if mode != 'demo' or repeat != 1 or concurrency != 1 or debug or paused:
        raise ValueError('上下文和生命周期页面验证仅支持 demo 单路单次，并关闭逐行调试和启动暂停')
    if preset == 'fixture':
        PlaybackContext.from_file(PRESET).validate_profile(resolve_profile('demo', profile))
