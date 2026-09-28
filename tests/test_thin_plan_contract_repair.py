"""S1/V4: current functions, isolated files; no API, scientific command or approval."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

FIXTURES = Path(__file__).parent / 'fixtures' / 'thin_plan_history'

@pytest.fixture(autouse=True)
def retained_strict_plan_policy(monkeypatch):
    """Exercise the retained validator explicitly; live admission is detached.

    Default-mode end-to-end coverage lives in test_plan_admission_detached.py.
    """
    monkeypatch.setattr('agent_core.plan_contract.PLAN_ADMISSION_POLICY_ENABLED', True)

@asynccontextmanager
async def fixture(name, kind='execute'):
    from tests.react_v3.support import task_fixture
    with patch('agent_core.command_execution._runtime_locations', return_value=({Path(sys.prefix)}, '', ['/usr/bin','/bin'])), \
         patch('execution.resource_evidence.ProcessResourceEvidence', side_effect=RuntimeError('ISOLATED_TEST_NO_TELEMETRY')):
        async with task_fixture(name, kind=kind) as value:
            yield value

def artifact(name, value):
    (Path(os.environ['BIOMIND_CLI_PROGRESS_ARTIFACTS']) / name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def tree_hashes(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}

def test_reference_current_parser_dryrun_and_independent_structure():
    async def run():
        from agent_core.plan_contract import executable_plan_reference,prepare_simple_plan,simple_plan_diagnostics
        from agent_core.plan_tracker import save_execution_plan
        from agent_core.work_validation import expected_inputs
        example=executable_plan_reference()['example']
        assert 'samples' not in example['parameters'] and set(example)=={'goal','inputs','parameters','steps'}
        async with fixture('thin-reference') as fx:
            candidate=deepcopy(example);candidate['inputs'][0]['path']=str(fx.root/'leaf.mcool')
            before=tree_hashes(fx.root)
            prepared=prepare_simple_plan(candidate)
            result=save_execution_plan(fx.root,fx.task_id,candidate,dry_run=True)
            assert tree_hashes(fx.root)==before
            assert expected_inputs(result)=={'leaf':str(fx.root/'leaf.mcool')}
            descriptive=prepare_simple_plan({'goal':'Explain an existing text file','steps':[{'description':'Read the text and describe its observed contents','dependencies':[]}]})
            assert not descriptive['steps'][0].get('validation')
            artifact('thin_plan_reference.json',{'reference':executable_plan_reference(),'current_parser':'PASS','dry_run':'PASS','official_state_unchanged':True})
        problems=simple_plan_diagnostics({'steps':[{'description':'','dependencies':['absent'],'validation':{'kind':'resolution','path':'x','resolution':40000}}, {'description':'also bad','dependencies':False}]})
        paths={i['field_path'] for i in problems}
        assert {'$.steps[0].description','$.steps[0].dependencies[0]','$.steps[0].validation.path','$.steps[0].validation.resolution','$.steps[1].dependencies'}<=paths
        assert all(i.get('reason') and i.get('alternative') and 'value' in i for i in problems)
        separate=simple_plan_diagnostics({'steps':[{'description':'missing edge','dependencies':['absent']},
            {'description':'cycle first','dependencies':[3]}, {'description':'cycle second','dependencies':[2]}]})
        assert {'PLAN_DEPENDENCY_MISSING','PLAN_DEPENDENCY_CYCLE'}<={i['code'] for i in separate}
    asyncio.run(run())

def test_historical_19_18_3_and_syntax_layers():
    async def run():
        from agent_core.plan_tracker import load_plan_state,save_plan_state,save_execution_plan
        from agent_core.plan_contract import plan_exception_issues
        evidence=[]
        async with fixture('thin-history') as fx:
            state=load_plan_state(fx.root,fx.task_id)
            authority=json.loads((FIXTURES/'authority.json').read_text())
            spec=authority.get('task_spec') or authority
            state['current_task']['task_spec']['output_contracts']=deepcopy(spec['output_contracts'])
            save_plan_state(fx.root,fx.task_id,state,allow_canonical_task_write=True)
            for number,expected in ((5,19),(6,18),(7,3)):
                raw=(FIXTURES/f'attempt_{number:02}.txt').read_text()
                before=tree_hashes(fx.root)
                with pytest.raises(ValueError) as caught:save_execution_plan(fx.root,fx.task_id,json.loads(raw),dry_run=True)
                issues=plan_exception_issues(caught.value)
                actual=[i for i in issues if i['code']=='OUTPUT_TEMPLATE_UNRESOLVED']
                assert len(actual)==expected,(number,issues)
                assert len({i['field_path'] for i in actual})==expected
                assert all(i['field_path'].startswith('$.steps[') and '.outputs[' in i['field_path'] and i.get('path') and 'value' in i for i in actual)
                assert tree_hashes(fx.root)==before
                evidence.append({'historical_attempt':number,'raw_sha256':hashlib.sha256(raw.encode()).hexdigest(),'issues':issues,'official_state_unchanged':True})
            for number in (3,4):
                body=json.loads((FIXTURES/f'attempt_{number:02}.txt').read_text())
                with pytest.raises(ValueError) as caught:save_execution_plan(fx.root,fx.task_id,body,dry_run=True)
                issues=plan_exception_issues(caught.value)
                assert len([i for i in issues if i['code']=='VALIDATION_FIELD_UNSUPPORTED'])==2
                assert len([i for i in issues if i['code']=='OUTPUT_TEMPLATE_UNRESOLVED'])==19
            # Structural admission of the final historical carrier is not a
            # claim that its weakened scientific check would pass runtime Gate.
            final=json.loads((FIXTURES/'attempt_08.txt').read_text())
            save_execution_plan(fx.root,fx.task_id,final,dry_run=True)
            # Independent structure + paths in the same parsed candidate.
            body=json.loads((FIXTURES/'attempt_05.txt').read_text());body['steps'][0]['dependencies']=['absent']
            with pytest.raises(ValueError) as caught:save_execution_plan(fx.root,fx.task_id,body,dry_run=True)
            issues=plan_exception_issues(caught.value)
            assert len([i for i in issues if i['code']=='OUTPUT_TEMPLATE_UNRESOLVED'])==19
            assert any(i['code']=='PLAN_DEPENDENCY_MISSING' for i in issues)
        for number in (1,2):
            with pytest.raises(json.JSONDecodeError) as caught:json.loads((FIXTURES/f'attempt_{number:02}.txt').read_text())
            issues=plan_exception_issues(caught.value)
            assert len(issues)==1 and issues[0]['code']=='PLAN_JSON_INVALID' and issues[0]['line']>=1
        artifact('thin_plan_historical_diagnostics.json',evidence)
    asyncio.run(run())

async def read_whole(session):
    offset=0;digest=None
    while True:
        args={'offset_chars':offset,'max_chars':2000}
        if digest:args['expected_sha256']=digest
        response=await session.dispatch('read_draft_file',args)
        digest=response['sha256']
        if response['next_offset_chars'] is None:
            assert response['full_draft_read'];return digest
        offset=response['next_offset_chars']




