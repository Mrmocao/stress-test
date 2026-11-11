"""Whole-client test profiles, selected once per worker in demo or live mode."""
from copy import deepcopy
import hashlib
import json
import random
import re
import secrets

from device_parameter_rules import (tv_apk_contract, SHAFA_TV_APK, HISTORICAL_TV_APK,
                                    QQTF_169_TV_APK, DANGBEI_189_TV_APK)

BASELINE = 'original-tv'
RANDOM = 'random-device'
LEGACY_RANDOM = 'random-offline'
# Compatibility lookup; evidence is tracked in each explicit APK contract.
SIMULATED_VERSIONS = {version: tv_apk_contract(version)['build'] for version in ('1.8.9', '1.8.8', '1.8.7')}
# Compatibility cases, not an inferred distribution of real televisions.
ANDROID_SYSTEMS = (
    ('8.1', '27', 'O_MR1'), ('9', '28', 'P'), ('10', '29', 'Q'),
    ('11', '30', 'R'), ('12', '31', 'S'), ('13', '33', 'T'),
)


def _profile(key, label, model, android, sdk, os_build, *, synthetic, version='1.8.9', brand=None, artifact_sha256=None):
    apk = tv_apk_contract(version, artifact_sha256=artifact_sha256)
    build = apk['build']
    return {
        'id': key, 'label': label, 'offline_only': synthetic, 'synthetic': synthetic,
        'provenance': 'generated compatibility-test parameters' if synthetic else 'existing code; actual device not verified',
        'app_version': version, 'android_version': android, 'os_build': os_build,
        'version_provenance': apk['evidence'],
        'application': apk,
        'live_eligible': apk['artifact_verified'] and apk['runtime_verified'],
        'common': {'platform': apk['platform'], 'mobi_app': apk['mobi_app'], 'build': build,
                   'channel': apk['channel'], 'sys_ver': sdk,
                   'brand': brand or ('offline-fixture' if synthetic else 'xiaomi'), 'model': model,
                   'teenager_mode': '0', 'child_lock': '0',
                   'statistics': json.dumps({'appId': apk['statistics_app_id'], 'platform': apk['statistics_platform'], 'version': build}, separators=(',', ':')),
                   **({'build_sn': apk['build_sn']} if apk['build_sn'] else {})},
        'user_agent': f'Mozilla/5.0 BiliTV/{version} os/android model/{model} mobi_app/{apk["mobi_app"]} build/{build} channel/{apk["channel"]} innerVer/{build} osVer/{android} network/2',
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


# Static cross-build comparisons are selectable offline until protocol parity is observed.
for key, version, digest in (
    ('apk-shafa-189', '1.8.9', SHAFA_TV_APK),
    ('apk-master-166', '1.6.6', HISTORICAL_TV_APK),
    ('apk-master-169', '1.6.9', QQTF_169_TV_APK),
    ('apk-dangbei-189', '1.8.9', DANGBEI_189_TV_APK),
):
    channel = tv_apk_contract(artifact_sha256=digest)['channel']
    _PROFILES[key] = _profile(key, f'{version} · {channel} APK 对照（仅离线）',
                              'sdk_gphone_x86_64', '11', '30', 'RSR1.240422.006',
                              synthetic=True, version=version, brand='google',
                              artifact_sha256=digest)

# Whole observed emulator configurations, not a distribution of physical TVs.
# Android 13 x86_64 failed ARM32 installation and is deliberately excluded.
_ENVIRONMENTS = (
    {'id': 'ldplayer-api28', 'brand': 'REDMI', 'model': '25102RKBEC',
     'android': '9', 'sdk': '28', 'os_build': 'P', 'compatible_abi': 'armeabi-v7a',
     'evidence': 'TV 1.8.9 runtime capture on configured LDPlayer; not physical Redmi hardware'},
    {'id': 'google-api30', 'brand': 'google', 'model': 'sdk_gphone_x86_64',
     'android': '11', 'sdk': '30', 'os_build': 'RSR1.240422.006',
     'compatible_abi': 'armeabi-v7a',
     'evidence': 'REA API30 build properties and ARM32 translation installation; not physical TV'},
)


def catalog():
    return deepcopy(list(_PROFILES.values()))


def baseline_profile():
    return deepcopy(_PROFILES[BASELINE])


def generation_rules():
    apk = tv_apk_contract()
    return {
        '规则版本': 2,
        '生成时机': '每轮启动时生成一次；本轮所有接口共用，实际参数写入 device-profile.json',
        'APK固定': {key: apk[key] for key in ('package_name', 'app_version', 'version_code', 'build', 'build_sn', 'platform', 'mobi_app', 'channel')},
        'APK规则': '按应用与具体构建成套取值；versionName 不推算 versionCode；旧版模拟组合不等于 APK 实测',
        '游戏样本库': '独立 game_device_library.py；手机/平板事件用于本地兼容性样本，不映射为 TV 型号或 TV APK 参数',
        '通用采样规则': '按设备去重；型号、厂商、系统、屏幕和应用渠道组合关联取样；异常与模拟器单独筛选',
        'Android与SDK配对': {env['android']: env['sdk'] for env in _ENVIRONMENTS},
        '环境配置': '只整套选择已观察到的 Android 9/11 模拟器配置；独立性由私有安装身份保障，型号无需唯一',
        '关联字段': 'UA 的 model/osVer/build/innerVer 与参数同步；statistics.version=build',
        '状态字段': 'teenager_mode=0、child_lock=0、network/2 沿用测试场景，不随机枚举',
        '身份规则': '新生成配置视为新测试设备，不复用其他配置的 DID；同一轮保持一致',
        '复现': 'demo 可显式指定种子；自动种子仅留在私有运行状态，公开配置不导出',
        '证据边界': '合成兼容性测试数据，不能推导为真实电视型号或设备分布',
    }


def generate_profile(seed=None):
    """Generate a coherent synthetic client; APK constants stay evidence-bound."""
    seed = secrets.token_hex(16) if seed is None else seed
    if not isinstance(seed, str) or not re.fullmatch(r'[0-9a-f]{32}', seed):
        raise ValueError('generation seed must contain 32 lowercase hex characters')
    digest = hashlib.sha256(('tv-profile-v2:' + seed).encode()).hexdigest()
    environment = deepcopy(random.Random(seed).choice(_ENVIRONMENTS))
    profile = _profile('generated-v2-' + digest[:32],
                       f"{environment['model']} · Android {environment['android']} · v1.8.9（独立测试安装）",
                       environment['model'], environment['android'], environment['sdk'],
                       environment['os_build'], synthetic=True, brand=environment['brand'])
    profile.update(generation_seed=seed, generation_version=2,
                   environment=environment,
                   identity_policy='new installation per unseeded launch; stable within run',
                   provenance='synthetic test installation using an observed emulator configuration')
    return profile


def validate_selection(mode, selection, confirmed=False):
    if mode not in ('demo', 'live'):
        raise ValueError('Unknown mode')
    if not isinstance(selection, str) or selection not in (*_PROFILES, RANDOM, LEGACY_RANDOM):
        raise ValueError('请选择有效的设备配置方案')
    if mode == 'live' and selection in _PROFILES and not _PROFILES[selection]['live_eligible']:
        raise ValueError('该 APK 构建仅用于离线对照，尚无匹配的 TV 运行时协议证据')
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
