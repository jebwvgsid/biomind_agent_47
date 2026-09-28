"""Current acceptance examples against real tools/parsers; no science or API."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.contracts_support import contract_fixture


def test_reference_natural_plan_and_assessment_use_existing_consumers():
    from agent_core.plan_contract import executable_plan_reference, prepare_simple_plan
    from agent_core.work_assessment import assessment_projection
    from agent_core.work_validation import validation_diagnostics

    reference = executable_plan_reference()
    # Keep the supported resolution example intact alongside descriptive work.
    resolution = prepare_simple_plan(reference['example'])
    assert resolution['steps'][0]['validation']['kind'] == 'resolution'
    assert not validation_diagnostics(resolution['steps'])[0]
    pair = reference['natural_language_example']
    candidate = prepare_simple_plan(pair['plan'])
    assert candidate['steps'][0]['validation'] == pair['plan']['steps'][0]['validation']
    assert 'kind' not in candidate['steps'][0]['validation']
    errors, notes = validation_diagnostics(candidate['steps'])
    assert not errors and notes[0]['status'] == 'not_evaluated'
    claim = deepcopy(pair['submit_step_result'])
    # This is a projection unit test, not a registered-reader/source witness.
    claim['assessment']['evidence'] = ['unit-read']
    accepted, issues = assessment_projection(claim, [{'evidence_id': 'unit-read'}], [])
    assert not issues and accepted['independent_scientific_verification'] is False
    assert assessment_projection(claim, [], [])[1][0]['code'] == 'ASSESSMENT_EVIDENCE_UNREGISTERED'
    for status in ('unknown', 'unsatisfied'):
        claim['assessment']['status'] = status
        assert assessment_projection(claim, [{'evidence_id': 'unit-read'}], [])[1][0]['code'] == 'RESULT_ASSESSMENT_NOT_SATISFIED'




def _state(root):
    return {'task_id': 'output-checks', 'current_task': {'task_spec': {'inputs': [], 'parameters': []}},
            'plan_document': {'path_bindings': {'project_dir': str(root)},
                              'inputs': [{'sample': sample, 'path': str(root / (sample + '.mcool'))}
                                         for sample in ('leaf', 'bud')],
                              'parameters': {'resolution_bp': 40000}}}


@pytest.mark.parametrize('fault', [None, 'sample', 'matrix', 'resolution', 'status', 'balanced'])
def test_preflight_output_example_checks_scientific_rows(tmp_path, fault):
    from agent_core.plan_contract import executable_plan_reference
    from agent_core.work_validation import validate_output

    row = deepcopy(executable_plan_reference()['declared_output_checks'][0])
    row['path'] = str(tmp_path / 'tad_preflight.tsv')
    data = [{'sample': sample, 'resolution_bp': '40000',
             'matrix': str(tmp_path / (sample + '.mcool')) + '::/resolutions/40000',
             'balanced': 'yes', 'status': 'success', 'message': 'recorded finite weights'}
            for sample in ('leaf', 'bud')]
    if fault:
        name = 'resolution_bp' if fault == 'resolution' else fault
        data[1][name] = {'sample': 'other', 'matrix': '/different/bud.mcool::/resolutions/40000',
                         'resolution': '10000', 'status': 'failed', 'balanced': 'no'}[fault]
    names = ['sample', 'resolution_bp', 'matrix', 'balanced', 'status', 'message']
    Path(row['path']).write_text('\t'.join(names) + '\n' + ''.join(
        '\t'.join(item[name] for name in names) + '\n' for item in data))
    if fault:
        with pytest.raises(ValueError):
            validate_output(_state(tmp_path), row, row['path'])
    else:
        facts = validate_output(_state(tmp_path), row, row['path'])
        assert facts['samples'] == ['leaf', 'bud'] and facts['resolution_bp'] == 40000
        assert facts['usable'] is True


@pytest.mark.parametrize('fault', [None, 'missing_sample', 'resolution', 'return_code'])
def test_result_summary_example_checks_every_sample(tmp_path, fault):
    from agent_core.plan_contract import executable_plan_reference
    from agent_core.work_validation import validate_output

    row = deepcopy(executable_plan_reference()['declared_output_checks'][1])
    row['path'] = str(tmp_path / 'tad_run_summary.tsv')
    data = [['leaf', '40000', 'success', '0'], ['bud', '40000', 'success', '0']]
    if fault == 'missing_sample': data.pop()
    if fault == 'resolution': data[1][1] = '10000'
    if fault == 'return_code': data[1][3] = '1'
    Path(row['path']).write_text('sample\tresolution_bp\tstatus\treturn_code\n' +
                                 ''.join('\t'.join(item) + '\n' for item in data))
    if fault:
        with pytest.raises(ValueError):
            validate_output(_state(tmp_path), row, row['path'])
    else:
        assert validate_output(_state(tmp_path), row, row['path'])['all_succeeded'] is True


@pytest.mark.parametrize('body', ['chr1\t0\t40000\n', 'chr1\t40000\t0\n'])
def test_bed_example_checks_intervals(tmp_path, body):
    from agent_core.plan_contract import executable_plan_reference
    from agent_core.work_validation import validate_output

    row = deepcopy(executable_plan_reference()['declared_output_checks'][2])
    row['path'] = str(tmp_path / 'leaf_domains.bed')
    Path(row['path']).write_text(body)
    if body.startswith('chr1\t0'):
        assert validate_output(_state(tmp_path), row, row['path'])['interval_count'] == 1
    else:
        with pytest.raises(ValueError):
            validate_output(_state(tmp_path), row, row['path'])
