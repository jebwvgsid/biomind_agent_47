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


def check_executable_action(action: dict) -> dict:
    """Check the same explicit operand used by helper materialization/Execute."""
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
