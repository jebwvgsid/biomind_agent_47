"""Package existing offline witness evidence; stdlib-only, no session execution."""
from pathlib import Path
from collections import defaultdict
import argparse
import csv
import hashlib
import json

ROOT = Path('/public/home/liusiqi/03.毕设/Biomind_v1')
OUT = ROOT / 'docs/audits/gwas_env_recovery_20260917/evidence'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('sandbox_path', nargs='?', help='Existing capture/restart sandbox (legacy positional form).')
parser.add_argument('--sandbox', help='Existing capture/restart sandbox; no hardcoded /tmp run is required.')
args = parser.parse_args()
if args.sandbox and args.sandbox_path:
    parser.error('Use either --sandbox or the positional path, not both.')
if not (args.sandbox or args.sandbox_path):
    parser.error('Provide --sandbox PATH containing completed capture and restart reports.')
run = Path(args.sandbox or args.sandbox_path).resolve()
OUT.mkdir(parents=True, exist_ok=True)

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def dump(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

def sha(data):
    return hashlib.sha256(data).hexdigest()

capture = json.loads((run / 'capture_report.json').read_text())
restart = json.loads((run / 'restart_report.json').read_text())
wire = json.loads((run / 'request_capture.json').read_text())
inputs = json.loads((run / 'input_manifest.json').read_text())
plan = json.loads((run / 'data/task_outputs/session_1789648505/plan_state.json').read_text())
source_manifest = json.loads((run / 'source_manifest.json').read_text())
assert capture['copied_session_plan_unchanged'] and restart['copied_session_plan_unchanged']
assert all(row['matches_frozen'] for report in (capture, restart) for row in report['imported_sources'].values())
assert sha(Path(inputs['original_session_plan']['source']).read_bytes()) == inputs['original_session_plan']['sha256']
initial_reads = {x['ref_id']: x for x in capture['persistent_reads']}
restart_reads = {x['ref_id']: x for x in restart['persistent_reads']}
for ref, first in initial_reads.items():
    second = restart_reads[ref]
    assert first['status'] == second['status']
    assert first.get('body') == second.get('body')
assert len(capture['manifest']) == len({x['ref_id'] for x in capture['manifest']}) == 27

for source, name in (
    ('capture_report.json', 'recovery_capture.json'),
    ('restart_report.json', 'recovery_restart.json'),
    ('request_capture.json', 'recovery_hypothetical_provider_request.json'),
    ('prior_measurement.json', 'recovery_prior_seed_measurement.json'),
    ('input_manifest.json', 'recovery_input_manifest.json'),
    ('source_manifest.json', 'recovery_frozen_source_manifest.json'),
):
    (OUT / name).write_bytes((run / source).read_bytes())

manifest_rows = []
manifest_groups = defaultdict(lambda: {'rows': 0, 'canonical_item_chars': 0})
duplicate_fields = defaultdict(lambda: {'rows': 0, 'canonical_field_chars_excluding_comma': 0})
for ordinal, row in enumerate(capture['manifest'], 1):
    scope = row.get('scope', {})
    read = initial_reads.get(row['ref_id'], {})
    version = scope.get('object_revision') or scope.get('artifact_version_id') or scope.get('skill_id') or ''
    category = row['backend']
    manifest_groups[category]['rows'] += 1
    manifest_groups[category]['canonical_item_chars'] += len(canonical(row))
    for key, value in row.items():
        if key != 'scope' and key in scope and scope[key] == value:
            duplicate_fields[key]['rows'] += 1
            duplicate_fields[key]['canonical_field_chars_excluding_comma'] += len(canonical({key: value})) - 2
    measured_result = read.get('status', 'metadata_review_only')
    if category == 'skill_access_service':
        measured_result = 'selected_materials_for_carry_exact; no tool service read in witness'
    if category == 'opaque_reference':
        measured_result = 'declared_unresolvable; no locator'
    manifest_rows.append({
        'ordinal': ordinal, 'ref_id': row['ref_id'], 'kind': row['kind'], 'backend': category,
        'object_kind': scope.get('object_kind', ''), 'digest': row.get('digest', ''),
        'version_or_selection': version, 'scope': canonical(scope),
        'allowed_tools': canonical(row.get('allowed_tools', [])),
        'readable_declaration': row.get('readable'), 'canonical_item_chars': len(canonical(row)),
        'read_test': measured_result, 'read_body_sha256': read.get('body', {}).get('sha256', ''),
        'restart_same_bytes': read.get('status') == 'read' and read.get('body') == restart_reads[row['ref_id']].get('body'),
        'unavailability': row.get('unavailable_reason', ''),
        'formal_consumption_allowed': scope.get('formal_consumption_allowed', 'not asserted by this descriptor'),
    })
with (OUT / 'recovery_reference_matrix.tsv').open('w') as f:
    writer = csv.DictWriter(f, fieldnames=list(manifest_rows[0]), delimiter='\t')
    writer.writeheader(); writer.writerows(manifest_rows)

body_parts = {}
plan_duplicates = defaultdict(list)
for kind in ('plan', 'task_context'):
    entry = next(x for x in plan['task_state']['inspection_ref_registry'].values() if x['scope']['object_kind'] == kind)
    name = entry['locator']['object_ref'].removeprefix('object:sha256:') + '.json'
    body = json.loads((run / 'data/task_outputs/session_1789648505/state_contract/objects' / name).read_text())['payload']['body']
    body_parts[kind] = {'canonical_body_chars': len(canonical(body)),
        'fields': {k: len(canonical(v)) for k, v in body.items()}}
    if kind == 'plan':
        body_parts[kind]['approved_plan_fields'] = {k: len(canonical(v)) for k, v in body['approved_plan'].items()}
        def walk(value, path):
            if isinstance(value, (dict, list)):
                encoded = canonical(value)
                if len(encoded) >= 500:
                    plan_duplicates[(sha(encoded.encode()), len(encoded))].append(path)
                for key, child in value.items() if isinstance(value, dict) else enumerate(value):
                    walk(child, path + '.' + str(key))
        walk(body['approved_plan'], 'approved_plan')

budget = capture['complete_request']['audit']['context_budget_checks'][0]['budget']
seed_message = wire['messages'][1]['content']
projected_seed = json.loads(seed_message.split('\n', 1)[1].rsplit('\n</recovery_seed>', 1)[0])
assert len(canonical(projected_seed)) == 32097
projected_groups = defaultdict(lambda: {'rows': 0, 'canonical_item_chars': 0})
projected_duplicates = defaultdict(lambda: {'rows': 0, 'canonical_field_chars_excluding_comma': 0})
for row in projected_seed['inspection_manifest']:
    projected_groups[row['backend']]['rows'] += 1
    projected_groups[row['backend']]['canonical_item_chars'] += len(canonical(row))
    for key, value in row.items():
        if key != 'scope' and key in row['scope'] and row['scope'][key] == value:
            projected_duplicates[key]['rows'] += 1
            projected_duplicates[key]['canonical_field_chars_excluding_comma'] += len(canonical({key: value})) - 2
dump('recovery_provider_projected_seed.json', projected_seed)
message_parts = [{'index': i, 'role': message.get('role', message.get('type')), 'content_chars': len(message.get('content', '')),
    'canonical_message_chars': len(canonical(message)), 'sha256': sha(canonical(message).encode())}
    for i, message in enumerate(wire['messages'])]
with (OUT / 'recovery_full_request_components.tsv').open('w') as f:
    writer = csv.writer(f, delimiter='\t'); writer.writerow(['component', 'estimated_tokens', 'measurement'])
    for key, value in budget['component_token_counts'].items():
        writer.writerow([key, value, 'real _context_budget_snapshot / reference fallback'])
    writer.writerow(['TOTAL_INPUT', budget['estimated_input_tokens'], 'not vendor tokenizer'])
    for key in ('output_reserve_tokens', 'estimation_margin_tokens', 'safety_headroom_tokens', 'safe_input_tokens', 'trigger_tokens', 'target_tokens', 'uncertainty_tokens'):
        writer.writerow([key, budget[key], 'policy or estimator telemetry; do not add safe/trigger/target to input'])

summary = {
    'scope': 'offline audit; original session never executed; original production source never edited',
    'sandbox': str(run), 'frozen_head': inputs['head'],
    'actual_session_recovery_provider_calls': 0,
    'hypothetical_exception': 'Only isolated runtime RECOVERY_SEED_HARD_LIMIT_CHARS raised; real assemblers, budget core, archive and readers used; no SDK/network request',
    'model_external_capacity': 'UNVERIFIED; 200000 is application configuration only',
    'input_integrity_checked_against_original': True,
    'seed': capture['seed'], 'seed_parts': capture['seed_parts'],
    'message_parts': message_parts, 'message_count': len(wire['messages']), 'tool_count': len(wire['tools']),
    'tool_schema_chars': len(canonical(wire['tools'])), 'complete_budget': budget,
    'raw_manifest_groups': dict(manifest_groups), 'raw_within_row_equal_duplicate_fields': dict(duplicate_fields),
    'provider_projected_manifest_groups': dict(projected_groups),
    'provider_projected_within_row_equal_duplicate_fields': dict(projected_duplicates),
    'raw_reference_matrix_measurement': '27 row matrix describes preprojection registry manifest; not seed character counts',
    'projected_manifest_chars': len(canonical(projected_seed['inspection_manifest'])),
    'manifest_unique_ref_count': 27, 'body_parts': body_parts,
    'plan_equal_subobjects_ge500chars': [{'sha256': h, 'chars': n, 'paths': paths}
        for (h, n), paths in plan_duplicates.items() if len(paths) > 1],
    'formal_read_summary': {'read': sum(x['status'] == 'read' for x in initial_reads.values()),
        'unavailable': sum(x['status'] != 'read' for x in initial_reads.values()), 'restart_same_bytes': True,
        'wrong_attempt': capture['wrong_attempt'], 'descriptor_tamper': capture['descriptor_tamper']},
    'compactor_output_reserve_gap': {'core_reserved_output': 8192, 'adapter_fallback_bound_max_tokens': 16000,
        'source': 'bio_agents.py:18194-18207; llm_kwargs:2640 has no max_tokens',
        'sample_admitted_map_input': capture['compactor']['calls'][0]['estimated_tokens'],
        'sample_with_possible_adapter_output': capture['compactor']['calls'][0]['estimated_tokens'] + 16000,
        'application_context': 200000, 'actual_provider_call': False},
    'externalized_read_gap': {'archive_survives_restart': True, 'formal_readers_accept_archive_ref': False,
        'code': 'INSPECTION_REF_NOT_DECLARED', 'archive_ref': restart['externalized_store_restart']['archive_ref']},
    'production_imports_match_frozen': True, 'source_file_count': len(source_manifest),
    'witness_sha256': sha((ROOT / 'docs/audits/gwas_env_recovery_20260917/witnesses/recovery_budget_reconstruction.py').read_bytes()),
}
stage_proof = OUT / 'recovery_stage_rebuild.json'
if stage_proof.exists():
    proof = json.loads(stage_proof.read_text())
    summary['witness_bootstrap_revision'] = {
        'original_capture_witness_sha256': proof['original_capture_witness_sha256'],
        'revised_witness_sha256': proof['new_witness_sha256'],
        'change_scope': 'stage/bootstrap source selection only; capture worker AST unchanged; evidence-pack CLI accepts --sandbox',
        'validation': 'new stage with historical /tmp reads denied; 78 recorded imported production modules have identical hashes; no capture/probe rerun',
        'proof': 'recovery_stage_rebuild.json',
    }
dump('recovery_budget_summary.json', summary)
print(json.dumps({'evidence': str(OUT), 'seed_chars': summary['seed']['chars'],
    'request_estimated_tokens': budget['estimated_input_tokens'], 'persistent_reads': summary['formal_read_summary']['read']}))
