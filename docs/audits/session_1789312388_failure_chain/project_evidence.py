"""Static, allowlisted projection. No BioMind imports, session restore or input reads.

Long model content is represented by hashes/shape only. Do not execute the
incident's scripts. Destination must be new; reports at the incident target are
only hashed and inspected for explicit identity markers.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re

SESSION = Path('/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789312388')
REPORTS = SESSION.parents[1] / 'remaining/BM-REMAIN-WGS-005/medium'
REPO = Path(__file__).resolve().parents[3]
SAFE_REPO_FILES = {
    'bio_agents.py', 'agent_core/step_effects.py',
    'agent_core/workflow/managed_stop.py', 'supervisor.py', 'prompts/manager.py',
    *('skills/genomics/'+name+'/SKILL.md' for name in (
        'reseq-variant-workflow', 'wgs-short-read-mapping',
        'wgs-gatk-snp-indel', 'wgs-bcftools-snp-indel')),
}


def safe_source(path):
    """Reject arbitrary file references from incident JSON before opening them."""
    if path in {REPO/name for name in SAFE_REPO_FILES}:
        return True
    if path in {REPORTS/name for name in ('report.md','stage_status.tsv','reproduce.sh')}:
        return True
    try:
        rel=path.relative_to(SESSION).as_posix()
    except ValueError:
        return False
    if any(part.startswith('.') for part in Path(rel).parts):
        return False
    if rel in {'plan_state.json','run_metrics.json','executions/execution_records.jsonl',
               'context_trace/events.jsonl',
               'state_contract/objects/f3959988aa4ffb0c226a096a1b0db8692cc253c7912a55074296a576b11a56b4.json'}:
        return True
    return bool(re.fullmatch(
        r'context_trace/[^/]+/(manifest|request|response)\.json|'
        r'attempts/attempt_[a-f0-9]+/(approved\.sh|executed\.sh|stderr\.log|exit_status\.json)|'
        r'scripts/plan_[a-f0-9]+/plan_v1/[^/]+/[^/]+/generated(\.revision-\d+)?\.sh',rel))


def pick(value, names):
    return {k: value[k] for k in names.split() if k in value}


def digest(body):
    return hashlib.sha256(body).hexdigest()


def project():
    sources = {}
    def read(path):
        assert safe_source(path), str(path)
        assert not path.is_symlink() and path.resolve()==path
        body = path.read_bytes()
        sources[str(path)] = {'bytes': len(body), 'sha256': digest(body)}
        return body
    def js(path):
        return json.loads(read(path))
    state = js(SESSION / 'plan_state.json')
    task = state['task_state']
    records = []
    for index, line in enumerate(read(SESSION / 'executions/execution_records.jsonl').splitlines()):
        row = json.loads(line)
        value = pick(row, 'execution_id plan_id plan_version step_id action_id attempt_id attempt execution_status purpose started_at finished_at duration_seconds exit_code code_hash failure_code expected_runtime_contract preflight_evidence_binding')
        value['pointer'] = f'executions/execution_records.jsonl:line:{index+1}'
        attempt=state['provenance_state']['attempts'][row['attempt_id']]
        value['provenance']=pick(attempt,'action_hash attempt_sequence pid pgid status returncode execution_record_id produced_artifact_ids artifact_version_ids')
        value['script_refs'] = {k: pick(row[k], 'version path sha256 producer mutation_reason') for k in ('generated_script', 'approved_script', 'executed_script')}
        for script in value['script_refs'].values():
            body = read(Path(script['path']))
            script['actual_sha256'] = digest(body)
            assert script['actual_sha256'] == script['sha256']
        effect = row['effect_evidence']
        value['declared_step_effect'] = row['declared_step_effect']
        value['parser'] = pick(effect['parser_receipt'], 'status emission_count issues scope raw_stdout_sha256')
        value['emitted_groups'] = {k: len(effect.get(k, [])) for k in ['produces_artifacts', 'produces_observations', 'explicit_side_effects']}
        if index:
            value['stderr'] = read(SESSION / 'attempts' / row['attempt_id'] / 'stderr.log').decode()
        else:
            obs = effect['produces_observations'][0]['value']
            value['observation'] = pick(obs, 'status total returned passed unstarted all_checks_passed full_file_validated scope')
            value['observation']['items'] = [pick(i, 'label status returncode scope passed') for i in obs['checks']]
        value['process_receipt'] = js(SESSION / 'attempts' / row['attempt_id'] / 'exit_status.json')
        records.append(value)
    calls = []
    for path in sorted((SESSION / 'context_trace').glob('*/manifest.json')):
        meta = js(path)
        response = js(path.with_name('response.json'))
        content = response.get('content', '')
        row = pick(meta, 'call_id call_kind timestamp completed_at step_id action_id attempt_id semantic_invocation_id status selected_skill_ids skill_versions runtime_identity generate_llm_policy prompt_budget_telemetry')
        row['source'] = str(path)
        row['response'] = {'id': response.get('id'), 'characters': len(content), 'utf8_bytes': len(content.encode()), 'sha256': digest(content.encode()), 'has_analysis_tag': '<analysis>' in content, 'shell_open_count': content.count('<shell'), 'shell_close_count': content.count('</shell>')}
        attempt = meta.get('annotations', {}).get('provider_attempt', {})
        row['provider'] = pick(attempt, 'finish_reason stream_completed completion_marker_seen provider_usage failure_type failure_detail exception_at cancellation_source state content_character_count configured_max_attempts attempt_ordinal sdk_max_retries retry_budget_before retry_budget_after retry_budget_exhausted')
        if meta.get('call_kind') == 'supervisor_reviewer':
            review = json.loads(content)
            row['review_shape'] = {'top_level_keys': sorted(review), 'issue_row_keys': [sorted(i) for i in review['issue_dispositions']], **pick(review, 'report_id assessment disposition confidence')}
        if meta.get('call_kind') == 'generate':
            request = js(path.with_name('request.json'))
            messages = request['messages']
            row['request_shape'] = [{'index': i, **pick(m, 'role type name'), 'characters': len(str(m.get('content',''))), 'sha256': digest(str(m.get('content','')).encode())} for i,m in enumerate(messages)]
            wire = json.dumps(messages, ensure_ascii=False)
            row['causal_refs_present'] = {ref: ref in wire for ref in ['attempt_d9036d7cbe68e8f79b4f', 'attempt_d957b4b5226dd056dac8', 'EFFECT_FACT_IDENTITIES_MISMATCH', 'STAGE_FIELD_REQUIRED', 'GENERATE_SCRIPT_BLOCK_INVALID']}
        calls.append(row)
    events = []
    kinds = {'recovery_started','recovery_directive_admitted','recovery_response_received','recovery_tool_started','recovery_tool_failed','recovery_tool_completed','recovery_response_feedback_committed'}
    for line_no, raw in enumerate(read(SESSION / 'context_trace/events.jsonl').splitlines(), 1):
        row=json.loads(raw)
        if row['event_kind'] not in kinds:
            continue
        payload=row['payload']
        value={**pick(row,'event_id event_kind timestamp'), 'line':line_no,
            'payload':pick(payload,'task_id step_id plan_id failure_id seed_id request_ordinal tool tool_ordinal tool_request_id source_ref error_code action directive_id command_started directive_adopted reception_status protocol finish_reason candidate_sha256 public_status')}
        if row['event_kind']=='recovery_response_feedback_committed':
            value['feedback']=pick(json.loads(payload['message']['content']), 'error_code detail format_corrections_remaining status directive_adopted')
        events.append(value)
    repair=[]
    for row in task['candidate_repair_state']['events'].values():
        item=pick(row,'event_id candidate_event_id scope candidate_fingerprint error_fingerprint repair_stage repair_domain repair_ordinal repair_limit progress_proven status terminal_reason_code execution_status recovery_entered outputs_produced')
        item['violations']=[pick(x,'code message field_path expected') for x in row['exact_violations']]
        repair.append(item)
    packets=[]
    for ref,row in task['causal_failure_packets'].items():
        item={'ref':ref, **pick(row,'source_kind source_outcome_ref source_fingerprint supersedes_packet_ref')}
        item['packet']=pick(row['failure_packet'],'failure_id incident_id step_id attempt_ref action_ref failure_type error_code summary evidence_refs')
        payload=row['failure_source']['payload']
        item['source_payload']=pick(payload,'failure_fingerprint attempt_id action_id return_code completion_gate')
        report=payload.get('completion_report',{})
        if report: item['completion']=pick(report,'report_id scope deterministic_status semantic_decision completion_gate process_outcome effective_disposition deterministic_veto semantic_review_requirements')
        packets.append(item)
    stop=task['managed_run_stop']
    stop_projection=pick(stop,'scope origin latest_failure execution associated_attempts stop_reason report_paths report_receipts secondary_failures summary_status delivery_status prior_completed_work')
    reports={}
    for name in ['report.md','stage_status.tsv','reproduce.sh']:
        path=REPORTS/name;body=read(path);content=body.decode();st=path.stat()
        reports[name]={**sources[str(path)],'path':str(path),'mtime_ns':st.st_mtime_ns,'inode':st.st_ino,
            'explicit_stop_ids':sorted(set(re.findall(r'managed-stop:[a-f0-9]+',content))),
            'explicit_session_ids':sorted(set(re.findall(r'session_\d+',content))),
            'explicit_attempt_ids':sorted(set(re.findall(r'attempt_[a-f0-9]+',content)))}
    skills=[]
    for ref in state['selected_skill_refs']:
        snap=state['skill_access_state']['snapshots'][ref['snapshot_id']]
        text=snap['exact_text']; repo_path=Path(__file__).resolve().parents[3]/ref['source_path']
        skills.append({**ref,'snapshot_text_sha256':digest(text.encode()), 'current_source_sha256':digest(read(repo_path)),
            'version_lines':[line for line in text.splitlines() if line.startswith('version:')],
            'dependency_excerpts':[{'line':n,'text':line} for n,line in enumerate(text.splitlines(),1) if any(w in line for w in ['picard','CreateSequenceDictionary','sambamba','index_artifacts','ploidy','PLOIDY'])]})
    spec=state['current_task']['task_spec']
    metrics=js(SESSION/'run_metrics.json')
    identity_ref='f3959988aa4ffb0c226a096a1b0db8692cc253c7912a55074296a576b11a56b4'
    identity=js(SESSION/'state_contract/objects'/(identity_ref+'.json'))['payload']
    identity_projection={'object_ref':'object:sha256:'+identity_ref,'revision':identity['revision'],'python':identity['python'],'sources':{},'loaded_callables':{}}
    repo=Path(__file__).resolve().parents[3]
    for name in ['bio_agents.py','agent_core/step_effects.py','agent_core/workflow/managed_stop.py','supervisor.py','prompts/manager.py']:
        source=identity['source_files'][name]
        identity_projection['sources'][name]={'captured_sha256':source['sha256'],'captured_text_hash_valid':digest(source['text'].encode())==source['sha256'],
            'current_sha256':digest(read(repo/name))}
    for module,names in {'agent_core.step_effects':['_emit_declared_rows','emit_declared_effects','match_step_effect_evidence'],
        'agent_core.workflow.managed_stop':['build_managed_stop','_associated_attempts','finalize_managed_stop'],
        'supervisor':['LLMReviewer._parse_decision']}.items():
        loaded=identity['loaded_modules'][module]
        identity_projection['loaded_callables'][module]={'location':loaded['location'],'callables':{n:loaded['callables'].get(n,'UNKNOWN') for n in names}}
    return {'classification':'VERIFIED_FACT; natural-language diagnoses are not copied as root causes',
        'session':str(SESSION),'source_files':sources,'executions':records,'calls':calls,'recovery_events':events,
        'repair_events':repair,'budgets':task['budgets'],'causal_packets':packets,'stop':stop_projection,
        'active_failure_packet_ref':task.get('active_failure_packet_ref'),
        'consumed_failure_packet_refs':task.get('consumed_failure_packet_refs'),
        'recovery_receipts':{rid:{**pick(row,'schema_version source_fingerprint directive_digest failure_packet_ref incident_id failure_packet_version supersedes_packet_ref recovery_archive_ref outcome_digest generation_authority execution_envelope_digest'),
            'directive':pick(row['directive'],'schema_version directive_id task_id action reason_code target_step_id evidence_refs'),
            'outcome':pick(row['outcome'],'schema_version type scope evidence_refs'),
            'format_correction_count':row['reception'].get('format_correction_count'),
            'admission':row['reception'].get('admission')}
            for rid,row in task['typed_recovery_receipts'].items()},
        'reports':reports,'skills':skills,'task_spec':pick(spec,'inputs parameters confirmed_decisions unresolved_questions requirements'),
        'runtime_identity':identity_projection,'plan_approval':pick(state['approval_record'],'approval_id status approved_plan_id approved_plan_version approved_plan_semantic_hash approved_at decision_source approval_source review_event_id approval_scope_hash'),
        'runtime_environment_facts':[pick(row,'producer source_ref scope observed_at facts acceptance') for row in task['runtime_evidence']['records']],
        'reference_artifact':state['plan_artifacts']['plan-artifact-b4a2996a00517f619eb4fb51'],
        'plan_steps':[pick(row,'step_id title purpose tool operation execution_capability_id skill_ids inputs outputs dependencies success_criteria status step_effect') for row in state['steps']],
        'commit_receipts':task['typed_step_commit_receipts'],
        'metrics_summary':metrics['summary'],
        'metrics_timeline':[{'index':i,**pick(e,'id type node started_at duration_seconds completion_tokens finish_reason completion_integrity protocol_status success confirmed')} for i,e in enumerate(metrics['events']) if e['type'] in {'llm','command','confirmation','node'}]}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('destination',type=Path)
    args=parser.parse_args()
    with args.destination.open('x') as out:json.dump(project(),out,ensure_ascii=False,indent=2)
