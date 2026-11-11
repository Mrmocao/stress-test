"""Redaction at public output boundaries; private state remains resumable."""
import re
import ipaddress

PRIVATE_FIELDS = frozenset({
    'buvid', 'did', 'rpdid', 'initial_did', 'sent_did', 'returned_rpdid',
    'session', 'session_id', 'session-id', 'request_session', 'guest_id',
    'guest_access_key', 'access_key', 'api_key', 'app_secret', 'aes_key',
    'aes_iv', 'click_salt', 'sign', 'private_dir', 'context_file',
    'client_state_namespace', 'installation_id', 'buvid_sha256',
    'binding', 'guest_contract', 'headers', 'payload', 'extra', 'click_extra', 'signed',
    'namespace', 'replacement', 'replacement_did', 'private',
    'user_id', 'uid', 'mid', 'device_id', 'android_id', 'ip', 'client_ip',
    'source_ip', 'exit_ip', 'endpoint', 'mac', 'wifimac', 'imei', 'oaid',
    'cookie', 'cookies', 'authorization', 'token', 'generation_seed', 'profile_seed',
})


_NORMALIZED_PRIVATE_FIELDS = frozenset(re.sub(r'[^a-z0-9]', '', key) for key in PRIVATE_FIELDS)


def is_private_field(name):
    normalized = re.sub(r'[^a-z0-9]', '', str(name).lower())
    return normalized in _NORMALIZED_PRIVATE_FIELDS


def public_copy(value, name=''):
    if is_private_field(name):
        return '<redacted>'
    if isinstance(value, dict):
        return {str(k): public_copy(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [public_copy(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'(https?://[^\s?]+)\?[^\s\x27\"]+', r'\1?<signed query hidden>', value)
        def mask_ip(match):
            try:
                ipaddress.ip_address(match.group())
                return '<ip hidden>'
            except ValueError:
                return match.group()
        return re.sub(r'(?<![\w.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![\w.])|(?<![\w:])(?:[0-9a-fA-F]{0,4}:){2,}[0-9a-fA-F:.]*(?![\w:])', mask_ip, value)
    return value
