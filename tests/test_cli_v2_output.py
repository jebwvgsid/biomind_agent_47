"""Approval, stdout and receipt display invariants using synthetic input only."""
import asyncio
from copy import deepcopy
import hashlib
import io
from unittest.mock import patch

import pytest
from agent_core.cli_activity import call_with_activity, resource_display_result, glob_display_result, skill_display_result
from agent_core.cli_display import bind_terminal, StreamDisplayCopy
from agent_core.cli_review import review_display, show_review




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
