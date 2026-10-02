"""Passive software registration and existing Conda inventory path metadata.

No configuration imports, shell startup, Conda invocation, scanning, healthcheck
or registration writes. Inventory only locates an already successful registration;
it cannot create one or resolve duplicate environment names by guesswork.
"""
import hashlib
import json
import os
import re
import time
from pathlib import Path


def registry_path(install_root, explicit=None, *, environment=None):
    environment = os.environ if environment is None else environment
    if explicit or environment.get('BIOMIND_PI_REGISTRY_PATH'):
        return Path(explicit or environment['BIOMIND_PI_REGISTRY_PATH']).expanduser()
    root = environment.get('BIO_SOFTWARE_ROOT') or install_root
    return Path(root).expanduser() / 'software_registry.json'


class RegistryView:
    def __init__(self, path, environment_inventory=None):
        self.path = Path(path) if path else None
        self.environment_inventory = (Path(environment_inventory).expanduser()
                                      if environment_inventory else Path.home()/'.conda/environments.txt')
        self.reload()

    def reload(self):
        self.registry = {'environments': {}, 'tools': {}}
        self.initialized = False
        self.read_error = None
        def failed(code, detail):
            self.read_error = {'reason_code': code, 'registry_path': str(self.path or ''),
                               'source_type': 'software_registry', 'detail': detail}
            return self
        if self.path is None:
            return failed('REGISTRY_UNINITIALIZED', 'No registry path resolved')
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return failed('REGISTRY_UNINITIALIZED', 'Registry does not exist')
        except (OSError, UnicodeError) as exc:
            return failed('REGISTRY_UNREADABLE', str(exc))
        except (ValueError, TypeError) as exc:
            return failed('REGISTRY_CORRUPT', str(exc))
        if not isinstance(data, dict) or data.get('schema_version', 1) != 1:
            return failed('REGISTRY_SCHEMA_INCOMPATIBLE', 'Unsupported registry object/schema_version')
        for section in ('environments', 'tools'):
            records = data.get(section, {})
            if not isinstance(records, dict) or any(not isinstance(k, str) or not isinstance(v, dict)
                                                   for k, v in records.items()):
                return failed('REGISTRY_SCHEMA_INCOMPATIBLE', section)
            for name, record in records.items():
                for field in ('entries', 'versions', 'runtime_env', 'capability_evidence', 'resolved_runtime_paths'):
                    if field in record and not isinstance(record[field], dict):
                        return failed('REGISTRY_SCHEMA_INCOMPATIBLE', f'{section}.{name}.{field}')
                if any(not isinstance(value, dict) for value in record.get('capability_evidence', {}).values()):
                    return failed('REGISTRY_SCHEMA_INCOMPATIBLE', f'{section}.{name}.capability_evidence')
                declared = record.get('env') if section == 'environments' else record.get('name')
                if declared and declared != name:
                    return failed('REGISTRY_IDENTITY_CONFLICT', f'{section}.{name}')
                if section == 'tools' and record.get('env') and record.get('env_name') and record['env'] != record['env_name']:
                    return failed('REGISTRY_IDENTITY_CONFLICT', f'tools.{name}.env_name')
        self.registry = data
        self.initialized = True
        return self

    def _known_environment_prefixes(self):
        found = {}
        try:
            lines = self.environment_inventory.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeError):
            lines = []
        for raw in lines:
            path = Path(raw.strip())
            if not path.is_absolute():
                continue
            # Inventory is a location hint; reading it is not a filesystem scan
            # or proof that an environment still exists.
            value = str(path)
            if value not in found.setdefault(path.name, []):
                found[path.name].append(value)
        return found

    def _registered_prefixes(self, name, row, known=None):
        explicit = (row.get('prefix') or row.get('path')
                    or (row.get('resolved_runtime_paths') or {}).get('RESOLVED_ENV_PREFIX'))
        if explicit:
            path = Path(str(explicit))
            return [str(path.resolve())] if path.is_absolute() else []
        return (known if known is not None else self._known_environment_prefixes()).get(name, [])

    def locate_candidates(self, requirements=None, *, limit=3):
        """Bounded fallback metadata, never a successful software registration.

        Only exact explicitly named prefixes or names in the static inventory are
        considered. No recursive scan, module command, imports or shell runs.
        """
        requirements = requirements or {}
        limit = max(1, min(3, int(limit)))
        deadline = time.monotonic() + 15
        wanted_env = str(requirements.get('env') or requirements.get('default_env') or '')
        explicit_prefix = str(requirements.get('prefix') or '')
        tools = requirements.get('tools') or []
        tools = [tools] if isinstance(tools, str) else tools
        tools = [str(tool) for tool in tools if re.fullmatch(r'[A-Za-z0-9_.+-]+', str(tool))][:3]
        known = {} if explicit_prefix else self._known_environment_prefixes()
        locations = [(wanted_env or Path(explicit_prefix).name, explicit_prefix)] if explicit_prefix else [
            (name, prefix) for name, prefixes in known.items() if not wanted_env or name == wanted_env
            for prefix in prefixes]
        rows = []
        for name, prefix_text in locations:
            if len(rows) >= limit or time.monotonic() >= deadline:
                break
            prefix = Path(prefix_text)
            if not prefix.is_absolute():
                continue
            # These are candidate path strings, not existence/health evidence.
            # Explicit entry diagnostics use the existing bounded probe path.
            entry_candidates = {tool: str(prefix / 'bin' / tool) for tool in tools}
            rows.append({'env': name, 'prefix': str(prefix), 'entry_candidates': entry_candidates,
                         'availability': 'UNKNOWN', 'reason_code': 'RUNTIME_CANDIDATE_UNVERIFIED',
                         'source_type': 'explicit_location' if explicit_prefix else 'known_environment_inventory',
                         'registry_path': str(self.path or ''), 'inspection_status': 'not_run',
                         'healthcheck_status': 'not_run', 'install_required': False})
        return {'status': 'location_hints' if rows else 'not_found', 'candidates': rows,
                'stages': [{'stage': 'static_location_metadata', 'status': 'completed',
                            'candidate_count': len(rows), 'execution_count': 0,
                            'entry_stat_count': 0}],
                'registry_path': str(self.path or ''), 'install_required': False,
                'inspection_status': 'not_run', 'healthcheck_status': 'not_run'}


def registered_scientific_tool(path, environment_inventory=None):
    """Use the product selection predicate; retain only nonsecret plan metadata."""
    from agent_core.registered_runtime import registered_runtime_reuse
    view = RegistryView(path, environment_inventory)
    for name in ('bcftools', 'samtools'):
        for env_name in view.registry.get('environments') or {}:
            result = registered_runtime_reuse(view, {'tools': [name]}, skill_record={}, target_env=env_name)
            if result.get('availability') != 'AVAILABLE':
                continue
            candidate = result['selected_candidate']
            binary = Path(candidate['prefix']) / 'bin' / name
            if not binary.is_file() or not os.access(binary, os.X_OK):
                continue
            record = view.registry['environments'][env_name]
            return {'name': name, 'env_name': env_name, 'prefix': candidate['prefix'],
                    'executable': str(binary.resolve()),
                    'registry_sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                    'registration_status': record.get('status'),
                    'last_verified_at': record.get('last_verified_at', ''),
                    'healthcheck_status': candidate['audit']['healthcheck_status'],
                    'source_ref': candidate['source_ref'],
                    'prefix_source': ('registry' if any(record.get(k) for k in
                        ('prefix', 'path', 'resolved_runtime_paths')) else 'existing_environment_inventory')}
    return None


# Fixed, isolated stdlib metadata program. Tool/user strings are JSON data, never
# shell or Python source. Neither phase executes an environment/tool candidate.
_LOCATION_METADATA_PROGRAM = r'''
import json, os, re, stat, sys
from pathlib import Path
p = json.loads(sys.argv[1])
if p['stage'] == 'inventory':
    rows = []
    if p.get('prefix'):
        candidates = [p['prefix']]
    else:
        try:
            with open(p['inventory'], encoding='utf-8') as stream:
                text = stream.read(262145)
            if len(text) > 262144:
                raise ValueError('INVENTORY_METADATA_LIMIT')
            candidates = text.splitlines()
        except FileNotFoundError:
            candidates = []
    for raw in candidates:
        value = raw.strip()
        if not os.path.isabs(value):
            continue
        value = os.path.normpath(value)
        name = os.path.basename(value)
        if p.get('env') and name != p['env']:
            continue
        if not any(row['prefix'] == value for row in rows):
            rows.append({'env': p.get('env') or name, 'prefix': value})
        if len(rows) == 3:
            break
    result = {'candidates': rows}
else:
    prefix = os.path.realpath(p['prefix'])
    def covered(path):
        if any(path == root or path.startswith(root.rstrip('/') + '/') for root in p.get('denied_roots', [])):
            return False
        private = re.compile(p.get('private_pattern') or r'(?!)')
        if any(private.fullmatch(part.lower()) or part.lower() in {'auth.json', 'privatekeys', '.biomind'} for part in Path(path).parts):
            return False
        return any(path == root or path.startswith(root.rstrip('/') + '/') for root in p.get('allowed_roots', []))
    if p['protected'] and not covered(prefix):
        raise PermissionError('LOCATION_CANONICAL_PREFIX_PERMISSION_REQUIRED')
    result = {'prefix': p['prefix'], 'resolved_prefix': prefix, 'entries': {}, 'missing_entries': []}
    for tool in p['tools']:
        candidate = os.path.join(prefix, 'bin', tool)
        resolved = os.path.realpath(candidate)
        if p['protected'] and not covered(resolved):
            raise PermissionError('LOCATION_CANONICAL_ENTRY_PERMISSION_REQUIRED')
        try:
            info = os.stat(candidate)
            if stat.S_ISREG(info.st_mode) and os.access(candidate, os.X_OK):
                result['entries'][tool] = {'path': candidate, 'resolved_path': resolved,
                    'evidence_kind': 'executable_path_metadata', 'executed': False}
            else:
                result['missing_entries'].append(tool)
        except FileNotFoundError:
            result['missing_entries'].append(tool)
    result['prefix_exists'] = os.path.isdir(prefix)
encoded = json.dumps(result, ensure_ascii=False)
if len(encoded.encode()) > 4096:
    raise ValueError('LOCATION_STAGE_SUMMARY_LIMIT')
print(encoded)
'''


def _location_read_roots(service, prefix):
    """Reuse protected file/runtime authority; registration is never a grant."""
    if service.execution_profile == 'native':
        return []
    from agent_core.command_execution import check_data_path
    from agent_core.execution_permissions import path_grants, PermissionDenied
    target = Path(prefix).absolute()
    facts = check_data_path(service.commands, service.task_id, target)
    roots = {facts['project'], *facts['runtime']}
    canonical = target.resolve()
    if not any(canonical == root or canonical.is_relative_to(root) for root in roots):
        grants = path_grants(service.commands, service.task_id, canonical, 'read')
        roots.update(Path(grant['root']) for grant in grants
                     if grant.get('target_kind') == 'directory' and grant.get('recursive') is True)
        if not any(canonical == root or canonical.is_relative_to(root) for root in roots):
            raise PermissionDenied('LOCATION_PREFIX_PERMISSION_REQUIRED')
    return sorted(str(root) for root in roots)


async def bounded_location_lookup(service, requirements):
    """Bounded metadata fallback through the existing executor process owner.

    Permission/owner validation retains its existing synchronous persistence
    semantics; potentially slow candidate metadata runs in owned child groups.
    Five seconds bounds each stage, fifteen bounds discovery work; executor
    cleanup can extend wall time and is reported from observed cleanup facts.
    """
    import sys
    from execution.executor import _run_native_command
    requirements = dict(requirements or {})
    service._ensure_request()
    request_scope = service.scope()
    started = time.monotonic()
    deadline = started + 15.0
    source = {'registry_path': str(service.registry.path or ''), 'source_type': 'software_registry'}
    stages, candidates = [], []
    requested_prefix = str(requirements.get('prefix') or '')
    requested_env = str(requirements.get('env') or requirements.get('default_env') or '')
    tools = requirements.get('tools') or []
    tools = [tools] if isinstance(tools, str) else tools
    if not isinstance(tools, (list, tuple)) or any(not re.fullmatch(r'[A-Za-z0-9_.+-]+', str(tool)) or str(tool) in {'.', '..'} for tool in tools):
        return {'status': 'lookup_failed', 'error': 'LOCATION_TOOL_NAMES_INVALID', **source, 'candidates': [], 'stages': []}
    tools = list(dict.fromkeys(str(tool) for tool in tools))[:3]
    if requested_prefix and (not Path(requested_prefix).is_absolute() or '\x00' in requested_prefix):
        return {'status': 'lookup_failed', 'error': 'LOCATION_PREFIX_ABSOLUTE_REQUIRED', **source, 'candidates': [], 'stages': []}

    def check_intent():
        service._ensure_request()
        if service.scope() != request_scope:
            raise ValueError('LOCATION_REQUEST_CHANGED')

    async def stage(name, payload):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stages.append({'stage': name, 'status': 'budget_exhausted', 'execution_started': False})
            return None
        observation = {}
        began = time.monotonic()
        command = [sys.executable, '-I', '-B', '-c', _LOCATION_METADATA_PROGRAM,
                   json.dumps(payload, ensure_ascii=False)]
        stdout, stderr, code = await _run_native_command(command, check_intent=check_intent,
            timeout=min(5.0, remaining), original_deadline=deadline, short_check=True,
            cwd=str(service.cwd), profile=False, display_streams=False, max_output_bytes=8192,
            runtime_observation=observation,
            env={'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'})
        row = {'stage': name, 'status': 'completed' if code == 0 else 'timed_out' if code == 124 else 'failed',
               'elapsed_seconds': round(time.monotonic() - began, 4), 'exit_code': code,
               'execution_started': bool(observation.get('pid')), 'pid': observation.get('pid'),
               'cleanup': 'confirmed' if observation.get('cleanup_confirmed') and observation.get('process_reaped') else 'unknown',
               'process_reaped': observation.get('process_reaped'), 'candidate_tools_executed': 0}
        if code != 0:
            row['detail'] = stderr[-1024:]
        stages.append(row)
        if code != 0:
            return None
        try:
            return json.loads(stdout)
        except (ValueError, TypeError):
            row.update(status='failed', detail='LOCATION_METADATA_OUTPUT_INVALID')
            return None

    # An explicit protected path is checked before the subprocess can observe it.
    if requested_prefix:
        try:
            _location_read_roots(service, requested_prefix)
        except (OSError, ValueError) as exc:
            return {'status': 'permission_required', 'error': str(exc), **source,
                    'candidates': [], 'stages': [], 'healthcheck_status': 'not_run', 'install_required': False}
    inventory = await stage('static_inventory', {'stage': 'inventory', 'prefix': requested_prefix,
        'env': requested_env, 'inventory': str(service.registry.environment_inventory)})
    if inventory is not None:
        for location in inventory.get('candidates', [])[:3]:
            if time.monotonic() >= deadline:
                stages.append({'stage': 'exact_entry_metadata', 'status': 'budget_exhausted', 'execution_started': False})
                break
            prefix = location['prefix']
            row = {**location, 'availability': 'UNKNOWN', 'reason_code': 'RUNTIME_CANDIDATE_UNVERIFIED',
                   'source_type': 'explicit_location' if requested_prefix else 'known_environment_inventory',
                   'inspection_status': 'not_run', 'healthcheck_status': 'not_run', 'install_required': False}
            try:
                roots = _location_read_roots(service, prefix)
            except (OSError, ValueError) as exc:
                row.update(metadata_status='permission_required', detail=str(exc))
                candidates.append(row)
                continue
            masks = {}
            if service.execution_profile != 'native':
                from agent_core.command_execution import _PRIVATE_PATTERN
                masks = {'private_pattern': _PRIVATE_PATTERN.pattern,
                         'denied_roots': [str(Path(value).absolute()) for value in
                             (*service.commands.deny_paths, service.data_root, service.cwd/'.biomind', service.cwd/'.git')]}
            facts = await stage('exact_entry_metadata', {'stage': 'entries', 'prefix': prefix,
                                'tools': tools, 'protected': service.execution_profile != 'native',
                                'allowed_roots': roots, **masks})
            row.update(metadata_status='completed' if facts is not None else 'incomplete')
            if facts is not None:
                row['path_metadata'] = facts
            candidates.append(row)
    incomplete = any(row['status'] not in {'completed'} for row in stages)
    result = {'status': 'incomplete' if incomplete else 'location_hints' if candidates else 'not_found',
              'candidates': candidates, 'stages': stages, 'elapsed_seconds': round(time.monotonic() - started, 4),
              'budget': {'max_candidates': 3, 'item_seconds': 5, 'total_seconds': 15, 'summary_bytes': 8192},
              'healthcheck_status': 'not_run', 'inspection_status': 'not_run', 'install_required': False, **source}
    if len(json.dumps(result, ensure_ascii=False).encode()) > 8192:
        result.update(status='incomplete', reason='LOOKUP_SUMMARY_LIMIT', candidates=[])
        result['stages'] = [{key: value for key, value in row.items() if key != 'detail'} for row in stages]
    return result
