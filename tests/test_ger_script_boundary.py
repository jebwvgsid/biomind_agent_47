"""Exact candidate source and controlled, non-executing syntax checks."""
import asyncio
import hashlib
import subprocess
from unittest.mock import patch

import pytest

from agent_core.controller_audit import parse_executable_script_block, ScriptBlockError
from execution.script_validation import check_script_body


@pytest.mark.parametrize('raw', [
    '<script language="bash">true</script>',
    '<shell><script language="bash">true</script></shell>',
    '<shell><script language="bash">true</shell>',
    '<shell>true</shell><shell>false</shell>',
])
def test_invalid_wrappers_cannot_be_approved(raw):
    try:
        block=parse_executable_script_block(raw)
    except ScriptBlockError:
        return
    assert check_script_body(block.body,block.language)['status']=='invalid'


@pytest.mark.parametrize('body', [
    "cat <<'DATA'\n<script language=\"bash\">中文</script>\n<shell>literal</shell>\nDATA\n",
    "printf '%s\\n' '<script language=\"bash\">合法字符串</script>'\n",
    "# <script language=\"bash\">comment</script>\nprintf 'ok\\n'\n",
])
def test_script_data_is_not_treated_as_transport(body):
    block=parse_executable_script_block('<shell>'+body+'</shell>')
    assert block.body==body
    receipt=check_script_body(body,'bash')
    assert receipt['status']=='valid'
    assert receipt['body_sha256']==hashlib.sha256(body.encode()).hexdigest()


def test_syntax_check_does_not_execute_or_source_startup(tmp_path,monkeypatch):
    marker=tmp_path/'sentinel'; inject=tmp_path/'injection.sh'; inject.write_text(f'touch {marker}\n')
    monkeypatch.setenv('BASH_ENV',str(inject)); monkeypatch.setenv('ENV',str(inject))
    assert check_script_body(f'touch {marker}\n','bash')['status']=='valid'
    assert check_script_body("echo 'unclosed",'bash')['status']=='invalid'
    assert not marker.exists()


@pytest.mark.parametrize('failure,code', [(FileNotFoundError(),'SCRIPT_SYNTAX_CHECK_UNAVAILABLE'),
    (subprocess.TimeoutExpired('bash',1),'SCRIPT_SYNTAX_CHECK_TIMEOUT')])
def test_checker_failure_is_not_scientific_failure(failure,code):
    with patch('execution.script_validation.subprocess.run',side_effect=failure):
        result=check_script_body('true','bash')
    assert result['status']=='unavailable' and result['code']==code
    assert result['execution_started'] is False
    assert 'returncode' not in result


def test_python_is_parsed_without_execution_and_r_is_explicit(tmp_path):
    body=f"open({str(tmp_path/'sentinel')!r},'w').write('bad')"
    assert check_script_body(body,'python')['status']=='valid'
    assert not (tmp_path/'sentinel').exists()
    assert check_script_body('if :','python')['status']=='invalid'
    assert check_script_body('x <- 1','r')['status']=='unavailable'


def test_real_generate_entry_repairs_wrapper_and_syntax_with_durable_budget():
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    async def run():
        case=PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
        await case.asyncSetUp()
        try:
            from tests.ger_fixture_support import configure_protocol, generate_until_ready_or_stopped
            configure_protocol(case)
            def candidate(payload,ordinal):
                if ordinal==1:return '<shell><script language="bash">true</script></shell>'
                if ordinal==2:return "<shell>echo 'unclosed</shell>"
                return case._script_action(payload)
            events,captures,_=await generate_until_ready_or_stopped(case,'ger-wrapper-and-syntax',candidate)
            text=str(events)
            assert len(captures)>=3,text
            assert 'GENERATE_SCRIPT_BLOCK_INVALID' in str(captures[1])
            assert 'GENERATE_SCRIPT_SYNTAX_INVALID' in str(captures[2])
            assert not any('script language=' in str(e.get('execute',{}).get('execute_outcome',{}).get('payload',{}).get('execution_record',{}).get('code','')) for e in events)
            from agent_core.plan_tracker import load_plan_state
            durable=load_plan_state(str(case.root),'ger-wrapper-and-syntax')
            ledger=durable['task_state']['candidate_repair_state']
            assert len(ledger.get('events',{}))>=2
        finally:
            await case.asyncTearDown()
    asyncio.run(run())


def test_identical_candidate_budget_survives_durable_reload():
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from tests.ger_fixture_support import configure_protocol, generate_until_ready_or_stopped
    from agent_core.plan_tracker import load_plan_state
    async def run():
        case=PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
        await case.asyncSetUp()
        try:
            configure_protocol(case)
            events,captures,_=await generate_until_ready_or_stopped(case,'ger-same-error',lambda *_:'invalid protocol')
            assert len(captures)==2
            assert 'GENERATE_IDENTICAL_CANDIDATE_EXHAUSTED' in str(events)
            assert not any('execute' in event for event in events)
            durable=load_plan_state(str(case.root),'ger-same-error')
            again=load_plan_state(str(case.root),'ger-same-error')
            assert again['task_state']['candidate_repair_state']==durable['task_state']['candidate_repair_state']
            assert durable['task_state']['candidate_repair_state']['events']
            assert not durable['provenance_state']['attempts']
        finally:await case.asyncTearDown()
    asyncio.run(run())
