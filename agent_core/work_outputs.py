"""Consume the current Plan's existing output declarations, without an alias store."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import re


class WorkOutputError(ValueError):
    def __init__(self, code, reference='', **details):
        self.issue = {'code': code, 'reference': reference, **details}
        super().__init__(code + ': ' + str(reference or details.get('path') or details.get('field_path') or '')[:1200])


def plan_identity(state):
    """Stable across CAS/status updates; distinct across explicit Plan revisions."""
    keys = ('description', 'dependencies', 'inputs', 'parameters', 'outputs', 'runtime', 'validation')
    payload = {'task_id': state.get('task_id'), 'plan_id': state.get('plan_id'),
        'plan_revision': state.get('plan_revision'), 'plan_version': state.get('plan_version'),
        'document': state.get('plan_document') or {},
        'steps': [{k: r.get(k) for k in ('step_id', *keys)} for r in state.get('steps') or []]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def plan_parameters(state):
    document = state.get('plan_document') or {}
    values = dict(document.get('parameters') or {})
    spec = (state.get('current_task') or {}).get('task_spec') or {}
    for row in spec.get('parameters') or []:
        if isinstance(row, dict) and row.get('name'):
            values.setdefault(row['name'], row.get('value'))
    return values


def declared_samples(state):
    from agent_core.input_manifest import manifest_view
    from agent_core.work_validation import explicit_input_rows
    view=manifest_view(state)
    explicit=list(dict.fromkeys(r['sample'] for r in explicit_input_rows(state)))
    values = plan_parameters(state).get('samples') or (state.get('plan_document') or {}).get('samples') or []
    if not isinstance(values, list):
        raise WorkOutputError('OUTPUT_SAMPLE_SET_INVALID')
    names = [str(r.get('sample') or r.get('name') or '') if isinstance(r, dict) else str(r) for r in values]
    chosen=view['samples'] if view else explicit or names
    if names and set(names)!=set(chosen) or view and explicit and set(explicit)!=set(chosen):
        raise WorkOutputError('INPUT_SAMPLE_SET_CONFLICT',field_path='$.inputs',value=chosen,reason='input sources disagree with repeated sample declarations')
    if len(names) != len(set(names)) or any(not s or s in {'.', '..'} for s in chosen):
        raise WorkOutputError('OUTPUT_SAMPLE_SET_INVALID')
    return chosen


def _spec(state):
    from agent_core.plan_tracker import canonical_task_spec_for_state
    return canonical_task_spec_for_state(state) or {}


def _location(row):
    return row.get('canonical_path') or row.get('path') or row.get('filename') or ''


def _absolute(state, value, project_dir):
    project = Path(project_dir).absolute()
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.absolute()
    root = next((_location(r) for r in _spec(state).get('output_contracts') or []
                 if isinstance(r, dict) and (r.get('artifact_kind') == 'directory' or r.get('role') == 'output_dir') and _location(r)), '')
    base = project
    if root:
        output_root = (project / root).absolute()
        if not (project / path).absolute().is_relative_to(output_root):
            base = output_root
    # Normalize .. lexically; authorization is checked again on the resolved target.
    import os
    return Path(os.path.abspath(base / path))


def authorized_output(state, target, project_dir):
    """Project outputs plus explicitly authorized external TaskSpec locations."""
    from agent_core.workspace_files import _PRIVATE_PATTERN
    lexical, resolved = Path(target).absolute(), Path(target).resolve()
    if any(_PRIVATE_PATTERN.fullmatch(part.lower()) or part in {'.biomind','.git','task_outputs'} for candidate in (lexical,resolved) for part in candidate.parts):
        raise WorkOutputError('OUTPUT_PATH_NOT_AUTHORIZED', str(target), reason='private path')
    project = Path(project_dir).resolve()
    allowed_roots = [project]
    exact = []
    for row in _spec(state).get('output_contracts') or []:
        if not isinstance(row, dict) or not _location(row):
            continue
        p = (Path(project_dir) / _location(row)).absolute()
        if row.get('source') in {'user', 'user_explicit', 'user_confirmed'} or p.is_relative_to(project):
            if row.get('artifact_kind') == 'directory' or row.get('role') == 'output_dir':
                allowed_roots.append(p)
            else:
                exact.append(p)
    # A new symlink cannot confer authority on an otherwise unapproved target.
    lexical_ok = lexical in exact or any(lexical.is_relative_to(r) for r in allowed_roots)
    resolved_ok = resolved in exact or any(resolved.is_relative_to(r) for r in allowed_roots)
    if not lexical_ok or not resolved_ok:
        raise WorkOutputError('OUTPUT_PATH_NOT_AUTHORIZED', str(target), resolved_path=str(resolved))
    return resolved


def materialize_outputs(state, rows, project_dir):
    """Exact references from path_bindings/output_contracts; explicit paths stay paths.

    artifact_ref is explicit. Legacy strings become references only on an exact
    current declaration match. Bare filenames (including extensionless) are legal.
    """
    document = state.get('plan_document') or {}
    bindings = document.get('path_bindings') or {}
    contracts = list(document.get('output_contracts') or []) + list(_spec(state).get('output_contracts') or [])
    def contract_path(contract):
        value = _location(contract)
        if not value: return ''
        if contract.get('root_ref') and not Path(value).is_absolute():
            roots = {_location(c) for c in contracts if isinstance(c,dict) and str(c.get('artifact_id') or c.get('output_id') or '') == contract['root_ref'] and _location(c)}
            if contract['root_ref'] in bindings: roots.add(bindings[contract['root_ref']])
            if len(roots) != 1:
                raise WorkOutputError('OUTPUT_MAPPING_CONFLICT', contract['root_ref'], targets=sorted(roots),
                    candidate_paths=[str(_absolute(state, str(Path(root) / value), project_dir)) for root in sorted(roots)])
            value = str(Path(roots.pop()) / value)
        return str(_absolute(state, value, project_dir))
    result = []
    for original in rows or []:
        row = deepcopy(original) if isinstance(original, dict) else {}
        ref = str(row.get('artifact_ref') or row.get('original_reference') or (original if isinstance(original, str) else row.get('artifact_id') or ''))
        explicit = bool(row.get('artifact_ref'))
        candidates = []
        declared_meta = {}
        if ref in bindings:
            value = bindings[ref]
            if isinstance(value, dict): value = _location(value)
            if not isinstance(value, str) or not value:
                raise WorkOutputError('OUTPUT_MAPPING_CONFLICT', ref, mapping=value)
            candidates.append(value)
        for contract in contracts:
            if isinstance(contract, dict) and str(contract.get('artifact_id') or contract.get('output_id') or '') == ref:
                if _location(contract): candidates.append(contract_path(contract))
                declared_meta.update(contract)
        raw_path = _location(row)
        # Already materialized rows are re-resolved from their original reference,
        # so a mapping-only Plan edit cannot silently retain their former target.
        if not candidates:
            if explicit or (row.get('original_reference') and row.get('resolved_from_reference')):
                raise WorkOutputError('OUTPUT_MAPPING_MISSING', ref)
            raw_path = raw_path or (original if isinstance(original, str) else '')
            if not raw_path:
                raise WorkOutputError('OUTPUT_MAPPING_MISSING', ref)
            candidates = [str(raw_path)]
        normalized = {str(_absolute(state, value, project_dir)) for value in candidates}
        if len(normalized) != 1:
            raise WorkOutputError('OUTPUT_MAPPING_CONFLICT', ref, targets=sorted(normalized))
        path = normalized.pop()
        literal_sources=[*contracts,*(item for step in state.get('steps') or [] for item in step.get('outputs') or [])]
        literal = row.get('literal') is True or any(isinstance(c,dict) and (c.get('literal') is True or c.get('source') in {'user','user_explicit','user_confirmed'}) and _location(c) and str(_absolute(state,_location(c),project_dir))==path for c in literal_sources)
        samples = declared_samples(state) if '{sample}' in path and not literal else ['']
        if not samples or (not literal and re.search(r'\{(?!sample\})[^}]*\}', path)):
            raise WorkOutputError('OUTPUT_TEMPLATE_UNRESOLVED', ref, path=path,
                value=deepcopy(original), unresolved_tokens=re.findall(r'\{[^}]*\}', path),
                approved_root=str(_absolute(state, '.', project_dir)),
                alternative='Use the confirmed concrete output path or an exact declared reference; only {sample} is expanded. Mark genuinely literal paths literal=true.')
        for sample in samples:
            if sample and (not re.fullmatch(r'[\w.-]+',sample) or sample in {'.','..'}):
                raise WorkOutputError('OUTPUT_SAMPLE_PATH_UNSAFE',sample,reason='sample labels used in output paths must be safe single components')
            selected = path if literal else path.replace('{sample}', sample)
            if not literal and ('{' in selected or '}' in selected):
                raise WorkOutputError('OUTPUT_TEMPLATE_UNRESOLVED', ref, path=path,
                value=deepcopy(original), unresolved_tokens=re.findall(r'\{[^}]*\}', path),
                approved_root=str(_absolute(state, '.', project_dir)),
                alternative='Use the confirmed concrete output path or an exact declared reference; only {sample} is expanded. Mark genuinely literal paths literal=true.')
            authorized_output(state, selected, project_dir)
            exact_meta = dict(declared_meta)
            # Only formal exact expanded paths may contribute content constraints.
            for contract in contracts:
                if not isinstance(contract,dict) or not _location(contract): continue
                target = contract_path(contract)
                target_samples = declared_samples(state) if '{sample}' in target and not (literal and target==path) else ['']
                if selected not in {target if literal and target==path else target.replace('{sample}', s) for s in target_samples}: continue
                for key in ('role','format','validation','artifact_kind'):
                    if key not in contract: continue
                    for existing in (exact_meta,row):
                        if key in existing and existing[key] != contract[key]:
                            raise WorkOutputError('OUTPUT_CONTRACT_CONFLICT', selected, field=key, values=[existing[key],contract[key]])
                    exact_meta[key] = deepcopy(contract[key])
            materialized = {**exact_meta, **row, 'canonical_path': selected,
                'original_reference': ref or str(raw_path),
                'resolved_from_reference': bool(ref in bindings or any(str(c.get('artifact_id') or c.get('output_id') or '') == ref for c in contracts if isinstance(c, dict)))}
            materialized.pop('path', None)
            materialized.pop('artifact_ref', None)
            if sample: materialized['sample'] = sample
            # Re-expanding an already materialized member must not duplicate others.
            if row.get('sample') and row['sample'] != sample: continue
            result.append(materialized)
    unique = {}
    for row in result:
        key = (row['canonical_path'], row.get('original_reference'))
        unique[key] = row
    return list(unique.values())


def materialize_plan_outputs(state, rows, project_dir, *, diagnostics=None, field_path='$.outputs'):
    """Admission projection only; unresolved declarations never become paths.

    Execution and verification continue to call materialize_outputs directly.
    The one host policy switch restores the former strict admission behavior.
    """
    from agent_core.plan_contract import plan_admission_policy_enabled
    if plan_admission_policy_enabled():
        return materialize_outputs(state, rows, project_dir)
    result = []
    advisory = {'OUTPUT_TEMPLATE_UNRESOLVED', 'OUTPUT_MAPPING_MISSING',
                'OUTPUT_MAPPING_CONFLICT', 'OUTPUT_CONTRACT_CONFLICT',
                'INPUT_MAPPING_CONFLICT', 'INPUT_SAMPLE_SET_CONFLICT'}
    def check_known_target(value, *, literal=False):
        path = str(_absolute(state, value, project_dir))
        samples = ['']
        if '{sample}' in path and not literal:
            try:
                samples = declared_samples(state) or ['']
            except WorkOutputError as exc:
                if exc.issue.get('code') not in advisory:
                    raise
                # An unresolved relation cannot grant an external exact path.
                # The remaining literal path must already lie in allowed roots.
        for sample in samples:
            if sample and (not re.fullmatch(r'[\w.-]+', sample) or sample in {'.','..'}):
                raise WorkOutputError('OUTPUT_SAMPLE_PATH_UNSAFE', sample,
                    reason='sample labels used in output paths must be safe single components')
            selected = path.replace('{sample}', sample) if sample else path
            authorized_output(state, selected, project_dir)
    # Keep the original whole-array projection, including its deduplication and
    # ordering, byte-for-byte for every declaration that already resolves.
    try:
        return materialize_outputs(state, rows, project_dir)
    except WorkOutputError as exc:
        if exc.issue.get('code') not in advisory:
            raise
    for index, original in enumerate(rows or []):
        try:
            result.extend(materialize_outputs(state, [original], project_dir))
        except WorkOutputError as exc:
            if exc.issue.get('code') not in advisory:
                raise
            # A missing/conflicting reference may have prevented materialization
            # before an explicit fallback path was examined. It grants no bypass.
            value = _location(original) if isinstance(original, dict) else original if isinstance(original, str) else ''
            if value:
                check_known_target(value, literal=isinstance(original, dict) and original.get('literal') is True)
            if exc.issue.get('code') == 'OUTPUT_MAPPING_CONFLICT':
                for target in exc.issue.get('candidate_paths', exc.issue.get('targets') or []):
                    check_known_target(target)
            elif exc.issue.get('code') == 'OUTPUT_TEMPLATE_UNRESOLVED' and exc.issue.get('path'):
                check_known_target(exc.issue['path'])
            elif exc.issue.get('code') == 'OUTPUT_CONTRACT_CONFLICT':
                check_known_target(exc.issue['reference'])
            if exc.issue.get('code') == 'OUTPUT_TEMPLATE_UNRESOLVED' and any(
                    token != '{sample}' for token in exc.issue.get('unresolved_tokens', [])):
                exc.issue['field_path'] = f'{field_path}[{index}]'
                raise
            if diagnostics is not None:
                diagnostics.append({**deepcopy(exc.issue), 'field_path':f'{field_path}[{index}]',
                    'value':deepcopy(original), 'blocking':False,
                    'scope':'plan_admission_only', 'resolution':'retained_unresolved; execution remains strict'})
            result.append(deepcopy(original))
    return result


def step_outputs(state, project_dir, step=None):
    step = step or next((s for s in state.get('steps') or [] if s.get('step_id') == state.get('current_step_id')), {})
    try:
        return materialize_outputs(state, step.get('outputs') or [], project_dir)
    except WorkOutputError as exc:
        step_index = next((i for i, row in enumerate(state.get('steps') or []) if row.get('step_id') == step.get('step_id')), 0)
        output_index = next((i for i, row in enumerate(step.get('outputs') or []) if row == exc.issue.get('value')), 0)
        exc.issue.setdefault('field_path', f'$.steps[{step_index}].outputs[{output_index}]')
        raise


def resolve_output(state, raw, project_dir):
    rows = materialize_outputs(state, [raw], project_dir)
    if len(rows) != 1:
        raise WorkOutputError('OUTPUT_MULTIPLE_TARGETS', str(raw), paths=[r['canonical_path'] for r in rows])
    return authorized_output(state, rows[0]['canonical_path'], project_dir)


def file_fingerprint(path, *, full_limit=4 * 1024 * 1024):
    """Bounded verification; never label a prefix hash as a full-file digest."""
    path = Path(path)
    info = path.stat()
    if not path.is_file():
        return {'path': str(path), 'kind': 'directory'}
    with path.open('rb') as stream: data = stream.read(full_limit if info.st_size <= full_limit else 65536)
    return {'path': str(path), 'size': info.st_size, 'mtime_ns': info.st_mtime_ns,
        'sha256' if info.st_size <= full_limit else 'sample_sha256': hashlib.sha256(data).hexdigest(),
        'hash_coverage_bytes': len(data), 'hash_coverage': 'full' if info.st_size <= full_limit else 'prefix'}
