#!/usr/bin/env python3
"""Probe the candidate's actual Main over pipes, without sending any model task.

This independently tests native initialization/tools when HTTP sockets are not
available. It cannot count as HTTP, Windows, real model, or search acceptance.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('config', 'release', 'project', 'isolation-root', 'evidence'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('isolated_http', Path(__file__).with_name('verify-isolated-candidate.py'))
    verify = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verify)
    receipt = verify.Receipt(args.evidence)
    receipt.data.update(schema='BioMindIsolatedMainPipeV1', http='NOT_RUN', realModel='NOT_RUN',
                        realSearch='NOT_RUN', modelPromptsSent=0, windowsInteraction='NOT_RUN')
    proc, stderr_log, frames, pending = None, None, [], b''
    try:
        config = verify.private_json(args.config)
        runtime_path = config['environment']['BIOMIND_RUNTIME_CONFIG']
        runtime = json.loads(Path(runtime_path).read_text())
        project, release = str(args.project.resolve()), args.release.resolve()
        verify.validate_isolation(config, runtime, project, args.isolation_root)
        if runtime.get('providerFixture'):
            raise verify.CheckError('SCRIPTED_PROVIDER_NOT_ALLOWED_IN_INITIALIZATION_PROBE')
        expected = json.loads((release / 'RELEASE.json').read_text())
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'LC_ALL') if key in os.environ}
        env.update(config['environment'])
        # Do not load environmentFile/model secrets. The init probe sends no task.
        env['BIOMIND_DISABLE_DOTENV'] = '1'
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        argv = [sys.executable, '-B', str(release / 'bio_agents.py'), '--pi-mode', 'json',
                '--project-dir', project, '--pi-config', runtime_path]
        stderr_path = args.evidence.with_suffix('.stderr.log')
        stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        stderr_log = os.fdopen(stderr_fd, 'wb')
        receipt.data['privateStderrEvidence'] = str(stderr_path)
        proc = subprocess.Popen(argv, cwd=project, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=stderr_log, start_new_session=True)

        def send(frame):
            proc.stdin.write((json.dumps(frame) + '\n').encode())
            proc.stdin.flush()

        def wait(predicate, timeout=120):
            nonlocal pending
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                for frame in frames:
                    if predicate(frame):
                        return frame
                if select.select([proc.stdout], [], [], min(0.1, max(0, deadline-time.monotonic())))[0]:
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    pending += chunk
                    if len(pending) > 2 * 1024 * 1024:
                        raise verify.CheckError('FRAME_TOO_LARGE')
                    while b'\n' in pending:
                        line, pending = pending.split(b'\n', 1)
                        if line:
                            frame = json.loads(line)
                            if frame.get('type') == 'error' and isinstance(frame.get('code'), str):
                                raise verify.CheckError(frame['code'])
                            frames.append(frame)
                    if len(frames) > 1000:
                        raise verify.CheckError('FRAME_LIMIT')
                if proc.poll() is not None:
                    raise verify.CheckError('MAIN_EXITED_BEFORE_REPLY')
            raise verify.CheckError('MAIN_INITIALIZATION_OR_CONTROL_TIMEOUT')

        ready = wait(lambda frame: frame.get('type') == 'ready')
        identity = verify.worker_identity(ready, expected, release, runtime['agentDir'], project, ready['sessionId'])
        receipt.stage('actualMainIdentity', identity)
        if identity['result'] != 'PASS':
            raise verify.CheckError('MAIN_IDENTITY_MISMATCH')
        scope = {'sessionId': ready['sessionId'], 'ownerGeneration': ready['ownerGeneration'], 'project': project}
        for command, stage in [('get_state', 'nativeState'), ('get_tools', 'nativeTools'), ('get_models', 'nativeModels')]:
            send({'type': command, 'id': command, **scope})
            frame = wait(lambda item: item.get('id') == command, 30)
            if command == 'get_tools':
                value = verify.tool_inventory(frame.get('data') or {})
            elif command == 'get_models':
                data = frame.get('data') or {}
                value = {'result': 'PASS' if data.get('current') and data.get('models') else 'FAIL',
                    'current': data.get('current'), 'models': [
                        {key: row.get(key) for key in ('provider', 'id', 'authenticated')} for row in data.get('models', [])]}
            else:
                value = {'result': 'PASS' if frame.get('active') is False and frame.get('sessionId') == ready['sessionId'] else 'FAIL',
                         'sessionId': frame.get('sessionId'), 'active': frame.get('active'), 'ownerGeneration': frame.get('ownerGeneration')}
            receipt.stage(stage, value)
    except (OSError, ValueError, KeyError, TypeError, verify.CheckError, subprocess.SubprocessError) as error:
        receipt.data['code'] = verify.safe_code(error)
        receipt.data['result'] = 'BLOCKED'
    finally:
        forced = False
        if proc is not None and proc.poll() is None:
            try:
                proc.stdin.write(b'{"type":"shutdown","id":"isolated-check-complete"}\n')
                proc.stdin.flush()
                proc.stdin.close()
            except (OSError, BrokenPipeError):
                pass
            try:
                proc.wait(30)
            except subprocess.TimeoutExpired:
                # Only the brand-new group created by this probe, never an owner
                # discovered on the host or an existing production session.
                forced = True
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(5)
        if proc is not None:
            receipt.stage('normalShutdown', {'result': 'PASS' if proc.returncode == 0 and not forced else 'FAIL',
                'exitCode': proc.returncode, 'forcedOwnProbeGroup': forced})
        if stderr_log is not None:
            stderr_log.close()
        if 'code' not in receipt.data:
            receipt.data['result'] = 'PASS' if all(v.get('result') == 'PASS' for v in receipt.data['stages'].values()) else 'FAIL'
        receipt.save()
        print(json.dumps({'result': receipt.data['result'], 'code': receipt.data.get('code'),
            'evidence': str(args.evidence), 'http': 'NOT_RUN', 'realModel': 'NOT_RUN', 'realSearch': 'NOT_RUN',
            'windowsInteraction': 'NOT_RUN', 'modelPromptsSent': 0}, ensure_ascii=False))
    return 0 if receipt.data['result'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
