"""Deterministic framing and transient input errors through the production owner."""
import io
from unittest.mock import patch
import pytest
from agent_core.cli_plain import PlainTerminal
from agent_core.human_io import HumanInputEOF, HumanInputTimeout


@pytest.mark.parametrize('wire,expected',[
    ('\x1b[200~first\nlast\n\n\x1b[201~\n','first\nlast\n\n'),
    ('\x1b[200~first\r\nlast\r\n\r\n\x1b[201~\r\n','first\nlast\n\n'),
])
def test_framed_task_tail_and_intentional_identical_feedback(wire,expected):
    owner=PlainTerminal(io.StringIO(),enabled=False)
    with patch('sys.stdin',io.StringIO(wire+'last\ny\n')):
        try:
            owner.set_context('synthetic','interactive_prompt',{})
            assert owner.read('Task > ')==expected
            owner.set_context('synthetic','plan_review',{})
            assert owner.read('Feedback > ')=='last'  # Deliberate repeat is valid.
            assert owner.read('Approve > ')=='y'
        finally:owner.close()


@pytest.mark.parametrize('wire',['','y','\x1b[200~unfinished\n','\x1b[200~\n\n'])
def test_eof_and_incomplete_paste_are_never_blank_approval(wire):
    owner=PlainTerminal(io.StringIO(),enabled=False)
    with patch('sys.stdin',io.StringIO(wire)):
        try:
            owner.set_context('synthetic','code_review',{})
            with pytest.raises(HumanInputEOF):owner.read('Approve > ')
        finally:owner.close()


def test_explicit_multiline_preserves_blank_lines_and_next_decision():
    owner=PlainTerminal(io.StringIO(),enabled=False)
    with patch('sys.stdin',io.StringIO('first\nlast\n\n/end\ny\n')):
        try:
            assert owner.read('Block > ',multiline=True)=='first\nlast\n'
            assert owner.read('Approve > ')=='y'
        finally:owner.close()


def test_no_paste_drain_called_by_production_prompt(tmp_path):
    import bio_agents
    from agent_core.cli_display import bind_terminal
    agent=bio_agents.BioAnalystAgent.__new__(bio_agents.BioAnalystAgent)
    agent.input_timeout_seconds=0;agent.data_path=str(tmp_path);agent.state_manager=None
    owner=PlainTerminal(io.StringIO(),enabled=False);previous=bind_terminal(owner)
    try:
        with patch('sys.stdin',io.StringIO('task\n')),patch.object(bio_agents,'read_pending_paste_lines',side_effect=AssertionError('timing heuristic called')):
            assert agent._prompt_user('synthetic','interactive_prompt','Task > ')=='task'
    finally:bind_terminal(previous);owner.close()


def test_preexisting_pipe_tail_is_retained_and_cannot_approve(monkeypatch):
    import os
    import queue
    import threading
    import time
    r,w=os.pipe()
    stdin=os.fdopen(r,'r')
    owner=PlainTerminal(io.StringIO(),enabled=False)
    monkeypatch.setattr('sys.stdin',stdin)
    owner.set_context('synthetic','code_review',{})
    os.write(w,b'last requirement\n\ny\n')
    result=queue.Queue()
    def read():
        try:result.put(owner.read('Approve > '))
        except BaseException as exc:result.put(exc)
    thread=threading.Thread(target=read,daemon=True);thread.start()
    try:
        deadline=time.monotonic()+3
        while sum(e['phase']=='draft_retained' for e in owner.approval_events)<3:
            assert time.monotonic()<deadline
            time.sleep(.01)  # Fixture waits for explicit owner events, not framing.
        assert result.empty()
        assert any('last requirement' in row for row in owner.model.history)
        os.write(w,b'y\n')
        assert result.get(timeout=3)=='y'
    finally:owner.close();os.close(w);thread.join(timeout=3);stdin.close()


@pytest.mark.parametrize('body',['','\n\n','  '])
def test_blank_paste_is_not_the_explicit_empty_approval(body):
    owner=PlainTerminal(io.StringIO(),enabled=False)
    with patch('sys.stdin',io.StringIO('\x1b[200~'+body+'\x1b[201~\ny\n')):
        try:
            owner.set_context('synthetic','code_review',{})
            assert owner.read('Approve > ')=='y'
            assert any(e['phase']=='draft_retained' for e in owner.approval_events)
        finally:owner.close()
