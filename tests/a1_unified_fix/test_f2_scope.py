"""F2 actual Python scope: new tiny processes and the real materializer.

Negative process witnesses are explicitly constructed reproductions of the old
literal substitution, never admitted candidates or full graph success claims.
All script/module paths are new synthetic files and all shells are non-login.
"""
import asyncio
import json
import os
from pathlib import Path
import shlex
import sys

import pytest

from agent_core.step_effects import (
    EFFECT_HELPER_MARKER,
    declared_effect_emission_contract,
    extract_structured_effect_evidence,
    materialize_effect_action,
)
from tests.test_preflight_recovery_audit import PYTHON, approved_tiny


def frozen():
    step = {'step_id': 'synthetic-scope', 'produces_observations': [
        {'identity': 'scope_probe', 'evidence_contract': {'required_fields': ['value']}}]}
    return declared_effect_emission_contract({'steps': [step]}, step['step_id'])


def save(name, value):
    root = Path(os.environ['BIOMIND_GER_ARTIFACTS'])
    (root / ('f2-scope-' + name + '.json')).write_text(json.dumps(value, indent=2) + '\n')


def tiny(path, body):
    try:
        return asyncio.run(approved_tiny(path, body))
    finally:
        sys.modules['__main__'].OFFLINE_ALLOWED_SCRIPT_PATHS.discard(str(path))


@pytest.mark.parametrize('mode', ['python_string', 'heredoc_string', 'command', 'module', 'file'])
def test_nonexecuted_helper_scope_is_rejected_and_old_substitution_fails(mode, tmp_path):
    template = frozen()
    call = "print(emit_effect_evidence({'produces_observations': {'scope_probe': {'value': False}}}))\n"
    probe = tmp_path / 'new_scope_probe.py'
    probe.write_text(call)
    if mode in {'python_string', 'heredoc_string'}:
        body = "ignored = '''\n" + EFFECT_HELPER_MARKER + "\n'''\n" + call
        prefix = [PYTHON, '-I', '-S', '-']
    else:
        body = EFFECT_HELPER_MARKER + '\n'
        prefix = [PYTHON, '-I', '-S'] + {
            'command': ['-c', call, '-'],
            'module': ['-m', 'json.tool', '-'],
            'file': [str(probe), '-'],
        }[mode]
    script = body if mode == 'python_string' else shlex.join(prefix) + " <<'SCOPE'\n" + body + 'SCOPE\n'
    action = {'type': 'execute', 'code': script, 'language': 'python' if mode == 'python_string' else 'bash', 'argv': []}
    error = ('EFFECT_HELPER_REQUIRES_PYTHON_COMMENT_SCOPE' if mode in {'python_string', 'heredoc_string'}
             else 'EFFECT_HELPER_REQUIRES_SINGLE_QUOTED_PYTHON_HEREDOC')
    with pytest.raises(ValueError, match=error):
        materialize_effect_action(action, template)
    # Reproduce the former byte replacement only in a fresh test script. This
    # witness establishes why the corrected admission gate must reject it.
    old_substitution = script.replace(EFFECT_HELPER_MARKER, template['python_template'].rstrip())
    command = ('exec ' + shlex.join([PYTHON, '-I', '-S', '-c', old_substitution])
               if mode == 'python_string' else old_substitution)
    out, err, rc = tiny(tmp_path / 'old-scope-witness.sh', command)
    expected_error = 'Expecting value' if mode == 'module' else "name 'emit_effect_evidence' is not defined"
    assert rc == 1 and expected_error in err
    assert 'BIOMIND_EFFECT_EVIDENCE_JSON=' not in out
    save(mode, {'fixed_materializer': error, 'old_literal_substitution_witness': {'rc': rc, 'stdout': out, 'stderr': err},
                'scope': 'component rejection plus deliberately nonadmitted old-substitution process; no approval/graph/scientific success'})


@pytest.mark.parametrize('mode', ['argv_python', 'heredoc_flags', 'heredoc_cluster', 'heredoc_x_stdin'])
def test_actual_comment_scope_and_stdin_selector_preserve_arguments(mode, tmp_path):
    expected = ['space and 中文', 'single\'double"', '-', ';']
    body = "label = '''a string completed before the real marker'''\n" + EFFECT_HELPER_MARKER + '\nimport sys\n'
    body += 'assert sys.argv[1:] == ' + repr(expected) + '\n'
    body += "items = [value for value in sys.argv[1:] if value == 'absent-value']\n"
    body += "print(emit_effect_evidence({'produces_observations': {'scope_probe': {'value': bool(items), 'items': items}}}))\n"
    if mode == 'argv_python':
        action = {'type': 'execute', 'argv': [PYTHON, '-I', '-S', '-c', body, *expected]}
    else:
        flags = ['-I', '-S', '-P', '-W', 'ignore', '-X', 'dev', '-'] if mode == 'heredoc_flags' else ['-IS', '-Wignore', '-Xdev', '--', '-']
        if mode == 'heredoc_x_stdin':
            flags = ['-I', '-S', '-x', '-']
            # On the tested Python, -x does not skip the first stdin source
            # line. Put the marker there to preserve that measured boundary.
            body = body.split('\n', 1)[1]
        script = shlex.join([PYTHON, *flags, *expected]) + " <<'SCOPE'\n" + body + 'SCOPE\n'
        action = {'type': 'execute', 'code': script, 'language': 'bash', 'argv': ['bash', '--noprofile', '--norc', '-c', script]}
    admitted = materialize_effect_action(action, frozen())
    assert materialize_effect_action(admitted, frozen()) == admitted
    if mode != 'argv_python':
        assert admitted['code'] == admitted['argv'][4]
    out, err, rc = tiny(tmp_path / 'new-valid-scope.sh', 'exec ' + shlex.join(admitted['argv']))
    assert rc == 0 and not err
    assert extract_structured_effect_evidence(out)['produces_observations'] == [
        {'identity': 'scope_probe', 'value': False, 'items': []}]
    save(mode, {'rc': rc, 'stdout': out, 'stderr': err, 'actual_script_arguments': expected,
                'scope': 'real materializer and tiny process; categorized actual False/empty result under synthetic declaration'})


@pytest.mark.parametrize('prefix', [
    "python -c 'print(1)' -", "python -m json.tool -", "python script.py -",
    'python -W -', 'python -X -', 'python -V -', 'python --help -',
    'python - && cat -', 'python -; cat -',
])
def test_nonstdin_modes_and_shell_command_rebinding_do_not_claim_helper_scope(prefix):
    body = prefix + " <<'SCOPE'\n" + EFFECT_HELPER_MARKER + '\nSCOPE\n'
    with pytest.raises(ValueError, match='EFFECT_HELPER_REQUIRES_SINGLE_QUOTED_PYTHON_HEREDOC'):
        materialize_effect_action({'type': 'execute', 'code': body, 'language': 'bash'}, frozen())
