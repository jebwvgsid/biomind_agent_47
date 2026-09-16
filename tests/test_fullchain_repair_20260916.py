"""Bounded offline regression witnesses; providers are explicit response stubs."""
import asyncio
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agent_core.managed_artifacts import build_managed_artifact_bindings, validate_concrete_action_artifacts
from agent_core.workflow.recovery_reception import SUBMIT_TOOL
from tests.test_recovery_reception import candidate, native, run_loop

SCRIPT = Path(__file__).parents[1] / 'skills/genomics/snpeff-annotation/scripts/ann_summary.py'
spec = importlib.util.spec_from_file_location('summary_fixture_helper', SCRIPT)
summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summary)

# Hand counted: 3 VCF records, 2 with ANN, 4 ANN entries; '&' stays one category.
VCF = ('##fileformat=VCFv4.2\n##SnpEffVersion="fixture"\n##INFO=<ID=ANN,Number=.,Type=String,Description="fixture">\n'
       '#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n'
       'chr1\t1\t.\tA\tG,C\t.\tPASS\tANN=G|missense_variant|MODERATE,C|splice_region_variant&intron_variant|LOW\n'
       'chr1\t2\t.\tC\tT\t.\tPASS\tANN=T|splice_region_variant&intron_variant|LOW,T|upstream_gene_variant|MODIFIER\n'
       'chr1\t3\t.\tG\tA\t.\tPASS\t.\n')
SUMMARY = ('metric\tvalue\nvariants_total\t3\nvariants_with_ann\t2\nann_records_total\t4\n'
           'header_has_snpeff_version\ttrue\nheader_has_ann_info\ttrue\n\nannotation_impact\tcount\n'
           'LOW\t2\nMODERATE\t1\nMODIFIER\t1\n\nsequence_ontology_effect\tcount\n'
           'missense_variant\t1\nsplice_region_variant&intron_variant\t2\nupstream_gene_variant\t1\n')


def raw_native(value, *, finish='tool_calls', raw=None):
    return AIMessage(content='继续使用当前证据。', response_metadata={'finish_reason': finish},
        additional_kwargs={'tool_calls': [{'id': 'submit-raw', 'type': 'function',
            'function': {'name': SUBMIT_TOOL, 'arguments': raw if raw is not None else json.dumps(value)}}]})


def test_native_complete_missing_constant_needs_one_request(tmp_path):
    value = candidate(); del value['schema_version']
    original = raw_native(value)
    before = deepcopy(original)
    result, requests, _ = run_loop(tmp_path, [original])
    assert len(requests) == 1 and result['directive']['schema_version'] == 1
    assert result['audit']['format_correction_count'] == 0 and original == before
    receipt = next(r for r in result['reception']['response_receipts'] if r.get('adaptation'))
    assert receipt['adaptation']['original_arguments'] == value
    assert receipt['adaptation']['adapted_arguments'] == {**value, 'schema_version': 1}


@pytest.mark.parametrize('case', ['null', 'string', 'bool', 'unknown', 'business', 'duplicate', 'half', 'length', 'normalized'])
def test_native_ambiguous_or_invalid_is_not_admitted(tmp_path, case):
    value = candidate()
    changes = {'null': None, 'string': '1', 'bool': True, 'unknown': 42}
    if case in changes:
        value['schema_version'] = changes[case]
    else:
        del value['schema_version']
    if case == 'business':
        value['action'] = 'invented_route'
    first = (native(value) if case == 'normalized' else
             raw_native(value, finish='length' if case == 'length' else 'tool_calls',
                raw=json.dumps(value)[:-1] + ',"action":"blocked"}' if case == 'duplicate' else
                    '{"action":' if case == 'half' else None))
    result, requests, _ = run_loop(tmp_path, [first, native()])
    if case == 'length':
        assert len(requests) == 1 and not result['directive'] and result['terminal_error']
        return  # Preserve the existing explicit truncation stop policy.
    assert len(requests) == 2 and result['audit']['format_correction_count'] == 1
    assert result['directive']['action'] == 'retry_step'
    assert result['audit']['tool_call_count'] == 0
    assert result['reception']['response_receipts'][0]['status'] == 'not_accepted'


def test_path_composition_not_literal_proof_but_real_inputs_required(tmp_path):
    source = tmp_path / '中文 input.fa'; source.write_text('>chr1\nA\n')
    task = {'inputs': [{'input_id': 'in', 'role': 'source', 'path': str(source)}],
            'output_contracts': [{'output_id': 'out', 'role': 'result', 'path': str(tmp_path / 'future.txt')}]}
    bindings = build_managed_artifact_bindings(task)
    for code, env in [('from pathlib import Path\np = Path(root) / filename\n', {}),
                      ('# ' + str(source) + '\npass\n', {'UNUSED': str(source)})]:
        action = {'code': code, 'script_adapter': 'script_block_v1', 'outputs': ['output.result'], 'env': env}
        checked, errors = validate_concrete_action_artifacts(action, bindings)
        assert errors == [] and checked['code'] == code and checked['env'] == env
        assert 'consumption_proven' not in checked
    source.unlink()
    _, errors = validate_concrete_action_artifacts(action, bindings)
    assert 'ACTION_INPUT_ARTIFACT_UNAVAILABLE' in {r.code for r in errors}
    _, errors = validate_concrete_action_artifacts({'argv': ['cat', '{input:source}']}, bindings)
    assert 'UNRESOLVED_ARTIFACT_REFERENCE' in {r.code for r in errors}


def test_summary_hand_count_units_and_small_table_only():
    assert summary.summarize(VCF.splitlines()) == SUMMARY
    facts = summary.validate(SUMMARY)
    assert facts['counts'] == {'variants_total': 3, 'variants_with_ann': 2, 'ann_records_total': 4}
    assert facts['independent_vcf_rescan'] == 'NOT_RUN'
    assert facts['units']['variants'] == 'VCF_data_records'


def test_build_receipts_keep_quality_checker_and_material_failures_separate(tmp_path):
    spec = importlib.util.spec_from_file_location('build_checks_fixture', SCRIPT.with_name('snpeff_checks.py'))
    checks = importlib.util.module_from_spec(spec); spec.loader.exec_module(checks)
    predictor = tmp_path / 'data/fixture/snpEffectPredictor.bin'; predictor.parent.mkdir(parents=True)
    predictor.write_bytes(b'recorded-predictor-fixture')
    log = 'Total Errors : 100 (1.82%)\nCDS STOP warning\nprotein check skipped\n'
    good = checks.build_facts(returncode=0, predictor=predictor, load_returncode=0, log=log)
    assert good['process_passed'] and good['predictor_present_nonempty'] and good['load_status'] == 'passed'
    assert not good['fatal_lines'] and '100 (1.82%)' in good['quality_lines'][0]
    for rc, load, fatal in [(1, 0, ''), (0, 1, ''), (0, 0, 'Database check failed')]:
        bad = checks.build_facts(returncode=rc, predictor=predictor, load_returncode=load, log=log + fatal)
        assert not (bad['process_passed'] and bad['load_status'] == 'passed' and not bad['fatal_lines'])
    predictor.write_bytes(b'')
    bad = checks.build_facts(returncode=0, predictor=predictor, load_returncode=None, log=log)
    assert not bad['predictor_present_nonempty'] and bad['load_status'] == 'NOT_RUN'


@pytest.mark.parametrize('bad', [SUMMARY.replace('annotation_impact', 'Annotation_Impact'),
    SUMMARY.replace('LOW\t2', 'LOW\t2\textra'), SUMMARY.replace('LOW\t2', 'LOW\tnan'),
    SUMMARY.replace('LOW\t2', 'LOW\t1'), SUMMARY.replace('variants_total\t3\n', ''),
    SUMMARY.replace('sequence_ontology_effect\tcount', 'impact\teffect\tcount')])
def test_summary_malformed_or_conflicting_schema_rejected(bad):
    with pytest.raises(ValueError):
        summary.validate(bad)


def test_progress_observation_content_changes_not_receipt_identity():
    from agent_core.workflow.recovery_runtime import meaningful_inspection_facts
    original = {'decision_ref': 'd1', 'receipt_sha256': 'old', 'observation': {
        'request_id': 'one', 'source_ref': 'artifact:current', 'error': None,
        'result': {'content': 'first relevant fact', 'start_line': 1, 'end_line': 1}}}
    clone = deepcopy(original); clone['decision_ref'] = 'd2'; clone['receipt_sha256'] = 'new'
    clone['observation']['request_id'] = 'new'
    assert meaningful_inspection_facts([original]) == meaningful_inspection_facts([clone])
    clone['observation']['result'] = {'content': 'second relevant fact', 'start_line': 2, 'end_line': 2}
    assert meaningful_inspection_facts([original]) != meaningful_inspection_facts([clone])
    clone['observation']['result']['content'] = ''
    assert meaningful_inspection_facts([clone]) == []
    clone['observation']['error'] = {'code': 'READ_FAILED'}
    assert meaningful_inspection_facts([clone]) == []
    for body in ({}, {'start_line': 900, 'end_line': 901}, {'status': 'NO_MATCH'},
                 {'status': 'RECOVERY_OBSERVATION_BUDGET_EXCEEDED'}):
        assert meaningful_inspection_facts([{'result': body}]) == []


@pytest.mark.parametrize('enabled', [False, True])
def test_effective_policy_receipt_reaches_production_provider_context(tmp_path, monkeypatch, enabled):
    from agent_core.workflow.recovery_node import Schema2RecoveryNode
    from agent_core.workflow.recovery_projection import CausalFailurePacketBinder
    from tests.test_reliability_evidence_publication import _artifact_case, _source
    if enabled:
        monkeypatch.setenv('BIOMIND_RECOVERY_RESOURCE_POLICY', 'phase_e_candidate_v2')
    else:
        monkeypatch.delenv('BIOMIND_RECOVERY_RESOURCE_POLICY', raising=False)
    state, _, _ = _artifact_case(tmp_path)
    CausalFailurePacketBinder(data_path=str(tmp_path)).bind(state, 'execute_outcome', _source(state, 'execute_outcome', 'execution_failed'))
    contexts = []
    async def provider(messages, *, model, call_context):
        contexts.append(deepcopy(call_context))
        return native()
    node = Schema2RecoveryNode(data_path=str(tmp_path), model=object(), provider_invoke=provider)
    asyncio.run(node.run(state))
    assert contexts
    policy = contexts[0]['effective_resource_policy']
    assert policy['enabled'] is enabled
    assert policy['source'] == ('environment_after_configuration_merge' if enabled else 'default')
    if enabled:
        assert policy['thresholds']['soft_tokens'] == 300000
    import bio_agents
    agent = bio_agents.BioAnalystAgent.__new__(bio_agents.BioAnalystAgent)
    agent.recovery_llm_policy = {}
    seen = []
    async def invoke(messages, **kwargs):
        seen.append(kwargs['trace_metadata'])
        return native()
    agent._ainvoke_llm = invoke
    asyncio.run(agent._schema2_recovery_provider_invoke([], model=object(), call_context=contexts[0]))
    assert seen[0]['effective_resource_policy'] == policy


@pytest.mark.parametrize('conflict', ['', 'plan_id', 'plan_version', 'producer_attempt_id', 'missing_path'])
def test_pathless_declaration_resolves_only_applicable_materialization(tmp_path, conflict):
    from tests.test_recovery_agent_loop import _seed_fixture
    from agent_core.workflow.phase_archive import PhaseArchiveStore
    from agent_core.workflow.recovery_tools import build_recovery_inspection_registry, RecoveryLocalToolDispatcher, RecoveryToolError
    _durable, seed = _seed_fixture()
    path = tmp_path / '真实汇总.tsv'; path.write_text(SUMMARY)
    packet = seed.failure_packet
    state = {'task_id': packet.task_id, 'plan_id': seed.plan_spec_ref.plan_id,
        'plan_version': seed.plan_spec_ref.plan_version, 'current_step_id': packet.step_id,
        'current_plan': {'plan_spec': {}}, 'task_state': {},
        'current_task': {'task_spec': {'revision': 1, 'goal': 'Read the declared summary.', 'inputs': [], 'output_contracts': [{
            'output_id': 'ann_summary', 'artifact_id': 'ann_summary', 'role': 'ann_summary',
            'canonical_path': '', 'path_policy': 'planner_materialized', 'required': True}]}},
        'task_output_materializations': [{'output_ref': 'ann_summary', 'output_id': 'ann_summary',
            'canonical_path': str(path), 'path': str(path), 'path_policy': 'planner_materialized'}]}
    record = {'artifact_version_id': 'version-one', 'logical_artifact_id': 'ann_summary',
        'producer_step_id': packet.step_id, 'producer_attempt_id': packet.attempt_ref,
        'path': str(path), 'plan_id': state['plan_id'], 'plan_version': state['plan_version'],
        'status': 'pending', 'validation_status': 'not_checked'}
    if conflict == 'missing_path': record['path'] = ''
    elif conflict: record[conflict] = 'foreign'
    state['provenance_state'] = {'artifacts': {'version-one': record}, 'artifact_heads': {'ann_summary': 'version-one'}}
    archive = PhaseArchiveStore(tmp_path)
    registry = build_recovery_inspection_registry(seed, state, phase_store=archive)
    row = next(r for r in registry.provider_manifest() if r['ref_id'] == 'artifact:ann_summary')
    dispatcher = RecoveryLocalToolDispatcher(seed=seed, durable=state, registry=registry, phase_store=archive)
    if conflict:
        assert row['readable'] is False
        with pytest.raises(RecoveryToolError): dispatcher.dispatch_full('artifact.read', {'ref_id': row['ref_id']})
    else:
        assert row['scope']['filesystem_readability'] == 'NOT_CHECKED'
        assert dispatcher.dispatch_full('artifact.read', {'ref_id': row['ref_id']})['content'] == SUMMARY.rstrip('\n')
        assert record['status'] == 'pending'  # inspection cannot accept an artifact
        partial = dispatcher.dispatch_full('artifact.read', {'ref_id': row['ref_id'], 'max_chars': 20})
        assert partial['truncated'] is True
        with pytest.raises(RecoveryToolError): dispatcher.dispatch_full('artifact.read', {'ref_id': row['ref_id'], 'path': str(path)})


def test_tiny_independent_reproduction_empty_outputs_and_missing_helper(tmp_path):
    """Real isolated Bash/Python, hand-authored input; no Java/snpEff invocation."""
    import subprocess
    import sys
    import __main__
    package = tmp_path / 'bundle'; package.mkdir()
    (package / 'ann_summary.py').write_bytes(SCRIPT.read_bytes())
    (package / 'input.vcf').write_text(VCF)
    (package / 'config.json').write_text('{"scope":"synthetic ANN summary only"}')
    script = package / 'reproduce.sh'
    script.write_text("set -eu\ncd -- \"$(dirname -- \"$0\")\"\ntest -f config.json\nmkdir -p output\n"
        "/usr/bin/python3 -I -S ann_summary.py summarize input.vcf > output/summary.tsv\n")
    env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'}
    __main__.OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(script))
    try:
        parsed = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-n'], input=script.read_text(),
            text=True, capture_output=True, env=env)
        assert parsed.returncode == 0 and not (package / 'output').exists()
        result = subprocess.run(['/bin/bash', '--noprofile', '--norc', str(script)], cwd=tmp_path,
            env=env, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        assert (package / 'output/summary.tsv').read_text().strip() == SUMMARY.strip()
        # No prior output can mask missing dependencies on the next run.
        (package / 'output/summary.tsv').unlink(); (package / 'output').rmdir()
        (package / 'ann_summary.py').unlink()
        failed = subprocess.run(['/bin/bash', '--noprofile', '--norc', str(script)], cwd=tmp_path,
            env=env, text=True, capture_output=True)
        assert failed.returncode != 0 and 'ann_summary.py' in failed.stderr
    finally:
        __main__.OFFLINE_ALLOWED_SCRIPT_PATHS.discard(str(script))
