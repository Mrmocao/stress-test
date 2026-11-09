"""Per-run Juliang HTTP proxy acquisition; never changes Windows proxy settings.

Docs: https://www.juliangip.com/help/api/company/dynamic/
      https://www.juliangip.com/help/api/sign/
"""
from dataclasses import dataclass
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from network_deadline import read_bounded

CONFIG_PATH = Path(__file__).resolve().parent / '.private' / 'juliang.json'
SOURCE_IP_URL = 'http://v2.api.juliangip.com/v2/dps/ip_search'
PARAMS = {'auto_white': '1', 'filter': '1', 'num': '1', 'pt': '1',
          'result_type': 'json', 'split': '1', 'ip_remain': '1'}
ERRORS = {401: '签名校验失败，请检查订单号和 API Key', 402: '请求参数错误',
          408: '订单暂无可用余量', 410: '未添加白名单，请检查订单白名单设置',
          429: '提取频率超限，请稍后再试'}


class ProxyError(RuntimeError):
    def __init__(self, message, *, code=None, retryable=True, source_ip=''):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.source_ip = source_ip


def load_config():
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding='utf-8')) if CONFIG_PATH.exists() else {}
        trade_no = os.environ.get('JULIANG_TRADE_NO') or config.get('trade_no', '')
        api_key = os.environ.get('JULIANG_API_KEY') or config.get('api_key', '')
        if not re.fullmatch(r'[0-9]+', str(trade_no)) or not isinstance(api_key, str) or not api_key.strip():
            raise ValueError()
        return str(trade_no), api_key.strip()
    except (OSError, ValueError, TypeError, AttributeError):
        raise ProxyError('请在 .private/juliang.json 配置 trade_no 和 api_key，或设置 JULIANG_TRADE_NO / JULIANG_API_KEY', retryable=False) from None


def signed_url(trade_no, api_key):
    params = {**PARAMS, 'trade_no': trade_no}
    raw = '&'.join(f'{key}={params[key]}' for key in sorted(params)) + '&key=' + api_key
    params['sign'] = hashlib.md5(raw.encode('utf-8')).hexdigest()
    # Vendor documents HTTP; its HTTPS certificate does not cover this hostname.
    return 'http://v2.api.juliangip.com/company/dynamic/getips?' + urllib.parse.urlencode(params)


class RequiredProxyHandler(urllib.request.ProxyHandler):
    """Explicit proxies must not be bypassed by NO_PROXY or Windows settings."""
    def proxy_open(self, req, proxy, type):
        req.set_proxy(urllib.parse.urlsplit(proxy).netloc, 'http')
        return None


def direct_opener():
    """Private control connection: ignores env/Windows proxies and global opener."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def direct_source_ip(opener):
    """Best-effort diagnostic through the same direct route as extraction."""
    try:
        with opener.open(SOURCE_IP_URL, timeout=5) as response:
            body = json.loads(response.read(8192))
        if body.get('code') != 0:
            return ''
        address = ipaddress.ip_address(body['data']['ip'])
        return str(address) if address.is_global else ''
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return ''


def source_label(source_ip):
    return f'本机直连公网 IP={source_ip}' if source_ip else '本机公网 IP 检测未成功（提取仍强制直连）'


@dataclass
class ProxyLease:
    endpoint: str
    remaining: object = None
    exit_ip: str = ''
    source_ip: str = ''
    lifetime_seconds: int = 60
    expires_at: float = 0
    lifetime_source: str = 'configured_60s'

    def seconds_left(self):
        return max(0, self.expires_at - time.monotonic())

    @property
    def url(self):
        return 'http://' + self.endpoint

    def opener(self):
        return urllib.request.build_opener(RequiredProxyHandler({'http': self.url, 'https': self.url}))

    def verify(self, publish=lambda **event: None, minimum_seconds=0):
        opener = self.opener()
        deadline = time.monotonic() + 6
        if self.expires_at:
            deadline = min(deadline, self.expires_at - minimum_seconds)
        for target in ('https://api.ipify.org?format=json', 'https://httpbin.org/ip'):
            started = time.monotonic()
            available = deadline - started
            if available < .1:
                break
            attempt_deadline = min(deadline, started + 3)
            event = {'service': urllib.parse.urlsplit(target).hostname, 'phase': 'connecting',
                     'status': 'checking', 'bytes_read': 0}
            try:
                with opener.open(target, timeout=attempt_deadline-started) as response:
                    event.update(phase='response_body', http_status=response.status)
                    data = json.loads(read_bounded(response, event, attempt_deadline, 8192))
                event['phase'] = 'validating'
                candidate = data.get('ip') or data.get('origin', '')
                address = ipaddress.ip_address(candidate.strip())
                if not address.is_global:
                    raise ValueError('Exit is not a public address')
                self.exit_ip = str(address)
                event.update(status='ok', phase='complete')
                return self.exit_ip
            except (OSError, http.client.HTTPException, ValueError, TypeError, AttributeError) as error:
                cause = error.reason if isinstance(error, urllib.error.URLError) else error
                category = ('timeout' if isinstance(cause, TimeoutError) else
                            'tls_certificate' if isinstance(cause, ssl.SSLCertVerificationError) else
                            'http_error' if isinstance(error, urllib.error.HTTPError) else
                            'invalid_response' if isinstance(error, (ValueError, TypeError, AttributeError)) else 'connection')
                event.update(status='failed', error_category=category, cause_type=type(cause).__name__)
                if isinstance(error, urllib.error.HTTPError):
                    event['http_status'] = error.code
                    error.close()
                continue
            finally:
                event['elapsed_ms'] = round((time.monotonic()-started)*1000)
                publish(**event)
        raise ProxyError('代理已提取，但 HTTPS 公网出口验证失败，请检查代理有效期、白名单或检测服务可达性')


def parse_response(body):
    if not isinstance(body, dict):
        raise ProxyError('巨量 IP 返回格式异常')
    code = body.get('code')
    if code != 200:
        # Do not forward vendor messages: they can echo signed URLs or credentials.
        code = code if isinstance(code, int) else None
        label = ERRORS.get(code, '提取失败，请在巨量 IP 控制台检查订单状态')
        raise ProxyError(f'巨量 IP：{label}（code={code if code is not None else "unknown"}）',
                         code=code, retryable=code not in (401, 402, 403, 404, 405, 406, 407, 408, 409, 410, 500, 501))
    data = body.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('proxy_list'), list) or not data['proxy_list']:
        raise ProxyError('巨量 IP 未返回可用代理（可能当天去重后无可用 IP）')
    endpoint = data['proxy_list'][0]
    lifetime, lifetime_source = 60, 'configured_60s'
    if isinstance(endpoint, str) and ',' in endpoint:
        endpoint, seconds = endpoint.rsplit(',', 1)
        if not re.fullmatch(r'[0-9]+', seconds) or not 0 < int(seconds) <= 86400:
            raise ProxyError('巨量 IP 返回的剩余寿命无效')
        lifetime, lifetime_source = int(seconds), 'provider'
    if not isinstance(endpoint, str) or not re.fullmatch(r'[0-9.]+:[0-9]{1,5}', endpoint):
        raise ProxyError('代理格式错误，预期白名单鉴权的 IPv4:端口')
    host, port = endpoint.split(':')
    try:
        address = ipaddress.IPv4Address(host)
        if not address.is_global or not 1 <= int(port) <= 65535:
            raise ValueError()
    except ValueError:
        raise ProxyError('巨量 IP 返回的代理地址或端口无效') from None
    remaining = data.get('surplus_quantity')
    return ProxyLease(endpoint, remaining if isinstance(remaining, int) else None,
                      lifetime_seconds=lifetime, lifetime_source=lifetime_source)


def acquire_proxy():
    trade_no, api_key = load_config()
    # Fetch outside the proxy being acquired, without inheriting system proxies.
    opener = direct_opener()
    source_ip = direct_source_ip(opener)
    requested_at = time.monotonic()
    try:
        with opener.open(signed_url(trade_no, api_key), timeout=15) as response:
            body = json.loads(response.read(65536))
    except urllib.error.HTTPError as error:
        raise ProxyError(f'巨量 IP 提取服务 HTTP {error.code}', code=error.code,
                         retryable=error.code == 429 or error.code >= 500, source_ip=source_ip) from None
    except (OSError, ValueError):
        raise ProxyError('巨量 IP 提取请求失败或返回非 JSON', source_ip=source_ip) from None
    try:
        lease = parse_response(body)
    except ProxyError as error:
        error.source_ip = source_ip
        raise
    lease.source_ip = source_ip
    lease.expires_at = requested_at + lease.lifetime_seconds
    return lease


def acquire_verified_proxy(retries=1, publish=lambda **event: None, wait=time.sleep, minimum_seconds=0):
    """Bounded retries apply only before playback; a retry can consume another IP."""
    if type(retries) is not int or not 0 <= retries <= 3:
        raise ValueError('IP 额外重试次数必须为 0～3')
    for attempt in range(1, retries + 2):
        publish(enabled=True, status='acquiring', attempt=attempt, max_attempts=retries + 1,
                message=f'使用本机直连提取代理（第 {attempt}/{retries + 1} 次尝试，不经过已获取的代理）')
        lease = None
        try:
            lease = acquire_proxy()
            publish(enabled=True, status='verifying', endpoint=lease.endpoint,
                    source_ip=lease.source_ip, remaining=lease.remaining, attempt=attempt,
                    message=f'{source_label(lease.source_ip)}；提取成功，正在验证代理 HTTPS 公网出口…')
            lease.verify(minimum_seconds=minimum_seconds,
                         publish=lambda **event: publish(diagnostic=True, enabled=True, **event))
            if minimum_seconds and lease.seconds_left() < minimum_seconds:
                raise ProxyError('代理剩余寿命不足以完成目标播放；请调整代理有效期或减少前置耗时', retryable=False)
            publish(enabled=True, status='ready', endpoint=lease.endpoint, exit_ip=lease.exit_ip,
                    source_ip=lease.source_ip,
                    lifetime_seconds=lease.lifetime_seconds, lifetime_source=lease.lifetime_source,
                    expires_at_epoch=time.time() + lease.seconds_left(),
                    remaining=lease.remaining, attempt=attempt, message='本轮接口请求和视频流使用此代理')
            return lease
        except ProxyError as error:
            retry = error.retryable and attempt <= retries
            publish(enabled=True, status='retrying' if retry else 'failed', attempt=attempt,
                    endpoint=lease.endpoint if lease else None,
                    source_ip=lease.source_ip if lease else error.source_ip,
                    error_code=error.code,
                    message=source_label(lease.source_ip if lease else error.source_ip) + '；' + str(error)
                    + ('；等待 5 秒后重新提取' if retry else '；本轮记为失败'))
            if not retry:
                raise
            wait(5)


if __name__ == '__main__':
    # One extraction, connectivity test only; no playback or reporting.
    try:
        lease = acquire_proxy()
        lease.verify()
        print(json.dumps({'proxy': lease.endpoint, 'exit_ip': lease.exit_ip,
                          'remaining': lease.remaining}, ensure_ascii=False))
    except ProxyError as error:
        print(str(error))
        raise SystemExit(1)
