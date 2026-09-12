"""Approval, stdout and receipt display invariants using synthetic input only."""
import asyncio
from copy import deepcopy
import hashlib
import io
from unittest.mock import patch

import pytest
from agent_core.cli_activity import call_with_activity, resource_display_result, glob_display_result, skill_display_result
from agent_core.cli_display import bind_terminal, StreamDisplayCopy
from agent_core.cli_terminal import PlainTerminal
from agent_core.cli_review import review_display, show_review
from tests.test_cli_v2_display import service_writer_callbacks


@pytest.mark.parametrize('mode', ['compact','verbose','off'])
@pytest.mark.parametrize('answer', ['y','n','q','e:print("replacement")','please revise'])
def test_v2_details_never_enter_approval_or_feedback(mode, answer):
    import bio_agents
    async def scenario():
        service_writer_callbacks(asyncio.get_running_loop())
        writer=io.StringIO(); owner=PlainTerminal(writer, enabled=mode!='off', verbosity=mode)
        previous=bind_terminal(owner)
        agent=bio_agents.BioAnalystAgent.__new__(bio_agents.BioAnalystAgent)
        agent.input_timeout_seconds=300; agent.data_path='synthetic-only'
        router=bio_agents.OutputRouter()
        original='print("合成测试")\n' * 2000
        payload={'content':original,'language':'Python','hash':hashlib.sha256(original.encode()).hexdigest()}
        snapshot=deepcopy(payload); seen=[]
        try:
            with patch.object(bio_agents,'output',router), patch.object(bio_agents,'safe_input',side_effect=['/details',answer]), patch.object(bio_agents,'clear_waiting_state',side_effect=lambda *args:seen.append(args)):
                router.prepare_review('代码审批', original, language='Python',risk='medium')
                actual=agent._prompt_user('synthetic-task','code_review','Approve?',payload=payload)
            await owner.drain(2)
            assert actual==answer and seen==[('synthetic-only','synthetic-task')]
            assert payload==snapshot
            if mode!='off':
                text=writer.getvalue()
                # This transport test mocks safe_input; presentation now belongs
                # to the real read owner (covered by the kernel-PTY tests).
                # Only the explicitly requested complete detail is written here.
                assert text.count('合成测试')==2000
                assert owner.review.body == original
                assert '需查看完整代码' in owner.review.summary and payload['hash'] in owner.review.summary
                assert '只读' not in text
        finally:bind_terminal(previous);owner.close()
    asyncio.run(scenario())


def test_v2_tool_classification_never_mutates_returned_observation():
    cases=[(resource_display_result,{'exists':False},'empty'),
           (resource_display_result,{'exists':True,'readable':False},'failed'),
           (resource_display_result,{'exists':True,'readable':True},'returned'),
           (glob_display_result,{'matches':['a','b'],'truncated':True},'partial'),
           (glob_display_result,{'matches':[]},'empty'),
           (skill_display_result,({}, {'status':'SKILL_CONTEXT_NO_PROGRESS','results':[]}), 'unknown')]
    events=[]
    with patch('agent_core.cli_activity.emit',side_effect=lambda *args,**kwargs:events.append((args,kwargs))):
        for projection,value,expected in cases:
            before=deepcopy(value)
            result=call_with_activity('合成读取',lambda:value,display_result=projection,
                display_scope={'tool_category':'read','tool_call_id':'one'})
            assert result is value and value==before and events[-1][1]['status']==expected
        error=RuntimeError('synthetic permission failure')
        def failed():raise error
        with pytest.raises(RuntimeError) as caught:call_with_activity('读取',failed)
        assert caught.value is error and events[-1][1]['status']=='failed'


@pytest.mark.parametrize('verbosity',['compact','verbose'])
def test_v2_stdout_chunks_and_final_observation_leave_raw_identical(verbosity):
    from bio_agents import OutputRouter
    async def scenario():
        service_writer_callbacks(asyncio.get_running_loop())
        writer=io.StringIO();owner=PlainTerminal(writer,verbosity=verbosity);previous=bind_terminal(owner)
        original=b'visible synthetic line\nBIOMIND_EFFECT_EVIDENCE_JSON={"value":true}\napi_key=synthetic-secret\n'
        before=hashlib.sha256(original).hexdigest()
        try:
            copy=StreamDisplayCopy('stdout',task_id='synthetic',execution_attempt_id='one')
            for part in [original[:31],original[31:53],original[53:]]:copy.feed(part)
            copy.feed(b'', final=True)
            OutputRouter().print_observation(original.decode(),summary='进程退出 rc=0，正式核验中')
            await owner.drain(1)
            shown=writer.getvalue()
            assert 'BIOMIND_EFFECT_EVIDENCE_JSON' not in shown
            assert 'synthetic-secret' not in shown
            assert '正式核验中' in shown
            assert hashlib.sha256(original).hexdigest()==before
        finally:bind_terminal(previous);owner.close()
    asyncio.run(scenario())


def test_v2_recovery_checkpoint_does_not_count_budget_projection_as_success(tmp_path):
    import bio_agents
    agent=bio_agents.BioAnalystAgent.__new__(bio_agents.BioAnalystAgent)
    agent.data_path=str(tmp_path)
    events=[];saved=[]
    payload={'task_id':'synthetic','tool':'evidence.glob','tool_request_id':'one',
        'status':'completed','truncated':False,
        'observation':{'error':{},'result':{'status':'RECOVERY_OBSERVATION_BUDGET_EXCEEDED'}}}
    original=deepcopy(payload)
    class Store:
        def __init__(self,*args):pass
        def record_event(self,*args,**kwargs):saved.append((args,kwargs))
    with patch.object(bio_agents,'ContextLineageStore',Store), patch.object(bio_agents,'emit_display',side_effect=lambda *args,**kwargs:events.append((args,kwargs))):
        agent._schema2_recovery_event_sink('recovery_tool_observation_committed',payload)
    assert payload==original and len(saved)==1
    assert events[-1][1]['status']=='partial' and events[-1][1]['tool_category']=='search'
    assert 'RECOVERY_OBSERVATION_BUDGET_EXCEEDED' in events[-1][0][1]


def test_v2_one_second_approval_timeout_is_never_rounded_to_unbounded():
    import bio_agents
    agent=bio_agents.BioAnalystAgent.__new__(bio_agents.BioAnalystAgent)
    agent.data_path='synthetic';agent.input_timeout_seconds=1;agent.state_manager=None
    timeouts=[]
    def input_value(*args,**kwargs):timeouts.append(kwargs['timeout_seconds']);return '/details'
    with patch.object(bio_agents,'interactive_terminal',return_value=None), patch.object(bio_agents.time,'monotonic',side_effect=[10,10.001,11.01]), patch.object(bio_agents,'safe_input',side_effect=input_value), patch.object(bio_agents.output,'show_review_details'), patch.object(bio_agents,'save_waiting_state',return_value='synthetic-wait'), patch.object(bio_agents,'restore_terminal_state'), patch.object(bio_agents,'display_print'):
        with pytest.raises(bio_agents.HumanInputTimeout):
            agent._prompt_user('synthetic-task','code_review','Approve?')
    assert timeouts==[1]


def test_v2_broken_review_modal_falls_back_to_full_body():
    from agent_core.cli_review import show_review
    class Broken:
        mode='tty';fallback=False
        def show_details(self,**kwargs):raise RuntimeError('synthetic renderer failure')
    printed=[]
    view=review_display('代码审批','line one\nline two',language='Python')
    with patch('agent_core.cli_review.terminal',return_value=Broken()), patch('agent_core.cli_review.emit',return_value=True), patch('agent_core.cli_review.display_print',side_effect=lambda value:printed.append(value)):
        show_review(view)
    assert printed==['代码审批','line one','line two']
