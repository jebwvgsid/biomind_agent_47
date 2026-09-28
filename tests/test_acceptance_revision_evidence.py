"""Real formal micro-execution -> local Plan revision -> evidence selection.

Only isolated engineering bytes are processed. Legacy metadata negatives are
explicit copies of synthetic receipts, never edits to production/history.
"""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib,json,os,sys
from pathlib import Path
import pytest
from tests.contracts_support import contract_fixture
from tests.react_v3.support import actual_micro_runner
from agent_core.plan_tracker import load_plan_state,save_plan_state,save_execution_plan
from agent_core.action_runtime import execute_formal_command
from agent_core.revision_evidence import revised_observations


def record(name,value):
    root=Path(os.environ['BIOMIND_CLI_PROGRESS_ARTIFACTS'])
    (root/('revision-evidence-'+name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str))


@asynccontextmanager
async def executed(name, *, product=True, large_input=False, large_output=False, machine_rule=False):
    async with contract_fixture('acceptance-'+name) as f:
        source=f.root/'input.txt';source.write_text('actual tiny engineering input')
        if large_input:
            with source.open('ab') as stream:stream.truncate(4*1024*1024+1)
        target=f.root/'product.txt'
        plan={'path_bindings':{'project_dir':str(f.root)},'steps':[
            {'step_id':'work','description':'Read the declared bytes and preserve the exact engineering result.',
             'inputs':[str(source)],'outputs':[str(target)] if product else [],
             'validation':{'kind':'custom','description':'Original acceptance wording.'}},
            {'step_id':'later','description':'Final delivery remains necessary.','dependencies':['work'],'outputs':['later.txt']}]}
        if machine_rule:
            plan['inputs']=[{'sample':'leaf','path':str(source)}]
            plan['parameters']={'resolution_bp':40000}
            plan['steps'][0]['validation']={'kind':'resolution'}
        save_execution_plan(f.root,f.task_id,plan)
        code='from pathlib import Path; data=Path('+repr(str(source))+').read_bytes(); '
        if product:
            code+='Path('+repr(str(target))+').write_bytes(data); '
            if large_output:code+='p=Path('+repr(str(target))+'); s=p.open("ab"); s.truncate(4*1024*1024+1); s.close(); '
        code+='print("Recorded engineering input bytes:",len(data))'
        if machine_rule:
            code+='; import json; present=b"contains-resolution-40000" in data; print(json.dumps({"resolution_bp":40000,"ok":present,"inputs":[{"resource_handle":"leaf","path":'+repr(str(source))+',"present":present,"target_group":"/resolutions/40000"}]}))'
        launches=[]
        with actual_micro_runner(launches):
            receipt=await execute_formal_command(f.agent.command_service,
                {'argv':[sys.executable,'-c',code],'workdir':str(f.root)},task_id=f.task_id,call_id='original-work')
        assert receipt['returncode']==0 and len(launches)==1
        raw=Path(receipt['receipt_ref']).read_bytes()
        assert receipt['review_step_context']['description']==plan['steps'][0]['description']
        assert not {'status','evidence','execution_history','current_approval','pending_work_result'} & set(receipt['review_step_context'])
        yield f,plan,source,target,receipt,raw,launches
        assert Path(receipt['receipt_ref']).read_bytes()==raw,'ORIGINAL_RECEIPT_CHANGED'


def revise(f,plan,**changes):
    revised=deepcopy(plan)
    revised['steps'][0]['validation']={'description':'Re-evaluate current obligations using the registered real evidence.'}
    revised['steps'][0].update(changes)
    return save_execution_plan(f.root,f.task_id,revised)


def select(f,state):
    issues=[];step=next(row for row in state['steps'] if row['step_id']=='work')
    selected=revised_observations(state,step,f.root,issues)
    return selected,issues


def test_natural_revision_preserves_original_receipt_and_proves_actual_product():
    async def run():
        async with executed('product') as (f,plan,source,target,receipt,raw,launches):
            assert receipt['after_files'][str(target)]['hash_coverage']=='full'
            assert receipt['after_files'][str(target)]['sha256']==hashlib.sha256(target.read_bytes()).hexdigest()
            state=revise(f,plan);selected,issues=select(f,state)
            assert len(selected)==1 and not issues,(selected,issues)
            row,actual=selected[0]
            assert row['original_product_paths']==[str(target)]
            assert row['revalidated_from_plan']==receipt['work_plan_identity']
            assert row['attempt_id']==receipt['attempt_id'] and row['plan_version']==receipt['plan_version']
            assert actual==json.loads(raw) and len(launches)==1
            assert state['steps'][1]['status']!='done'
            assert not state['task_state'].get('result_review')
            record('product',{'selected':selected,'issues':issues,'launches':launches,'original_unchanged':True})
    asyncio.run(run())


def test_pure_observation_never_claims_production_after_output_added():
    async def run():
        async with executed('readonly',product=False) as (f,plan,source,target,receipt,raw,launches):
            target.write_text('Unrelated pre-existing file added after the old observation.')
            state=revise(f,plan,outputs=[str(target)]);selected,issues=select(f,state)
            assert len(selected)==1 and selected[0][0]['original_product_paths']==[]
            assert str(target) not in selected[0][1]['after_files']
            record('readonly-no-production',{'selected':selected,'issues':issues,'launches':launches})
    asyncio.run(run())


@pytest.mark.parametrize('change',['description','parameters','input_bytes','input_prefix','receipt_hash','unknown_kind'])
def test_unsafe_source_revalidation_is_rejected_with_original_identity(change):
    async def run():
        async with executed('reject-'+change,large_input=change=='input_prefix') as (f,plan,source,target,receipt,raw,launches):
            changes={}
            if change=='description':changes['description']='Use a different execution method, not the old computation.'
            if change=='parameters':changes['parameters']={'different_method':True}
            if change=='input_bytes':source.write_text('Changed bytes after the recorded execution.')
            if change=='unknown_kind':changes['validation']={'kind':'unknown_future_machine_check'}
            state=revise(f,plan,**changes)
            if change=='receipt_hash':
                # Corrupt only the cloned in-memory registration; original bytes
                # and durable source receipt are never rewritten.
                state['provenance_state']['attempts'][receipt['attempt_id']]['command_receipt_sha256']='0'*64
            selected,issues=select(f,state)
            assert not selected,(change,selected,issues)
            expected={'description':'REVALIDATION_METHOD_CONTEXT_CHANGED','parameters':'REVALIDATION_METHOD_CONTEXT_CHANGED',
                      'input_bytes':'REVALIDATION_INPUT_VERSION_CHANGED','input_prefix':'REVALIDATION_INPUT_FINGERPRINT_INCOMPLETE',
                      'receipt_hash':'REVALIDATION_RECEIPT_INTEGRITY_UNCONFIRMED'}
            if change in expected:assert expected[change] in {row['code'] for row in issues},issues
            record('rejected-'+change,{'selected':selected,'issues':issues,'launches':launches,'original_unchanged':True})
    asyncio.run(run())


@pytest.mark.parametrize('change',['prefix','changed_bytes','legacy_mtime_only','missing_context','authority'])
def test_output_version_evidence_is_not_strengthened_after_the_fact(change):
    async def run():
        async with executed('output-'+change,large_output=change=='prefix') as (f,plan,source,target,receipt,raw,launches):
            if change=='changed_bytes':target.write_text('Subsequent unrelated writer changed the result.')
            state=revise(f,plan)
            if change in {'legacy_mtime_only','missing_context'}:
                # Explicit legacy-format fixture copied from this synthetic
                # receipt. It is not an actual restored production receipt.
                copied=json.loads(raw)
                if change=='legacy_mtime_only':
                    for row in copied['after_files'].values():
                        for key in ('sha256','sample_sha256','hash_coverage','hash_coverage_bytes','path'):row.pop(key,None)
                else:copied.pop('review_step_context')
                legacy=f.root/('synthetic-'+change+'.json');legacy.write_text(json.dumps(copied))
                reg=state['provenance_state']['attempts'][receipt['attempt_id']]
                reg['command_receipt']=str(legacy);reg['command_receipt_sha256']=hashlib.sha256(legacy.read_bytes()).hexdigest()
            if change=='authority':
                issues=[]
                selected=revised_observations(state,state['steps'][0],f.root/'new-authorized-project',issues)
            else:
                selected,issues=select(f,state)
            if change=='missing_context':
                assert not selected and any(i['code']=='REVALIDATION_STEP_CONTEXT_UNAVAILABLE' for i in issues)
            else:
                assert len(selected)==1 and selected[0][0]['original_product_paths']==[]
                expected={'changed_bytes':'REVALIDATION_OUTPUT_VERSION_CHANGED','authority':'REVALIDATION_OUTPUT_AUTHORITY_CHANGED'}.get(change,'REVALIDATION_OUTPUT_FINGERPRINT_INCOMPLETE')
                assert expected in {i['code'] for i in issues},issues
            if change=='prefix':
                assert receipt['after_files'][str(target)]['hash_coverage']=='prefix'
                assert 'sha256' not in receipt['after_files'][str(target)]
            record('output-'+change,{'selected':selected,'issues':issues,'launches':launches,'legacy_copy':change in {'legacy_mtime_only','missing_context'}})
    asyncio.run(run())


def test_cancelled_old_output_does_not_block_reusing_the_observation():
    async def run():
        async with executed('cancel-output',large_output=True) as (f,plan,source,target,receipt,raw,launches):
            state=revise(f,plan,outputs=[]);selected,issues=select(f,state)
            assert len(selected)==1 and not selected[0][0]['original_product_paths']
            assert any(i['code']=='REVALIDATION_OUTPUT_FINGERPRINT_INCOMPLETE' for i in issues)
            assert state['steps'][0]['outputs']==[] and state['steps'][1]['outputs']
            record('cancel-output',{'selected':selected,'issues':issues,'current_outputs':[],'final_delivery_retained':True,'launches':launches})
    asyncio.run(run())


def test_explicit_machine_failure_cannot_be_laundered_into_natural_acceptance():
    async def run():
        async with executed('machine-rule',machine_rule=True) as (f,plan,source,target,receipt,raw,launches):
            assert '"ok": false' in receipt['stdout'] and receipt['returncode']==0
            state=revise(f,plan);selected,issues=select(f,state)
            assert not selected and any(i['code']=='REVALIDATION_MACHINE_RULE_CHANGED' for i in issues)
            record('machine-rule',{'issues':issues,'launches':launches,'old_stdout':receipt['stdout'],'original_unchanged':True})
    asyncio.run(run())




