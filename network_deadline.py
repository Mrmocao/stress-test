"""Bound a complete response, including TLS reads at the deadline boundary."""
import http.client
import ssl
import time
import zlib


class ResponseEncodingError(ValueError):
    pass


def read_json_body(response, event, deadline, max_bytes=2 * 1024 * 1024):
    """Bound both wire bytes and decompressed bytes; never accept truncated gzip."""
    raw = read_bounded(response, event, deadline, max_bytes)
    headers = getattr(response, 'headers', None) or {}
    encoding = headers.get('Content-Encoding', 'identity').strip().lower()
    event['content_encoding'] = encoding
    if encoding in ('', 'identity'):
        body = raw
    elif encoding == 'gzip':
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            body = decoder.decompress(raw, max_bytes + 1)
        except zlib.error:
            raise ResponseEncodingError('Invalid gzip response') from None
        if len(body) > max_bytes or decoder.unconsumed_tail:
            raise ValueError('Decompressed response exceeds size limit')
        if not decoder.eof or decoder.unused_data:
            raise ResponseEncodingError('Incomplete or trailing gzip response')
    else:
        raise ResponseEncodingError('Unsupported response encoding')
    if time.monotonic() > deadline:
        raise TimeoutError('Response decoding deadline exceeded')
    event['decoded_bytes'] = len(body)
    return body


def read_bounded(response, event, deadline, max_bytes=2 * 1024 * 1024):
    chunks, size = [], 0
    headers = getattr(response, 'headers', {})
    event['content_length'] = headers.get('Content-Length') if headers is not None else None
    while True:
        remaining = deadline - time.monotonic()
        # Never pass a sub-millisecond timeout that may become nonblocking TLS.
        if remaining <= .001:
            raise TimeoutError('Response deadline exceeded')
        sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
        if sock is not None:
            sock.settimeout(remaining)
        try:
            chunk = getattr(response, 'read1', response.read)(min(65536, max_bytes + 1 - size))
        except ssl.SSLWantReadError:
            remaining = deadline - time.monotonic()
            if remaining <= .001:
                raise TimeoutError('TLS response deadline exceeded') from None
            time.sleep(min(.005, remaining))
            continue
        if time.monotonic() > deadline:
            raise TimeoutError('Response deadline exceeded')
        if not chunk:
            missing = getattr(response, 'length', 0)
            if isinstance(missing, int) and missing > 0:
                raise http.client.IncompleteRead(b''.join(chunks), missing)
            return b''.join(chunks)
        size += len(chunk)
        event['bytes_read'] = size
        if size > max_bytes:
            raise ValueError('Response exceeds size limit')
        chunks.append(chunk)
