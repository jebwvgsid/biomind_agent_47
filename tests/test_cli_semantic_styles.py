"""Display fragments preserve text and never turn model prose into execution facts."""
from copy import deepcopy

from agent_core.cli_display import DisplayModel
from agent_core.cli_terminal import Terminal
from prompt_toolkit.styles import DEFAULT_ATTRS


def test_public_completion_claim_does_not_get_a_tool_success_marker():
    text='模型合成说明：任务已完成。\n仍保留模型原文。'
    item=DisplayModel().receive('model_public',text,source='model_public')
    before=deepcopy(item.scope)
    fragments=Terminal.item_fragments(item)
    assert ''.join(t for t,a in fragments)==text+'\n\n'
    assert all(a==DEFAULT_ATTRS for t,a in fragments)
    assert item.text==text and item.scope==before and not item.confirmation.done()


def test_long_error_body_is_neutral_and_ansi_tool_text_is_still_sanitized():
    body='合成错误\n'+'long error detail\n'*100
    item=DisplayModel().receive('error',body)
    fragments=Terminal.item_fragments(item)
    assert fragments[0][0]=='× ' and fragments[0][1].color=='ansired'
    assert ''.join(t for t,a in fragments[1:])==item.text+'\n'
    assert all(a==DEFAULT_ATTRS for t,a in fragments[1:])
    tool=DisplayModel(verbosity='verbose').receive('tool_end','read /tmp/synthetic.txt\x1b[31m',status='returned')
    parts=Terminal.item_fragments(tool)
    assert '\x1b' not in ''.join(t for t,a in parts)
    assert next(a for t,a in parts if t=='/tmp/synthetic.txt').dim
    assert next(a for t,a in parts if t=='read ').color=='ansicyan'
    assert parts[0][1].color=='ansigreen'
    assert tool.status=='returned'
