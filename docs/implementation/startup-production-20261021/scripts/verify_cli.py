"""Load the real production CLI, inspect tool registration, and close without a model request."""
from pathlib import Path
import argparse
import json
import os
import select
import subprocess
import time

B = Path('/public/home/liusiqi/03.毕设')
O = B / 'BioMind_Startup_Production_20261021'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--launcher', type=Path, default=Path('/public/home/liusiqi/.local/bin/biomind'))
    parser.add_argument('--name', default='official-cli')
    args = parser.parse_args()
    run = O / 'evidence' / args.name
    run.mkdir(parents=True, exist_ok=False)
    project = run / 'project'
    project.mkdir()
    expected = json.loads((O / 'build-result.json').read_text())
    frames, buf = [], b''
    receipt = {'status': 'FAIL', 'launcher': str(args.launcher), 'modelRequests': 0,
               'scientificRuns': 0, 'defaultRuntimeConfig': True}
    env = dict(os.environ, BIOMIND_DISABLE_DOTENV='1', PYTHONDONTWRITEBYTECODE='1')
    with (run / 'stderr.log').open('wb') as stderr:
        proc = subprocess.Popen([str(args.launcher), '--project-dir', str(project), '--pi-mode', 'json',
                                 '--session-dir', str(run / 'sessions')], cwd=project, env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr)

        def wait(predicate, seconds):
            nonlocal buf
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                found = next((f for f in frames if predicate(f)), None)
                if found is not None:
                    return found
                if proc.poll() is not None:
                    raise RuntimeError('CLI_EXITED_BEFORE_RESPONSE')
                if select.select([proc.stdout], [], [], .1)[0]:
                    buf += os.read(proc.stdout.fileno(), 65536)
                    while b'\n' in buf:
                        line, buf = buf.split(b'\n', 1)
                        try:
                            frames.append(json.loads(line))
                        except ValueError:
                            pass
            raise TimeoutError('CLI_RESPONSE_TIMEOUT')

        def send(frame):
            proc.stdin.write((json.dumps(frame) + '\n').encode())
            proc.stdin.flush()

        try:
            ready = wait(lambda f: f.get('type') == 'ready', 120)
            receipt['ready'] = ready
            assert ready['loaded_build']['buildId'] == expected['releaseId'], 'LOADED_BUILD_MISMATCH'
            for method in ('get_tools', 'get_resources', 'get_state'):
                send({'type': method, 'id': method})
                receipt[method] = wait(lambda f: f.get('id') == method, 30)
            names = {t['name'] for t in receipt['get_tools']['data']['registered']}
            required = {'run_command', 'inspect_run', 'wait_run', 'cancel_run',
                        'web_search', 'fetch_content', 'get_search_content'}
            assert required <= names, 'PRODUCTION_TOOLS_MISSING'
            receipt['requiredTools'] = sorted(required)
            assert receipt['get_state']['active'] is False, 'UNEXPECTED_ACTIVE_TURN'
            receipt['status'] = 'PASS'
        except Exception as error:
            receipt['error_type'] = type(error).__name__
            receipt['error'] = str(error)
        finally:
            if proc.poll() is None:
                try:
                    send({'type': 'shutdown', 'id': 'close'})
                    proc.stdin.close()
                    proc.wait(30)
                except (OSError, subprocess.TimeoutExpired):
                    # Only this isolated load-only child belongs to this script.
                    proc.terminate()
                    proc.wait(10)
            receipt['exitCode'] = proc.returncode
            if proc.returncode != 0:
                receipt['status'] = 'FAIL'
    (run / 'frames.jsonl').write_text(''.join(json.dumps(f, ensure_ascii=False) + '\n' for f in frames))
    (run / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: receipt.get(k) for k in ['status', 'launcher', 'requiredTools', 'exitCode', 'error']}))
    return 0 if receipt['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
