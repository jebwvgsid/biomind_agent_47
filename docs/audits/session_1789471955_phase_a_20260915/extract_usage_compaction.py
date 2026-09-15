#!/usr/bin/env python3
"""Offline incident extraction. stdlib only; never imports BioMind or calls providers.

Reads only named session trace/metrics/plan and indexed phase archives. Historical
commands and source bodies are parsed as data and never executed. The optional
AST replay loads only the three pure compaction validation classes and helpers.
"""
from __future__ import annotations

import ast
import collections
import csv
import hashlib
import json
import math
import re
import sys
import types
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
RUNS = REPO.parent / 'benchmarks/biomind_agent_benchmark/runs'
MAIN = 'session_1789471955'
CONTROL = 'session_1789464978'
READS = {}


def read(path):
    path = Path(path)
    raw = path.read_bytes()
    READS[str(path)] = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
    return raw.decode('utf-8')


def js(path):
    return json.loads(read(path))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                      separators=(',', ':')).encode()).hexdigest()


def ts(value):
    return datetime.fromisoformat(value).timestamp() if value else None


def envelope(req):
    return json.loads(req['messages'][1]['content'].split('\n', 1)[1].rsplit('\n', 1)[0])


def sum_fields(rows):
    fields = ['prompt_tokens', 'completion_tokens', 'total_tokens',
              'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens']
    out = {k: sum(r[k] for r in rows if r.get(k) is not None) for k in fields}
    out['rows'] = len(rows)
    out['usage_known_rows'] = sum(r.get('total_tokens') is not None for r in rows)
    out['cache_covered_input_tokens'] = sum(r['prompt_tokens'] for r in rows if r.get('prompt_tokens') is not None and r.get('prompt_cache_hit_tokens') is not None)
    out['cache_usage_known_rows'] = sum(r.get('prompt_cache_hit_tokens') is not None for r in rows)
    out['cache_hit_rate'] = out['prompt_cache_hit_tokens'] / out['cache_covered_input_tokens'] if out['cache_covered_input_tokens'] else None
    if not out['cache_usage_known_rows']:
        out['prompt_cache_hit_tokens'] = None
        out['prompt_cache_miss_tokens'] = None
    return out


def session(sid):
    base = RUNS / 'task_outputs' / sid
    metrics = js(base / 'run_metrics.json')
    events = [e for e in metrics['events'] if e['type'] == 'llm']
    manifests = sorted((js(p) for p in (base / 'context_trace').glob('*/manifest.json')),
                       key=lambda d: d['timestamp'])
    complete = [m for m in manifests if m['status'] == 'completed']
    if sid == MAIN:
        assert len(events) == len(complete), (sid, len(events), len(complete))
    assert len({e['id'] for e in events}) == len(events)
    matches = {}
    for event in events:
        nearest = min(complete, key=lambda m: abs(ts(event['started_at']) - ts(m['timestamp'])))
        assert nearest['call_id'] not in matches
        matches[nearest['call_id']] = event
    rows = []
    for number, m in enumerate(manifests, 1):
        p = base / 'context_trace' / m['call_id']
        req, res = js(p / 'request.json'), js(p / 'response.json')
        a = m.get('annotations', {}).get('provider_attempt', {})
        u = a.get('provider_usage', {})
        e = matches.get(m['call_id'], {})
        if e:
            assert abs(ts(e['started_at']) - ts(m['timestamp'])) < 3, (sid, m['call_id'])
            assert e['usage_source'] == 'provider_response' and not e['tokens_estimated']
            assert e['total_tokens'] == e['prompt_tokens'] + e['completion_tokens']
            assert e['prompt_cache_hit_tokens'] + e['prompt_cache_miss_tokens'] == e['prompt_tokens']
            if u:
                assert u['input_tokens'] == e['prompt_tokens']
                assert u['output_tokens'] == e['completion_tokens']
                assert u['total_tokens'] == e['total_tokens']
        row = {
            'ordinal': number, 'task_id': sid, 'call_id': m['call_id'],
            'call_kind': m['call_kind'], 'status': m['status'],
            'physical_evidence': 'provider_attempt_record' if a else ('completed_response_and_profiler' if e else 'failed_logical_trace_dispatch_unproven'),
            'provider_attempt_id': a.get('provider_attempt_id', ''),
            'semantic_invocation_id': a.get('semantic_invocation_id', m.get('semantic_invocation_id', '')),
            'attempt_ordinal': a.get('attempt_ordinal'),
            'sdk_max_retries': a.get('sdk_max_retries'),
            'sdk_internal_retry_observable': a.get('sdk_internal_retry_observable'),
            'sdk_internal_retry_count': a.get('sdk_internal_retry_count'),
            'provider_request_id': a.get('provider_request_id', ''),
            'provider_request_id_note': 'lc_run identifiers are SDK callback IDs, not independently observed HTTP request IDs',
            'started_at': a.get('request_started_at', m['timestamp']),
            'completed_at': a.get('attempt_completed_at', m.get('completed_at', '')),
            'duration_seconds': a.get('total_elapsed_seconds', e.get('duration_seconds')),
            'model': a.get('model_name', e.get('model', '')),
            'node': m.get('node_name', ''), 'step_id': m.get('step_id', ''),
            'context_epoch_id': m.get('context_epoch_id', ''),
            'compaction_request_id': m.get('compaction_request_id', ''),
            'compaction_operation': '', 'metrics_event_id': e.get('id', ''),
            'usage_status': a.get('usage_status', 'REPORTED_IN_PROFILER' if e else 'UNKNOWN'),
            'usage_source': e.get('usage_source', 'provider_attempt_manifest' if u else ''),
            'prompt_tokens': e.get('prompt_tokens', u.get('input_tokens')), 'completion_tokens': e.get('completion_tokens', u.get('output_tokens')),
            'total_tokens': e.get('total_tokens', u.get('total_tokens')),
            'prompt_cache_hit_tokens': e.get('prompt_cache_hit_tokens'),
            'prompt_cache_miss_tokens': e.get('prompt_cache_miss_tokens'),
            'cache_metric_status': e.get('cache_metric_status', 'unknown'),
            'request_snapshot_sha256': READS[str(p / 'request.json')]['sha256'],
            'request_hash_from_manifest': m.get('request_hash', ''),
            'wire_fingerprint': m.get('wire_fingerprint', ''),
            'request_messages': len(req.get('messages', [])),
            'request_content_chars': sum(len(str(x.get('content', ''))) for x in req.get('messages', [])),
            'manifest_path': str(p / 'manifest.json'), 'request_path': str(p / 'request.json'),
            'response_path': str(p / 'response.json'),
        }
        if m['call_kind'] == 'schema2_context_compaction':
            env = envelope(req)
            row['compaction_operation'] = env['request'].get('operation', 'final_compaction_plan')
        rows.append(row)
    known = [r for r in rows if r['total_tokens'] is not None]
    assert all(sum_fields([r for r in rows if r['metrics_event_id']])[k] == metrics['summary']['tokens'][k] for k in
               ['prompt_tokens', 'completion_tokens', 'total_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens'])
    ids = [r['provider_attempt_id'] for r in rows if r['provider_attempt_id']]
    assert len(ids) == len(set(ids))
    summary = {
        'task_id': sid, 'metrics_path': str(base / 'run_metrics.json'),
        'manifest_count': len(manifests), 'completed_calls_with_usage': len(known),
        'physical_attempt_records': len(ids),
        'completed_without_physical_attempt_record': sum(r['physical_evidence'] == 'completed_response_and_profiler' for r in rows),
        'unknown_usage_calls': [r['call_id'] for r in rows if r['total_tokens'] is None],
        'no_observed_application_transport_retries': all(r['attempt_ordinal'] == 1 for r in rows if r['provider_attempt_id']),
        'sdk_internal_retry_count_claim': 'not_observable; consultant attempts configure sdk_max_retries=1, other captured primary attempt kinds configure 0; no claim of zero hidden retries',
        'sdk_max_retries_by_call_kind': {kind: sorted({r['sdk_max_retries'] for r in rows if r['call_kind'] == kind and r['provider_attempt_id']}, key=str) for kind in sorted({r['call_kind'] for r in rows})},
        'totals_known_only': sum_fields(rows),
        'profiler_summary_tokens': metrics['summary']['tokens'],
        'usage_outside_profiler': [r['call_id'] for r in rows if r['total_tokens'] is not None and not r['metrics_event_id']],
        'by_call_kind': {kind: sum_fields([r for r in rows if r['call_kind'] == kind]) for kind in sorted({r['call_kind'] for r in rows})},
        'metrics_cache_statuses': dict(collections.Counter(r['cache_metric_status'] for r in known)),
        'mapping': 'completed manifest chronological order bijects to llm metrics rows; time delta <3s and every available provider usage triple must match',
    }
    return rows, summary, manifests


def message_index(row):
    req = js(row['request_path'])
    result = []
    for i, m in enumerate(req['messages']):
        content = str(m.get('content', ''))
        item = {'index': i, 'role': m.get('role'), 'chars': len(content),
                'sha256': hashlib.sha256(content.encode()).hexdigest(),
                'tag': content.split('\n', 1)[0][:100] if content.startswith('<') else m.get('role')}
        if content.startswith(('<inherited_recovery_evidence ', '<inherited_skill_material ')):
            desc = json.loads(content.splitlines()[1])
            item['descriptor'] = desc
            if desc.get('object_kind') == 'causal_history':
                body = content.split('---BEGIN EXACT SOURCE BODY---\n', 1)[1].split('\n---END EXACT SOURCE BODY---', 1)[0]
                obj = json.loads(body)
                item['causal_history_components'] = {k: len(json.dumps(v, ensure_ascii=False, separators=(',', ':'))) for k, v in obj.items()}
                item['parent_evidence'] = [{'object_kind': x.get('object_kind'), 'chars': len(json.dumps(x, ensure_ascii=False, separators=(',', ':'))),
                                          'ref_id': x.get('ref_id')} for x in obj.get('parent_evidence', [])]
        result.append(item)
    return {'call_id': row['call_id'], 'request_path': row['request_path'],
            'snapshot_sha256': row['request_snapshot_sha256'], 'messages': result,
            'content_chars': sum(x['chars'] for x in result), 'provider_prompt_tokens': row['prompt_tokens']}


def pure_schema_replay(rows):
    source_path = REPO / 'agent_core/workflow/context_compaction.py'
    source = read(source_path)
    tree = ast.parse(source)
    names = {'ContextCompactionError', 'MaterialRetrievalState', 'CompactionPlan'}
    constants = {'CONTEXT_COMPACTION_SCHEMA_VERSION', '_SHA256_RE', '_FORBIDDEN_MUTATION_KEYS'}
    safe_nodes = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == '__future__':
            safe_nodes.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Only definitions are loaded; validation invokes pure helper paths.
            if node.lineno > 500:
                continue
            if node.decorator_list or any(isinstance(x, ast.Call) for v in list(node.args.defaults) + list(node.args.kw_defaults) if v for x in ast.walk(v)):
                continue
            safe_nodes.append(node)
        elif isinstance(node, ast.ClassDef) and node.name in names:
            safe_nodes.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in node.targets):
            safe_nodes.append(node)
    module = types.ModuleType('_offline_pure_compaction_validation')
    sys.modules[module.__name__] = module
    module.__dict__.update({k: globals()[k] for k in ['Any', 'Mapping', 'Sequence', 'deepcopy', 'MappingProxyType', 'dataclass', 'math', 're', 'json', 'hashlib']})
    exec(compile(ast.Module(body=safe_nodes, type_ignores=[]), str(source_path), 'exec'), module.__dict__)
    checks = []
    for ordinal in [46, 62, 63]:
        row = next(r for r in rows if r['ordinal'] == ordinal)
        payload = json.loads(js(row['response_path'])['content'])
        try:
            module.CompactionPlan.from_mapping(payload)
            raise AssertionError('historical invalid response unexpectedly parsed')
        except module.ContextCompactionError as exc:
            expected = 'CONTEXT_SCHEMA_UNKNOWN_FIELD' if ordinal == 46 else 'CONTEXT_FIELD_REQUIRED'
            assert exc.code == expected
            checks.append({'call_id': row['call_id'], 'replayed_error': exc.to_dict(), 'implementation': 'actual AST-extracted pure CompactionPlan.from_mapping and MaterialRetrievalState'})
    row = next(r for r in rows if r['ordinal'] == 45)
    payload = json.loads(js(row['response_path'])['content'])
    typed = module.CompactionPlan.from_mapping(payload)
    env = envelope(js(row['request_path']))
    selected = {u['unit_id'] for u in env['request']['source_units'] if u['selected']}
    affected = sorted(selected & set(typed.summary_source_unit_ids))
    assert affected and typed.mode == 'normal'
    checks.append({'call_id': row['call_id'], 'asserted_error_condition': 'COMPACTION_SELECTED_SUMMARY_REQUIRES_CAPACITY_PRESSURE',
                   'selected_summary_units': affected, 'implementation': 'actual parser + literal predicate from validate_compaction_plan lines 1539-1545; no full validator invocation'})
    return checks


def main():
    rows, primary, manifests = session(MAIN)
    control_rows, control, _ = session(CONTROL)
    log_path = REPO / 'logs/biomind_1030526.log'
    log_values = collections.defaultdict(set)
    log_refs = collections.defaultdict(list)
    for lineno, line in enumerate(read(log_path).splitlines(), 1):
        if 'PROVIDER_ATTEMPT_EVENT ' not in line:
            continue
        event = json.loads(line.split('PROVIDER_ATTEMPT_EVENT ', 1)[1])
        call_id = event.get('call_id')
        log_values[call_id].add(event.get('sdk_max_retries'))
        log_refs[call_id].append(lineno)
    conflicts = []
    for row in rows:
        values = sorted(log_values.get(row['call_id'], set()), key=str)
        row['sdk_max_retries_log_values'] = json.dumps(values)
        row['sdk_policy_conflict'] = bool(values and any(v != row['sdk_max_retries'] for v in values))
        row['provider_log_line_refs'] = ','.join(str(n) for n in log_refs.get(row['call_id'], [])) or 'NOT_OBSERVED'
        if row['sdk_policy_conflict']:
            conflicts.append({'call_id': row['call_id'], 'manifest_sdk_max_retries': row['sdk_max_retries'],
                              'log_sdk_max_retries': values, 'log_path': str(log_path), 'log_lines': log_refs[row['call_id']]})
    primary['sdk_policy_conflicts'] = conflicts
    with (HERE / 'request_ledger.tsv').open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter='\t', lineterminator='\n')
        w.writeheader(); w.writerows(rows)
    with (HERE / 'control_request_ledger.tsv').open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(control_rows[0]), delimiter='\t', lineterminator='\n')
        w.writeheader(); w.writerows(control_rows)
    arch_base = RUNS / 'phase_archive'
    indexes = [js(p) for p in sorted((arch_base / 'by_invocation' / hashlib.sha256(MAIN.encode()).hexdigest()).rglob('*.json'))]
    archives = []
    for idx in indexes:
        h = idx['digest']
        path = arch_base / 'objects' / h[:2] / (h + '.json')
        obj = js(path)
        assert obj['task_id'] == MAIN
        archives.append((path, obj))
    failure_archives = [(p, d) for p, d in archives if d['phase'] == 'recovery' and d['summary'].get('context_compaction_failures')]
    final_path, final_archive = max(failure_archives, key=lambda pd: len(pd[1]['summary']['context_compaction_failures']))
    failures = final_archive['summary']['context_compaction_failures']
    assert len(failures) == 2
    tx = []
    comp = [r for r in rows if r['call_kind'] == 'schema2_context_compaction']
    for comp_id in dict.fromkeys(r['compaction_request_id'] for r in comp):
        rr = [r for r in comp if r['compaction_request_id'] == comp_id]
        first, last = rr[0], rr[-1]
        tx.append({'compaction_request_id': comp_id, 'call_ids': [r['call_id'] for r in rr],
                   'physical_requests': len(rr), 'operations': dict(collections.Counter(r['compaction_operation'] for r in rr)),
                   'usage': sum_fields(rr), 'started_at': first['started_at'], 'completed_at': last['completed_at'],
                   'wall_seconds': round(ts(last['completed_at']) - ts(first['started_at']), 6),
                   'provider_duration_sum_seconds': round(sum(r['duration_seconds'] or 0 for r in rr), 6),
                   'activated': False, 'status': 'cancelled_during_map' if last['status'] == 'cancelled' else 'final_plan_attempts_exhausted'})
    for obj, failure in zip(tx, failures):
        obj['rejections'] = failure['attempts']
        obj['fallback'] = failure['fallback']
        obj['source_archive_ref'] = failure['source_archive_ref']
    plan = js(RUNS / 'task_outputs' / MAIN / 'plan_state.json')
    final_business = next(r for r in rows if r['ordinal'] == 64)
    initial_last_episode = next(r for r in rows if r['ordinal'] == 47)
    first_messages = js(initial_last_episode['request_path'])['messages']
    final_messages = js(final_business['request_path'])['messages']
    unchanged = first_messages == final_messages[:len(first_messages)]
    assert unchanged
    assert not plan['task_state'].get('recovery_context_compaction')
    assert not [idx for idx in indexes if idx['phase'] == 'recovery-context-epoch']
    assert all(m.get('context_epoch_id', '').endswith(':context-epoch:0') for m in manifests if m['call_kind'] == 'recovery_turn')
    inds = [message_index(next(r for r in rows if r['ordinal'] == n)) for n in [15, 24, 29, 47, 64, 78, 79]]
    events_path = RUNS / 'task_outputs' / MAIN / 'context_trace/events.jsonl'
    events = [json.loads(x) for x in read(events_path).splitlines()]
    cmp_complete = [e for e in events if e['event_kind'] == 'recovery_compaction_completed']
    assert len(cmp_complete) == 42
    # Completed compactor event usage is a duplicate view of physical responses.
    assert sum(e['payload']['usage_metadata']['total_tokens'] for e in cmp_complete) == sum_fields(comp)['total_tokens']
    summary = {'primary': primary, 'control_only': control,
               'deduplication': 'One row per trace invocation, one provider_attempt_id where available. Do not add events.jsonl/manifest/archive duplicated usage. Failed post-cancel logical trace not assumed physical.',
               'cache_semantics': 'hit is provider-returned cached-input telemetry as normalized locally; miss=prompt-hit is derived. Cached tokens remain included in prompt and total. No prices or billed cost inferred.',
               'limits': ['Provider HTTP IDs/raw HTTP usage not preserved; lc_run IDs are SDK callback IDs.', 'SDK internal retries are unobservable; primary consultant attempts configure SDK retries 1, other captured primary attempt kinds configure 0; log and manifest values agree.', 'Cancelled 0078 and failed exit triage 0079 have unknown usage, not zero.', 'request.json is provider-visible message snapshot, not a complete raw HTTP payload including every provider wrapper.']}
    evidence = {'task_id': MAIN, 'transactions': tx,
                'applied_evidence': {'activated_transactions': 0, 'durable_active_pointer_present': False,
                  'recovery_context_epoch_archive_count': 0, 'all_business_recovery_epochs_zero': True,
                  'last_business_prefix_exactly_unchanged': unchanged,
                  'last_business_prefix_message_count': len(first_messages),
                  'last_business_prompt_tokens_before_after': [initial_last_episode['prompt_tokens'], final_business['prompt_tokens']],
                  'summary': 'Map completions are not accepted epochs. Two final-plan transactions rejected; third cancelled. Original 13-message prefix survives byte-equivalent at JSON message-value level.'},
                'failure_archive_path': str(final_path),
                'failures': [{k: v for k, v in x.items() if k != 'compactor_observability'} for x in failures],
                'budget_checks': final_archive['summary']['provider_observability']['context_budget_checks'],
                'request_message_indexes': inds,
                'pure_schema_replay': pure_schema_replay(rows),
                'phase_archive_objects_read': [str(p) for p, _ in archives],
                'trace_event_kinds': dict(collections.Counter(e['event_kind'] for e in events)),
                'completed_compactor_cache_raw_usage': [{'event_id': e['event_id'], 'timestamp': e['timestamp'], 'usage_metadata': e['payload']['usage_metadata']} for e in cmp_complete]}
    # Read-only source references; never run historical scripts or import BioMind.
    for path in ['agent_core/workflow/recovery_node.py', 'agent_core/workflow/inspection_evidence.py', 'prompts/context_compaction.py', 'execution/profiling.py']:
        read(REPO / path)
    evidence['source_files'] = [{'path': p, **data} for p, data in sorted(READS.items())]
    (HERE / 'usage_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (HERE / 'compaction_evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'primary': primary, 'transactions': tx, 'offline_validation': evidence['pure_schema_replay']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
