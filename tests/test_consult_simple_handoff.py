"""Public Consult facts stay small; host plumbing never invents science."""
import asyncio
from copy import deepcopy
import json

import pytest

from agent_core.consult_handoff import handoff_outputs, host_inspection_observations
from agent_core.plan_tracker import load_plan_state
from tests.test_consult_v2_roundtrip import native
from tests import test_schema2_integration_a_production as fixture


@pytest.mark.parametrize('root_form', ['output_dir', 'directory_row', 'existing'])
def test_host_binds_unambiguous_output_without_model_internal_fields(root_form):
    directory = {'role': 'tad_root', 'artifact_kind': 'directory', 'path': '/results.v1'}
    rows = [{'role': 'leaf_domains', 'artifact_kind': 'file', 'format': 'bed'},
            {'role': 'bud_boundaries', 'artifact_kind': 'file', 'format': 'bed'},
            {'role': 'readme', 'artifact_kind': 'file', 'path': '/other/README'}]
    payload = {'outputs': deepcopy(rows)}; spec = None
    if root_form == 'output_dir': payload['output_dir'] = '/results.v1'
    elif root_form == 'directory_row': payload['outputs'].insert(0, directory)
    else: spec = {'output_contracts': [directory]}
    before = deepcopy(payload)
    outputs = handoff_outputs(payload, spec)
    files = {row['role']: row for row in outputs}
    for role in ('leaf_domains', 'bud_boundaries'):
        assert files[role]['root_ref'] == ('output_dir' if root_form == 'output_dir' else 'tad_root')
        assert files[role]['path_policy'] == 'planner_materialized'
        assert 'path' not in files[role]
    assert files['readme'] == rows[-1]
    assert payload == before


def test_output_directory_and_file_conflicts_are_not_guessed():
    with pytest.raises(ValueError, match='HANDOFF_OUTPUT_DIR_CONFLICT'):
        handoff_outputs({'output_dir': '/a', 'outputs': [
            {'role': 'output_dir', 'artifact_kind': 'directory', 'path': '/b'}]})
    with pytest.raises(ValueError, match='HANDOFF_OUTPUT_LOCATION_AMBIGUOUS'):
        handoff_outputs({'outputs': [
            {'role': 'a', 'artifact_kind': 'directory', 'path': '/a'},
            {'role': 'b', 'artifact_kind': 'directory', 'path': '/b'},
            {'role': 'domains', 'artifact_kind': 'file'}]})


def test_explicit_directory_resolves_multiple_roots_without_changing_exact_paths():
    rows = handoff_outputs({'output_dir': '/b', 'outputs': [
        {'role': 'a', 'artifact_kind': 'directory', 'path': '/a'},
        {'role': 'b', 'artifact_kind': 'directory', 'path': '/b'},
        {'role': 'domains', 'artifact_kind': 'file'}]})
    assert len(rows) == 3 and rows[-1]['root_ref'] == 'b'


@pytest.mark.parametrize('value', [None, 42, {}, 'text'])
def test_malformed_new_output_directory_is_repairable(value):
    if value is None:
        assert handoff_outputs({'output_dir': value}) == []
    elif isinstance(value, str):
        assert handoff_outputs({'output_dir': value})[0]['path'] == value
    else:
        with pytest.raises(ValueError, match='HANDOFF_OUTPUT_DIR_REQUIRES_PATH'):
            handoff_outputs({'output_dir': value})


def test_unavailable_receipt_reader_keeps_unknown_instead_of_blocking_handoff():
    class Service:
        def recent_observations(self, *args, **kwargs):
            raise OSError('controlled receipt read failure')
    value = host_inspection_observations(Service(), 'task')
    assert value['status'] == 'unavailable' and value['observations'] == []
    assert value['observation_only'] is True


