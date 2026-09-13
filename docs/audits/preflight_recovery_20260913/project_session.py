"""Static, field-scoped projection of the single authorized session.

Never imports BioMind, loads a business database, executes a historical script,
reads runtime_env_manifest/config, or publishes raw model reasoning/prose.
"""
import hashlib
import json
from pathlib import Path
import re
import sys

REPO = Path('/public/home/liusiqi/03.毕设/Biomind_v1')
SESSION = Path('/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789266028')
SOURCES = {}


def read(path):
    path = Path(path)
    assert path.is_relative_to(SESSION) or path.is_relative_to(REPO)
    assert path.name not in {'runtime_env_manifest.json', '启动.sh'} and not path.name.startswith('.env')
    assert path.suffix not in {'.db', '.sqlite', '.sqlite3'} and not path.is_symlink()
    raw = path.read_bytes()
    SOURCES[str(path)] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    return raw.decode()


def obj(path): return json.loads(read(path))
def digest(text): return hashlib.sha256(text.encode()).hexdigest()
def select(value, fields): return {k: value[k] for k in fields if k in value}


def main(destination):
    destination = Path(destination).resolve()
    assert destination.is_relative_to(Path('/tmp'))
    destination.mkdir(exist_ok=False)
    state = obj(SESSION / 'plan_state.json')
    task = state['current_task']['task_spec']
    output_root = next(x['canonical_path'] for x in task['output_contracts'] if x['artifact_id'] == 'output_dir')
    active_plan = obj(SESSION / 'plans/active_plan.json')
    records = [json.loads(line) for line in read(SESSION / 'executions/execution_records.jsonl').splitlines()]
    attempts = []
    for record in records:
        aid = record['attempt_id']; root = SESSION / 'attempts' / aid
        code = read(root / 'approved.sh'); wrapper = read(root / 'executed.sh')
        stderr, stdout = read(root / 'stderr.log'), read(root / 'stdout.log')
        policy = obj(root / 'execution_policy_manifest.json')
        generated = read(Path(record['generated_script']['path']))
        evidence_dir = re.search(r'^EVIDENCE_DIR="([^"]+)"', code, re.M).group(1)
        row = select(record, ['execution_id','plan_id','plan_version','step_id','action_id','attempt_id',
            'started_at','finished_at','duration_seconds','exit_code','execution_status','command'])
        row.update(approved_code=code, script_identities={}, execution_wrapper_sha256=digest(wrapper),
            generated_equals_approved=generated == code, evidence_dir=evidence_dir,
            directory_under_user_output_root=Path(evidence_dir).is_relative_to(Path(output_root)),
            caller_precreates_leaf='mkdir -p "${EVIDENCE_DIR}"' in code,
            shell_form='LABEL and PATH joined in one quoted word' if '--item "S1_R1 ' in code else 'LABEL and PATH separate words',
            argv_limit='Historical batch argv was not independently captured; shell text, parser and per-item receipts are separate evidence.',
            stdout_sha256=digest(stdout), stderr_sha256=digest(stderr),
            exit_status=obj(root / 'exit_status.json'), skill_binding=policy['skill_resources'],
            execution_policy=policy['policy'], command_trace=read(root/'command_trace.log'))
        for key in ['generated_script','approved_script','executed_script']:
            value=record[key]; row['script_identities'][key]=select(value,['path','sha256','producer','mutation_reason'])
            assert SOURCES[value['path']]['sha256'] == value['sha256']
        row['parser_receipt'] = select(record['effect_evidence']['parser_receipt'],
            ['parser_id','raw_stdout_sha256','emission_count','status','issues','scope'])
        row['effect_status'] = record['effect_evidence']['status']
        if stdout:
            value = json.loads(stdout.split('=',1)[1])['produces_observations'][0]['value']
            row['observation'] = select(value,['status','scope','full_file_validated','total','returned','passed','unstarted'])
            row['checks'] = [select(x,['label','returncode','status','passed','raw']) for x in value['checks']]
            last = value['checks'][-1]; path=Path(last['raw']['stderr'])
            # Only the exact fifth-item metadata log referenced by this session;
            # never read the FASTA itself or enumerate unrelated output logs.
            assert path.parent == Path(evidence_dir) and path.name == 'item-005.stderr.log'
            body=path.read_bytes(); SOURCES[str(path)]={'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()}
            row['nested_error'] = json.loads(body)
            row['outer_progress'] = [select(json.loads(x.split('=',1)[1]),
                ['event','item','index','total','completed','passed','returncode','check_status','status'])
                for x in stderr.splitlines() if x.startswith('BIOMIND_PREFLIGHT_PROGRESS_JSON=')]
        else: row['raw_stderr'] = stderr
        attempts.append(row)
    receipts = state['task_state']['typed_recovery_receipts']; repairs=[]
    for key, value in receipts.items():
        repairs.append({'directive_id':key,'directive':value['directive'],
            'admission':value['reception']['admission'],'outcome':value['outcome'],
            'response_receipts':value['reception']['response_receipts'],
            'failure_packet_ref':value['failure_packet_ref'],'recovery_archive_ref':value['recovery_archive_ref']})
    traces = {}; request_objects = {}
    for name in ['generate-0009-b50cc719fd74','generate-0012-8d977fac31bc',
                 'generate-0013-ace12cccc970','generate-0016-bb42c9ce9c75']:
        root=SESSION/'context_trace'/name
        request=obj(root/'request.json'); response=obj(root/'response.json'); manifest=obj(root/'manifest.json')
        request_objects[name]=request
        messages=request['messages']; text=response['content']
        envelope_metadata = None
        if text.lstrip().startswith('{'):
            envelope = json.loads(text)
            assert envelope['response_type'] == 'biomind_generate_display_v1'
            envelope_metadata = {'response_type': envelope['response_type'],
                'public_chars': len(envelope['public_message']),
                'public_sha256': digest(envelope['public_message'])}
            text = envelope['payload']
        script = re.search(r'<shell\b[^>]*>(.*?)</shell>\s*$',text,re.S)
        traces[name]={'request_sha256':SOURCES[str(root/'request.json')]['sha256'],
            'response_sha256':SOURCES[str(root/'response.json')]['sha256'],
            'runtime_identity':manifest['runtime_identity'], 'selected_skill_ids':manifest['selected_skill_ids'],
            'messages':[{'index':i, 'role':m['role'],'name':m.get('name'),'chars':len(str(m.get('content',''))),
                'content_sha256':digest(str(m.get('content',''))),
                'directive_instructions_present':[d['directive_id'] for d in repairs if any(v in str(m.get('content',''))
                    for v in [d['directive']['instruction'],json.dumps(d['directive']['instruction'],ensure_ascii=False)[1:-1]])],
                'relative_authorized_root_visible':'remaining/BM-REMAIN-WGS-005/medium' in str(m.get('content','')),
                'correct_absolute_root_visible':output_root in str(m.get('content',''))}
                for i,m in enumerate(messages)],
            'display_envelope_metadata':envelope_metadata,
            'response_script_body_sha256':digest(script.group(1)) if script else None,
            'matches_approved_after_edge_whitespace_normalization': [a['attempt_id'] for a in attempts
                if script and script.group(1).strip() == a['approved_code'].strip()],
            'prose_outside_block_chars':len(text[:script.start()].strip()) if script else None}
    initial=request_objects['generate-0012-8d977fac31bc']['messages']
    corrected=request_objects['generate-0013-ace12cccc970']['messages']
    assert corrected[:len(initial)] == initial
    feedback=json.loads(corrected[-1]['content'])['feedback']['action_repair']
    format_result={'prior_messages_byte_value_equal':True,'prior_count':len(initial),'corrected_count':len(corrected),
                   'exact_error':feedback['exact_error'],'no_execution_for_invalid_response':True}
    events=[json.loads(x) for x in read(SESSION/'context_trace/events.jsonl').splitlines()]
    skill_response=obj(SESSION/'context_trace/recovery_turn-0010-15951f85526b/response.json')
    skill_request=obj(SESSION/'context_trace/recovery_turn-0011-a29bb41e7438/request.json')
    search_call=next(c for c in skill_response['tool_calls'] if c['name']=='skill_search')
    search_result=next(json.loads(m['content']) for m in skill_request['messages'] if m.get('name')=='skill_search')
    skill={'owner':'recovery','request':search_call,'next_provider_result':search_result,
        'events':[d for d in events if d['event_kind'] in ['recovery_tool_started','recovery_tool_completed',
            'recovery_directive_admitted']],'plan_id':state['plan_id'],'plan_version':state['plan_version'],
        'final_selected_skill_ids':state['selected_skill_ids']}
    long_root=SESSION/'context_trace/recovery_turn-0014-2e89c1c7257c'
    response=obj(long_root/'response.json'); manifest=obj(long_root/'manifest.json')
    evt=next(x['payload'] for x in events if x['event_kind']=='recovery_response_received'
             and len(x['payload'].get('explanation',''))==len(response['content']))
    display={'content_chars':len(response['content']), 'content_sha256':digest(response['content']),
        'response_keys':list(response),'public_event_source_message_id':evt['source_message_id'],
        'event_explanation_equals_content':evt['explanation']==response['content'],
        'status':evt['status'],'error_code':evt['error_code'],
        'provider_attempt':select(manifest['annotations']['provider_attempt'],
            ['content_char_count','reasoning_char_count','content_chunk_count','attempt_id','call_id','finish_reason']),
        'raw_prose_intentionally_not_exported':True}
    identity_ref=manifest['runtime_identity']['object_ref'].split(':')[-1]
    identity=obj(SESSION/'state_contract/objects'/f'{identity_ref}.json')['payload']
    source_comparison=[]
    for name in ['bio_agents.py','prompts/manager.py','agent_core/workflow/recovery_agent.py',
                 'agent_core/workflow/generate_handoff.py','agent_core/inspection_readers.py','execution/preflight.py']:
        current=read(REPO/name); captured=identity['source_files'].get(name)
        source_comparison.append({'file':name,'current_sha256':digest(current),
            'captured_sha256':captured.get('sha256') if captured else None,
            'matches_captured':digest(current)==captured['sha256'] if captured else None})
    for path in ['skills/genomics/wgs-short-read-mapping/SKILL.md',
                 'skills/genomics/wgs-short-read-mapping/scripts/inspect_fastq.py',
                 'skills/genomics/wgs-short-read-mapping/scripts/inspect_fastq_batch.py']:
        read(REPO/path)
    result={'session_id':SESSION.name,'attempts':attempts,'repairs':repairs,'generate_messages':traces,
        'format_correction':format_result,'skill':skill,'display_provenance':display,
        'runtime_revision':identity['revision'],'source_comparison':source_comparison,
        'trace_call_directory_names': sorted(p.name for p in (SESSION/'context_trace').iterdir() if p.is_dir()),
        'preflight_step': select(active_plan['steps'][0], ['step_id','title','purpose','inputs','outputs',
            'success_criteria','produces_observations','skill_ids','execution_capability_id']),
        'authorization':{'output_root':output_root,'internal_attempt_log_root':str(SESSION/'attempts'),
            'plan_approval':state['approval_record'],'input_bindings':[select(x,['artifact_id','canonical_path','role']) for x in task['inputs']]},
        'sources':SOURCES,'limits':['No historical inner batch OS argv capture.',
            'No original data or runtime environment values read; no live process inspection.',
            'No raw model reasoning/prose copied; provenance is hashes, lengths, fields and events.']}
    (destination/'session-projection.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'attempts':len(attempts),'sources':len(SOURCES),'output':str(destination)}))


if __name__=='__main__': main(sys.argv[1])
