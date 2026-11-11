"""Build an aggregate game-device library and reproducible LOCAL test fixtures.

No network calls. Analytics identifiers are used only for in-memory counting.
The exported whitelist contains hardware/app/runtime observations, never user
IDs, device IDs, IPs, precise timestamps or arbitrary event payloads.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import random

from device_parameter_rules import android_sdk_reference

SCHEMA_VERSION = 1
MISSING = {'', 'null', 'none', '--'}
HARDWARE_FIELDS = {
    'os_family': '#os', 'manufacturer': '#manufacturer',
    'model': '#device_model', 'device_type': '#device_type',
    'os_version': '#os_version', 'simulator': '#simulator',
    'screen_width_reported': '#screen_width', 'screen_height_reported': '#screen_height',
}
APP_FIELDS = {
    'game_name': 'game_name', 'os_family': '#os', 'bundle_id': '#bundle_id',
    'app_version': '#app_version', 'channel_id': 'channel_id',
    'sub_channel_id': 'sub_channel_id', 'platform_id': 'platformid',
    'analytics_library': '#lib', 'analytics_library_version': '#lib_version',
    'data_source': '#data_source',
}
RUNTIME_FIELDS = {
    'network_type': '#network_type', 'language': '#system_language',
    'zone_offset': '#zone_offset', 'carrier': '#carrier', 'fps_reported': '#fps',
}
REQUIRED = {'#device_id', '#event_time', '$part_event', '#ram', '#disk',
            *HARDWARE_FIELDS.values(), *APP_FIELDS.values(), *RUNTIME_FIELDS.values()}
RULES = {
    'scope': 'game-app local compatibility fixtures; not TV hardware evidence',
    'weights': 'one latest observed row per nonempty #device_id; not event frequency',
    'joint_sampling': 'hardware + app + runtime are selected from the same observed row',
    'app_contract': 'bundle/version/channel/platform/analytics SDK kept as an observed tuple',
    'sdk': 'SDK_INT was not measured; Android release lookup is reference-only',
    'memory_disk': 'raw numerator/denominator retained; units and numerator meaning unverified',
    'screen': 'reported width/height retained together; pixel/point units unverified',
    'identity': 'original identifiers excluded; local test installation ID derived from seed',
    'default_filter': 'exclude review flags and anything not explicitly simulator=false',
    'sampling_limit': 'sample subset only; cannot estimate the full player population',
    'lifecycle': 'reuse a fixture in one installation/session; do not regenerate per event',
}


def clean(value):
    value = (value or '').strip()
    return None if value.casefold() in MISSING else value


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value, prefix):
    return prefix + hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()[:24]


def read_csv(path, required):
    raw = Path(path).read_bytes()
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError('CSV must be UTF-8 or GB18030')
    reader = csv.DictReader(io.StringIO(text, newline=''))
    fields = reader.fieldnames or []
    if len(fields) != len(set(fields)) or not required.issubset(fields):
        raise ValueError('Missing required columns or duplicate column names')
    rows = []
    for line, row in enumerate(reader, 2):
        if None in row or any(v is None for v in row.values()):
            raise ValueError(f'Malformed CSV record at row {line}')
        rows.append(row)
    return rows, {'name': Path(path).name, 'sha256': hashlib.sha256(raw).hexdigest(),
                  'encoding': encoding, 'rows': len(rows), 'columns': len(fields)}


def capacity_pair(value):
    raw = clean(value)
    if raw is None:
        return None, False
    try:
        a, b = map(float, raw.split('/'))
        valid = all(math.isfinite(x) for x in (a, b)) and 0 <= a <= b and b > 0
    except ValueError:
        a = b = None
        valid = False
    # Do not write NaN/Infinity to JSON, even in bad input observations.
    if not valid:
        return {'raw': raw, 'numerator': None, 'denominator': None, 'unit': 'unverified'}, True
    return {'raw': raw, 'numerator': a, 'denominator': b, 'unit': 'unverified'}, False


def normalize(row):
    hardware = {k: clean(row.get(v)) for k, v in HARDWARE_FIELDS.items()}
    app = {k: clean(row.get(v)) for k, v in APP_FIELDS.items()}
    runtime = {k: clean(row.get(v)) for k, v in RUNTIME_FIELDS.items()}
    flags = []
    family, release = hardware['os_family'], hardware['os_version'] or ''
    hardware['android_sdk_observed'] = None
    hardware['android_sdk_reference'] = android_sdk_reference(family, release)
    if family == 'Android' and hardware['android_sdk_reference'] is None:
        flags.append('unknown_android_release')
    if family not in ('Android', 'iOS', 'HarmonyOS'):
        flags.append('unclassified_os')
    if not all(hardware[k] for k in ('manufacturer', 'model', 'device_type', 'os_version')):
        flags.append('missing_hardware_fields')
    # SDK #manufacturer is not Android Build.BRAND; do not invent a brand alias.
    hardware['brand'] = None
    hardware['screen_unit'] = 'unverified'
    for key in ('screen_width_reported', 'screen_height_reported'):
        value = hardware[key]
        if not value or not value.isdecimal() or int(value) <= 0:
            flags.append('invalid_screen')
    sim = (hardware['simulator'] or '').casefold()
    hardware['simulator'] = {'true': True, 'false': False}.get(sim)
    if hardware['simulator'] is None:
        flags.append('unknown_simulator_status')
    app.update(version_code=None, build_sn=None, artifact_verified=False,
               evidence='analytics observation; no APK/IPA/HAP supplied')
    if not app['bundle_id'] or not app['app_version']:
        flags.append('missing_app_fields')
    if clean(row.get('versionname')) not in (None, app['app_version']):
        flags.append('app_version_alias_mismatch')
    if app['platform_id'] != app['channel_id']:
        flags.append('platform_channel_mismatch')
    for name in ('ram', 'disk'):
        pair, invalid = capacity_pair(row.get('#' + name))
        runtime[name] = pair
        if invalid:
            flags.append('invalid_' + name + '_pair')
    ram = runtime['ram']
    # Review threshold in SOURCE units, not a hardware capacity guarantee.
    if ram and ram['denominator'] is not None and ram['denominator'] > 64:
        flags.append('ram_total_above_review_threshold_64')
    return hardware, app, runtime, flags


def build_library(rows, dictionary, sources=None):
    if not rows:
        raise ValueError('No event rows')
    dictionary_map = {}
    for entry in dictionary:
        event = clean(entry.get('事件名'))
        if event:
            label = clean(entry.get('显示名')) or event
            if event in dictionary_map and dictionary_map[event] != label:
                raise ValueError('Conflicting event dictionary labels')
            dictionary_map[event] = label
    devices = defaultdict(list)
    hardware_map, app_map, joint_map = {}, {}, {}
    quality_rows, quality_devices = Counter(), defaultdict(set)
    event_counts = Counter()
    normalized = []
    times = []
    for index, row in enumerate(rows):
        device_id = clean(row.get('#device_id'))
        hardware, app, runtime, flags = normalize(row)
        try:
            event_time = datetime.fromisoformat(row['#event_time'])
            if event_time.tzinfo is not None:
                raise ValueError('Expected source wall-clock time')
            times.append(event_time)
        except (KeyError, ValueError):
            event_time = datetime.min
            flags.append('invalid_event_time')
        if device_id is None:
            flags.append('missing_device_id')
        else:
            devices[device_id].append(index)
        event_counts[clean(row.get('$part_event')) or '(missing)'] += 1
        h_id, a_id = fingerprint(hardware, 'hw-'), fingerprint(app, 'app-')
        for mapping, key, values in ((hardware_map, h_id, hardware), (app_map, a_id, app)):
            group = mapping.setdefault(key, {'id': key, **values, 'event_count': 0, '_devices': set()})
            group['event_count'] += 1
            if device_id:
                group['_devices'].add(device_id)
        normalized.append({'hardware_id': h_id, 'app_id': a_id, 'runtime': runtime,
                           '_time': event_time, '_flags': flags, '_device': device_id})

    # A rotating screen or changing OS version is not proof of an ID collision.
    stable = ('os_family', 'manufacturer', 'model', 'device_type', 'simulator')
    conflicts = set()
    changed_fields = {}
    for field in HARDWARE_FIELDS:
        changed_fields[field] = sum(len({canonical(hardware_map[normalized[i]['hardware_id']][field])
                                        for i in indices}) > 1 for indices in devices.values())
    for device, indices in devices.items():
        signatures = {tuple(str(hardware_map[normalized[i]['hardware_id']][f]).casefold()
                            for f in stable) for i in indices}
        if len(signatures) > 1:
            conflicts.add(device)
    latest = {device: max(indices, key=lambda i: (normalized[i]['_time'], i))
              for device, indices in devices.items()}
    latest_indices = set(latest.values())
    for i, entry in enumerate(normalized):
        flags = sorted(set(entry['_flags'] + (['device_id_hardware_conflict']
                          if entry['_device'] in conflicts else [])))
        quality_rows.update(flags)
        for flag in flags:
            if entry['_device']:
                quality_devices[flag].add(entry['_device'])
        # Store only latest-per-device snapshots for sampling. Counts of ALL
        # event observations remain in the hardware and app libraries.
        if i not in latest_indices:
            continue
        observation = {k: entry[k] for k in ('hardware_id', 'app_id', 'runtime')}
        observation['review_flags'] = flags
        o_id = fingerprint(observation, 'obs-')
        group = joint_map.setdefault(o_id, {'id': o_id, **observation, 'device_weight': 0})
        group['device_weight'] += 1

    def export_groups(mapping):
        result = []
        for key in sorted(mapping):
            group = dict(mapping[key])
            group['observed_device_ids'] = len(group.pop('_devices'))
            result.append(group)
        return result

    latest_rows = [rows[i] for i in latest.values()]
    latest_os = Counter(clean(r.get('#os')) or '(missing)' for r in latest_rows)
    latest_sim = Counter(clean(r.get('#simulator')) or '(missing)' for r in latest_rows)
    eligible = [o for o in joint_map.values() if not o['review_flags']
                and hardware_map[o['hardware_id']]['simulator'] is False]
    summary = {
        'event_rows': len(rows), 'device_ids': len(devices),
        'missing_device_id_rows': sum(e['_device'] is None for e in normalized),
        'device_weight_sum': sum(o['device_weight'] for o in joint_map.values()),
        'event_types': {k: {'count': n, 'display_name': dictionary_map.get(k),
                           'dictionary_matched': k in dictionary_map}
                        for k, n in sorted(event_counts.items())},
        'dictionary_events': len(dictionary_map),
        'time_range_source_wall_clock': [min(times).isoformat(), max(times).isoformat()] if times else None,
        'os_by_latest_device': dict(sorted(latest_os.items())),
        'os_by_event': dict(sorted(Counter(clean(r.get('#os')) or '(missing)' for r in rows).items())),
        'simulator_by_latest_device': dict(sorted(latest_sim.items())),
        'model_labels': len({clean(r.get('#device_model')) for r in rows}),
        'bundle_ids': len({clean(r.get('#bundle_id')) for r in rows}),
        'app_versions': len({clean(r.get('#app_version')) for r in rows}),
        'hardware_profiles': len(hardware_map), 'app_contracts': len(app_map),
        'latest_joint_observations': len(joint_map), 'hardware_conflict_device_ids': len(conflicts),
        'device_ids_with_changing_fields': changed_fields,
        'review_flags': {f: {'event_rows': n, 'device_ids': len(quality_devices[f])}
                         for f, n in sorted(quality_rows.items())},
        'default_eligible_devices': sum(o['device_weight'] for o in eligible),
        'default_eligible_observations': len(eligible),
    }
    result = {'schema_version': SCHEMA_VERSION, 'sources': sources or [], 'rules': RULES,
              'summary': summary, 'hardware': export_groups(hardware_map),
              'applications': export_groups(app_map),
              'observations': [joint_map[k] for k in sorted(joint_map)]}
    result['library_id'] = fingerprint(result, 'library-')
    return result


def sample_fixture(library, seed, *, os_family=None, include_simulators=False, allow_review=False):
    if library.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('Unsupported library schema')
    if not isinstance(seed, str) or not seed.strip():
        raise ValueError('A nonempty seed is required')
    hardware = {h['id']: h for h in library['hardware']}
    apps = {a['id']: a for a in library['applications']}
    candidates = []
    for item in library['observations']:
        h = hardware[item['hardware_id']]
        if os_family and h['os_family'] != os_family:
            continue
        if not include_simulators and h['simulator'] is not False:
            continue
        if item['review_flags'] and not allow_review:
            continue
        if type(item['device_weight']) is not int or item['device_weight'] <= 0:
            raise ValueError('Invalid device weight')
        candidates.append(item)
    if not candidates:
        raise ValueError('No eligible observations for these filters')
    candidates.sort(key=lambda x: x['id'])
    # Integer cumulative weights avoid floating point rounding and event bias.
    pick = random.Random(seed).randrange(sum(o['device_weight'] for o in candidates))
    for observation in candidates:
        pick -= observation['device_weight']
        if pick < 0:
            break
    h = deepcopy(hardware[observation['hardware_id']])
    app = deepcopy(apps[observation['app_id']])
    for obj in (h, app):
        for key in ('event_count', 'observed_device_ids'):
            obj.pop(key, None)
    return {
        'schema_version': SCHEMA_VERSION, 'local_test_only': True,
        'identity_source': 'synthetic local fixture; no production identifier reused',
        'installation_id': fingerprint([library['library_id'], observation['id'], seed], 'test-install-'),
        'library_id': library['library_id'],
        'observation_id': observation['id'], 'hardware': h, 'application': app,
        'runtime': deepcopy(observation['runtime']),
        'review_flags': list(observation['review_flags']),
        'sampling': {'weight': 'latest_device', 'os_family': os_family,
                     'include_simulators': include_simulators, 'allow_review': allow_review},
    }


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    build = sub.add_parser('build')
    build.add_argument('--events', required=True, type=Path)
    build.add_argument('--dictionary', required=True, type=Path)
    build.add_argument('--output', required=True, type=Path, help='New output directory')
    sample = sub.add_parser('sample')
    sample.add_argument('--library', required=True, type=Path)
    sample.add_argument('--seed', required=True)
    sample.add_argument('--os', dest='os_family')
    sample.add_argument('--include-simulators', action='store_true')
    sample.add_argument('--allow-review', action='store_true')
    sample.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'build':
            rows, source = read_csv(args.events, REQUIRED)
            names, dictionary_source = read_csv(args.dictionary, {'事件名', '显示名'})
            library = build_library(rows, names, [source, dictionary_source])
            args.output.mkdir(parents=True, exist_ok=False)
            write_json(args.output / 'device-library.json', library)
            write_json(args.output / 'summary.json', library['summary'])
            print(json.dumps(library['summary'], ensure_ascii=False, indent=2))
        else:
            library = json.loads(args.library.read_text(encoding='utf-8'))
            fixture = sample_fixture(library, args.seed, os_family=args.os_family,
                                     include_simulators=args.include_simulators, allow_review=args.allow_review)
            if args.output:
                if args.output.exists():
                    raise ValueError('Output exists; choose a new file')
                write_json(args.output, fixture)
            else:
                print(json.dumps(fixture, ensure_ascii=False, indent=2))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
