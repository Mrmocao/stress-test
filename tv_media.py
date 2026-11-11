"""Resolve an unencrypted TV playurl response and build paced decoder inputs.

Selection is deliberately deterministic: use the server-selected quality when
available, then fall back to the nearest lower quality before trying a higher
one.  The decision is returned as safe metadata so a failed stream can be
compared with the original playurl response without exposing signed URLs.
"""
from hashlib import sha256
from urllib.parse import urlsplit


def media_url(stream):
    url = stream.get('base_url') or stream.get('baseUrl') or stream.get('url')
    if not isinstance(url, str) or urlsplit(url).scheme not in ('http', 'https'):
        raise ValueError('Media URL must use HTTP(S)')
    return url


def _quality(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _candidate(video, sound, *, quality, requested_quality, reason):
    video_url, audio_url = media_url(video), media_url(sound)
    return {
        'kind': 'dash', 'urls': [video_url, audio_url], 'audio_input': 1,
        'quality': quality, 'selection_reason': reason,
        'url_fingerprints': [sha256(url.encode()).hexdigest() for url in (video_url, audio_url)],
    }


def select_media(result):
    if result.get('is_drm') or result.get('is_preview'):
        raise ValueError('Response is DRM-protected or a preview')
    dash = result.get('dash') or {}
    if dash:
        videos = dash.get('video') or []
        audio = dash.get('audio') or []
        if not videos or not audio:
            raise ValueError('DASH response lacks video or audio')
        requested = _quality(result.get('quality'))
        available = sorted({_quality(v.get('id')) for v in videos if _quality(v.get('id')) >= 0}, reverse=True)
        if not available:
            raise ValueError('DASH response lacks a valid video quality')
        lower = [quality for quality in available if quality <= requested]
        selected = lower[0] if lower else available[-1]
        reason = 'server-quality' if selected == requested else ('lower-quality-fallback' if lower else 'lowest-quality-fallback')
        sound = max(audio, key=lambda a: _quality(a.get('bandwidth')))
        if sound.get('drm') or sound.get('drm_tech_type'):
            raise ValueError('Encrypted DASH audio is unsupported')
        candidates = []
        for quality in [selected] + [value for value in available if value != selected]:
            quality_videos = [v for v in videos if _quality(v.get('id')) == quality]
            quality_videos = sorted(quality_videos,
                                    key=lambda v: (v.get('codecid') != 7, -_quality(v.get('bandwidth'))))
            for video in quality_videos:
                if video.get('drm') or video.get('drm_tech_type'):
                    continue
                candidate_reason = reason if quality == selected else f'quality-{quality}-fallback'
                try:
                    candidates.append(_candidate(video, sound, quality=quality,
                                                 requested_quality=requested, reason=candidate_reason))
                except ValueError:
                    continue
        if not candidates:
            raise ValueError('DASH response has no usable unencrypted video/audio')
        selected_candidate = candidates[0]
        selected_candidate['candidates'] = candidates
        selected_candidate['selection'] = {
            'requested_quality': requested, 'selected_quality': selected,
            'available_qualities': available, 'reason': reason,
            'candidate_count': len(candidates),
        }
        return selected_candidate
    media = result.get('durl') or []
    if not media:
        raise ValueError('Expected one progressive stream or DASH audio/video')
    candidates = []
    for index, stream in enumerate(media):
        try:
            url = media_url(stream)
        except ValueError:
            continue
        candidates.append({'kind': 'progressive', 'urls': [url], 'audio_input': 0,
                           'quality': _quality(result.get('quality')),
                           'selection_reason': 'server-progressive' if index == 0 else 'progressive-fallback',
                           'url_fingerprints': [sha256(url.encode()).hexdigest()]})
    if not candidates:
        raise ValueError('Progressive response has no usable HTTP(S) stream')
    selected = candidates[0]
    selected['candidates'] = candidates
    selected['selection'] = {'requested_quality': _quality(result.get('quality')),
                             'selected_quality': _quality(result.get('quality')),
                             'available_qualities': [_quality(result.get('quality'))],
                             'reason': 'server-progressive', 'candidate_count': len(candidates)}
    return selected


def decoder_command(executable, media, target, ua, proxy=None, budget=False):
    command = [executable, '-hide_banner', '-nostdin', '-loglevel', 'info',
               '-filter_threads', '1', '-filter_complex_threads', '1']
    for url in media['urls']:
        command += ['-re', '-rw_timeout', '3000000' if budget else '8000000',
                    '-user_agent', ua, '-threads', '2']
        if proxy:
            command += ['-http_proxy', proxy]
        command += ['-i', url]
    return command + ['-t', str(target), '-threads', '1', '-map', '0:v:0',
                      '-map', f"{media['audio_input']}:a:0", '-progress', 'pipe:1',
                      '-stats_period', '1', '-nostats', '-f', 'null', '-']
