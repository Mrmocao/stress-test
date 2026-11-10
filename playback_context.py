"""Explicit per-playback context. Missing provenance is never filled by random data."""
import json
from pathlib import Path
import re
import hashlib
import time
from copy import deepcopy


FIELDS = {
    'device': {'cpu', 'memory', 'tv_brand', 'mpi_model', 'mpi_id', 'mpi_type'},
    'scene': {'teenager_mode', 'child_lock', 'mode_switch', 'first_start', 'from_out',
              'explor_attr', 'top_speed', 'product_top_speed', 'resource_from', 'resource_id'},
    'guest': {'guest_id', 'guest_access_key'},
    'playurl': {'ad_type', 'ad_extra', 'disable_ad', 'drm_tech_type', 'from_outside',
                'is_global_preview_end', 'is_portrait', 'is_switch_qn', 'ogv_aid',
                'play_model', 'play_scene', 'preview', 'preview_end', 'preview_from_guest'},
    'report': {'spmid', 'from_spmid', 'track_id', 'polaris_action_id', 'auto_play'},
    'extra': {'resource_id', 'internal_track_id', 'section_id', 'resource_from', 'is_preview',
              'watch_right', 'section_type', 'ott_track_id', 'session_more',
              'is_continuous_play', 'ogv_copyright_index'},
    'history': {'scene', 'from_scene', 'is_interacted', 'rtime', 'history_ab_184'},
}


class PlaybackContext:
    def __init__(self, data=None):
        data = {} if data is None else data
        if not isinstance(data, dict) or set(data) - (set(FIELDS) | {'profile_id', 'binding', 'guest_contract'}):
            raise ValueError('播放上下文包含未知分组')
        self.binding = deepcopy(data.get('binding', {}))
        self.guest_contract = deepcopy(data.get('guest_contract', {}))
        self._runtime = None
        if self.binding:
            if (not isinstance(self.binding, dict) or set(self.binding) != {'installation_id', 'buvid_sha256', 'client_slot'}
                    or not isinstance(self.binding['installation_id'], str)
                    or not re.fullmatch(r'[0-9a-f]{32}', self.binding['installation_id'])
                    or not isinstance(self.binding['buvid_sha256'], str)
                    or not re.fullmatch(r'[0-9a-f]{64}', self.binding['buvid_sha256'])
                    or not isinstance(self.binding['client_slot'], str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', self.binding['client_slot'])):
                raise ValueError('binding 必须包含合法安装标识、Buvid摘要和client_slot')
        elif not isinstance(self.binding, dict):
            raise ValueError('binding 必须是对象')
        if not isinstance(self.guest_contract, dict):
            raise ValueError('guest_contract 必须是对象')
        self.profile_id = data.get('profile_id')
        if self.profile_id is not None and (not isinstance(self.profile_id, str)
                                           or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}', self.profile_id)):
            raise ValueError('播放上下文 profile_id 无效')
        self._data = {}
        for section, allowed in FIELDS.items():
            values = data.get(section, {})
            if not isinstance(values, dict) or set(values) - allowed:
                raise ValueError(f'播放上下文 {section} 包含未知字段')
            normalized = {}
            for key, value in values.items():
                if type(value) not in (str, int, bool):
                    raise ValueError(f'播放上下文 {section}.{key} 必须为字符串、整数或布尔值')
                value = str(value).lower() if isinstance(value, bool) else str(value)
                # The click codec uses raw pairs and the SDK extra format has no
                # documented escape convention. Reject ambiguous input, don't guess.
                if (len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value)
                        or any(c in value for c in "&'\\")):
                    raise ValueError(f'播放上下文 {section}.{key} 含不支持字符或过长；extra转义协议尚未核验')
                normalized[key] = value
            self._data[section] = normalized
        guest = self._data['guest']
        if guest and (set(guest) != FIELDS['guest'] or not all(guest.values())):
            raise ValueError('guest_id 与 guest_access_key 必须同时提供且非空')
        if guest:
            contract = self.guest_contract
            if (set(contract) != {'source', 'issued_at', 'expires_at', 'refresh'}
                    or contract['source'] not in ('offline_fixture', 'manual_verified')
                    or contract['refresh'] != 'manual'
                    or type(contract['issued_at']) is not int or type(contract['expires_at']) is not int
                    or not 0 < contract['issued_at'] < contract['expires_at']
                    or not self.binding):
                raise ValueError('游客凭据需要安装绑定、来源、有效期及manual刷新契约')
        elif self.guest_contract:
            raise ValueError('guest_contract 不能脱离游客凭据单独使用')
        if any(self._data.values()) and self.profile_id is None:
            raise ValueError('非空播放上下文必须通过 profile_id 绑定设备配置')
        for key in ('resource_id', 'resource_from'):
            if (key in self._data['scene'] and key in self._data['extra']
                    and self._data['scene'][key] != self._data['extra'][key]):
                raise ValueError(f'播放上下文 scene 与 extra 的 {key} 不一致')
        memory = self._data['device'].get('memory')
        if memory is not None and (not re.fullmatch(r'[0-9]+', memory) or int(memory) <= 0):
            raise ValueError('memory 必须是正整数 MiB')
        for section, key in (('scene', 'mode_switch'), ('history', 'is_interacted')):
            value = self._data[section].get(key)
            if value is not None and value not in ('true', 'false'):
                raise ValueError(f'{section}.{key} 必须为 true 或 false')
        # Confirmed representation only; do not invent enum meanings or maxima.
        for section, key in (('scene', 'teenager_mode'), ('scene', 'child_lock'), ('report', 'auto_play')):
            value = self._data[section].get(key)
            if value is not None and not re.fullmatch(r'[0-9]+', value):
                raise ValueError(f'{section}.{key} 必须为非负十进制整数；枚举含义仍待核验')

    @classmethod
    def from_file(cls, path):
        try:
            with Path(path).open('rb') as handle:
                raw = handle.read(65537)
            if len(raw) > 65536:
                raise ValueError('播放上下文文件超过 64 KiB')
            def unique_pairs(pairs):
                obj = {}
                for key, value in pairs:
                    if key in obj:
                        raise ValueError('上下文JSON存在重复字段')
                    obj[key] = value
                return obj
            return cls(json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique_pairs))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ValueError('无法读取 UTF-8 播放上下文 JSON 文件') from None

    def validate_profile(self, profile):
        if self.profile_id is not None and self.profile_id != profile['id']:
            raise ValueError('播放上下文与当前设备配置不匹配；不能跨配置复用')

    def common(self):
        return {**self._data['device'], **self._data['scene'], **self._data['guest']}

    def bind_runtime(self, profile, identity, slot, mode, *, clock=time.time):
        self.validate_profile(profile)
        if mode not in ('demo', 'live'):
            raise ValueError('上下文运行模式无效')
        if mode == 'live' and any(self._data.values()) and not self.binding:
            raise ValueError('真实上下文必须绑定安装、Buvid和client_slot')
        if self.binding:
            expected = {'installation_id': identity['installation_id'],
                        'buvid_sha256': hashlib.sha256(identity['buvid'].encode()).hexdigest(),
                        'client_slot': slot}
            if self.binding != expected:
                raise ValueError('上下文安装绑定不匹配')
        self._runtime = (mode, clock)
        self.validate_runtime()

    def validate_runtime(self, mode=None):
        if mode == 'live' and self.has_values and (self._runtime is None or self._runtime[0] != 'live'):
            raise ValueError('真实上下文未完成安装绑定校验')
        if self.guest_contract:
            if self._runtime is None:
                raise ValueError('游客上下文尚未绑定运行实例')
            mode, clock = self._runtime
            if mode == 'live':
                # A user declaration is not proof of an SDK credential lifecycle.
                raise ValueError('游客来源和刷新协议尚未核验；当前仅允许离线契约验证')
            now = clock()
            if not self.guest_contract['issued_at'] <= now < self.guest_contract['expires_at']:
                raise ValueError('游客凭据未生效或已过期；需手动更新，不自动刷新或重放')

    @property
    def has_values(self):
        return any(self._data.values())

    def fields(self, endpoint):
        report = self._data['report']
        if endpoint == 'playurl':
            return {**self._data['playurl'], **{k: v for k, v in report.items() if k == 'spmid'},
                    **({'fromspmid': report['from_spmid']} if 'from_spmid' in report else {})}
        if endpoint in ('heartbeat', 'click'):
            return dict(report)
        if endpoint == 'history':
            return {**self._data['history'],
                    **{k: v for k, v in report.items() if k == 'from_spmid'}}
        raise ValueError('未知播放上下文接口')

    def extra(self, session, common, endpoint):
        values = {'session_id': session, 'resource_id': '', 'internal_track_id': '', 'section_id': '',
                  'guest_id': '', 'resource_from': '', 'is_preview': '0', 'watch_right': '',
                  'section_type': '', 'ott_track_id': '', 'session_more': '',
                  'teenager_mode': str(common.get('teenager_mode', '0'))}
        for key in ('guest_id', 'resource_id', 'resource_from'):
            if key in common:
                values[key] = str(common[key])
        values.update(self._data['extra'])
        if endpoint == 'heartbeat' and 'from_spmid' in self._data['report']:
            values['from_spmid'] = self._data['report']['from_spmid']
        if endpoint == 'click':
            values.pop('ogv_copyright_index', None)
        return '{' + ','.join(f"'{k}':'{v}'" for k, v in values.items()) + '}'

    def summary(self):
        # Persist names only; neither credentials nor trace values belong in UI state.
        return {'profile_id': self.profile_id,
                'installation_bound': bool(self.binding),
                'guest_contract_present': bool(self.guest_contract),
                'fields': {key: sorted(value) for key, value in self._data.items() if value}}
