"""Host-owned mechanical completion of the small Consult handoff interface."""
from copy import deepcopy
from collections.abc import Mapping

from agent_core.task_spec import effective_output_contracts


def handoff_outputs(payload, current_task_spec=None):
    """Bind unspecified filenames only when the output directory is unambiguous.

    Never interpret prose, guess scientific roles, choose among directories, or
    change a literal filename. Internal root_ref/path_policy remain readable for
    older callers, but ordinary Consult output need not author them.
    """
    declared = payload.get('outputs', [])
    if declared is None:
        declared = []
    if not isinstance(declared, list):
        raise ValueError('HANDOFF_OUTPUTS_MUST_BE_A_LIST')
    if any(not isinstance(row, Mapping) for row in declared):
        raise ValueError('HANDOFF_OUTPUT_MUST_BE_AN_OBJECT')
    rows = deepcopy(declared)
    directory = payload.get('output_dir')
    preferred = ''
    if directory is not None:
        if not isinstance(directory, str) or not directory.strip():
            raise ValueError('HANDOFF_OUTPUT_DIR_REQUIRES_PATH: output_dir must be a non-empty path string')
        matching = [row for row in rows if row.get('artifact_kind') == 'directory'
                    and row.get('path') == directory and row.get('role')]
        if matching:
            preferred = matching[0]['role']
        else:
            if any(row.get('role') == 'output_dir' for row in rows):
                raise ValueError('HANDOFF_OUTPUT_DIR_CONFLICT: output_dir and outputs disagree; provide one directory declaration')
            preferred = 'output_dir'
            rows.insert(0, {'role': preferred, 'artifact_kind': 'directory', 'path': directory})

    bindings = {row.get('role'): row for row in effective_output_contracts(current_task_spec or {})
                if row.get('role')}
    bindings.update({row['role']: row for row in rows if row.get('role')})
    roots = [role for role, row in bindings.items() if row.get('artifact_kind') == 'directory'
             and (row.get('path') or row.get('canonical_path'))]
    for index, row in enumerate(rows):
        if row.get('artifact_kind') != 'file' or row.get('path') or row.get('canonical_path'):
            continue
        if row.get('root_ref'):
            row.setdefault('path_policy', 'planner_materialized')
        elif row.get('required', True):
            root = preferred or (roots[0] if len(roots) == 1 else '')
            if not root:
                raise ValueError(
                    f'HANDOFF_OUTPUT_LOCATION_AMBIGUOUS: outputs[{index}] needs a path or one unambiguous output_dir; '
                    'do not invent a filename or select a directory without a basis')
            row['root_ref'] = root
            row.setdefault('path_policy', 'planner_materialized')
    return rows


def host_inspection_observations(service, task_id):
    """Attach bounded host receipts, never model-supplied evidence identities."""
    reader = getattr(service, 'recent_observations', None)
    if not callable(reader):
        return {}
    try:
        snapshot = reader(task_id, max_records=8, max_chars=12000)
    except (OSError, ValueError) as exc:
        return {'source': 'host_inspection_receipts', 'observation_only': True,
                'status': 'unavailable', 'error': str(exc), 'observations': []}
    if not snapshot.get('observations'):
        return {}
    return {**deepcopy(snapshot), 'source': 'host_inspection_receipts', 'observation_only': True}


def parameter_provenance(key, payload, binding):
    """Resolve only explicit origin metadata; absent origin never means user approval."""
    from agent_core.node_context import resolve_response_handles
    sources = payload.get('parameter_sources') or {}
    if not isinstance(sources, Mapping):
        raise ValueError('HANDOFF_PARAMETER_SOURCES_MUST_BE_OBJECT')
    raw = dict(sources.get(key) or {})
    kind = raw.get('source_type', 'system_suggestion')
    if kind not in {'user_explicit', 'skill_default', 'system_suggestion', 'tool_observation', 'memory', 'unknown'}:
        raise ValueError('HANDOFF_PARAMETER_SOURCE_INVALID')
    result = {'source_type': kind}
    if kind == 'user_explicit':
        handle = str(raw.get('source_turn') or '')
        if not handle.startswith('U'):
            raise ValueError('USER_PARAMETER_REQUIRES_REAL_USER_TURN')
        rows = resolve_response_handles(binding, [handle], object_type='evidence', usage='cite')
        result['source_ref'] = rows[0]['stable_identity']
    elif kind == 'skill_default':
        if not raw.get('skill_id') or not raw.get('version'):
            raise ValueError('SKILL_DEFAULT_REQUIRES_SKILL_AND_VERSION')
        result.update(skill_id=raw['skill_id'], version=raw['version'],
                      source_ref='skill:'+str(raw['skill_id'])+'@'+str(raw['version']))
    elif raw.get('source_ref'):
        result['source_ref'] = str(raw['source_ref'])
    adopted = str(raw.get('adopted_by') or '')
    if adopted:
        if not adopted.startswith('U'):
            raise ValueError('PARAMETER_ADOPTION_REQUIRES_USER_TURN')
        result['adopted_by'] = resolve_response_handles(binding, [adopted], object_type='evidence', usage='cite')[0]['stable_identity']
    return result


def report_preservation_authorization(request, binding, durable, proposed_task, report_root):
    """Bind the model's explicit decision interpretation to a real turn and exact reports.

    No text matching grants authority. The finite business field is resolved
    through the same user evidence handles as other interpreted user decisions.
    """
    if not request:
        return None
    from agent_core.node_context import resolve_response_handles
    from agent_core.workflow.runtime_recovery_reporting import report_publication_preflight
    handle = str(request.get('source_turn') or '')
    if request.get('operation') != 'date_suffix' or not handle.startswith('U'):
        raise ValueError('REPORT_PRESERVATION_REQUIRES_EXPLICIT_USER_DECISION')
    reference = resolve_response_handles(binding, [handle], object_type='evidence', usage='cite')[0]['stable_identity']
    state = deepcopy(durable)
    from agent_core.task_spec import task_spec_revision
    state['current_task'] = {'task_spec': proposed_task, 'task_revision': task_spec_revision(proposed_task)}
    target_rows = report_publication_preflight(state, report_root).get('targets') or []
    exact = {str(row.get('path') or row.get('target_path') or '') for row in target_rows}
    exact.discard('')
    requested = request.get('paths')
    if requested is None:
        pending = (durable.get('task_state') or {}).get('pending_question') or {}
        consultation = (durable.get('task_state') or {}).get('consultation_state') or {}
        evidence = pending.get('evidence') or (consultation.get('report_publication_preflight') or {})
        requested = [row.get('path') or row.get('target_path') for row in evidence.get('targets') or []]
    paths = list(dict.fromkeys(requested or []))
    if not paths or any(path not in exact for path in paths):
        raise ValueError('REPORT_PRESERVATION_REQUIRES_EXACT_REQUESTED_REPORTS')
    return {'operation':'preserve_reports_date_suffix','source':'user_explicit',
            'source_ref':reference,'paths':paths,'naming':'date_suffix'}


def output_preservation_authorization(request, binding, durable, proposed_task, project, *, frozen=None):
    """Bind an explicit user decision to exact existing output identities, without writes.

    The model interprets the user's decision through the existing evidence handle.
    The host checks its scope and captures the existing write guard; prose never
    grants permission and repeating a decision cannot approve changed old bytes.
    """
    if not request:
        return None
    from pathlib import Path
    from agent_core.node_context import resolve_response_handles
    from agent_core.work_outputs import authorized_output
    from agent_core.request_state import request_scope
    from agent_core.action_runtime import capture_output_write_guard, validate_output_write_guard
    if not isinstance(request, Mapping) or set(request) - {'operation', 'source_turn', 'paths'}:
        raise ValueError('OUTPUT_PRESERVATION_REQUEST_INVALID')
    handle = str(request.get('source_turn') or '')
    if request.get('operation') != 'backup_replace' or not handle.startswith('U'):
        raise ValueError('OUTPUT_PRESERVATION_REQUIRES_EXPLICIT_USER_DECISION')
    source = resolve_response_handles(binding, [handle], object_type='evidence', usage='cite')[0]['stable_identity']
    raw_paths = request.get('paths')
    if (not isinstance(raw_paths, list) or not raw_paths or len(raw_paths) > 64
            or any(not isinstance(p, str) or not p or not Path(p).is_absolute() for p in raw_paths)):
        raise ValueError('OUTPUT_PRESERVATION_REQUIRES_EXACT_PATHS')
    paths = sorted(set(raw_paths))
    rows = effective_output_contracts(proposed_task)
    roots, exact = [], []
    for row in rows:
        raw = row.get('canonical_path') or row.get('path')
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            continue
        (roots if row.get('artifact_kind') == 'directory' else exact).append(Path(raw))
    state = deepcopy(durable)
    from agent_core.task_spec import task_spec_revision
    state['current_task'] = {'task_spec': proposed_task, 'task_revision': task_spec_revision(proposed_task)}
    contracts = []
    for raw in paths:
        path = Path(raw)
        if str(path) != raw or '..' in path.parts or path.resolve() != path:
            raise ValueError('OUTPUT_PRESERVATION_TARGET_NOT_EXACT')
        if path not in exact and not any(path.is_relative_to(root) for root in roots):
            raise ValueError('OUTPUT_PRESERVATION_OUTSIDE_DECLARED_OUTPUTS')
        authorized_output(state, path, project)
        for row in proposed_task.get('inputs') or []:
            value = row.get('canonical_path') or row.get('path')
            if not value:
                continue
            source_path = Path(value).resolve()
            if path == source_path or (row.get('artifact_kind') == 'directory' and path.is_relative_to(source_path)):
                raise ValueError('OUTPUT_PRESERVATION_INPUT_OVERLAP')
        contracts.append({'expected_path': raw, 'artifact_kind': 'file'})
    identity = {'operation': 'backup_replace', 'source': 'user_explicit', 'source_ref': source,
                'paths': paths, 'request_scope': request_scope(durable), 'task_id': durable.get('task_id')}
    prior = frozen or (durable.get('task_state') or {}).get('output_write_authorization') or {}
    if frozen or prior.get('source_ref') == source:
        if any(prior.get(key) != value for key, value in identity.items()):
            raise ValueError('OUTPUT_PRESERVATION_DECISION_SCOPE_CHANGED')
        validate_output_write_guard(prior.get('guard') or {}, contracts)
        return deepcopy(prior)
    guard = capture_output_write_guard(contracts)
    if not guard['targets'] or any(not row['before']['exists'] for row in guard['targets']):
        raise ValueError('OUTPUT_PRESERVATION_REQUIRES_EXISTING_FILES')
    return {**identity, 'guard': guard}


def deduplicate_loaded_skill_documents(loaded_documents, inherited_materials):
    """Reference only identical Skill text actually carried by this phase.

    Keep unmatched, partial, summarized and externalized bodies unchanged. The
    visible material keeps the body; this projection neither records a read nor
    changes source messages/protocol groups or the semantic task state.
    """
    result = {}
    for skill_id, body in dict(loaded_documents or {}).items():
        match = next((row for row in inherited_materials or ()
            if isinstance(row, Mapping) and row.get('skill_id') == skill_id
            and row.get('residency') == 'exact' and row.get('body') == body
            and isinstance(body, str) and body
            and (row.get('coverage') or {}).get('kind') == 'full'), None)
        result[skill_id] = ({'provided_in': 'inherited_skill_material',
            'source_ref': match.get('source_ref'), 'content_hash': match.get('content_hash'),
            'coverage': deepcopy(match.get('coverage'))} if match else body)
    return result
