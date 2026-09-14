"""F4: real constructor callback/adapters, conditional edge and stop consumer."""
import asyncio
from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _pulse():
    loop = asyncio.get_running_loop()
    def pulse():
        if not loop.is_closed():
            loop.call_later(.001, pulse)
    pulse()


class _Model:
    def __init__(self): self.calls = 0
    def bind(self, **kwargs): return self
    def model_copy(self, update=None): return self
    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        raise AssertionError('F4 requires no provider call')


def _agent(root, stack):
    import bio_agents
    model = _Model()
    stack.enter_context(patch.object(bio_agents, 'init_chat_model', return_value=model))
    stack.enter_context(patch.object(bio_agents, 'BIOMIND_MEMORY_DIR', str(root/'memory')))
    stack.enter_context(patch.object(bio_agents, 'BIOMIND_WIKI_ENABLED', False))
    agent = bio_agents.BioAnalystAgent(data_path=str(root), enable_persistence=False, use_llm_supervisor=False)
    agent.terminal_summary_policy = {'enabled': False}
    return agent, model


def _save(name, value):
    import hashlib, sys
    root=Path(os.environ['BIOMIND_GER_ARTIFACTS'])
    loaded={name:{'path':mod.__file__,'sha256':hashlib.sha256(Path(mod.__file__).read_bytes()).hexdigest()}
        for name,mod in tuple(sys.modules.items()) if getattr(mod,'__file__',None) and
        (name=='bio_agents' or name.startswith('agent_core.')) and str(mod.__file__).endswith('.py')}
    (root/('f4-'+name+'.json')).write_text(json.dumps({'loaded':loaded,**value},ensure_ascii=False,indent=2,default=str))


@pytest.mark.parametrize('fault', [OSError, ValueError])
def test_f4_registered_callback_exception_reaches_real_graph_end_and_stop(tmp_path, fault):
    async def run():
        _pulse()
        import bio_agents
        from langgraph.graph import StateGraph, START, END
        from agent_core.state import AgentState
        from agent_core.workflow.runtime_graph import route_ask_user_outcome
        from agent_core.workflow.managed_stop import observe_managed_node, terminal_boundary
        from agent_core.plan_tracker import load_plan_state
        with ExitStack() as stack:
            agent,model = _agent(tmp_path, stack)
            task='f4-source-'+fault.__name__.lower()
            agent._persist_user_submission(task, 'Synthetic unresolved question; no execution authorized.')
            agent.profiler.start_task(task)
            counts={'consultant':0,'callback':0}
            def fail(_payload):
                counts['callback'] += 1
                raise fault('synthetic AskUser input callback failure')
            stack.enter_context(patch.object(bio_agents,'interrupt',side_effect=fail))
            async def consultant(_state):
                counts['consultant'] += 1
                raise AssertionError('harness failure must not reconsult')
            graph=StateGraph(AgentState)
            graph.add_node('ask_user',observe_managed_node('ask_user',agent._schema2_early_node_handlers['ask_user']))
            graph.add_node('consultant',consultant)
            graph.add_edge(START,'ask_user')
            async def route(state):return route_ask_user_outcome(state)
            graph.add_conditional_edges('ask_user',route,['consultant',END])
            graph.add_edge('consultant',END)
            initial={'task_id':task,'pending_human_question':{'question_id':'q-current','question':'请明确合成参数 / synthetic parameter','type':'consultant_question'}}
            try:
                final=await graph.compile().ainvoke(initial)
                outcome=final['ask_user_outcome'];boundary=terminal_boundary(final)
                assert outcome['type']=='harness_error'
                assert outcome['payload']['error_code']=='FRONT_HALF_NODE_HARNESS_ERROR'
                assert route_ask_user_outcome(final)==END and boundary['status']=='failed'
                terminal=await agent._finalize_managed_terminal(task,SimpleNamespace(values=final))
                assert terminal['status']=='failed'
                durable=load_plan_state(str(tmp_path),task)
                assert not durable.get('approval_scope') and not durable.get('provenance_state',{}).get('attempts')
                assert counts=={'consultant':0,'callback':1} and model.calls==0
                _save(fault.__name__,{'outcome':outcome,'boundary':boundary,'terminal':terminal,'counts':counts,'provider_calls':model.calls,
                    'entry':'real registered AskUser handler in isolated StateGraph segment; actual outer finalizer',
                    'substitute':'input callback raises deliberate ordinary exception; summary disabled; no provider/science'})
            finally:await agent.aclose(timeout_seconds=1)
    asyncio.run(run())


def test_f4_actual_answer_missing_question_and_old_answer_stay_distinct(tmp_path):
    async def run():
        _pulse()
        import bio_agents
        from agent_core.workflow.early_nodes import TypedAskUser
        from agent_core.workflow.runtime_graph import route_ask_user_outcome
        from agent_core.workflow.managed_stop import observe_managed_node,terminal_boundary
        with ExitStack() as stack:
            agent,model=_agent(tmp_path,stack);task='f4-answer'
            agent._persist_user_submission(task,'Synthetic scientific parameter requires clarification.')
            state={'task_id':task,'pending_human_question':{'question_id':'q-current','question':'Which synthetic threshold?','type':'consultant_question'}}
            try:
                with patch.object(bio_agents,'interrupt',return_value='Use synthetic threshold 7'):
                    answered=await agent._schema2_early_node_handlers['ask_user'](state)
                assert answered['ask_user_outcome']['type']=='answer_recorded'
                receipt=answered['consultation_state']['pending_question']
                assert receipt['answer_turn_ref'] and receipt['question_id']=='q-current'
                assert route_ask_user_outcome(answered)=='consultant'
                no_question=await agent._schema2_early_node_handlers['ask_user']({'task_id':task})
                assert no_question['ask_user_outcome']['type']=='question_missing'
                assert route_ask_user_outcome(no_question)=='consultant'
                async def missing(_):return {}
                async def old_answer(_):return deepcopy(answered)
                rows={}
                for name,source,current in [('missing',missing,state),('old_question',old_answer,{**state,'pending_human_question':{**state['pending_human_question'],'question_id':'q-new'}})]:
                    update=await observe_managed_node('ask_user',TypedAskUser(data_path=str(tmp_path),source=source).run)(current)
                    assert update['ask_user_outcome']['payload']['error_code']=='ASK_USER_ANSWER_RECEIPT_MISSING'
                    assert route_ask_user_outcome(update)=='__end__' and terminal_boundary(update)['status']=='failed'
                    rows[name]=update['ask_user_outcome']
                assert model.calls==0
                _save('answers',{'actual_answer':answered['ask_user_outcome'],'answer_turn_receipt':receipt,'question_missing':no_question['ask_user_outcome'],'negative_receipts':rows,'provider_calls':0})
            finally:await agent.aclose(timeout_seconds=1)
    asyncio.run(run())


@pytest.mark.parametrize('kind',['cancel','interrupt'])
def test_f4_callback_cancel_and_graph_interrupt_propagate(tmp_path,kind):
    async def run():
        _pulse()
        import bio_agents
        from langgraph.errors import GraphInterrupt
        with ExitStack() as stack:
            agent,model=_agent(tmp_path,stack);task='f4-control-'+kind
            agent._persist_user_submission(task,'Synthetic question only.')
            error=asyncio.CancelledError() if kind=='cancel' else GraphInterrupt(())
            try:
                with patch.object(bio_agents,'interrupt',side_effect=error):
                    with pytest.raises(type(error)):
                        await agent._schema2_early_node_handlers['ask_user']({'task_id':task,'pending_human_question':{'question_id':'q-control','question':'Synthetic?'}})
                assert model.calls==0
                _save(kind,{'propagated':type(error).__name__,'provider_calls':0,'approved':False})
            finally:await agent.aclose(timeout_seconds=1)
    asyncio.run(run())


def test_f4_real_question_interrupt_and_eof_wait_without_answer_or_approval(tmp_path):
    async def run():
        _pulse()
        import io
        from langgraph.graph import StateGraph,START,END
        from langgraph.checkpoint.memory import MemorySaver
        from agent_core.state import AgentState
        from agent_core.cli_plain import PlainTerminal
        from agent_core.cli_display import bind_terminal
        from agent_core.human_io import HumanInputEOF
        from agent_core.plan_tracker import load_plan_state
        from agent_core.workflow.runtime_graph import route_ask_user_outcome
        with ExitStack() as stack:
            agent,model=_agent(tmp_path,stack);task='f4-real-eof'
            agent._persist_user_submission(task,'Synthetic unresolved parameter; no execution.')
            graph=StateGraph(AgentState);graph.add_node('ask_user',agent._schema2_early_node_handlers['ask_user'])
            graph.add_edge(START,'ask_user');graph.add_edge('ask_user',END)
            app=graph.compile(checkpointer=MemorySaver());config={'configurable':{'thread_id':task}}
            initial={'task_id':task,'pending_human_question':{'question_id':'q-eof','question':'Synthetic threshold?'}}
            rfd,wfd=os.pipe();os.close(wfd)
            with os.fdopen(rfd,'r') as stream:
                owner=PlainTerminal(io.StringIO());previous=bind_terminal(owner)
                try:
                    suspended=await app.ainvoke(initial,config=config)
                    assert suspended.get('__interrupt__') and not suspended.get('ask_user_outcome')
                    with patch('agent_core.cli_plain.sys.stdin',stream):
                        with pytest.raises(HumanInputEOF):
                            agent._prompt_user(task,'consultant','Synthetic threshold? ',payload=initial['pending_human_question'])
                    snapshot=await app.aget_state(config)
                    durable=load_plan_state(tmp_path,task)
                    assert snapshot.next==('ask_user',)
                    assert not durable.get('approval_scope') and not durable.get('provenance_state',{}).get('attempts')
                    assert not any(e['phase']=='decision_received' for e in owner.approval_events)
                    assert model.calls==0
                    _save('eof',{'graph_next':snapshot.next,'interrupted':True,'input_exception':'HumanInputEOF',
                        'decision_events':list(owner.approval_events),'provider_calls':0,'answer_recorded':False,
                        'resume_dispatched':False,'entry':'real AskUser graph interrupt then actual prompt/input on newly closed synthetic pipe'})
                finally:owner.close();bind_terminal(previous);await agent.aclose(timeout_seconds=1)
    asyncio.run(run())
