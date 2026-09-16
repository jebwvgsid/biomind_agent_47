"""Read accepted execution facts for Generate; never reconstruct a business plan.

Original approved bodies and execution wrappers retain separate identities.
This catalog is evidence for an export, not a claim of independent replay.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
from agent_core.approved_resource_locator import open_approved_resource

# Values outside this explicit non-secret runtime vocabulary are not exported.
ENV_NAMES = frozenset({'JAVA_HOME', 'SNPEFF_JAR', 'GENOME_VERSION', 'DATA_DIR',
                       'SNPEFF_DATA_DIR', 'SNPEFF_CONFIG', 'PYTHONPATH'})
PARAMETER_NAMES = frozenset({'genome_version', 'snpeff_db', 'snpeff_data_dir',
                             'snpeff_config', 'java_executable', 'snpeff_jar', 'threads'})


def accepted_execution_materials(durable, data_path):
    task = durable.get('task_state') or {}
    provenance = durable.get('provenance_state') or {}
    consumptions = task.get('semantic_completion_consumptions') or {}
    steps = {r['step_id']: r for r in durable.get('steps', [])}
    root = Path(data_path).absolute() / 'task_outputs' / str(durable.get('task_id') or '')
    sources, unavailable = [], []
    for receipt in (task.get('typed_step_commit_receipts') or {}).values():
        outcome = receipt.get('outcome') or {}; payload = outcome.get('payload') or {}
        consumed = consumptions.get(payload.get('semantic_consumption_ref')) or {}
        scope = consumed.get('scope') or {}; step_id = payload.get('completed_step_id')
        if (not payload.get('commit_id') or consumed.get('commit_id') != payload['commit_id']
                or step_id != scope.get('step_id') or step_id not in steps
                or any(str(scope.get(k) or '') != str(durable.get(k) or '') for k in ('task_id', 'plan_id', 'plan_version'))):
            continue
        attempt = (provenance.get('attempts') or {}).get(scope.get('attempt_id')) or {}
        if (attempt.get('status') != 'COMPLETED' or attempt.get('returncode') != 0
                or attempt.get('step_id') != step_id or attempt.get('action_id') != scope.get('action_id')):
            unavailable.append({'step_id': step_id, 'reason': 'ACCEPTED_EXECUTION_IDENTITY_UNAVAILABLE'})
            continue
        action = (provenance.get('actions') or {}).get(scope['action_id']) or {}
        action = action.get('normalized_action') or {}
        row = {'step_id': step_id, 'commit_id': payload['commit_id'],
               'attempt_id': scope['attempt_id'], 'action_id': scope['action_id'],
               'operation': steps.get(step_id, {}).get('operation'),
               'dependencies': deepcopy(steps.get(step_id, {}).get('dependencies') or []),
               'argv': deepcopy(action.get('argv') or []),
               'cwd': action.get('cwd') or action.get('working_directory') or 'UNKNOWN',
               'env': {k: v for k, v in (action.get('env') or {}).items() if k in ENV_NAMES},
               'environment_completeness': 'ONLY_EXPLICIT_NONSECRET_FIELDS',
               'tool_environment': {'tool': steps.get(step_id, {}).get('tool') or 'UNKNOWN',
                                    'language': action.get('language') or 'UNKNOWN',
                                    'version': 'NOT_RECORDED_IN_ACCEPTED_ACTION'},
               'runtime_bindings': {k: deepcopy(v) for k, v in (action.get('runtime_bindings') or {}).items()
                                    if k in PARAMETER_NAMES},
               'bound_resources': deepcopy(action.get('concrete_artifact_bindings') or []),
               'scripts': {}}
        for kind in ('approved_script', 'executed_script'):
            descriptor = attempt.get(kind) or {}; path = Path(str(descriptor.get('path') or ''))
            try:
                # Descriptor-bound read, never a search for same-name scripts.
                if not path.is_absolute() or not path.resolve().is_relative_to(root.resolve()) or path.name.startswith('.env') or path.name == '启动.sh':
                    raise ValueError('REPRODUCTION_SOURCE_SCOPE_INVALID')
                with open_approved_resource(path) as stream:
                    body = stream.read(512_001)
                if len(body) > 512_000:
                    raise ValueError('REPRODUCTION_SOURCE_SIZE_LIMIT')
                digest = hashlib.sha256(body).hexdigest()
                if not descriptor.get('sha256') or digest != descriptor['sha256']:
                    raise ValueError('REPRODUCTION_SOURCE_CHANGED')
                row['scripts'][kind] = {'path': str(path), 'sha256': digest, 'body': body.decode('utf-8')}
            except (OSError, ValueError, UnicodeError) as exc:
                row['scripts'][kind] = {'status': 'unavailable', 'reason': str(exc)}
        sources.append(row)
    return {'scope': {k: durable.get(k) for k in ('task_id', 'plan_id', 'plan_version')},
            'sources': sorted(sources, key=lambda r: list(steps).index(r['step_id'])),
            'unavailable': unavailable, 'independent_reproduction': 'NOT_VERIFIED',
            'export_requirements': [
                'Preserve approved body and executor wrapper separately; wrappers may depend on BioMind.',
                'Include config, prepare and helper dependencies required by the approved reproduction scope.',
                'Keep original cwd/argv; document each adaptation and its evidence instead of global replacement.',
                'Declare required input/environment paths; test empty-output reconstruction separately from workspace replay.']}


def validate_package_sources(package, catalog, *, required_step_ids=(), required_dependencies=()):
    """Validate explicit source/dependency claims; never infer missing operations."""
    errors = []
    accepted = {r['attempt_id']: r for r in catalog['sources']}
    for entry in package.get('steps') or []:
        source = accepted.get(entry.get('attempt_id'))
        if (source is None or entry.get('commit_id') != source.get('commit_id')
                or entry.get('step_id') != source.get('step_id')):
            errors.append('REPRODUCTION_SOURCE_NOT_ACCEPTED'); continue
        approved = source['scripts'].get('approved_script') or {}
        if 'body' not in approved or not approved.get('sha256'):
            errors.append('REPRODUCTION_SOURCE_UNAVAILABLE'); continue
        if entry.get('original_body') != approved.get('body') or entry.get('original_sha256') != approved.get('sha256'):
            errors.append('REPRODUCTION_ORIGINAL_CHANGED')
        for field in ('argv', 'cwd'):
            if entry.get(field) != source.get(field) and field not in (entry.get('adaptations') or {}):
                errors.append('REPRODUCTION_UNEXPLAINED_' + field.upper())
        if entry.get('body') != entry.get('original_body') and not (entry.get('adaptations') or {}).get('body'):
            errors.append('REPRODUCTION_UNEXPLAINED_BODY')
    expected = set(required_step_ids) | set(package.get('required_step_ids') or [])
    actual = {r.get('step_id') for r in package.get('steps') or []}
    if expected - actual:
        errors.append('REPRODUCTION_REQUIRED_STEP_MISSING')
    if set(required_dependencies) - {r.get('name') for r in package.get('dependencies') or []}:
        errors.append('REPRODUCTION_DEPENDENCY_UNDECLARED')
    for row in package.get('dependencies') or []:
        p = Path(str(row.get('packaged_path') or ''))
        if row.get('provided_by') == 'declared_external_input':
            continue
        try:
            with open_approved_resource(p) as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if not row.get('sha256') or digest != row['sha256']:
                errors.append('REPRODUCTION_DEPENDENCY_CHANGED')
        except (OSError, ValueError):
            errors.append('REPRODUCTION_DEPENDENCY_MISSING')
    return {'status': 'invalid' if errors else 'source_checks_passed', 'errors': errors,
            'independent_execution': 'NOT_RUN', 'scientific_equivalence': 'NOT_RUN'}
