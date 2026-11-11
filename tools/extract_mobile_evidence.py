"""Derive the mobile catalog from REA manifest and recorded HAR Evidence.

Inputs remain private; public output contains only explicit application constants,
endpoint templates and parameter names. Never copy device package user_id.
"""
import argparse
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.extract_platform_evidence import add, endpoint, finalize

ANDROID = '{http://schemas.android.com/apk/res/android}'


def evidence(path):
    value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if 'structuredContent' in value:
        return value['structuredContent']
    if 'content' in value:
        return json.loads(value['content'][0]['text'])
    return value


def application(manifest_evidence):
    n = manifest_evidence['normalized_result']
    if n['package_name'] != 'tv.danmaku.bili':
        raise ValueError('Expected Android mobile APK tv.danmaku.bili')
    root = ET.fromstring(n['manifest']['text'])
    metadata = {e.get(ANDROID + 'name'): e.get(ANDROID + 'value')
                for app in root.findall('application') for e in app.findall('meta-data')}
    build = metadata.get('BUILD_SN')
    # An unknown/missing resource reference is not a fabricated numeric build.
    build = int(build) if build and re.fullmatch(r'\d+', build) else None
    return {'package_name': n['package_name'], 'app_version': n['version_name'],
            'version_code': int(n['version_code']) if n['version_code'] is not None else None,
            'build_sn': build, 'channel': None, 'mobi_app': metadata.get('MOBI_APP'),
            'min_sdk': n['min_sdk'], 'target_sdk': n['target_sdk'],
            'artifact_sha256': manifest_evidence['subject']['digest']['sha256'],
            'manifest_evidence_id': manifest_evidence['evidence_id']}


def mobile_endpoint(url):
    u = urlsplit(url)
    if u.hostname in ('api.bilibili.com', 'www.bilibili.com') and u.path == '/':
        return u.hostname, '/'
    if u.hostname == 'i0.hdslb.com' and u.path.startswith('/bfs/fawkes/'):
        # Only the observed startup route families; numeric release paths are templates.
        path = re.sub(r'/\d+/', '/{release}/', u.path)
        path = re.sub(r'_\d+\.zip$', '_{release}.zip', path)
        if re.fullmatch(r'/bfs/fawkes/(?:dd|ff|config)/[A-Za-z0-9_./{}-]+\.zip', path):
            return u.hostname, path
    return endpoint(url)


def project(manifest, installed, capture):
    rows = {}
    for r in capture['normalized_result']['records']:
        req = r['reported']['request']
        post = req.get('postData')
        body = None
        if post:
            body = {'state': 'captured', 'media_type': post.get('mimeType'),
                    'content': post.get('text', ''), 'encoding': post.get('encoding')}
        add(rows, {**req, 'header_names': [h['name'] for h in req.get('headers', [])]},
            capture['evidence_id'], {'record_ordinal': r['ordinal']},
            r['reported']['response'].get('status'), body, endpoint_resolver=mobile_endpoint)
    return {
        'id': 'apk', 'label': '安卓手机版 APK',
        'status': '构建已核验；已采集独立模拟器游客启动与初始化接口',
        'projection': 'derived_names_only', 'playback_supported': False,
        'authority': 'historical-reference', 'evidence_id': capture['evidence_id'],
        'scope': '独立 Android 11 / API 30 雷电模拟器，官方 64 位 APK，匿名游客模式，接受访客协议后观察初始化；已打开视频详情，但未确认独立播放地址请求。',
        'application': application(manifest),
        'source': {'landing_page': 'https://app.bilibili.com/',
                   'download_url': 'https://dl.hdslb.com/mobile/latest/android64/iBiliPlayer-bili.apk?t=20261111',
                   'page_advertised_version': '9.19.0', 'downloaded_manifest_version': '9.18.0',
                   'page_matches_artifact': False},
        'environment_check': {'operation': 'inspect_adb_package',
                              'evidence_id': installed['evidence_id'],
                              'target_package_installed': True,
                              'scope': '本次新建的独立 Android 11 测试模拟器'},
        'coverage': {'status': 'partial', 'phase': 'after_guest_consent_initialization',
                     'captured_records': len(capture['normalized_result']['records']),
                     'playback_observed': 'video_detail_loaded_without_confirmed_playurl',
                     'runtime_attribution_by_rea': 'unknown'},
        'limitations': ['已捕获初始化、指纹、反成瘾状态、资源模块、冷启动 gRPC、点击 now、日志和 DNS 请求；视频详情已打开。',
                       '未确认独立 playurl 或 Protobuf 播放请求；静态 PlayViewUnite 结构只证明 APK 声明的 RPC，不证明本次被调用。',
                       '响应体未进入公共目录；HTTP 状态不证明业务成功；签名值、用户/设备 ID、IP 和日志载荷均已排除。',
                       '官网页面版本与实际下载包不一致，以 APK SHA-256 和 Manifest 为准。',
                       '渠道没有可靠实测值，保留为空；手机版不继承 TV 构建与设备参数。',
                       '尚未接入手机版播放工作进程；不得使用 TV worker 回放。'],
        'interfaces': finalize(rows),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--installed', required=True)
    parser.add_argument('--capture', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = project(evidence(args.manifest), evidence(args.installed), evidence(args.capture))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'{len(result["interfaces"])} mobile startup interface groups')


if __name__ == '__main__':
    main()
