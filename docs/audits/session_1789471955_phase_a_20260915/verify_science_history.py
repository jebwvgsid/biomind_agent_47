"""Read archived JSON evidence only; never import or execute BioMind/scientific code.

Run with Python 3. The only writes are the adjacent evidence_science.json/tsv.
These assertions concern historical receipts, not biological validity or snpEff readiness.
"""
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path('/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789471955')
OUT = Path(__file__).resolve().parent
sources = {}


def read(relative):
    data = (ROOT / relative).read_bytes()
    sources[relative] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
    return data.decode('utf-8')


def obj(relative):
    return json.loads(read(relative))


state = obj('plan_state.json')
events = [json.loads(line) for line in read('context_trace/events.jsonl').splitlines()]
executions = [json.loads(line) for line in read('executions/execution_records.jsonl').splitlines()]
assert len(executions) == 6
assert [x['step_id'] for x in executions] == ['check_inputs_readable', 'check_contig_compatibility'] + ['prepare_database'] * 4
assert all(x['exit_code'] == 0 and x['execution_status'] == 'succeeded' for x in executions)
commits = state['task_state']['typed_step_commit_receipts']
assert len(commits) == 2
assert {x['outcome']['payload']['completed_step_id'] for x in commits.values()} == {'check_inputs_readable', 'check_contig_compatibility'}
steps = {s['step_id']: s for s in state['steps']}
assert steps['prepare_database']['status'] == 'verification_pending'
assert all(steps[name]['status'] == 'pending' for name in ['build_database', 'annotate_variants', 'summarize_ann', 'finalize_outputs'])

report_paths = [
    'state_contract/objects/bfc96a2038a28b03972fc380ce23fe5cd934e23d7f908a7b430b8cf99d17eb6d.json',
    'state_contract/objects/bfa8d0c46f6d8dafec0798ff6a01504513629180d48ea51fdad960c6a72a0118.json',
    'state_contract/objects/e56c71385ecdc865a1633a1e4467eeab0b87b0a16b1e5b5f7c437a5a02f29f1e.json',
]
reports = {x['payload']['body']['scope']['attempt_id']: (path, x['payload']['body'])
           for path in report_paths for x in [obj(path)]}
review_names = ['supervisor_reviewer-0023-f1b5410cf1b0', 'supervisor_reviewer-0028-2b0a25c94b67', 'supervisor_reviewer-0033-643a3412fb10']
rows = []
for number, execution in enumerate(executions, 1):
    attempt_id = execution['attempt_id']
    row = {k: execution[k] for k in ['attempt_id', 'action_id', 'step_id', 'started_at', 'finished_at', 'duration_seconds', 'exit_code', 'execution_status']}
    row.update(execution_line=number, effect_status=execution['effect_evidence']['status'],
               receipt_ref='', attestation_status='not_applicable', semantic_disposition='completion_accepted' if number <= 2 else 'not_called',
               committed=number <= 2)
    if number >= 3:
        journal_path = 'attempts/' + attempt_id + '/mutation_intent.json'
        receipt = obj(journal_path)['receipts'][0]
        row.update(receipt_ref=receipt['receipt_id'], receipt_source=journal_path + '#/receipts/0',
                   attestation_status=receipt['attestation_status'], postcondition_satisfied=receipt['postcondition_satisfied'],
                   postconditions=[{k: p[k] for k in ['relative_path', 'status', 'formal_target_path']} for p in receipt['postcondition_results']],
                   runtime_access_evidence_validation=receipt['runtime_access_evidence_validation'])
        assert receipt['postcondition_satisfied'] is (number >= 4)
        action = state['provenance_state']['actions'][execution['action_id']]['normalized_action']
        assert set(action['produced_bindings']) == {'genome_version', 'snpeff_db'}
        row['normalized_binding_values'] = action['produced_bindings']
        row['normalized_binding_source'] = 'plan_state.json#/provenance_state/actions/' + execution['action_id'] + '/normalized_action/produced_bindings'
    if number >= 4:
        path, report = reports[attempt_id]
        assert report['completion_gate']['passed'] is True
        assert report['effective_disposition'] == 'inspection_required'
        observed_bindings = report['effect_presence']['observed']['produces_bindings']
        assert {b['identity'] for b in observed_bindings} == {'genome_version', 'snpeff_db'}
        assert all(b['value_present'] is False for b in observed_bindings)
        row.update(semantic_disposition=report['effective_disposition'], report_ref=report['report_id'], report_source=path + '#/payload/body',
                   deterministic_gate_pass=True, review_binding_projection=observed_bindings)
        request_path = 'context_trace/' + review_names[number - 4] + '/request.json'
        request = obj(request_path)
        joined = '\n'.join(m['content'] for m in request['messages'])
        upstream_ids = ['contig-receipt-319394754435026b86261be2892fa388502bbd0c1afaecf6cdc876d9f462215b',
                        'validation-1cbb27e5219f0bdb5bd6', 'commit-step-48f494e8a6e44d98773dcde3']
        assert all(identifier not in joined for identifier in upstream_ids)
        assert 'data_root: "<snpeff_db artifact>/data"' in joined
        row['review_request_source'] = request_path
        row['absent_upstream_identifiers'] = upstream_ids
    rows.append(row)

# String/path algebra and immutable receipts; no filesystem inspection of scientific files.
latest = executions[-1]['effect_evidence']['produces_observations'][0]
workspace = executions[-1]['effect_evidence']['workspace_mutations'][0]['formal_concrete_path']
assert latest['data_dir'] == str(Path(workspace) / 'data')
assert latest['genome_fasta'] == str(Path(workspace) / 'data' / 'genomes' / (latest['genome_version'] + '.fa'))
assert latest['genes_gtf'] == str(Path(workspace) / 'data' / latest['genome_version'] / 'genes.gtf')
assert latest['config_data_dir_line'] == 'data.dir = ' + latest['data_dir']
prior_paths = {x['relative_path'] for x in executions[3]['effect_evidence']['workspace_mutations'][0]['after_evidence']['entries']}
assert {'genomes/benchmark_part2_remap_ref.fa', 'benchmark_part2_remap_ref/genes.gtf',
        'data/genomes/benchmark_part2_remap_ref.fa', 'data/benchmark_part2_remap_ref/genes.gtf'} <= prior_paths

episodes = {}
for line_number, event in enumerate(events, 1):
    payload = event['payload']
    seed = payload.get('seed_id')
    if not seed:
        continue
    episode = episodes.setdefault(seed, {'events': [], 'model_turns': 0, 'admitted': [], 'tool_observations': []})
    if event['event_kind'] in ['recovery_started', 'recovery_directive_admitted', 'recovery_response_feedback_committed']:
        entry = {'line': line_number, 'event_kind': event['event_kind'], 'timestamp': event['timestamp']}
        if event['event_kind'] == 'recovery_response_feedback_committed':
            content = json.loads(payload['message']['content'])
            entry.update({k: content.get(k) for k in ['status', 'error_code', 'detail', 'format_corrections_remaining']})
        episode['events'].append(entry)
    if event['event_kind'] == 'recovery_model_turn_started':
        episode['model_turns'] += 1
    if event['event_kind'] == 'recovery_directive_admitted':
        episode['admitted'].append(payload['directive_id'])
    if event['event_kind'] == 'recovery_tool_observation_committed':
        m = payload['message']
        episode['tool_observations'].append({'line': line_number, 'name': m['name'], 'content_chars': len(m['content'])})
assert [e['model_turns'] for e in episodes.values()] == [7, 3, 3, 2]
assert sum(len(e['admitted']) for e in episodes.values()) == 3

deliveries = [dict(record_id=k, **{a: v.get(a) for a in ['path', 'status', 'producer_step_id', 'producer_attempt_id', 'validation_id']})
              for k, v in state['provenance_state']['artifacts'].items()
              if v.get('producer_step_id') in ['annotate_variants', 'summarize_ann', 'finalize_outputs']]
assert len(deliveries) == 5
assert all(x['status'] == 'expected' and not x['producer_attempt_id'] and not x['validation_id'] for x in deliveries)

evidence = {
    'schema_version': 1, 'session_id': ROOT.name, 'method': 'stdlib-only assertions on archived JSON; no production import, model/API or scientific execution',
    'assertions': {'status': 'PASS', 'scientific_validity_verified': False, 'database_readiness_verified': False,
                   'binding_values_present_but_review_projection_false': True, 'last_layout_matches_archived_contract': True,
                   'three_upstream_receipt_ids_absent_in_each_later_review_request': True,
                   'old_first_attempt_files_survived_second_attempt': True, 'no_current_final_delivery_receipts': True},
    'physical_execution_count': len(executions), 'execution_duration_seconds': round(sum(x['duration_seconds'] for x in executions), 3),
    'step_statuses': {k: v['status'] for k, v in steps.items()}, 'typed_commits': commits,
    'execution_rows': rows, 'recovery_episodes': episodes,
    'recovery_directives': state['task_state']['typed_recovery_receipts'],
    'failure_lifecycle': state['task_state']['causal_failure_packet_lifecycle'],
    'final_deliveries': deliveries, 'source_manifest': sources,
}
(OUT / 'evidence_science.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
columns = ['execution_line', 'step_id', 'attempt_id', 'action_id', 'started_at', 'finished_at', 'duration_seconds', 'exit_code',
           'execution_status', 'effect_status', 'attestation_status', 'receipt_ref', 'semantic_disposition', 'committed']
with (OUT / 'evidence_science.tsv').open('w', encoding='utf-8', newline='') as file:
    writer = csv.DictWriter(file, fieldnames=columns, delimiter='\t', extrasaction='ignore', lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
print(json.dumps(evidence['assertions'], ensure_ascii=False))
