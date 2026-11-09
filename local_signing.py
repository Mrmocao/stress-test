"""Pure Python signer for the analyzed bilibili TV 1.8.9 APK.

The APK's libbili.so dispatches android_tv to app index 3. The four secret
words are at 0x9024, 0x9038, 0x904c, 0x9060 and are formatted as %08x.
See evidence/local-signing-validation.json for artifact identity and vectors.
"""
import hashlib
import time
from urllib.parse import quote

APP_KEY = '4409e2ce8ffd12b8'
APP_SECRET = '59b43e04ad6965f34319062b478f83dd'


def sign_query(params):
    """Match the previous Java helper and LibBili.s, including auto-added ts."""
    values = {str(key): str(value) for key, value in params.items()}
    values['appkey'] = APP_KEY
    if 'ts' not in values:
        values['ts'] = str(int(time.time()))
    # Java TreeMap compares UTF-16 code units, which matters for non-BMP keys.
    keys = sorted((key for key in values if key), key=lambda key: key.encode('utf-16-be'))
    query = '&'.join(quote(key, safe='-_.~') + '=' + quote(values[key], safe='-_.~') for key in keys)
    signature = hashlib.md5((query + APP_SECRET).encode('utf-8')).hexdigest()
    return query + '&sign=' + signature
