"""Non-executing validation of the exact candidate bytes shown for approval."""
from __future__ import annotations

import ast
import hashlib
import shutil
import subprocess


def check_script_body(body: str, language: str, *, timeout: float = 5.0) -> dict:
    """Return syntax facts only; never clean source or infer scientific success."""
    digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
    result = {'schema_version': 1, 'body_sha256': digest, 'execution_started': False,
              'execution_attempt_id': '', 'status': 'unavailable', 'language': language.lower()}
    language = language.lower()
    if language in {'python', 'py'}:
        try:
            ast.parse(body, filename='<candidate>', mode='exec')
        except (SyntaxError, ValueError) as exc:
            return {**result, 'status':'invalid', 'code':'GENERATE_SCRIPT_SYNTAX_INVALID', 'detail':str(exc)}
        return {**result, 'status':'valid', 'checker':'python.ast'}
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
    return {**result, 'status':'valid' if checked.returncode==0 else 'invalid',
            'code':'' if checked.returncode==0 else 'GENERATE_SCRIPT_SYNTAX_INVALID',
            'checker':interpreter, 'checker_returncode':checked.returncode,
            'detail':checked.stderr.decode('utf-8',errors='replace')[:3000]}
