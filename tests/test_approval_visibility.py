"""Focused parser/automatic-policy checks beside the real PTY regression.

Run through cli_v2_support.py from an explicit isolated source export. PTY
visibility assertions live in replay_approval_visibility_pty.py; these checks
cover unchanged admission semantics without replacing that replay.
"""
from unittest.mock import patch
import pytest


@pytest.mark.parametrize('response, expected', [
    ('', 'approve'), ('   ', 'approve'), ('y', 'approve'), ('yes', 'approve'),
    ('n', 'reject'), ('n: keep the current input', 'reject'), ('q', 'quit'),
    ('e: printf edited', 'edit'), ('edit: printf edited', 'edit'),
    ('please use the current scope', 'reject'),
    ('this is pasted feedback\n/details\nkeep the original files', 'reject'),
    (None, 'reject'),
])
def test_original_approval_parser_controls(response, expected):
    from agent_core.middleware import HITLMiddleware
    code="printf 'unchanged candidate\\n'"
    action, selected=HITLMiddleware(auto_approve_safe=False)._parse_code_response(response, code)
    assert action==expected
    if action=='approve':assert selected==code
    if action=='edit':assert selected=='printf edited'
    if response is None:assert action!='approve'


def test_existing_safe_automatic_approval_and_unknown_manual_scope():
    from agent_core.middleware import HITLMiddleware
    import agent_core.middleware as middleware
    from execution.confirmation import RiskAnalyzer, RiskLevel
    safe="echo 'synthetic read-only fixture'"
    unknown="printf 'synthetic unknown command'"
    assert RiskAnalyzer.analyze(safe,'Bash')['risk_level']==RiskLevel.SAFE
    assert RiskAnalyzer.analyze(unknown,'Bash')['risk_level']!=RiskLevel.SAFE
    with patch.object(middleware,'interrupt',side_effect=AssertionError('automatic branch must not request input')):
        assert HITLMiddleware(auto_approve_safe=True).review_code(safe,'Bash')==('approve',safe)
    with patch.object(middleware,'interrupt',return_value='n') as reader:
        assert HITLMiddleware(auto_approve_safe=True).review_code(unknown,'Bash')[0]=='reject'
        assert reader.call_count==1
        assert reader.call_args.args[0]['editable_content']==unknown
    with patch.object(middleware,'interrupt',return_value='n') as reader:
        assert HITLMiddleware(auto_approve_safe=False).review_code(safe,'Bash')[0]=='reject'
        assert reader.call_count==1
