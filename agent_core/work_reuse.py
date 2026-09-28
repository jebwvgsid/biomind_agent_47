"""Request-scoped selection of historical receipts for the existing verifier.

This is a read-only acceptance route, never an execution/recovery launcher.
"""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import time
from agent_core.work_outputs import plan_identity


def begin_result_review(data_path, task_id, *, source_attempt_ids, requirements, expected_scope):
    from agent_core.plan_tracker import load_plan_state, save_plan_state
    from agent_core.request_state import request_scope, current_finish
    state = load_plan_state(data_path, task_id)
    if state.get('task_state', {}).get('status') == 'cancelled': raise ValueError('REQUEST_CANCELLED')
    if request_scope(state) != expected_scope or current_finish(state): raise ValueError('NEW_REVIEW_REQUEST_REQUIRED')
    attempts = (state.get('provenance_state') or {}).get('attempts') or {}
    selected = []
    for identity in source_attempt_ids:
        entry = attempts.get(identity) or {}
        ref = entry.get('command_receipt') or entry.get('exit_status_path')
        if not ref: raise ValueError('REUSE_SOURCE_RECEIPT_MISSING: '+str(identity))
        path = Path(ref)
        content = path.read_bytes()
        if len(content) > 8*1024*1024: raise ValueError('REUSE_RECEIPT_SIZE_LIMIT')
        actual = json.loads(content)
        if actual.get('attempt_id') != identity or actual.get('task_id') != task_id or actual.get('terminal') is not True or actual.get('plan_id') != state.get('plan_id'):
            raise ValueError('REUSE_SOURCE_IDENTITY_MISMATCH')
        # Same historical plan, or a preserved prior Plan version whose method
        # parameters were retained by save_execution_plan. No model-authored proof.
        source_version = str(actual.get('plan_version'))
        parameters = (state.get('plan_document') or {}).get('parameters') or {}
        if source_version != str(state.get('plan_version')):
            prior = next((r for r in state.get('revision_history') or [] if str(r.get('plan_version')) == source_version and 'plan_parameters' in r), None)
            from agent_core.work_validation import expected_inputs
            if (prior is None or prior['plan_parameters'] != parameters
                or prior.get('plan_runtime_binding') != ((state.get('plan_document') or {}).get('runtime_binding') or {})
                or prior.get('plan_input_bindings') != expected_inputs(state)):
                raise ValueError('REUSE_SOURCE_PLAN_METHOD_MISMATCH')
        selected.append({'attempt_id':identity,'step_id':actual.get('step_id'),'plan_version':actual.get('plan_version'),
            'receipt_ref':str(path),'receipt_sha256':hashlib.sha256(content).hexdigest()})
    valid_steps = {r['step_id'] for r in state.get('steps') or []}
    if set(requirements)-valid_steps: raise ValueError('REUSE_TARGET_STEP_UNKNOWN')
    task = state.setdefault('task_state', {})
    if task.get('result_review'): task.setdefault('result_review_history', []).append(task['result_review'])
    task['result_review'] = {'request_scope':deepcopy(expected_scope),'plan_identity':plan_identity(state),
        'sources':selected,'requirements':deepcopy(requirements),'selected_at':time.time(),
        'mode':'verify_existing_only','scientific_execution_allowed':False}
    save_plan_state(data_path, task_id, state)
    return deepcopy(task['result_review'])


def review_sources(state):
    from agent_core.request_state import request_scope
    review = (state.get('task_state') or {}).get('result_review') or {}
    if not review or review.get('request_scope') != request_scope(state): return None
    if review.get('plan_identity') != plan_identity(state): raise ValueError('REUSE_PLAN_CHANGED')
    sources = []
    attempts = (state.get('provenance_state') or {}).get('attempts') or {}
    for row in review['sources']:
        data = Path(row['receipt_ref']).read_bytes()
        if hashlib.sha256(data).hexdigest() != row['receipt_sha256']: raise ValueError('REUSE_SOURCE_CHANGED')
        actual = json.loads(data)
        if actual['attempt_id'] not in attempts or actual.get('task_id') != state.get('task_id'): raise ValueError('REUSE_SOURCE_IDENTITY_MISMATCH')
        sources.append((row, actual))
    return review, sources


def reuse_file_evidence(path, actual, fingerprint):
    """Adopt current bytes against a recorded observation; never rewrite old stats."""
    after = (actual.get('after_files') or {}).get(str(path)) or {}
    before = (actual.get('before_files') or {}).get(str(path)) or {}
    if before != after and after.get('exists') and after.get('size') == fingerprint.get('size') and after.get('mtime_ns') == fingerprint.get('mtime_ns'):
        return 'recorded_output_snapshot'
    # The old samples/balance/statistics tables were printed in full. Require
    # complete nonempty current content, not merely the name or a model summary.
    if fingerprint.get('hash_coverage') == 'full' and fingerprint.get('size',0) <= 1024*1024:
        try: text = Path(path).read_text(encoding='utf-8')
        except UnicodeError: return None
        normalized = text.replace('\r\n','\n').strip()
        if normalized and normalized in str(actual.get('stdout') or '').replace('\r\n','\n'):
            return 'current_content_matches_recorded_stdout'
    return None


def write_review_deliverables(data_path, task_id, *, project_dir):
    """Explicit non-scientific write entry, fenced to the current review scope."""
    from agent_core.plan_tracker import load_plan_state, save_plan_state
    from agent_core.work_outputs import resolve_output, file_fingerprint, declared_samples, plan_parameters
    from agent_core.work_validation import expected_inputs
    from agent_core.delivery_files import render_delivery, reproduction_script
    from agent_core.state_contract_store import StateContractStore
    state=load_plan_state(data_path,task_id);selected=review_sources(state)
    if not selected or state.get('task_state',{}).get('status')=='cancelled':raise ValueError('ACTIVE_RESULT_REVIEW_REQUIRED')
    review,_=selected;doc=state.get('plan_document') or {};bindings=doc.get('path_bindings') or {}
    accepted=[r for r in state.get('steps') or [] if r.get('status')=='done']
    # Rendering cannot be used to turn unaccepted scientific data into success.
    verifications=[r['verification'] for r in state['task_state'].get('step_commit_receipts',{}).values() if r['step_id'] in {s['step_id'] for s in accepted}]
    facts=[fact for verification in verifications for fact in verification.get('validated_facts',[])]
    result_sources=[verification for verification in verifications if any(fact.get('kind')=='sample_results' and fact.get('all_succeeded') for fact in verification.get('validated_facts',[]))]
    if not result_sources:
        raise ValueError('DELIVERY_REQUIRES_ACCEPTED_SAMPLE_RESULTS')
    root=resolve_output(state,{'canonical_path':bindings.get('out_root') or str(project_dir)},project_dir)
    scripts=bindings.get('skill_scripts_dir')
    params=plan_parameters(state)
    from agent_core.input_manifest import manifest_view,validate_manifest_unchanged
    view=manifest_view(state,project_dir)
    input_snapshot=None
    if view:
        # Delivery inherits the input version actually accepted with the source
        # results. A fresh snapshot cannot retrospectively certify old products.
        for source in result_sources:
            snapshot=source.get('input_manifest')
            if not isinstance(snapshot,dict) or not snapshot:
                raise ValueError('DELIVERY_SOURCE_MANIFEST_SNAPSHOT_REQUIRED')
            validate_manifest_unchanged(snapshot,state,project_dir)
        input_snapshot=deepcopy(result_sources[0]['input_manifest'])
        from agent_core.work_outputs import materialize_outputs
        results=[row for step in state.get('steps') or [] for row in materialize_outputs(state,step.get('outputs') or [],project_dir)
            if (row.get('validation') if isinstance(row.get('validation'),dict) else {}).get('kind')=='result_manifest' or row.get('role')=='result_manifest']
        unique={r['canonical_path']:r for r in results}
        if len(unique)!=1:raise ValueError('DELIVERY_RESULT_MANIFEST_REQUIRED')
        result=next(iter(unique.values()))
        manifest={'input_manifest':view['descriptor'],'input_manifest_snapshot':input_snapshot,
            'result_manifest':{'path':result['canonical_path'],'format':result.get('format','tsv'),
                'output_root':(result.get('validation') if isinstance(result.get('validation'),dict) else {}).get('output_root') or str(Path(result['canonical_path']).parent)},
            'reproduction':doc.get('reproduction') or {},'method':doc.get('description') or doc.get('objective') or 'Recorded input-manifest workflow'}
    else:
        if not scripts:raise ValueError('DELIVERY_SCRIPT_PATHS_UNDECLARED')
        manifest={'samples':declared_samples(state),'resolution_bp':params['resolution_bp'],'inputs':expected_inputs(state),
        'launcher':doc['runtime_binding']['launcher'],'multiple_testing':params.get('correct_for_multiple_testing','fdr'),
        'scripts':{'resolution':str(Path(scripts)/'check_cooler_resolution.py'),'balance':bindings.get('check_balance') or str(Path(scripts)/'check_mcool_balance.py'),'analysis':bindings.get('call_tads') or str(Path(scripts)/'call_tads.py')},
        'balance_table':str(resolve_output(state,'matrix_balance_report',project_dir).relative_to(root)),
        'statistics':str(resolve_output(state,'tad_summary',project_dir).relative_to(root)),
        'run_summary':str(Path(str(params['resolution_bp']))/'tad_run_summary.tsv'),
        'accepted_facts':facts,'method':'HiCExplorer hicFindTADs at the declared working resolution, using the selected Skills '+str(doc.get('selected_skills') or [])+' to produce domains, boundaries and per-sample summaries. Both input preflights and the usable gate are required; parameters: '+str(params)}
    from agent_core.work_outputs import authorized_output
    from agent_core import delivery_files
    import tempfile
    names=['report.md','stage_status.tsv','reproduce.sh','delivery_manifest.json','deliver_existing.py']
    if view:names.append('input_manifest.json')
    # Render in a private staging directory. Reads of saved evidence stay explicit;
    # publication checks every destination and the current CAS generation again.
    targets={name:root/name for name in names}
    for name,ref in [('report.md','report'),('stage_status.tsv','stage_status'),('reproduce.sh','reproduce_script')]:
        if ref in bindings or any(r.get('artifact_id')==ref for r in doc.get('output_contracts') or []):
            targets[name]=resolve_output(state,ref,project_dir)
    before={}
    for name,path in targets.items():
        if path.is_symlink(): raise ValueError('DELIVERY_SYMLINK_TARGET')
        authorized_output(state,path,project_dir)
        before[name]=file_fingerprint(path) if path.exists() else None
    for key in ['balance_table','statistics','run_summary']:
        if key in manifest:manifest[key]=str(root/manifest[key])
    with tempfile.TemporaryDirectory(prefix='biomind-delivery-') as staging:
        staging=Path(staging)
        result=render_delivery(staging,manifest)
        # Freeze portable relative paths for the future NEW-directory workflow.
        for key in ['balance_table','statistics','run_summary']:
            if key in manifest:manifest[key]=str(Path(manifest[key]).relative_to(root))
        (staging/'deliver_existing.py').write_text(Path(delivery_files.__file__).read_text())
        (staging/'delivery_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
        (staging/'reproduce.sh').write_text(reproduction_script(manifest))
        if view:(staging/'input_manifest.json').write_text(json.dumps(view['descriptor'],ensure_ascii=False,indent=2)+'\n')
        from agent_core.workflow.runtime_recovery_reporting import publish_report
        from agent_core.request_state import request_scope, current_finish
        selected_step_id=state.get("current_step_id")
        def scope_guard(latest):
            if view:
                try:validate_manifest_unchanged(input_snapshot,latest,project_dir)
                except (ValueError,OSError):return False
            return (latest.get('task_state',{}).get('status')!='cancelled' and not current_finish(latest)
                and request_scope(latest)==review['request_scope'] and plan_identity(latest)==review['plan_identity']
                and latest.get('current_step_id')==selected_step_id)
        latest=load_plan_state(data_path,task_id)
        if not scope_guard(latest):raise ValueError('DELIVERY_STATE_CHANGED')
        if view:validate_manifest_unchanged(input_snapshot,latest,project_dir)
        for name,path in targets.items():
            authorized_output(latest,path,project_dir)
            if path.is_symlink() or (file_fingerprint(path) if path.exists() else None)!=before[name]:
                raise ValueError('DELIVERY_TARGET_CHANGED')
        publication_id='review-delivery:'+hashlib.sha256(json.dumps([review['request_scope'],review['plan_identity']],sort_keys=True).encode()).hexdigest()
        selection_ref=StateContractStore(data_path,task_id).put_object('result_review_delivery_selection',review)
        permissions=(latest['task_state'].get('intent_admission') or {}).get('output_overwrite_authorizations') or []
        publications=[]
        for name,path in targets.items():
            path.parent.mkdir(parents=True,exist_ok=True)
            permission=next(({**row,'path':str(path)} for row in permissions
                if resolve_output(latest,row['path'],project_dir)==path),None)
            # Preserve existing exact rename/overwrite authorization through the
            # common journal and no-follow publisher; no second approval layer.
            publications.append(publish_report(data_path,task_id,path,(staging/name).read_text(),
                owner=publication_id,authorization_ref=selection_ref,scope_guard=scope_guard,
                overwrite_authorization=permission if path.exists() else None))
        state=load_plan_state(data_path,task_id)
        if not scope_guard(state):raise ValueError('DELIVERY_STATE_CHANGED')
    files=[file_fingerprint(path) for path in targets.values()]
    record={'mode':'non_scientific_delivery_write','request_scope':review['request_scope'],'plan_identity':review['plan_identity'],
        'files':files,'written_at':time.time(),'source_commit_ids':list(state['task_state']['step_commit_receipts']),
        'scientific_execution_count':0,'publication_receipts':publications}
    store=StateContractStore(data_path,task_id)
    ref=store.put_object('work_delivery_receipt',record)
    receipt_file=store.object_root/(ref.rsplit(':',1)[-1]+'.json')
    row={**record,'receipt_ref':ref,'receipt_file':str(receipt_file),'receipt_sha256':hashlib.sha256(receipt_file.read_bytes()).hexdigest()}
    state['task_state']['result_review'].setdefault('delivery_receipts',[]).append(row)
    save_plan_state(data_path,task_id,state)
    return result


def delivery_sources(state):
    from agent_core.request_state import request_scope
    review=(state.get('task_state') or {}).get('result_review') or {}
    rows=review.get('delivery_receipts') or []
    for row in rows:
        if row['request_scope']!=request_scope(state) or row['plan_identity']!=plan_identity(state):
            raise ValueError('DELIVERY_RECEIPT_SCOPE_CHANGED')
        data=Path(row['receipt_file']).read_bytes()
        if hashlib.sha256(data).hexdigest()!=row['receipt_sha256']:raise ValueError('DELIVERY_RECEIPT_CHANGED')
        actual=json.loads(data)
        payload={k:v for k,v in row.items() if k not in {'receipt_ref','receipt_file','receipt_sha256'}}
        if actual.get('object_type')!='work_delivery_receipt' or actual.get('payload')!=payload:
            raise ValueError('DELIVERY_RECEIPT_MISMATCH')
        if not set(row['source_commit_ids']) <= set(state['task_state'].get('step_commit_receipts',{})):
            raise ValueError('DELIVERY_SOURCE_COMMIT_MISSING')
    return rows
