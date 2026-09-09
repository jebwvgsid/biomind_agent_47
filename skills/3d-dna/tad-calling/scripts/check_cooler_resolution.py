"""Read-only Cooler metadata query; never reads pixel arrays or balances inputs.

Resource paths and observation identity must be supplied by the approved action.
This program grants no path authority; Execute admission remains responsible for it.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

VERSION = '1.0.0'


class UnsupportedLink(ValueError):
    pass


class GroupMetadata(list):
    root_resolution = None


def event(stage, **values):
    print('BIOMIND_CHECK_PROGRESS=' + json.dumps({
        'stage': stage, 'observed_at': datetime.now(timezone.utc).isoformat(),
        **values}, ensure_ascii=False), file=sys.stderr, flush=True)


def query_groups(path):
    """One read-only open, local hard links only; metadata, not matrix validity."""
    import h5py
    from cooler.fileops import _is_cooler
    groups = GroupMetadata()
    required = {'chroms': ('name', 'length'), 'bins': ('chrom', 'start', 'end'),
                'pixels': ('bin1_id', 'bin2_id', 'count'),
                'indexes': ('chrom_offset', 'bin1_offset')}
    try:
        opened = h5py.File(path, 'r')
    except OSError as exc:
        if 'file signature not found' in str(exc).lower():
            raise ValueError('HDF5_SIGNATURE_INVALID') from exc
        raise
    with opened as root:
        seen = set()
        def scan(group):
            address = h5py.h5o.get_info(group.id).addr
            if address in seen:
                return
            seen.add(address)
            # Reject links before any library predicate can dereference them.
            for name in group:
                if not isinstance(group.get(name, getlink=True), h5py.HardLink):
                    raise UnsupportedLink('UNSUPPORTED_HDF5_LINK: external/soft links are not followed')
            if _is_cooler(group):
                for name, columns in required.items():
                    if name not in group or not isinstance(group[name], h5py.Group):
                        raise ValueError('INVALID_COOLER_STRUCTURE')
                    child = group[name]
                    for column in columns:
                        if column not in child or not isinstance(child.get(column, getlink=True), h5py.HardLink):
                            raise ValueError('INVALID_COOLER_STRUCTURE')
                        if not isinstance(child[column], h5py.Dataset):
                            raise ValueError('INVALID_COOLER_STRUCTURE')
                groups.append(group.name)
                if group.name == '/':
                    groups.root_resolution = group.attrs.get('bin-size')
            for name in group:
                child = group[name]
                if isinstance(child, h5py.Group):
                    scan(child)
        scan(root)
    if not groups:
        raise ValueError('NO_VALID_COOLER_GROUPS')
    groups.sort()
    return groups


def check(samples, resolution, *, query=query_groups):
    if type(resolution) is not int or resolution <= 0:
        raise ValueError('resolution must be a positive integer')
    target = f'/resolutions/{resolution}'
    cache = {}
    rows = []
    for handle, path in samples:
        key = str(Path(path).resolve())
        if key not in cache:
            started = time.monotonic()
            event('query_started', resource=handle)
            row = {'target_group': target, 'groups': [], 'present': None,
                   'scope': 'cooler_structure_and_group_metadata_only', 'checker_version': VERSION}
            try:
                groups = query(path)
                normalized = ['/' + p.lstrip('/') for p in groups]
                root_resolution = getattr(groups, 'root_resolution', None)
                effective_target = '/' if '/' in normalized and root_resolution == resolution else target
                present = effective_target in normalized
                row.update(groups=normalized, target_group=effective_target, present=present,
                           status='present' if present else 'absent')
                if normalized == ['/'] and (root_resolution is None or str(root_resolution) == 'null'):
                    row.update(present=None, status='resolution_undefined')
                event('predicate_evaluated', resource=handle, present=row['present'])
            except FileNotFoundError as exc:
                row.update(status='file_missing', error=str(exc))
            except PermissionError as exc:
                row.update(status='access_denied', error=str(exc))
            except TimeoutError as exc:
                row.update(status='query_timeout', error=str(exc))
            except UnsupportedLink as exc:
                row.update(status='link_not_supported', error=str(exc))
            except ValueError as exc:
                row.update(status='format_error', error=str(exc))
            except OSError as exc:
                # HDF5 open errors do not prove corruption or absence.
                row.update(status='io_error_unknown', error=str(exc))
            row['query_seconds'] = time.monotonic() - started
            cache[key] = row
            event('query_completed', resource=handle, status=row['status'], elapsed_seconds=row['query_seconds'])
        rows.append({'resource_handle': handle, **cache[key]})
    return {'checker_version': VERSION, 'resolution_bp': resolution,
            'ok': all(row['status'] == 'present' for row in rows), 'inputs': rows,
            'query_count': len(cache), 'scientific_validation': 'NOT_PERFORMED'}


def main(argv=None):
    event('python_started')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', nargs=2, action='append', required=True, metavar=('HANDLE', 'PATH'))
    parser.add_argument('--resolution-bp', type=int, required=True)
    parser.add_argument('--observation', required=True, help='Exact declared observation handle from the current action contract')
    args = parser.parse_args(argv)
    if len(args.input) > 64 or len({x[0] for x in args.input}) != len(args.input):
        parser.error('requires at most 64 uniquely named approved input handles')
    started = time.monotonic()
    import h5py  # noqa: F401
    import cooler  # noqa: F401
    event('imports_completed', elapsed_seconds=time.monotonic() - started)
    result = check(args.input, args.resolution_bp)
    # Use the existing emitter. This source location is relative to the registered
    # Skill installation, never a guessed checkout name or an input directory.
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from agent_core.step_effects import emit_observation
    print(json.dumps(result, ensure_ascii=False), flush=True)
    print(emit_observation(args.observation, result), flush=True)
    event('observation_emitted', ok=result['ok'])
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
