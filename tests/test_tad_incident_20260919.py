"""Replay archived candidates without executing TAD or changing their scripts."""
import asyncio
import ast
import concurrent.futures
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_core.step_effects import declared_effect_emission_contract, materialize_effect_template
from agent_core.controller_audit import parse_executable_script_block
from agent_core.public_response import project_generate_response


def template():
    return declared_effect_emission_contract({'steps': [{'step_id': 'preflight',
        'produces_observations': [{'identity': 'hicexplore_env_ready',
                                  'evidence_contract': {'required_fields': ['value']}}]}]}, 'preflight')


def test_real_conda_heredoc_repairs_are_recognized_without_changing_commands():
    rows = json.loads((Path(__file__).parent/'fixtures/tad_20260919/generate_responses.json').read_text())
    for index in (0, 1, 3):
        projection = project_generate_response(rows[index]['content'], script_protocol=True)
        assert not projection.error
        script = parse_executable_script_block(projection.payload)
        body = script.body
        if index == 0:
            with pytest.raises(ValueError, match='EFFECT_HELPER_REQUIRES_SINGLE_QUOTED_PYTHON_HEREDOC'):
                materialize_effect_template(body, template(), language='bash')
        else:
            helper = template()
            actual = materialize_effect_template(body, helper, language='bash')
            assert actual == body.replace('# BIOMIND_EFFECT_HELPER', helper['python_template'].rstrip())
            assert 'conda run -n hicexplore --no-capture-output python - ' in actual
            assert 'HC_RC=$?' in actual and 'HELP_RC=$?' in actual


@pytest.mark.parametrize('prefix', [
    'conda run -n fixed --no-capture-output python -',
    '/opt/conda/bin/conda run --prefix /opt/env --live-stream /opt/env/bin/python3 -I -',
    '/usr/bin/python3 -',
])
def test_supported_stdin_launch_is_expanded_without_execution(prefix):
    text = prefix + " <<'PY'\n# BIOMIND_EFFECT_HELPER\nprint('untouched')\nPY\n"
    result = materialize_effect_template(text, template(), language='bash')
    assert result.startswith(prefix + " <<'PY'\n") and result.endswith("print('untouched')\nPY\n")


@pytest.mark.parametrize('prefix', [
    'conda run -n env --no-capture-output python -c "print(1)" -',
    'conda run -n env --no-capture-output python -m json.tool -',
    'conda run -n env --no-capture-output python script.py -',
    'conda run -n env --no-capture-output bash -',
    'conda run -n "$ENV" --no-capture-output python -',
    '"$ROOT/conda" run -n env --no-capture-output python -',
    'conda run --no-capture-output python -',
    'conda run -n env python -',
    'conda run -n env --cwd /tmp --no-capture-output python -',
    'conda run -n env --no-capture-output python -; cat -',
])
def test_launch_scope_is_not_inferred_from_embedded_python_word(prefix):
    with pytest.raises(ValueError, match='EFFECT_HELPER_'):
        materialize_effect_template(prefix + " <<'PY'\n# BIOMIND_EFFECT_HELPER\nPY\n", template(), language='bash')




