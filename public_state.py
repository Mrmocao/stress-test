"""Redaction at public output boundaries; private state remains resumable."""
import re

PRIVATE_FIELDS = frozenset({
    'buvid', 'did', 'rpdid', 'initial_did', 'sent_did', 'returned_rpdid',
    'session', 'session_id', 'session-id', 'request_session', 'guest_id',
    'guest_access_key', 'access_key', 'api_key', 'app_secret', 'aes_key',
    'aes_iv', 'click_salt', 'sign', 'private_dir', 'context_file',
    'client_state_namespace', 'installation_id', 'buvid_sha256',
    'binding', 'guest_contract', 'headers', 'payload', 'extra', 'click_extra', 'signed',
    'namespace', 'replacement', 'replacement_did', 'private',
})


def public_copy(value, name=''):
    if name.lower() in PRIVATE_FIELDS:
        return '<redacted>'
    if isinstance(value, dict):
        return {str(k): public_copy(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [public_copy(v) for v in value]
    if isinstance(value, str):
        return re.sub(r'(https?://[^\s?]+)\?[^\s\x27\"]+', r'\1?<signed query hidden>', value)
    return value
