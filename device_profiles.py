"""Whole-client test profiles, selected once per worker in demo or live mode."""
from copy import deepcopy
import hashlib
import json
import random
import re
import secrets

BASELINE = 'original-tv'
RANDOM = 'random-device'
LEGACY_RANDOM = 'random-offline'
# Synthetic version/build pairs for compatibility tests, not APK evidence.
SIMULATED_VERSIONS = {'1.8.9': '108900', '1.8.8': '108800', '1.8.7': '108700'}
# Compatibility cases, not an inferred distribution of real televisions.
ANDROID_SYSTEMS = (
    ('8.1', '27', 'O_MR1'), ('9', '28', 'P'), ('10', '29', 'Q'),
    ('11', '30', 'R'), ('12', '31', 'S'), ('13', '33', 'T'),
)


def _profile(key, label, model, android, sdk, os_build, *, synthetic, version='1.8.9', brand=None):
    build = SIMULATED_VERSIONS[version]
    return {
        'id': key, 'label': label, 'offline_only': synthetic, 'synthetic': synthetic,
        'provenance': 'generated compatibility-test parameters' if synthetic else 'existing code; actual device not verified',
        'app_version': version, 'android_version': android, 'os_build': os_build,
        'version_provenance': 'synthetic; not verified against APK' if synthetic else 'existing code',
        'common': {'platform': 'android', 'mobi_app': 'android_tv_yst', 'build': build,
                   'channel': 'master', 'sys_ver': sdk,
                   'brand': brand or ('offline-fixture' if synthetic else 'xiaomi'), 'model': model,
                   'teenager_mode': '0', 'child_lock': '0',
                   'statistics': json.dumps({'appId': '18', 'platform': '3', 'version': build}, separators=(',', ':')),
                   **({'build_sn': '29938492'} if version == '1.8.9' else {})},
        'user_agent': f'Mozilla/5.0 BiliTV/{version} os/android model/{model} mobi_app/android_tv_yst build/{build} channel/master innerVer/{build} osVer/{android} network/2',
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


def generation_rules():
    return {
        '规则版本': 1,
        '生成时机': '每轮启动时生成一次；本轮所有接口共用，实际参数写入 device-profile.json',
        'APK固定': {'app_version': '1.8.9', 'build': '108900', 'build_sn': '29938492',
                    'platform': 'android', 'mobi_app': 'android_tv_yst', 'channel': 'master'},
        'Android与SDK配对': {android: sdk for android, sdk, _ in ANDROID_SYSTEMS},
        '合成型号': 'TVLAB- 加随机种子派生的 12 位十六进制字符；brand=tv-lab',
        '关联字段': 'UA 的 model/osVer/build/innerVer 与参数同步；statistics.version=build',
        '状态字段': 'teenager_mode=0、child_lock=0、network/2 沿用测试场景，不随机枚举',
        '身份规则': '新生成配置视为新测试设备，不复用其他配置的 DID；同一轮保持一致',
        '复现': '保存 generation_seed；generate_profile(seed) 可复现整套配置',
        '证据边界': '合成兼容性测试数据，不能推导为真实电视型号或设备分布',
    }


def generate_profile(seed=None):
    """Generate a coherent synthetic client; APK constants stay evidence-bound."""
    seed = secrets.token_hex(16) if seed is None else seed
    if not isinstance(seed, str) or not re.fullmatch(r'[0-9a-f]{32}', seed):
        raise ValueError('generation seed must contain 32 lowercase hex characters')
    digest = hashlib.sha256(('tv-profile-v1:' + seed).encode()).hexdigest()
    android, sdk, os_build = random.Random(seed).choice(ANDROID_SYSTEMS)
    model = 'TVLAB-' + digest[:12].upper()
    profile = _profile('generated-v1-' + seed,
                       f'{model} · Android {android} · v1.8.9（动态合成）',
                       model, android, sdk, os_build, synthetic=True, brand='tv-lab')
    profile.update(generation_seed=seed, generation_version=1,
                   version_provenance='official captured APK 1.8.9 / 108900 / 29938492',
                   provenance='dynamic synthetic compatibility fixture; not real hardware')
    return profile


def validate_selection(mode, selection, confirmed=False):
    if mode not in ('demo', 'live'):
        raise ValueError('Unknown mode')
    if not isinstance(selection, str) or selection not in (*_PROFILES, RANDOM, LEGACY_RANDOM):
        raise ValueError('请选择有效的设备配置方案')
    if mode == 'live' and selection == BASELINE:
        if confirmed is not True:
            raise ValueError('请核对配置，并确认它与实际客户端一致；未核对时请选择其他设备方案')


def resolve_profile(mode, selection, confirmed=False, *, seed=None):
    validate_selection(mode, selection, confirmed)
    if seed is not None and (mode != 'demo' or selection not in (RANDOM, LEGACY_RANDOM)):
        raise ValueError('固定种子仅用于 demo 动态合成配置')
    profile = generate_profile(seed) if selection in (RANDOM, LEGACY_RANDOM) else deepcopy(_PROFILES[selection])
    return {**profile, 'selection': selection, 'mode': mode,
            'actual_client_confirmed': mode == 'live' and selection == BASELINE and confirmed is True}
