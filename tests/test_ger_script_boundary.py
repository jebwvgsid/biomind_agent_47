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




