#!/usr/bin/env python3
"""Privately add a missing native candidate credential; never restart services.

--verify explicitly requests one model task and one search on the original A/B
owners. A durable receipt makes repeated invocations read-only, never retries.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import getpass
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import uuid
import warnings

DEFAULT = Path('/public/home/liusiqi/03.毕设/BioMind_Unified_Core_20261009/deployments/isolated-server-1.3.0-rc.20261010.1')


class Refused(Exception):
    pass


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INPUT_ERRORS = {
    'CREDENTIAL_EMPTY': '未收到输入。隐藏输入不会显示字符；请粘贴 API key 后按回车。',
    'CREDENTIAL_LENGTH_INVALID': '输入长度不符合要求。请复制完整的 API key 本身。',
    'CREDENTIAL_WHITESPACE': '输入中含空格或换行。请只复制 API key 本身，不带前后空白。',
    'CREDENTIAL_CONTROL_CHARACTER': '输入中含不可见控制字符。请清空输入后重新粘贴 API key。',
    'CREDENTIAL_COMMAND_LITERAL_REFUSED': '此处只接受 API key 本身，不能输入命令。',
}


def literal_key(key):
    if not isinstance(key, str):
        raise Refused('INVALID_LITERAL_CREDENTIAL')
    if not key:
        raise Refused('CREDENTIAL_EMPTY')
    if not 8 <= len(key) <= 8192:
        raise Refused('CREDENTIAL_LENGTH_INVALID')
    if re.search(r'\s', key):
        raise Refused('CREDENTIAL_WHITESPACE')
    if re.search(r'[\x00-\x1f\x7f-\x9f]', key):
        raise Refused('CREDENTIAL_CONTROL_CHARACTER')
    if key.startswith('!'):
        raise Refused('CREDENTIAL_COMMAND_LITERAL_REFUSED')
    return key


def read_hidden_key():
    if not sys.stdin.isatty():
        raise Refused('INTERACTIVE_TERMINAL_REQUIRED')
    print('请粘贴 DeepSeek API key 本身，不加引号或 KEY= 前缀。不是工作台口令或 SSH 密码。输入时不会显示字符。', flush=True)
    for attempt in range(3):
        try:
            # getpass normally warns and falls back to echoed input if it cannot
            # disable terminal echo. Never accept that fallback for a credential.
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                key = getpass.getpass('输入 DeepSeek API key（隐藏输入，仅保存到隔离候选）：')
        except getpass.GetPassWarning:
            raise Refused('HIDDEN_INPUT_UNAVAILABLE') from None
        except (EOFError, KeyboardInterrupt):
            raise Refused('CREDENTIAL_INPUT_CANCELLED') from None
        try:
            return literal_key(key)
        except Refused as error:
            del key
            code = str(error)
            print(INPUT_ERRORS.get(code, '输入格式不符合要求。请只复制 API key 本身。'), flush=True)
            if attempt == 2:
                raise
            print('尚未保存密钥或发送模型请求，请重新输入（还可输入 ' + str(2 - attempt) + ' 次）。', flush=True)


def layout(root, verify):
    prepared = json.loads((root / 'PREPARED.json').read_text())
    if prepared.get('version') != 'isolated-server-1.3.0-rc.20261010.1' or prepared.get('root') != str(root):
        raise Refused('PREPARED_ISOLATED_CANDIDATE_REQUIRED')
    config = verify.private_json(root / 'private/service.json')
    if config.get('mode') != 'local' or config.get('serverId') != prepared['serverId']:
        raise Refused('ISOLATED_SERVICE_IDENTITY_REQUIRED')
    verify.under(root / 'private/service.json', root)
    runtime_path = verify.under(config['environment']['BIOMIND_RUNTIME_CONFIG'], root)
    runtime = json.loads(Path(runtime_path).read_text())
    verify.validate_isolation(config, runtime, prepared['project'], root)
    if runtime.get('credentials') or config['environment'].get('BIOMIND_DISABLE_DOTENV') != '1':
        raise Refused('NATIVE_ISOLATED_AUTH_REQUIRED')
    core = Path(prepared['coreRoot'])
    release = json.loads((core / 'RELEASE.json').read_text())
    if release['releaseId'] != prepared['coreBuildId'] or prepared['piVersion'] != '0.87.1' or Path(config['environment']['BIOMIND_ENTRYPOINT']) != core / 'bio_agents.py':
        raise Refused('PINNED_CORE_REQUIRED')
    auth = Path(verify.under(Path(runtime['agentDir']) / 'auth.json', root))
    verify.private_json(auth)  # Permission/schema check only; never resolve configured credentials.
    module = core / 'integrations/pi_runtime/node_modules/@earendil-works/pi-coding-agent/dist/core/auth-storage.js'
    return prepared, config, runtime, auth, module


def connected(root, config, verify):
    service = load_module('candidate_model_service', root / 'service-v1/server-service.py')
    state = service.discover(service.load(root / 'private/service.json'))
    if state.get('status') != 'ready' or state.get('host') != socket.gethostname():
        raise Refused('RUN_ON_READY_CANDIDATE_HOST')
    password = verify.private_json(config['environmentFile'])['PI_WEB_PASSWORD']
    client = verify.Client('http://127.0.0.1:' + str(state['port']), password)
    health = client.json('/_biomind_studio/health')
    if any(health.get(k) != state.get(k) for k in ('serverId', 'instanceId')) or health.get('profile') != 'biomind' or health.get('status') != 'ready':
        raise Refused('CONNECTED_CANDIDATE_IDENTITY_MISMATCH')
    return state


def save_native(config, module, auth, key):
    command = [config['environment']['BIOMIND_PI_NODE'], str(Path(__file__).with_name('set-isolated-native-credential.mjs')), str(module), str(auth)]
    # Keep key out of argv and environment. No dotenv or alternate SDK Agent.
    child = subprocess.run(command, input=json.dumps({'key': literal_key(key)}), text=True,
        capture_output=True, timeout=45, env={**os.environ, 'BIOMIND_DISABLE_DOTENV': '1'})
    try:
        result = json.loads(child.stdout)
    except ValueError:
        raise Refused('NATIVE_CREDENTIAL_SAVE_UNCONFIRMED') from None
    if child.returncode or result.get('result') != 'PASS':
        code = result.get('code', '')
        raise Refused(code if re.fullmatch('[A-Z_]{3,100}', code) else 'NATIVE_CREDENTIAL_SAVE_UNCONFIRMED')


def verify_command(root, prepared, config, receipt, existing):
    command = [config['environment']['BIOMIND_LAUNCHER_PYTHON'], '-B', str(Path(__file__).with_name('verify-isolated-candidate.py')),
        '--service', str(root / 'service-v1/server-service.py'), '--config', str(root / 'private/service.json'),
        '--release', prepared['coreRoot'], '--project', prepared['project'], '--isolation-root', str(root), '--evidence', str(receipt)]
    return command + (['--resume'] if existing else ['--reuse-sessions-from', str(root / 'evidence/http-acceptance-search.json'),
        '--probe-native-credential', '--real-model', '--real-search'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment', type=Path, default=DEFAULT)
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--attempt', default='first-key')
    args = parser.parse_args()
    if not re.fullmatch('[a-zA-Z0-9_-]{1,50}', args.attempt):
        parser.error('attempt must be a short alphanumeric label')
    root = args.deployment.absolute()
    result = {'schema': 'BioMindCandidateCredentialSetupV1', 'atUTC': datetime.now(timezone.utc).isoformat(),
        'productionChanged': False, 'servicesStopped': False, 'sessionsReplaced': False, 'automaticReplay': False,
        'credentialPrinted': False, 'result': 'BLOCKED'}
    lock = None
    verified_layout = False
    try:
        verify = load_module('candidate_model_verify', Path(__file__).with_name('verify-isolated-candidate.py'))
        prepared, config, runtime, auth, module = layout(root, verify)
        evidence = root / 'evidence'
        verify.under(evidence, root)
        lock = os.open(evidence / 'model-credential-operation.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        verified_layout = True
        state = connected(root, config, verify)
        result.update(serverId=state['serverId'], instanceId=state['instanceId'], host=state['host'])
        receipt = evidence / ('http-acceptance-credential-' + args.attempt + '.json')
        if receipt.exists():
            result['credential'] = 'UNCHANGED_EXISTING_ACCEPTANCE_READ_ONLY'
        else:
            stored = verify.private_json(auth)
            if 'deepseek' in stored:
                result['credential'] = 'EXISTING_CANDIDATE_NATIVE_CREDENTIAL_REUSED'
            else:
                key = read_hidden_key()
                save_native(config, module, auth, key)
                del key
                result['credential'] = 'SAVED_TO_CANDIDATE_NATIVE_AUTH'
            del stored
        print(json.dumps({'stage': 'credential', 'status': result['credential'], 'servicesStopped': False}, ensure_ascii=False), flush=True)
        if args.verify:
            print('正在核对原 A/B 会话并验收；忙碌时停止验收，不重发已有任务。', flush=True)
            check = subprocess.run(verify_command(root, prepared, config, receipt, receipt.exists()), check=False)
            result['acceptance'] = {'exitCode': check.returncode, 'report': str(receipt)}
            result['result'] = 'PASS' if check.returncode == 0 else 'BLOCKED'
        else:
            result.update(result='PASS', actualModelAcceptance='NOT_RUN')
    except Exception as error:
        code = str(error) if isinstance(error, Refused) else getattr(error, 'code', '')
        result['code'] = code if re.fullmatch('[A-Z_0-9]{3,100}', code) else type(error).__name__.upper()
    finally:
        if verified_layout:
            path = root / 'evidence' / ('model-credentials-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8] + '.json')
            with path.open('x') as handle:
                os.chmod(path, 0o600)
                json.dump(result, handle, ensure_ascii=False, indent=2)
            result['report'] = str(path)
        if lock is not None:
            os.close(lock)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result['result'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
