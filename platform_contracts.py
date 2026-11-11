"""Platform evidence is independent of the TV playback configuration."""
import json
from pathlib import Path

EVIDENCE_DIR = Path(__file__).resolve().parent / 'research' / 'platform-captures'


def platform_catalog():
    return [json.loads((EVIDENCE_DIR / f'{name}.json').read_text(encoding='utf-8'))
            for name in ('tv', 'apk', 'web')]


def validate_playback_platform(platform):
    if platform != 'tv':
        raise ValueError('当前播放工作进程仅支持 TV；APK 和网页请在平台采集证据中查看，不可复用 TV 播放流程')
