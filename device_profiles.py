"""Whole-client test profiles, selected once per worker in demo or live mode."""
from copy import deepcopy
import secrets

BASELINE = 'original-tv'
RANDOM = 'random-device'
LEGACY_RANDOM = 'random-offline'
# Synthetic version/build pairs for compatibility tests, not APK evidence.
SIMULATED_VERSIONS = {'1.8.9': '108900', '1.8.8': '108800', '1.8.7': '108700'}


def _profile(key, label, model, android, sdk, os_build, *, synthetic, version='1.8.9'):
    build = SIMULATED_VERSIONS[version]
    return {
        'id': key, 'label': label, 'offline_only': False, 'synthetic': synthetic,
        'provenance': 'generated compatibility-test parameters' if synthetic else 'existing code; actual device not verified',
        'app_version': version, 'android_version': android, 'os_build': os_build,
        'version_provenance': 'synthetic; not verified against APK' if synthetic else 'existing code',
        'common': {'platform': 'android', 'mobi_app': 'android_tv_yst', 'build': build,
                   'channel': 'master', 'sys_ver': sdk,
                   'brand': 'offline-fixture' if synthetic else 'xiaomi', 'model': model,
                   'teenager_mode': '0', 'child_lock': '0'},
        'user_agent': f'BiliDroid/{version} (Linux; U; Android {android}; zh_CN; {model} Build/{os_build}) mobi_app/android_tv_yst build/{build} channel/master',
    }


_PROFILES = {
    BASELINE: _profile(BASELINE, '原有 MiTV-ASTP0 · Android 9（需核对实际客户端）',
                       'MiTV-ASTP0', '9', '28', 'P', synthetic=False),
}

# Keep existing IDs so saved selections remain valid. These ten distinct names
# were randomly sampled once and persisted so preview and worker agree.
_FIXTURES = (
    ('offline-android9',  'QuartzTV',   '9',   '28', 'P',    '1.8.9'),
    ('offline-android11', 'JasperTV',   '11',  '30', 'R',    '1.8.9'),
    ('offline-tv03',      'MapleTV',    '10',  '29', 'Q',    '1.8.9'),
    ('offline-tv04',      'RowanTV',    '12',  '31', 'S',    '1.8.9'),
    ('offline-tv05',      'AspenTV',    '8.1', '27', 'O_MR1','1.8.8'),
    ('offline-tv06',      'BirchTV',    '9',   '28', 'P',    '1.8.8'),
    ('offline-tv07',      'MagnoliaTV', '11',  '30', 'R',    '1.8.8'),
    ('offline-tv08',      'CypressTV',  '8.1', '27', 'O_MR1','1.8.7'),
    ('offline-tv09',      'HazelTV',    '10',  '29', 'Q',    '1.8.7'),
    ('offline-tv10',      'SpruceTV',   '13',  '33', 'T',    '1.8.7'),
)
for key, model, android, sdk, os_build, version in _FIXTURES:
    _PROFILES[key] = _profile(key, f'{model} · Android {android} · v{version}（生成参数）',
                              model, android, sdk, os_build, synthetic=True, version=version)


def catalog():
    return deepcopy(list(_PROFILES.values()))


def baseline_profile():
    return deepcopy(_PROFILES[BASELINE])


def validate_selection(mode, selection, confirmed=False):
    if mode not in ('demo', 'live'):
        raise ValueError('Unknown mode')
    if not isinstance(selection, str) or selection not in (*_PROFILES, RANDOM, LEGACY_RANDOM):
        raise ValueError('请选择有效的设备配置方案')
    if mode == 'live' and selection == BASELINE:
        if confirmed is not True:
            raise ValueError('请核对配置，并确认它与实际客户端一致；不一致时请使用离线演示')


def resolve_profile(mode, selection, confirmed=False):
    validate_selection(mode, selection, confirmed)
    key = secrets.choice([key for key, value in _PROFILES.items() if value['synthetic']]) if selection in (RANDOM, LEGACY_RANDOM) else selection
    return {**deepcopy(_PROFILES[key]), 'selection': selection, 'mode': mode,
            'actual_client_confirmed': mode == 'live' and key == BASELINE and confirmed is True}
