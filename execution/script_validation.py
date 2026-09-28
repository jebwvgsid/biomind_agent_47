"""Non-executing validation of the exact candidate bytes shown for approval."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import json
import re
import shlex
import sys
import time
from pathlib import PurePath


def resolve_direct_script_argv(arguments: dict) -> dict:
    """Legacy exact file-template adapter. Never called for general execution."""
    argv, cmd = arguments.get('argv'), arguments.get('cmd')
    result = {'carrier': 'argv' if argv is not None else 'cmd', 'argv': None,
              'script_index': None, 'interpreter': None}
    def reject(reason, index=None):
        return {**result, 'code': 'EXPLICIT_SCRIPT_REQUIRES_DIRECT_INTERPRETER_ARGV',
                'reason': reason, 'position': index,
                'instruction': 'Use script_path + script_args + interpreter (host Python file mode preserves unbuffered output), retaining expected_sha256 and helpers. Complex legacy interpreter options require an explicitly supported file runner; do not remove protection or flatten shell text.'}
    if (argv is None) == (cmd is None):
        return reject('provide_exactly_one_cmd_or_argv')
    if argv is None:
        if not isinstance(cmd, str) or not cmd.strip() or '\x00' in cmd:
            return reject('invalid_cmd')
        # Intentionally conservative even inside quotes: use argv for these
        # characters. shlex is not a shell parser; do not silently change cmd.
        for chars, reason in [('\n\r', 'multiline_explicit_script_cmd'),
                              ('|;&<>`$(){}*?[]~#', 'shell_syntax_requires_argv')]:
            positions = [cmd.index(c) for c in chars if c in cmd]
            if positions:
                return reject(reason, min(positions))
        try:
            argv = shlex.split(cmd)
        except ValueError:
            return reject('shell_quoting_invalid')
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or '\x00' in a for a in argv):
        return reject('invalid_argv')
    result['argv'] = list(argv)
    exe = PurePath(argv[0]).name
    language = ('python' if re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', exe)
                else 'bash' if exe in {'bash', 'sh'} else 'r' if exe == 'Rscript' else None)
    result['interpreter'] = language
    if language is None:
        return reject('not_a_direct_supported_interpreter', 0)
    # Compatibility only: templates already used by native file calls, plus
    # the incident's Python -u template. New source calls use host construction.
    i = 1
    if language == 'python' and len(argv) > 1 and argv[1] == '-u':
        i = 2
    if i >= len(argv) or not argv[i] or argv[i].startswith('-'):
        return reject('legacy_file_template_requires_migration', i)
    return {**result, 'script_index': i}


def explicit_script_diagnostic(arguments: dict) -> dict | None:
    def refusal(code, reason):
        return {'status':'not_run', 'terminal':True, 'execution_started':False,
                'exit_code':None, 'cleanup_confirmed':True, 'phase':'carrier_admission',
                'error':code, 'reason':reason, 'position':None,
                'instruction':'Provide cmd OR argv for general execution, or script_path + script_args + interpreter for a frozen file. Retain version and helper protection.'}
    if arguments.get('legacy_action'):
        return None  # Checked after the existing legacy producer has materialized.
    if 'helper_paths' in arguments and (not isinstance(arguments['helper_paths'],list) or any(not isinstance(x,str) or not x or '\x00' in x for x in arguments['helper_paths'])):
        return refusal('HELPER_PATHS_INVALID','helper_paths_must_be_paths')
    source, argv, cmd = arguments.get('script_path'), arguments.get('argv'), arguments.get('cmd')
    if source is not None and (not isinstance(source,str) or not source or '\x00' in source):
        return refusal('EXPLICIT_SCRIPT_INVALID','invalid_source_path')
    source_only = bool(source) and argv is None and cmd is None
    if source_only:
        values = arguments.get('script_args', [])
        runner = arguments.get('interpreter')
        if not isinstance(values,list) or any(not isinstance(x,str) or '\x00' in x for x in values):
            return refusal('SCRIPT_ARGUMENTS_INVALID','script_args_must_be_strings')
        if runner is not None and (not isinstance(runner,str) or not runner or '\x00' in runner or runner.startswith('-')):
            return refusal('SCRIPT_INTERPRETER_INVALID','one_runner_path_required')
        return None
    if 'script_args' in arguments or 'interpreter' in arguments:
        return refusal('CONTROLLED_SCRIPT_CARRIER_CONFLICT','source_only_fields_with_general_or_legacy_carrier')
    if (argv is None) == (cmd is None):
        return refusal('PROVIDE_EXACTLY_ONE_CMD_OR_ARGV','invalid_carrier_count')
    if argv is not None and (not isinstance(argv,list) or not argv or not argv[0] or any(not isinstance(x,str) or '\x00' in x for x in argv)):
        return refusal('EXECUTION_ARGV_INVALID','invalid_argv')
    if cmd is not None and (not isinstance(cmd,str) or not cmd.strip() or '\x00' in cmd):
        return refusal('EXECUTION_COMMAND_REQUIRED','invalid_cmd')
    if not source:
        return None
    resolved = resolve_direct_script_argv(arguments)
    if not resolved.get('code'):
        return None
    return {**refusal(resolved['code'],resolved['reason']), **{k:resolved[k] for k in ('position','instruction')}}


def check_executable_action(action: dict) -> dict:
    """Check the same explicit operand used by helper materialization/Execute."""
    diagnostic = explicit_script_diagnostic(action) if (action.get('argv') or 'cmd' in action or 'script_path' in action or 'script_args' in action) else None
    if diagnostic:
        return {**diagnostic, 'required': True, 'check_status': 'FAIL', 'code': diagnostic['error']}
    if action.get('argv'):
        # Native arrays are not an inline-source declaration. The target owns
        # -c/-m and option syntax; explicit files are checked after selection.
        return {'schema_version':1,'check_id':'executable_syntax','required':False,
                'check_status':'NOT_APPLICABLE','execution_started':False,
                'source':'external_argv','language':'',
                'detail':'Direct argv is opaque; no interpreter or script position inference.'}
    from agent_core.step_effects import executable_carrier
    selected = executable_carrier(action)
    record = {"schema_version": 1, "check_id": "executable_syntax",
        "rule": "bounded_static_syntax_v1", "source": selected["source"],
        "language": selected["language"], "execution_started": False}
    if not selected["script_available"]:
        return {**record, "check_status": "NOT_APPLICABLE", "required": False,
            "detail": "External argv/file command has no declared inline source; file identity and execution authorization remain separately required."}
    # R remains a supported execution carrier without a configured static
    # checker. Preserve that existing boundary explicitly, never call it PASS.
    record.update(required=selected["language"] != "r", body_sha256=hashlib.sha256(selected["body"].encode()).hexdigest())
    try:
        result = check_script_body(selected["body"], selected["language"])
    except Exception as exc:
        return {**record, "check_status": "CHECK_ERROR", "code": "SCRIPT_SYNTAX_CHECK_INTERNAL_ERROR",
            "detail": type(exc).__name__}
    status = {"valid": "PASS", "invalid": "FAIL", "unavailable": "NOT_EVALUATED"}.get(result.get("status"), "CHECK_ERROR")
    if result.get("code") == "SCRIPT_SYNTAX_CHECK_PROCESS_FAILED":
        status = "CHECK_ERROR"
    return {**record, "check_status": status, "result": result,
        "code": result.get("code", ""), "detail": result.get("detail", "")}


def check_script_body(body: str, language: str, *, timeout: float = 5.0) -> dict:
    """Return syntax facts only; never clean source or infer scientific success."""
    deadline = time.monotonic() + min(5.0, max(0.01, timeout))
    digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
    result = {'schema_version': 1, 'body_sha256': digest, 'execution_started': False,
              'execution_attempt_id': '', 'status': 'unavailable', 'language': language.lower()}
    language = language.lower()
    if len(body.encode('utf-8')) > MAX_SOURCE_BYTES:
        return {**result, 'code':'SCRIPT_SYNTAX_CHECK_SIZE_LIMIT',
                'detail':'Syntax source exceeds the bounded checker size; not compiled.'}
    if language in {'python', 'py'}:
        return {**result, **_compile_python(body, timeout=timeout, start_line=1, fragment='whole_python')}
    if language not in {'bash','sh','shell'}:
        return {**result, 'code':'SCRIPT_SYNTAX_CHECK_UNAVAILABLE',
                'detail':'No non-executing syntax checker is configured for this language.'}
    # Resolve exactly as Execute resolves its approved Bash runner. The child
    # still receives only the controlled environment below, never BASH_ENV.
    interpreter = shutil.which('bash')
    if not interpreter:
        return {**result, 'code':'SCRIPT_SYNTAX_CHECK_UNAVAILABLE', 'detail':'Bash syntax checker unavailable.'}
    try:
        checked = subprocess.run([interpreter, '--noprofile', '--norc', '-n'],
            input=body.encode('utf-8'), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={'PATH':'/usr/bin:/bin', 'LC_ALL':'C.UTF-8'}, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {**result, 'code':'SCRIPT_SYNTAX_CHECK_TIMEOUT', 'detail':'Syntax checking timed out; script was not executed.'}
    except OSError as exc:
        return {**result, 'code':'SCRIPT_SYNTAX_CHECK_UNAVAILABLE', 'detail':type(exc).__name__}
    outer = {**result, 'status':'valid' if checked.returncode==0 else 'invalid',
            'code':'' if checked.returncode==0 else 'GENERATE_SCRIPT_SYNTAX_INVALID',
            'checker':interpreter, 'checker_returncode':checked.returncode,
            'detail':checked.stderr.decode('utf-8',errors='replace')[:3000]}
    if checked.returncode:
        return outer
    fragments, uncovered = _static_python_fragments(body)
    receipts = []
    for identity, line, source in fragments:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            receipts.append({'status':'unavailable','code':'SCRIPT_SYNTAX_CHECK_TIMEOUT',
                'detail':'Total syntax checking deadline reached.','fragment_id':identity})
            break
        receipts.append(_compile_python(source, timeout=remaining, start_line=line, fragment=identity))
    coverage={'static_python_fragments':receipts,'uncovered':uncovered,
              'coverage':'outer_bash_and_identified_static_python_only',
              'not_checked':['dynamic_source','runtime_branches','scientific_validity','runtime_dependencies']}
    failed=next((r for r in receipts if r['status']!='valid'),None)
    return {**outer, **(failed or {}), **coverage}


MAX_SOURCE_BYTES = 512 * 1024
MAX_STATIC_FRAGMENTS = 16
# Fixed compiler, no import of candidate modules and no execution of candidate
# code. A separate process bounds AST/compiler stack and memory failure.
PYTHON_COMPILE_DRIVER = """import sys,json,resource
resource.setrlimit(resource.RLIMIT_CPU,(2,2))
resource.setrlimit(resource.RLIMIT_AS,(268435456,268435456))
raw=sys.stdin.buffer.read(524289)
try:
 if len(raw)>524288: raise ValueError('source_size_limit')
 compile(raw.decode('utf-8'),'<candidate-fragment>','exec',dont_inherit=True)
 result={'status':'valid','code':''}
except SyntaxError as exc:
 result={'status':'invalid','code':'GENERATE_SCRIPT_SYNTAX_INVALID','detail':str(exc),'line':exc.lineno,'column':exc.offset}
except (ValueError,UnicodeError,MemoryError,RecursionError) as exc:
 result={'status':'unavailable','code':'SCRIPT_SYNTAX_CHECK_INCOMPLETE','detail':type(exc).__name__}
print(json.dumps(result))
"""


def _compile_python(source, *, timeout, start_line, fragment):
    receipt={'fragment_id':fragment,'fragment_sha256':hashlib.sha256(source.encode()).hexdigest(),
             'source_start_line':start_line,'checker':'bounded_python_compile',
             'execution_started':False,'execution_attempt_id':''}
    try:
        checked=subprocess.run([sys.executable,'-I','-S','-c',PYTHON_COMPILE_DRIVER],
            input=source.encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            env={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8'},timeout=min(5.0,max(0.01,timeout)),check=False)
        if checked.returncode:
            return {**receipt,'status':'unavailable','code':'SCRIPT_SYNTAX_CHECK_PROCESS_FAILED',
                'checker_returncode':checked.returncode,'detail':checked.stderr.decode(errors='replace')[:2000]}
        result=json.loads(checked.stdout)
        if result.get('line'):
            result['source_line']=start_line+result['line']-1
        return {**receipt,**result}
    except subprocess.TimeoutExpired:
        return {**receipt,'status':'unavailable','code':'SCRIPT_SYNTAX_CHECK_TIMEOUT',
                'detail':'Python compile timeout; candidate was not executed.'}
    except (OSError,ValueError) as exc:
        return {**receipt,'status':'unavailable','code':'SCRIPT_SYNTAX_CHECK_UNAVAILABLE','detail':type(exc).__name__}


def _static_python_fragments(body):
    """Recognize one narrow literal stdin convention, never a general shell AST.

    Only a single direct python command with '-' and a quoted heredoc delimiter
    provides immutable Python source. Unquoted/expanded/eval/generated scripts
    are deliberately not guessed. Shell arguments remain shell arguments.
    """
    lines=body.splitlines(keepends=True);found=[];uncovered=[];index=0
    command=re.compile(r"^\s*(?:/[^\s'\";$`|&<>]+/)?python(?:3(?:\.\d+)?)?\s+")
    delimiter=re.compile(r"<<'([A-Za-z_][A-Za-z_0-9]*)'\s*$")
    while index<len(lines):
        line=lines[index];start=index;index+=1
        if not command.match(line):
            continue
        match=delimiter.search(line)
        try:tokens=shlex.split(line[:match.start()] if match else line)
        except ValueError:tokens=[]
        if not match or '-' not in tokens or any(t in {';','&&','||','|','eval'} for t in tokens):
            uncovered.append({'source_line':start+1,'reason':'python_source_not_literal_stdin'})
            continue
        stop=index
        while stop<len(lines) and lines[stop].rstrip('\r\n')!=match[1]:stop+=1
        if stop==len(lines):
            uncovered.append({'source_line':start+1,'reason':'heredoc_boundary_unknown'})
            continue
        if len(found)>=MAX_STATIC_FRAGMENTS:
            uncovered.append({'source_line':start+1,'reason':'static_fragment_count_limit'})
        else:
            found.append(('python_heredoc:'+str(start+1),index+1,''.join(lines[index:stop])))
        index=stop+1
    return found,uncovered


def check_registered_script_sources(resources: list[dict]) -> list[dict]:
    """Check only caller-verified registered resources, never arbitrary argv paths.

    Call after bind_action_skill_resources has verified selection and identity.
    The recorded hash binds the static check to exactly those full source bytes.
    """
    import os
    import stat
    from pathlib import Path
    records = []
    for resource in resources:
        path = str(resource.get('canonical_path') or '')
        language = {'.py': 'python', '.sh': 'bash', '.r': 'r'}.get(Path(path).suffix.lower())
        if not language:
            continue
        base = {'schema_version': 1, 'check_id': 'executable_syntax', 'source': path,
                'handle': resource.get('handle'), 'carrier': 'registered_file',
                'execution_started': False, 'required': language != 'r'}
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError('REGISTERED_SCRIPT_NOT_REGULAR')
                raw = stream.read(MAX_SOURCE_BYTES + 1)
            if len(raw) > MAX_SOURCE_BYTES:
                raise ValueError('REGISTERED_SCRIPT_SIZE_LIMIT')
            digest = hashlib.sha256(raw).hexdigest()
            if digest != str(resource.get('content_hash') or '').removeprefix('sha256:'):
                raise ValueError('REGISTERED_SCRIPT_CONTENT_DRIFT')
            checked = check_executable_action({'code': raw.decode('utf-8'), 'language': language})
            records.append({**checked, **base, 'body_sha256': digest})
        except (OSError, ValueError) as exc:
            records.append({**base, 'check_status': 'CHECK_ERROR', 'code': str(exc),
                            'detail': 'The current registered source could not be fully verified; no execution occurred.'})
    return records
