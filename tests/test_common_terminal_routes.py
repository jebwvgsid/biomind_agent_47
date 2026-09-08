"""Route inventory acceptance: real production edges + outer + provider caller.

Node outcomes are synthetic typed facts; this suite tests terminal consumption,
not each producer's independent business decision policy.
"""
import asyncio,json
from copy import deepcopy
from pathlib import Path
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from agent_core.workflow import outcomes as o
from agent_core.workflow.runtime_graph import build_schema2_production_graph
from agent_core.workflow.managed_stop import current_managed_stop
from execution.profiling import RunProfiler
from tests.test_inspection_workflow import make_inspection_fixture
from tests.test_provider_attempt_reliability import _agent
from tests.test_common_finalization_workflow import SummaryModel

CLASSES={'triage':o.TriageOutcome,'consultant':o.ConsultantOutcome,'planner':o.PlannerOutcome,'plan_review':o.PlanReviewOutcome,'ask_user':o.AskUserOutcome,'install_subagent':o.InstallSubagentOutcome,'generate':o.GenerateOutcome,'execute':o.ExecuteOutcome,'supervisor':o.VerificationOutcome,'commit_step':o.CommitStepOutcome,'recovery':o.RecoveryOutcome,'final_audit':o.FinalAuditOutcome}
CASES=[('generate',k) for k in ('candidate_rejected','provider_failed','internal_protocol_error','blocked','harness_error','runtime_recovery_stopped')]+[('execute',k) for k in ('pre_execution_rejected','blocked','harness_error')]+[('recovery',k) for k in ('blocked','internal_protocol_error','harness_error','runtime_recovery_stopped')]+[('supervisor','harness_error'),('commit_step','harness_error')]+[('final_audit',k) for k in ('task_incomplete','entry_rejected','harness_error')]+[(n,k) for n in ('consultant','planner','plan_review','install_subagent') for k in ('terminal','harness_error')]+[('triage','harness_error'),('step_worker','blocked'),('step_worker','harness_error')]

CASES.extend([('triage','resume_classified'),('ask_user','harness_error')])

def typed(node,kind,task_id,**extra):
    if node=='step_worker':
        from agent_core.workflow.step_worker import StepOutcome
        return StepOutcome(schema_version=1,type=kind,summary='synthetic terminal',blocker={'reason':'fixture','error_code':'WORKER_FAILED'}).to_dict()
    cls=CLASSES[node]
    payload={k:'fixture' for k in cls.REQUIRED_PAYLOAD.get(kind,())}
    payload.update(error_code='SYNTHETIC_STOP',reason_code='SYNTHETIC_STOP',status='failed',terminal_status='failed',
        budget_receipt={},repairable=False,owner=node,violation_owner='INTERNAL_PROTOCOL',violation_code='PRELAUNCH_FAILED',
        execution_status='not_started',resolution_disposition='unresolved',explanation='Runtime discovery did not establish availability.',original_error='UNKNOWN')
    payload.update(extra)
    return cls(schema_version=1,type=kind,summary='Synthetic terminal fact; dependency not established.',scope={'task_id':task_id},payload=payload).to_dict()

async def route_case(tmp_path,node,kind,*,inject=None,payload=None,interrupt_at_node=False):
    f=make_inspection_fixture(tmp_path, create_attempt=False)
    # Use an existing safe managed identity, but do not claim its recorded attempt
    # as execution by a synthetic Generate/early terminal invocation.
    if node=='triage' and kind=='resume_classified':
        payload={**(payload or {}),'semantic_state':'terminal'}
        f.state['task_status']='failed'
    if node=='ask_user' and kind=='harness_error':
        payload={**(payload or {}),'semantic_state':'terminal'}
    target=typed(node,kind,f.task_id,**(payload or {}))
    values={};visits=[]
    async def handler(name,state):
        visits.append(name)
        if name==node:
            if interrupt_at_node:
                from langgraph.types import interrupt
                interrupt({'type':'code_review','content':'synthetic action awaiting approval','risk':{'level':'MEDIUM'}})
                raise AssertionError('No approval supplied')
            raw=target
        elif name=='triage':
            semantic={'generate':'step_execution_ready','execute':'action_execution_ready','supervisor':'verification_pending','recovery':'recovery_pending','final_audit':'final_audit_pending','planner':'planning_ready','plan_review':'plan_review_pending','install_subagent':'installation_pending','consultant':'consultation_ready','commit_step':'verification_pending','step_worker':'plan_review_pending','ask_user':'awaiting_user_input'}[node]
            raw=typed('triage','resume_classified',f.task_id,semantic_state=semantic)
        elif name=='supervisor' and node=='commit_step': raw=typed('supervisor','verified',f.task_id)
        elif name=='plan_review' and node=='step_worker': raw=typed('plan_review','approved_for_execution',f.task_id,execution_backend='external_worker')
        else: raise AssertionError(('unexpected node',name,visits))
        field='verification_outcome' if name=='supervisor' else name+'_outcome'
        return {field:raw}
    handlers={n:(lambda state,n=n:handler(n,state)) for n in (*CLASSES,'direct_response','step_worker')}
    graph=build_schema2_production_graph(handlers).compile(checkpointer=InMemorySaver())
    c={'configurable':{'thread_id':f.task_id},'recursion_limit':10}
    model=SummaryModel();agent=_agent(model,data_path=str(tmp_path),attempts=1)
    agent.llm_timeout_seconds=agent.llm_readonly_timeout_seconds=agent.llm_total_timeout_seconds=1
    agent.terminal_summary_llm=model;agent.terminal_summary_policy={'enabled':True}
    agent.wiki_enabled=False;agent.default_project_id="fixture";agent.state_manager=None;agent.profiler=RunProfiler(tmp_path/'task_outputs');agent.profiler.start_task(f.task_id)
    agent._app_for_task=lambda _:graph
    async def go(*args,**kwargs): await graph.ainvoke(deepcopy(f.state),c)
    agent.go=go
    if inject:inject(agent,f)
    result=await agent._run_agent_interrupt_loop('synthetic terminal routing',f.task_id)
    return f,agent,model,result,graph,c

@pytest.mark.parametrize('node,kind',CASES)
def test_all_terminal_routes_production_outer(tmp_path,node,kind):
    async def scenario():
        f,a,m,result,g,c=await route_case(tmp_path,node,kind)
        stop=current_managed_stop(f.reload())
        assert stop,(node,kind,result)
        assert stop['stage']==node and result.stage==node
        assert len(m.requests)==1
        assert result.failure_code and result.failure_code!='TASK_BLOCKED'
        assert stop['execution']['committed'] is False
    asyncio.run(scenario())

@pytest.mark.parametrize('node,kind', [('generate','cancelled'),('execute','cancelled'),('step_worker','cancelled'),('execute','lightweight_completed'),('plan_review','approval_pending'),('final_audit','task_verified'),('consultant','answer_ready'),('consultant','terminal')])
def test_pause_cancel_success_do_not_summarize(tmp_path,node,kind):
    async def scenario():
        f,a,m,result,g,c=await route_case(tmp_path,node,kind,payload={'status':'completed' if node=='consultant' else 'cancelled' if kind=='cancelled' else 'waiting_user' if kind=='approval_pending' else 'completed'})
        assert not m.requests
        assert not current_managed_stop(f.reload())
        if kind=='cancelled': assert result.status=='cancelled'
        if kind=='approval_pending': assert result.status=='waiting_user'
    asyncio.run(scenario())


def test_real_graph_interrupt_outer_wait_has_no_summary(tmp_path):
    from agent_core.human_io import HumanInputUnavailable
    async def scenario():
        def inject(agent,fixture):
            async def unavailable(*args,**kwargs): raise HumanInputUnavailable('synthetic unavailable human channel')
            agent._aprompt_user=unavailable
        f,a,m,result,g,c=await route_case(tmp_path,'generate','blocked',inject=inject,interrupt_at_node=True)
        assert result.status=='waiting_user'
        assert g.get_state(c).tasks[0].interrupts
        assert not m.requests and not current_managed_stop(f.reload())
    asyncio.run(scenario())

@pytest.mark.parametrize('kind',['answer_recorded','question_missing'])
def test_ask_user_unknown_continuation_end_is_wait_not_failure(tmp_path,kind):
    async def scenario():
        f,a,m,result,g,c=await route_case(tmp_path,'ask_user',kind,payload={'semantic_state':'awaiting_user_input'})
        assert result.status=='waiting_user' and not m.requests
        assert not current_managed_stop(f.reload())
    asyncio.run(scenario())

def test_caller_cancellation_propagates_without_summary(tmp_path):
    async def scenario():
        holder={}
        def inject(agent,fixture):
            holder.update(agent=agent,fixture=fixture)
            async def cancelled(*args,**kwargs): raise asyncio.CancelledError()
            agent.go=cancelled
        with pytest.raises(asyncio.CancelledError):
            await route_case(tmp_path,'generate','blocked',inject=inject)
        assert not holder['agent'].terminal_summary_llm.requests
        assert not current_managed_stop(holder['fixture'].reload())
    asyncio.run(scenario())
