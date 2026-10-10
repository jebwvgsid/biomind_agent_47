#!/usr/bin/env python3
"""Start only the prepared isolated Studio candidate and record real readiness.

No production maintenance, host inventory, scheduler submission or task replay.
Credentials are entered locally with getpass; receipts never contain them.
"""
import argparse
from datetime import datetime, timezone
import getpass
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import uuid

DEFAULT = Path('/public/home/liusiqi/03.毕设/BioMind_Unified_Core_20261009/deployments/isolated-server-1.3.0-rc.20261010.1')


class Refused(Exception):
    pass


def contained(path, root):
    path = Path(path)
    return path.is_absolute() and path.resolve().is_relative_to(root.resolve())


def validate_layout(root, config):
    if root.is_symlink() or not (root / 'PREPARED.json').is_file():
        raise Refused('PREPARED_ISOLATED_CANDIDATE_REQUIRED')
    if config.get('mode') != 'local':
        raise Refused('ISOLATED_LOCAL_SERVICE_REQUIRED')
    for key in ('stateRoot', 'environmentFile', 'cwd'):
        if not contained(config.get(key, ''), root):
            raise Refused('ISOLATED_SERVICE_PATH_REQUIRED')
    environment = config.get('environment', {})
    for key in ('BIOMIND_RUNTIME_CONFIG', 'BIOMIND_SESSION_ROOT', 'PI_WEB_UPLOADS_DIR'):
        if not contained(environment.get(key, ''), root):
            raise Refused('ISOLATED_RUNTIME_PATH_REQUIRED')
    runtime = json.loads(Path(environment['BIOMIND_RUNTIME_CONFIG']).read_text())
    if not contained(runtime.get('agentDir', ''), root):
        raise Refused('ISOLATED_AGENT_DIR_REQUIRED')
    if environment.get('BIOMIND_DISABLE_DOTENV') != '1':
        raise Refused('ISOLATED_DOTENV_DISABLE_REQUIRED')


def service_module(root):
    spec = importlib.util.spec_from_file_location('isolated_service_manager', root / 'service-v1/server-service.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure_credentials(root, service, config, password, model_key):
    """Serialize with ensure and refuse credential changes under a live holder."""
    if not isinstance(password, str) or not 16 <= len(password.encode()) <= 4096 or any(c in password for c in '\0\r\n'):
        raise Refused('WORKBENCH_PASSWORD_AT_LEAST_16_BYTES_REQUIRED')
    if model_key and (len(model_key) > 8192 or any(c in model_key for c in '\0\r\n')):
        raise Refused('MODEL_CREDENTIAL_FORMAT_INVALID')
    with service.locked(config):
        previous = service.read_record(config)
        state = service.inspect(config, previous)
        if state['status'] not in ('stopped', 'failed') or service.backend_lease_held(config) or (previous and service.process_alive(previous)):
            raise Refused('CREDENTIAL_CHANGE_REQUIRES_STOPPED_CANDIDATE')
        path = Path(config['environmentFile'])
        service.private(path)
        values = json.loads(path.read_text())
        values['PI_WEB_PASSWORD'] = password
        if model_key:
            values['DEEPSEEK_API_KEY'] = model_key
        service.atomic(path, values)


def safe_error(error):
    text = str(error)
    return text if re.fullmatch(r'[A-Z][A-Z0-9_]{1,100}', text) else type(error).__name__


def execute(root, interactive=False, timeout=190):
    result = {'schema': 'BioMindIsolatedStartV1', 'atUTC': datetime.now(timezone.utc).isoformat(),
              'deployment': str(root), 'productionChanged': False, 'schedulerSubmitted': False,
              'automaticTaskReplay': False, 'ready': False, 'actualBackendIdentity': 'NOT_RUN'}
    try:
        config_path = root / 'private/service.json'
        raw = json.loads(config_path.read_text())
        validate_layout(root, raw)
        service = service_module(root)
        config = service.load(config_path)
        if interactive:
            if not sys.stdin.isatty():
                raise Refused('INTERACTIVE_TERMINAL_REQUIRED')
            existing = service.discover(config)
            if existing['status'] in ('stopped', 'failed'):
                password = getpass.getpass('设置隔离候选工作台口令（至少16字节，稍后填入Windows）：')
                model_key = getpass.getpass('DeepSeek API key（仅存此候选；留空跳过真实模型验收）：')
                configure_credentials(root, service, config, password, model_key)
                del password, model_key
            else:
                print('候选已有实例；复用现有口令和实例，不修改运行中配置。', flush=True)
        # This is evidence about the current host, not a readiness substitute.
        try:
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
            result['listenerCapability'] = {'status': 'PASS'}
        except OSError as error:
            result['listenerCapability'] = {'status': 'BLOCKED', 'errno': error.errno}
        python = config['environment']['BIOMIND_LAUNCHER_PYTHON']
        base = [python, '-B', str(root / 'service-v1/server-service.py')]
        # Exactly one ensure. Poll discover only; never submit duplicate starts.
        launch = subprocess.run(base + ['ensure', '--config', str(config_path)], capture_output=True, text=True, timeout=35)
        if launch.returncode:
            raise Refused('CANDIDATE_ENSURE_FAILED')
        state = json.loads(launch.stdout)
        result['ensure'] = state
        deadline = time.monotonic() + timeout
        last_status = None
        while True:
            state = service.discover(config)
            if state.get('status') != last_status:
                print(json.dumps({'stage': 'service', 'status': state.get('status'), 'code': state.get('code')}, ensure_ascii=False), flush=True)
                last_status = state.get('status')
            if state.get('status') in ('ready', 'failed', 'stopped', 'unknown', 'configuration-changed') or time.monotonic() >= deadline:
                break
            time.sleep(1)
        result['service'] = state
        result['ready'] = state.get('status') == 'ready'
        result['status'] = 'READY_IDENTITY_CHECK_REQUIRED' if result['ready'] else 'BLOCKED'
        result['code'] = None if result['ready'] else state.get('code', 'CANDIDATE_START_NOT_READY')
    except Exception as error:
        result.update(status='BLOCKED', code=safe_error(error))
    return result


def verify(root, sessions=False, real_model=False, real_search=False):
    config = json.loads((root / 'private/service.json').read_text())
    environment = config['environment']
    runtime = json.loads(Path(environment['BIOMIND_RUNTIME_CONFIG']).read_text())
    release = Path(environment['BIOMIND_ENTRYPOINT']).parent
    prepared = json.loads((root / 'PREPARED.json').read_text())
    project = prepared['project']
    # Repeating the same acceptance request is read-only. A deliberately added
    # test stage gets its own receipt, so identity-only checks don't prevent it.
    mode = 'search' if real_search else 'model' if real_model else 'sessions' if sessions else 'identity'
    receipt = root / 'evidence' / ('http-acceptance-' + mode + '.json')
    command = [environment['BIOMIND_LAUNCHER_PYTHON'], '-B', str(Path(__file__).with_name('verify-isolated-candidate.py')),
               '--service', str(root / 'service-v1/server-service.py'), '--config', str(root / 'private/service.json'),
               '--release', str(release), '--project', project, '--evidence', str(receipt), '--isolation-root', str(root)]
    if receipt.exists():
        command.append('--resume')
    else:
        for enabled, flag in ((sessions, '--sessions'), (real_model, '--real-model'), (real_search, '--real-search')):
            if enabled:
                command.append(flag)
    result = subprocess.run(command, check=False)
    return {'exitCode': result.returncode, 'report': str(receipt), 'agentDir': runtime['agentDir']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment', type=Path, default=DEFAULT)
    parser.add_argument('--interactive', action='store_true')
    parser.add_argument('--timeout', type=int, default=190)
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--sessions', action='store_true')
    parser.add_argument('--real-model', action='store_true')
    parser.add_argument('--real-search', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.timeout <= 300:
        parser.error('timeout must be between 1 and 300 seconds')
    root = args.deployment.absolute()
    result = execute(root, args.interactive, args.timeout)
    if result.get('ready') and (args.verify or args.sessions or args.real_model or args.real_search):
        try:
            result['acceptance'] = verify(root, args.sessions, args.real_model, args.real_search)
            data = json.loads(Path(result['acceptance']['report']).read_text())
            result['actualBackendIdentity'] = data.get('stages', {}).get('httpIdentity', {}).get('result', 'NOT_RUN')
        except Exception as error:
            result['acceptance'] = {'status': 'BLOCKED', 'code': safe_error(error)}
    # Root must already be prepared; never create an arbitrary deployment on error.
    if (root / 'PREPARED.json').is_file():
        evidence = root / 'evidence'
        evidence.mkdir(mode=0o700, exist_ok=True)
        path = evidence / ('start-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8] + '.json')
        service = service_module(root)
        service.atomic(path, result)
        result['report'] = str(path)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    accepted = result.get('acceptance', {}).get('exitCode', 0) == 0 and result.get('acceptance', {}).get('status') != 'BLOCKED'
    return 0 if result.get('ready') and accepted else 2


if __name__ == '__main__':
    raise SystemExit(main())
