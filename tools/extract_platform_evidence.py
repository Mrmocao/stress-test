"""Project private REA observations to a names-only public interface catalog.

No request values, body contents, cookies, signed URLs or device paths are copied.
The result is a derived catalog, NOT a canonical REA Evidence bundle.
"""
import argparse
import base64
from collections import Counter
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlsplit

FIELD = re.compile(r'[A-Za-z_][A-Za-z0-9_.\[\]-]{0,63}\Z')
API_HOSTS = {'api.bilibili.com', 'data.bilibili.com', 'api.snm0516.aisee.tv',
             'app.bilibili.com', 'passport.bilibili.com'}


def names(values):
    return sorted({v for v in values if isinstance(v, str) and FIELD.fullmatch(v)})


def endpoint(url):
    u = urlsplit(url)
    host = u.hostname or ''
    if host.endswith('.bilivideo.com') and u.path.endswith(('.m4s', '.mp4', '.flv')):
        return '*.bilivideo.com', '/{media-object}' + Path(u.path).suffix
    if host not in API_HOSTS:
        return None
    if not re.fullmatch(r'/(?:x|bapis|v2|log)/[A-Za-z0-9_./-]+', u.path):
        return None
    segments = u.path.split('/')
    return host, '/'.join('{id}' if re.fullmatch(r'(?:[0-9]+|[a-fA-F0-9]{16,})', s) else s for s in segments)


def body_fields(body):
    if body.get('state') != 'captured':
        return [], body.get('state', 'unavailable')
    media = (body.get('media_type') or '').split(';')[0]
    try:
        raw = body.get('content', '')
        if body.get('encoding') == 'base64':
            raw = base64.b64decode(raw, validate=True).decode('utf-8')
        if media == 'application/x-www-form-urlencoded':
            return names(k for k, _ in parse_qsl(raw, keep_blank_values=True)), 'form_names_only'
        if media == 'application/json':
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return names(obj), 'json_top_level_names_only'
    except (ValueError, UnicodeError):
        pass
    return [], 'opaque_not_decoded'


def add(rows, request, evidence_id, reference, response_status=None, body=None, body_ref=None,
        endpoint_resolver=endpoint):
    url = request['url']
    key = endpoint_resolver(url)
    if key is None:
        return
    host, path = key
    method = request['method']
    if method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'):
        return
    row = rows.setdefault((method, host, path), {
        'method': method, 'host': host, 'path': path, 'query_parameters': set(),
        'body_parameters': set(), 'request_header_names': set(),
        'body_states': Counter(), 'http_status_counts': Counter(), 'observations': [],
        'query_shape': 'field_names_only',
    })
    if host == 'data.bilibili.com' and path == '/log/web':
        # Whole-query telemetry contains nested URLs, not ordinary query fields.
        row['query_shape'] = 'opaque_telemetry_not_decoded'
    else:
        row['query_parameters'].update(names(k for k, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)))
    fields, state = body_fields(body or {})
    row['body_parameters'].update(fields)
    row['body_states'][state] += 1
    row['request_header_names'].update(names(request.get('header_names', [])))
    if isinstance(response_status, int):
        row['http_status_counts'][str(response_status)] += 1
    ref = {'evidence_id': evidence_id, **reference}
    if body_ref is not None:
        ref['body_event_sequence'] = body_ref
    row['observations'].append(ref)


def finalize(rows):
    result = []
    for _, row in sorted(rows.items()):
        for key in ('query_parameters', 'body_parameters', 'request_header_names'):
            row[key] = sorted(row[key])
        row['request_count'] = len(row['observations'])
        row['response_count'] = sum(row['http_status_counts'].values())
        row['parameters_are_required'] = False
        result.append(row)
    return result


def extract_web(record):
    n = record['normalized_result']
    events = n['events']['items']
    responses = {e['transaction_id']: e for e in events if e['kind'] == 'response'}
    bodies = {e['transaction_id']: e for e in events if e['kind'] == 'network-content' and e['phase'] == 'request'}
    rows = {}
    for e in events:
        if e['kind'] != 'request':
            continue
        content = bodies.get(e['transaction_id'], {})
        add(rows, {'method': e['method'], 'url': e['url']['url'], 'header_names': e['header_names']},
            record['evidence_id'], {'request_event_sequence': e['sequence']},
            responses.get(e['transaction_id'], {}).get('status'), content.get('body'), content.get('sequence'))
    return {
        'id': 'web', 'label': '网页播放', 'status': '已采集部分运行时接口',
        'authority': record['authority'], 'projection': 'derived_names_only',
        'playback_supported': False, 'evidence_id': record['evidence_id'],
        'scope': '匿名 Chromium 页面加载及自动播放启动，观察 10 秒；未登录',
        'browser': {k: n['browser'][k] for k in ('product', 'version')},
        'coverage': {**n['completeness'], 'events_retained': n['events']['retained'], 'events_dropped': n['events']['dropped']},
        'limitations': ['仅代表本次场景，参数并集不表示全部必填字段。',
                       '部分请求体不可读取；响应体未采集，HTTP 状态不代表业务成功。',
                       '未观察到独立 playurl 请求，不能据此确认播放地址的生成流程。',
                       '日志二进制或不透明载荷未解码；未推导签名或设备标识生成算法。',
                       '仅供采集对照，尚未接入网页播放工作进程。'],
        'interfaces': finalize(rows),
    }


def extract_tv(record):
    rows = {}
    for r in record['normalized_result']['records']:
        req = r['reported'].get('request', {})
        post = req.get('postData', {})
        body = {'state': 'captured', 'media_type': post.get('mimeType'), 'content': post.get('text', '')} if post else None
        add(rows, {**req, 'header_names': [h['name'] for h in req.get('headers', [])]},
            record['evidence_id'], {'record_ordinal': r['ordinal']},
            r['reported'].get('response', {}).get('status'), body)
    return {'id': 'tv', 'label': 'TV（现有平台）', 'status': '历史抓包对照',
            'authority': 'historical-reference', 'projection': 'derived_names_only',
            'playback_supported': True, 'evidence_id': record['evidence_id'],
            'package_name': 'com.xiaodianshi.tv.yst',
            'scope': 'REA 重新解析既有脱敏 TV HAR，不属于本次新增 APK 运行时采集',
            'limitations': ['历史文件无法证明当前 APK 的运行时行为。', '参数值已被原始脱敏流程处理，仅保留名称用于对照。'],
            'interfaces': finalize(rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--web-evidence', required=True)
    parser.add_argument('--tv-evidence', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    records = {r['evidence_id']: r for r in json.loads(args.bundle.read_text(encoding='utf-8'))['records']}
    args.output.mkdir(parents=True, exist_ok=True)
    for name, result in [('web', extract_web(records[args.web_evidence])), ('tv', extract_tv(records[args.tv_evidence]))]:
        (args.output / f'{name}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f'{name}: {len(result["interfaces"])} interface groups')


if __name__ == '__main__':
    main()
