"""Resolve an unencrypted TV playurl response and build paced decoder inputs."""
from urllib.parse import urlsplit


def media_url(stream):
    url = stream.get('base_url') or stream.get('baseUrl') or stream.get('url')
    if not isinstance(url, str) or urlsplit(url).scheme not in ('http', 'https'):
        raise ValueError('Media URL must use HTTP(S)')
    return url


def select_media(result):
    if result.get('is_drm') or result.get('is_preview'):
        raise ValueError('Response is DRM-protected or a preview')
    dash = result.get('dash') or {}
    if dash:
        videos = dash.get('video') or []
        audio = dash.get('audio') or []
        videos = [v for v in videos if int(v.get('id', -1)) == int(result['quality'])]
        if not videos or not audio:
            raise ValueError('DASH response lacks selected quality or audio')
        # Prefer AVC when several codecs represent the returned quality.
        video = sorted(videos, key=lambda v: (v.get('codecid') != 7, -int(v.get('bandwidth', 0))))[0]
        sound = max(audio, key=lambda a: int(a.get('bandwidth', 0)))
        if any(s.get('drm') or s.get('drm_tech_type') for s in (video, sound)):
            raise ValueError('Encrypted DASH is unsupported')
        return {'kind': 'dash', 'urls': [media_url(video), media_url(sound)], 'audio_input': 1}
    media = result.get('durl') or []
    if len(media) != 1:
        raise ValueError('Expected one progressive stream or DASH audio/video')
    return {'kind': 'progressive', 'urls': [media_url(media[0])], 'audio_input': 0}


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
