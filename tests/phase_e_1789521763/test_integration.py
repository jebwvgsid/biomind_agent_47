"""Final consumers use real owners; only external model/science leaves are fixtures."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from langchain_core.messages import AIMessage
from agent_core.workflow.commit_step import CommitStep
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from tests.test_inspection_workflow import make_inspection_fixture, ReadThenRevalidateModel, InspectionReviewer, _tag_json
from tests.phase_e_1789521763.test_chains import MissingThenRepair, drive
from tests.phase_e_1789521763.test_c2 import receipt


def test_chain3_useful_pages_revalidate_same_attempt_commit(tmp_path, monkeypatch):
    monkeypatch.setenv('BIOMIND_RECOVERY_RESOURCE_POLICY','phase_e_candidate_v2')
    async def scenario():
        f=make_inspection_fixture(tmp_path);await f.inspect_handoff()
        original=deepcopy(f.execution)
        class Model(ReadThenRevalidateModel):
            async def ainvoke(self,messages,**kwargs):
                assert kwargs['max_tokens']==8192
                reply=await super().ainvoke(messages,**kwargs)
                reply.usage_metadata={'input_tokens':5000,'output_tokens':100,'total_tokens':5100}
                return reply
        model=Model();node=Schema2RecoveryNode(data_path=str(tmp_path),model=model)
        result=await node.run(deepcopy(f.state))
        assert result['recovery_outcome']['type']=='revalidation_required',result
        assert len(model.requests)>=3 and len(model.bodies)==4
        f.state.update(result);f.state.update(await f.new_supervisor()(deepcopy(f.state)))
        assert f.state['verification_outcome']['type']=='verified'
        supplied=deepcopy(f.state);commit=await CommitStep(data_path=str(tmp_path)).run(supplied)
        assert commit['commit_step_outcome']['type']=='next_step_ready'
        after=f.reload()
        assert await CommitStep(data_path=str(tmp_path)).run(supplied)==commit
        assert f.reload()['state_generation']==after['state_generation']
        assert f.execution==original and f.runner_dispatches==[]
        assert len(after['task_state']['typed_step_commit_receipts'])==1
        ledger=json.loads(next((tmp_path/'task_outputs'/f.task_id/'context_trace/recovery_transport').glob('resources-*.json')).read_text())
        assert len(ledger['attempts'])==len(model.requests)
        receipt('e-chain3',{'policy':ledger['policy'],'ledger':ledger,'recovery_calls':len(model.requests),
            'tools':ledger['business']['tool_call_count'],'execute_calls':0,'commits':1,'original_attempt':original['attempt_id'],
            'first_request':[m.model_dump() for m in model.requests[0]],'commit':commit,'replay_additional_commits':0})
    asyncio.run(scenario())


def test_chain4_reading_never_deciding_stops_real_graph_without_summary(monkeypatch):
    monkeypatch.setenv('BIOMIND_RECOVERY_RESOURCE_POLICY','phase_e_candidate_v2')
    class Pages(MissingThenRepair):
        def __init__(self):super().__init__('e-pages-stop')
        class Recovery:
            def __init__(self,case):self.case=case
            async def ainvoke(self,messages,**kwargs):
                c=self.case;c.counts['recovery']+=1
                seed=_tag_json(list(messages),'recovery_seed')
                ref=next(r['ref_id'] for r in seed['inspection_manifest'] if r.get('readable') and r.get('scope',{}).get('object_kind')=='step_attempt')
                n=c.counts['recovery']
                c.requests.append({'stage':'recovery','messages':[m.model_dump() for m in messages]})
                return AIMessage(content='继续读取下一页。',tool_calls=[{'name':'evidence_read','id':'page-'+str(n),
                    'args':{'evidence_ref':ref,'start_line':n,'end_line':n}}],
                    usage_metadata={'input_tokens':100000,'output_tokens':200,'total_tokens':100200})
    async def scenario():
        f=Pages();await f.setup()
        try:
            await drive(f)
            assert f.durable['task_state']['status']=='blocked',f.values
            assert len(f.executions)==2 and len(f.commits)==1
            assert f.counts['recovery']==4 and f.hook_hits.get('terminal_summary_provider',0)==0
            transport=f.root/'task_outputs'/f.task/'context_trace/recovery_transport'
            ledger=json.loads(next(transport.glob('resources-*.json')).read_text())
            assert ledger['business']['tool_call_count']==3 and ledger['terminal_reason']=='RECOVERY_RESOURCE_LIMIT'
            stop=f.values['recovery_outcome']['payload']['inspection_stop']
            assert stop['stop_reason']['model_turns']==4 and stop['stop_reason']['tool_calls']==3
            assert stop['stop_reason']['resource_receipt']['occupied_tokens']==400800
            assert 'dependency' not in stop['technical_classification']
            replay=await f.recovery_node.run(deepcopy(f.values))
            assert replay['recovery_outcome']['type']=='blocked' and f.counts['recovery']==4
            receipt('e-chain4',{'counts':f.counts,'executions':len(f.executions),'approvals':len(f.approvals),
                'commits':len(f.commits),'ledger':ledger,'stop':stop,'summary_calls':0,'terminal_status':'blocked'})
        finally:await f.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('when',['before_read','before_write','after_commit'])
def test_v19_cancellation_linearizes_at_normal_commit(tmp_path,when):
    async def scenario():
        from bio_agents import BioAnalystAgent
        from agent_core.plan_tracker import save_plan_state
        f=make_inspection_fixture(tmp_path);f.reviewer=InspectionReviewer(accept_first=True)
        f.state.update(await f.new_supervisor()(deepcopy(f.state)))
        assert f.state['verification_outcome']['type']=='verified'
        original=deepcopy(f.reload()['provenance_state']['attempts'])
        agent=BioAnalystAgent.__new__(BioAnalystAgent)
        agent.data_path=str(tmp_path);agent.state_manager=Mock();agent.profiler=Mock()
        def cancel():return agent._commit_user_cancellation(f.task_id,source='phase-e-offline-user',reason='explicit synthetic cancellation')
        if when=='before_read':cancel()
        def competing_write(data_path,task_id,candidate):
            cancel()
            return save_plan_state(data_path,task_id,candidate)
        if when=='before_write':
            with patch('agent_core.workflow.commit_step.save_plan_state',side_effect=competing_write):
                result=await CommitStep(data_path=str(tmp_path)).run(deepcopy(f.state))
        else:result=await CommitStep(data_path=str(tmp_path)).run(deepcopy(f.state))
        if when=='after_commit':
            assert result['commit_step_outcome']['type']=='next_step_ready'
            committed=deepcopy(f.reload()['task_state']['typed_step_commit_receipts']);cancel()
            assert f.reload()['task_state']['typed_step_commit_receipts']==committed
        else:
            assert result['commit_step_outcome']['type'] in {'contract_rejected','commit_conflict'},result
            assert not f.reload()['task_state'].get('typed_step_commit_receipts')
            assert f.reload()['provenance_state']['attempts']==original
        after=f.reload();assert after['task_state']['status']=='cancelled'
        replay=await CommitStep(data_path=str(tmp_path)).run(deepcopy(f.state))
        assert f.reload()==after and f.runner_dispatches==[]
        receipt('e-commit-cancel-'+when,{'commit':result,'late_replay':replay,'task_status':'cancelled',
            'commit_receipts':len(after['task_state'].get('typed_step_commit_receipts',{})),
            'execute_calls':0,'remote_cancel_billing':'UNKNOWN'})
    asyncio.run(scenario())
