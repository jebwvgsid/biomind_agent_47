"""Revalidate registered observations under a revised step, without rebinding them."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from agent_core.work_outputs import file_fingerprint, plan_identity
from agent_core.work_validation import expected_inputs
from agent_core.request_state import request_scope


def method_context(state, step):
    doc = state.get('plan_document') or {}
    return {'inputs': deepcopy(step.get('inputs') or []), 'parameters': deepcopy(step.get('parameters') or {}),
            'runtime': deepcopy(step.get('runtime') or {}), 'plan_parameters': deepcopy(doc.get('parameters') or {}),
            'runtime_binding': deepcopy(doc.get('runtime_binding') or {}),
            'formal_inputs': expected_inputs(state), 'input_manifest': deepcopy(doc.get('input_manifest'))}


def observed_inputs(state, step, project):
    rule = step.get('validation') if isinstance(step.get('validation'), dict) else {}
    if rule.get('kind') in ('observation', 'resolution'):
        from agent_core.work_validation import validation_diagnostics
        errors, _ = validation_diagnostics([{'validation':rule}], check_field_policy=False)
        if errors:
            raise ValueError(json.dumps(errors, ensure_ascii=False))
    bindings = (state.get('plan_document') or {}).get('path_bindings') or {}
    paths = set(expected_inputs(state).values())
    # Only the observation verifier consumes checks. A retained extra field on
    # another rule must not become a new source path or evidence identity.
    if rule.get('kind') == 'observation':
        paths.update(c['path'] for c in rule['checks'])
    for value in step.get('inputs') or []:
        if isinstance(value, dict):
            value = value.get('canonical_path') or value.get('path') or value.get('artifact_ref')
        if isinstance(value, str):
            paths.add(bindings.get(value, value))
    from agent_core.input_manifest import manifest_fingerprints
    result = manifest_fingerprints(state, project)
    for value in paths:
        if not value: continue
        p = Path(str(value))
        if not p.is_absolute(): p = Path(project) / p
        try: result[str(p)] = file_fingerprint(p)
        except (OSError, ValueError) as exc: result[str(p)] = {'error': type(exc).__name__}
    return result


def _original_product_paths(state, project, actual, diagnostics):
    """Only a recorded change plus its original full hash proves a product.

    An unprovable/cancelled old output does not invalidate the observation itself;
    the current Gate must require each still-declared product independently.
    """
    before, after = actual.get('before_files') or {}, actual.get('after_files') or {}
    products = []
    for path, old in after.items():
        if old == before.get(path) or not old.get('exists') or old.get('kind') != 'file':
            continue
        if old.get('hash_coverage') != 'full' or not old.get('sha256') or old.get('hash_coverage_bytes') != old.get('size'):
            diagnostics.append({'code':'REVALIDATION_OUTPUT_FINGERPRINT_INCOMPLETE',
                'attempt_id':actual['attempt_id'], 'path':path, 'hash_coverage':old.get('hash_coverage','unrecorded')})
            continue
        try:
            target = Path(path)
            if not target.is_absolute() or target.resolve() != target or target.is_symlink():
                raise ValueError('output target changed')
            from agent_core.work_outputs import authorized_output, WorkOutputError
            try:
                target = authorized_output(state, target, project)
            except WorkOutputError as exc:
                diagnostics.append({'code':'REVALIDATION_OUTPUT_AUTHORITY_CHANGED',
                    'attempt_id':actual['attempt_id'], 'path':path, 'issue':deepcopy(exc.issue)})
                continue
            now = file_fingerprint(target)
        except (OSError, ValueError):
            now = {}
        if any(now.get(key) != old.get(key) for key in ('path','size','mtime_ns','sha256','hash_coverage','hash_coverage_bytes')):
            diagnostics.append({'code':'REVALIDATION_OUTPUT_VERSION_CHANGED','attempt_id':actual['attempt_id'],'path':path})
            continue
        products.append(path)
    return sorted(products)


def revised_observations(state, step, project, diagnostics=None):
    """Select original receipts; the existing gate evaluates the new obligation."""
    from agent_core.work_validation import normalized_validation
    diagnostics = diagnostics if diagnostics is not None else []
    rule = normalized_validation(step.get('validation'))
    machine = rule.get('kind') in {'observation', 'resolution'}
    natural = 'kind' not in rule
    if not machine and not natural:
        return []
    current_method = method_context(state, step)
    now_inputs = observed_inputs(state, step, project)
    identities = {row.get('plan_identity') for row in state.get('revision_history') or []
                  if row.get('request_scope') == request_scope(state)}
    current_identity = plan_identity(state)
    selected = []
    registered_history = []
    scoped_terminal = []
    for aid, registration in ((state.get('provenance_state') or {}).get('attempts') or {}).items():
        path = registration.get('command_receipt')
        if not path or registration.get('step_id') != step['step_id']:
            continue
        try:
            data = Path(path).read_bytes()
            if hashlib.sha256(data).hexdigest() != registration.get('command_receipt_sha256'):
                diagnostics.append({'code':'REVALIDATION_RECEIPT_INTEGRITY_UNCONFIRMED','attempt_id':aid})
                continue
            actual = json.loads(data)
        except (OSError, ValueError): continue
        if (actual.get('work_plan_identity') not in identities | {current_identity}
                or actual.get('task_id') != state.get('task_id') or actual.get('attempt_id') != aid
                or actual.get('step_id') != step['step_id'] or actual.get('request_scope') != request_scope(state)
                or actual.get('terminal') is not True or actual.get('status') != 'exited'):
            continue
        scoped_terminal.append(actual)
        if actual.get('work_plan_identity') != current_identity:
            registered_history.append((aid, path, data, actual))
    if natural and registered_history and any(actual.get('returncode') != 0 for actual in scoped_terminal):
        # The global execution pointer can now belong to another step. Do not
        # let filtering failures select an older success when this step is
        # reopened. Only a strictly later registered success supersedes failure.
        if any(type(actual.get('finished_at')) not in (int, float)
               or not math.isfinite(actual['finished_at']) or actual['finished_at'] <= 0
               for actual in scoped_terminal):
            raise ValueError('REVALIDATION_EXECUTION_ORDER_UNCONFIRMED')
        latest = max(actual['finished_at'] for actual in scoped_terminal)
        failed = [actual['attempt_id'] for actual in scoped_terminal
                  if actual['finished_at'] == latest and actual.get('returncode') != 0]
        if failed:
            # Returning an empty selection alone would allow a current read-only
            # assessment to conceal the step's latest real execution failure.
            raise ValueError('REVALIDATION_LATEST_EXECUTION_FAILED: ' + ','.join(sorted(failed)))
    if (machine and not now_inputs) or any('error' in value for value in now_inputs.values()):
        diagnostics.append({'code':'REVALIDATION_INPUT_VERSION_UNAVAILABLE'})
        return []
    if any(value.get('hash_coverage') != 'full' for value in now_inputs.values()):
        diagnostics.append({'code':'REVALIDATION_INPUT_FINGERPRINT_INCOMPLETE',
            'paths':[path for path,value in now_inputs.items() if value.get('hash_coverage') != 'full']})
        return []
    for aid, path, data, actual in registered_history:
        if actual.get('review_method_context') != current_method:
            diagnostics.append({'code':'REVALIDATION_METHOD_CONTEXT_CHANGED','attempt_id':aid,
                'changed_fields':[k for k,v in current_method.items() if (actual.get('review_method_context') or {}).get(k)!=v]})
            continue
        if natural:
            if actual.get('returncode') != 0:
                # A failed older try is history, not positive evidence. The Gate
                # still rejects the current/latest failed execution; a successful
                # retry must not inherit unrelated older process failures.
                diagnostics.append({'code':'REVALIDATION_PRIOR_EXECUTION_FAILED','attempt_id':aid})
                continue
            prior_step = actual.get('review_step_context')
            if not isinstance(prior_step, dict) or not isinstance(prior_step.get('description'),str) or not prior_step['description'].strip():
                diagnostics.append({'code':'REVALIDATION_STEP_CONTEXT_UNAVAILABLE','attempt_id':aid})
                continue
            if 'kind' in normalized_validation(prior_step.get('validation')):
                diagnostics.append({'code':'REVALIDATION_MACHINE_RULE_CHANGED','attempt_id':aid})
                continue
            # Validation/output obligations may change. Execution method prose,
            # declared tool and Skill choices may not be inferred from the new Plan.
            method_fields = ('description','title','tool','purpose','action','skill_ids','workflow_stage_ids','execution_capability_id','operation')
            changed = [key for key in method_fields if prior_step.get(key) != step.get(key)]
            if changed:
                diagnostics.append({'code':'REVALIDATION_METHOD_CONTEXT_CHANGED','attempt_id':aid,'changed_fields':changed})
                continue
        before, after = actual.get('before_input_files'), actual.get('after_input_files')
        before = before if isinstance(before, dict) else None
        after = after if isinstance(after, dict) else None
        if before is None or after is None or before != after or (before != now_inputs if natural else any(before.get(path) != fp for path,fp in now_inputs.items())):
            diagnostics.append({'code':'REVALIDATION_INPUT_VERSION_CHANGED','attempt_id':aid,
                'paths':[path for path,fp in now_inputs.items() if (before or {}).get(path)!=fp or (after or {}).get(path)!=fp]})
            continue
        # Retain the original machine-observation behavior. Natural-language
        # acceptance may reuse actual products only via their original full hash.
        if machine and actual.get('before_files') != actual.get('after_files'):
            continue
        products = _original_product_paths(state, project, actual, diagnostics) if natural else []
        selected.append(({'attempt_id':aid, 'step_id':step['step_id'], 'plan_version':actual.get('plan_version'),
                          'receipt_ref':path, 'receipt_sha256':hashlib.sha256(data).hexdigest(),
                          'revalidated_from_plan':actual['work_plan_identity'],
                          'original_product_paths':products}, actual))
    return selected
