"""Parameter contracts shared by local compatibility fixtures and TV profiles."""
import re


# Release-to-API reference, NOT an SDK_INT measurement from analytics.
ANDROID_RELEASE_SDK = {
    '7.0': 24, '7.1': 25, '8.0': 26, '8.1': 27, '9': 28,
    '10': 29, '11': 30, '12': 31, '12L': 32, '12.1': 32,
    '13': 33, '14': 34, '15': 35, '16': 36,
}


def android_sdk_reference(os_family, release):
    """Unknown releases and non-Android families stay unknown, never guessed."""
    if os_family != 'Android':
        return None
    if release in ANDROID_RELEASE_SDK:
        return ANDROID_RELEASE_SDK[release]
    if not re.fullmatch(r'\d+(?:\.\d+){0,2}', release):
        return None
    parts = release.split('.')
    key = '.'.join(parts[:2]) if int(parts[0]) < 9 else parts[0]
    if parts[0] == '12' and len(parts) > 1 and parts[1] == '1':
        key = '12.1'
    return ANDROID_RELEASE_SDK.get(key)


# APK identity is the file digest, not versionName (which is non-unique).
OFFICIAL_TV_APK = '73229ae195f45cdbfd4577b81962a4b4c9345a36abcd846e00dbc0647c127ef2'
SHAFA_TV_APK = '541058b22798c3d5863b4f257840fb348b38233645dd88899829d4fd36234fb1'
HISTORICAL_TV_APK = '24a28ef769bd4b5c2c9819fe1971803f272f09bdcd388ab83cea25eb672615bb'
QQTF_169_TV_APK = 'e6d7cb3999bf95c44c3551f6e7f160d311205df5a9c21aed21e0aae3a1539493'
DANGBEI_189_TV_APK = '8ed6446a3258f899394a14ff22be505708a7784fb4c478a630b1a05c9bfa6ba3'
_SIGNER = '93ba270f5521139ecafe4bb638ac5b1198bc548f62d9fd8f8580a079faf5910e'
_APKS = {
    OFFICIAL_TV_APK: ('1.8.9', 108900, 29938492, 'master', True),
    SHAFA_TV_APK: ('1.8.9', 108900, 29938502, 'shafa', False),
    HISTORICAL_TV_APK: ('1.6.6', 106600, 13482127, 'master', False),
    QQTF_169_TV_APK: ('1.6.9', 106900, 14500739, 'master', False),
    DANGBEI_189_TV_APK: ('1.8.9', 108900, 29674886, 'dangbei', False),
}
# Retained only for existing offline regression cases, never APK evidence.
_LEGACY = {'1.8.8': '108800', '1.8.7': '108700'}


def tv_apk_contract(version=None, *, artifact_sha256=None):
    """Resolve one whole artifact. Default/version alias selects master explicitly."""
    if artifact_sha256 is None and version in _LEGACY:
        return {
            'package_name': 'com.xiaodianshi.tv.yst', 'app_version': version,
            'version_code': None, 'build': _LEGACY[version], 'build_sn': None,
            'artifact_sha256': None, 'signer_sha256': None, 'artifact_verified': False,
            'runtime_verified': False, 'channel': 'master',
            'platform': 'android', 'mobi_app': 'android_tv_yst',
            'statistics_app_id': '18', 'statistics_platform': '3',
            'evidence': 'legacy-synthetic-fixture',
        }
    if artifact_sha256 is None:
        aliases = {None: OFFICIAL_TV_APK, '1.8.9': OFFICIAL_TV_APK,
                   '1.6.6': HISTORICAL_TV_APK, '1.6.9': QQTF_169_TV_APK}
        artifact_sha256 = aliases.get(version)
    if artifact_sha256 not in _APKS:
        raise ValueError('Unknown TV APK artifact; an explicit verified contract is required')
    name, code, serial, channel, runtime = _APKS[artifact_sha256]
    if version is not None and version != name:
        raise ValueError('TV version does not match the selected APK artifact')
    return {
        'package_name': 'com.xiaodianshi.tv.yst', 'app_version': name,
        'version_code': code, 'build': str(code), 'build_sn': str(serial),
        'artifact_sha256': artifact_sha256, 'signer_sha256': _SIGNER,
        'build_sn_hex': hex(serial),
        'artifact_verified': True, 'runtime_verified': runtime,
        'native_abis': ['armeabi-v7a'],
        'platform': 'android', 'mobi_app': 'android_tv_yst', 'channel': channel,
        'statistics_app_id': '18', 'statistics_platform': '3',
        'evidence': 'apk-manifest-signature-and-channel-resource',
        'runtime_evidence': 'TV 1.8.9 master historical captures' if runtime else None,
    }


def tv_apk_catalog():
    return [tv_apk_contract(artifact_sha256=key) for key in _APKS]
