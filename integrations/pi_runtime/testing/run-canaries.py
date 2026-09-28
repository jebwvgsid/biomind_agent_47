#!/usr/bin/env python3
"""Bounded R1/R2/R3 real-provider driver; --plan is read-only and fails closed.

This driver never loads dotenv/auth files and never resets/refunds the canonical
send ledger. Only --execute starts the already configured runtime. Every actual
send, including Pi internal retries/summaries, must reserve through the installed
observer and SQLite budget adapter before HTTP dispatch.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import uuid

RUNTIME = Path(__file__).resolve().parents[1]
REPO = RUNTIME.parents[1]
DELIVERY = REPO.parent / 'BioMind_Pi_migration_implementation_20260926'
LEDGER = RUNTIME / '.runtime/live-canary-20260926.sqlite'
SCENARIOS = ('R1', 'R2', 'R3')
SOURCE_REQUIREMENTS = (
    'integrations/pi_runtime/extensions/biomind.ts',
    'integrations/pi_runtime/host.ts',
    'integrations/pi_runtime/domain_adapter/service.py',
    'integrations/pi_runtime/usage-observer.ts',
    'integrations/pi_runtime/canary-budget.ts',
    'integrations/pi_runtime/canary-dispatch.ts',
    'integrations/pi_runtime/canary_ledger.py',
    'integrations/pi_runtime/compat/pi-0.87.1-observer.patch.json',
)


class CanaryError(Exception):
    """Stable nonsecret driver failure."""


@contextmanager
def exclusive_execution():
    """Serialize this task's runners without resetting its durable send ledger.

    Shared before/after scenario counts have a single runner owner. The stable
    lock file is never unlinked; process exit releases only the advisory lock.
    --plan does not invoke this function or create any file.
    """
    lock_path = LEDGER.with_suffix('.runner.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise CanaryError('CANARY_RUNNER_LOCK_UNAVAILABLE') from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise CanaryError('CANARY_RUNNER_LOCK_NOT_REGULAR')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise CanaryError('CANARY_RUNNER_ALREADY_ACTIVE') from error
        yield descriptor
    finally:
        os.close(descriptor)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def safe_code_path(relative):
    value = Path(relative)
    if value.is_absolute() or '..' in value.parts or any(part.startswith('.env') or part == 'auth.json' for part in value.parts):
        raise CanaryError('UNSAFE_SOURCE_MANIFEST_PATH')
    actual = (REPO / value).resolve()
    if not actual.is_relative_to(REPO):
        raise CanaryError('SOURCE_MANIFEST_ESCAPE')
    return actual


def ledger_counts():
    """Read only. Missing ledger means no observed reservations, never init."""
    if not LEDGER.exists():
        return {'R1': 0, 'R2': 0, 'R3': 0, 'total': 0, 'exists': False}
    if LEDGER.is_symlink():
        raise CanaryError('CANARY_LEDGER_SYMLINK')
    with sqlite3.connect(LEDGER.as_uri() + '?mode=ro', uri=True) as db:
        config = db.execute('SELECT task_id,per_scenario,total FROM config WHERE singleton=1').fetchone()
        if config != ('BioMind-Pi-20260926', 6, 18):
            raise CanaryError('CANARY_LEDGER_IDENTITY_MISMATCH')
        result = dict.fromkeys(SCENARIOS, 0)
        for scenario, count in db.execute('SELECT scenario,count(*) FROM sends GROUP BY scenario'):
            if scenario not in SCENARIOS:
                raise CanaryError('CANARY_LEDGER_UNKNOWN_SCENARIO')
            result[scenario] = count
        return {**result, 'total': sum(result.values()), 'exists': True}


def registered_tool(registry):
    """Only declared absolute prefixes; no conda discovery or guessed paths."""
    document = read_json(registry)
    # Keep this conservative subset aligned with registered_runtime._SUCCESS.
    # Merely 'registered'/'available'/'active' metadata is not accepted there.
    successful = {'success', 'verified', 'installed'}
    def items(value):
        return [str(item) for item in value] if isinstance(value, (list, tuple)) else [str(value)] if value else []
    for name in ('bcftools', 'samtools'):
        tool = document.get('tools', {}).get(name, {})
        for env_name, env in document.get('environments', {}).items():
            declared = ((tool.get('env') or tool.get('env_name')) == env_name
                        and str(tool.get('status') or '').lower() in successful)
            recorded = {value.casefold() for key in ('tools', 'bound_tools') for value in items(env.get(key))}
            recorded.update(re.split(r'[<>=!~\s]', value)[0].casefold() for value in items(env.get('packages')))
            recorded.update(value.removeprefix('tool:').casefold() for value in items(env.get('capabilities')))
            declared |= name in recorded
            if not declared or str(env.get('status') or '').lower() not in successful:
                continue
            prefix = env.get('prefix') or env.get('path') or env.get('resolved_runtime_paths', {}).get('RESOLVED_ENV_PREFIX')
            if not isinstance(prefix, str) or not Path(prefix).is_absolute():
                continue
            binary = Path(prefix) / 'bin' / name
            if binary.is_file() and os.access(binary, os.X_OK):
                return {'name': name, 'env_name': env_name, 'prefix': str(Path(prefix).resolve()),
                        'executable': str(binary.resolve()), 'registry_sha256': digest(registry)}
    return None


def preflight(options):
    blockers = []
    identity = {}
    try:
        with (options.delivery / 'ACCEPTANCE_RESULTS.tsv').open() as table:
            rows = list(csv.DictReader(table, delimiter='\t'))
        if len(rows) != 82 or len({row['case_id'] for row in rows}) != 82:
            blockers.append('ACCEPTANCE_TABLE_NOT_82_UNIQUE_CASES')
        for row in rows:
            if row['requirement'] == 'REQUIRED' and row['required_stage'] in ('B1_EXIT', 'B2_EXIT', 'B3_EXIT') and row['status'] != 'PASS':
                blockers.append('ENGINEERING_' + row['case_id'] + '_' + row['status'])
        identity['acceptance_sha256'] = digest(options.delivery / 'ACCEPTANCE_RESULTS.tsv')
        manifest_path = options.delivery / 'SOURCE_MANIFEST.json'
        rows = read_json(manifest_path)
        if not isinstance(rows, list):
            raise CanaryError('SOURCE_MANIFEST_SCHEMA')
        paths = {row['path']: row for row in rows}
        for name in SOURCE_REQUIREMENTS:
            if name not in paths:
                blockers.append('UNFROZEN_SOURCE_' + name)
        for row in rows:
            actual = safe_code_path(row['path'])
            if not actual.is_file() or digest(actual) != row.get('after_sha256'):
                blockers.append('SOURCE_HASH_MISMATCH_' + row['path'])
        identity['source_manifest_sha256'] = digest(manifest_path)
        lock = read_json(options.delivery / 'RUNTIME_LOCK.json')
        checks = lock.get('verification', {})
        if checks.get('clean_directory_reinstall') != 'PASS':
            blockers.append('CLEAN_LOCK_REINSTALL_NOT_PASS')
        if digest(RUNTIME / 'package-lock.json') != lock.get('lock', {}).get('sha256'):
            blockers.append('PACKAGE_LOCK_HASH_MISMATCH')
        compiled = {
            'extension_dist_sha256': 'dist/extensions/biomind.js',
            'host_dist_sha256': 'dist/host.js',
            'usage_observer_dist_sha256': 'dist/usage-observer.js',
            'canary_budget_dist_sha256': 'dist/canary-budget.js',
            'canary_dispatch_dist_sha256': 'dist/canary-dispatch.js',
        }
        for key, relative in compiled.items():
            actual = digest(RUNTIME / relative)
            identity[key] = actual
            if checks.get(key) != actual:
                blockers.append('RUNTIME_LOCK_' + key + '_NOT_CURRENT')
        patch_path = RUNTIME / 'compat/pi-0.87.1-observer.patch.json'
        patch = read_json(patch_path)
        package = RUNTIME / 'node_modules/@earendil-works/pi-coding-agent'
        if read_json(package / 'package.json').get('version') != '0.87.1':
            blockers.append('PI_PACKAGE_VERSION_MISMATCH')
        for row in patch['files']:
            relative = Path(row['path'])
            if relative.is_absolute() or '..' in relative.parts or digest(package / relative) != row['patchedSha256']:
                blockers.append('COMPAT_PATCH_NOT_APPLIED_' + str(relative))
        identity['compat_patch_sha256'] = digest(patch_path)
        identity['runtime_label'] = patch['runtimeLabel']
        identity['registry_sha256'] = digest(options.registry)
        counts = ledger_counts()
        selected = SCENARIOS if options.scenario == 'all' else (options.scenario,)
        if counts['total'] >= 18:
            blockers.append('CANARY_TOTAL_BUDGET_EXHAUSTED')
        for scenario in selected:
            if counts[scenario] >= 6:
                blockers.append(scenario + '_BUDGET_EXHAUSTED')
        tool = registered_tool(options.registry)
        if 'R3' in selected and tool is None:
            blockers.append('R3_NO_DECLARED_REGISTERED_EXECUTABLE_PREFIX')
        if not options.provider or not re.fullmatch(r'[A-Za-z0-9._-]+', options.provider):
            blockers.append('EXPLICIT_NONSECRET_PROVIDER_REQUIRED')
        if not options.model or not re.fullmatch(r'[A-Za-z0-9._:/-]+', options.model):
            blockers.append('EXPLICIT_NONSECRET_MODEL_REQUIRED')
        if not options.python.is_absolute() or not options.python.is_file():
            blockers.append('EXISTING_ABSOLUTE_PYTHON_REQUIRED')
        node = RUNTIME / '.runtime/node/bin/node'
        if not node.is_file():
            blockers.append('PINNED_PRIVATE_NODE_MISSING')
        return {'status': 'BLOCKED' if blockers else 'READY_FOR_EXPLICIT_EXECUTE', 'blockers': blockers,
                'identity': identity, 'ledger': str(LEDGER), 'budget': counts, 'selected_scenarios': list(selected),
                'registered_tool': tool, 'real_provider_sends_in_plan': 0,
                'limits': {'per_scenario': 6, 'total': 18},
                'credential_policy': 'Only child runtime inherits existing environment; no dotenv/auth file reads or export.'}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, CanaryError) as error:
        return {'status': 'BLOCKED', 'blockers': blockers + [str(error) if isinstance(error, CanaryError) else type(error).__name__],
                'identity': identity, 'real_provider_sends_in_plan': 0}


def safe_frame(frame):
    """Persist public outputs/identities only; provider error/config text omitted."""
    kind = frame.get('type')
    if kind == 'event':
        event = frame.get('event', {})
        row = {key: event[key] for key in ('type', 'toolCallId', 'toolName', 'isError', 'attempt', 'willRetry', 'success') if key in event}
        if event.get('type') == 'message_end' and event.get('message', {}).get('role') == 'assistant':
            message = event['message']
            row['stopReason'] = message.get('stopReason')
            if message.get('stopReason') == 'stop':
                row['text'] = ''.join(part.get('text', '') for part in message.get('content', []) if part.get('type') == 'text')
        if event.get('type') == 'tool_execution_end':
            details = event.get('result', {}).get('details', {})
            row['facts'] = {key: details[key] for key in ('status', 'run_ref', 'terminal', 'exit_code', 'path', 'sha256', 'receipt_ref') if key in details}
        return {'type': kind, 'event': row}
    allowed = {'ready': ('engine', 'version', 'extension_sha256', 'sessionId'),
               'biomind_state': ('data', 'inputId'), 'biomind_settled': ('data',),
               'diagnostic': ('code',), 'error': ('code',),
               'question': ('id', 'requestId', 'businessRequestId', 'sessionId', 'question'),
               'approval': ('id', 'requestId', 'businessRequestId', 'sessionId'),
               'result': ('requestId', 'runtime_status', 'model_conclusion', 'scientific_success', 'usage', 'usage_scope', 'extension_sha256')}
    return {'type': kind, **{key: frame[key] for key in allowed.get(kind, ()) if key in frame}}


def make_scenario(directory, scenario, tool_info, fixtures):
    cwd = directory / scenario
    cwd.mkdir()
    (cwd / 'agent').mkdir()
    (cwd / 'preference.md').write_text('只处理本次隔离合成资料；不安装、不读取旧会话；基于实际工具回执报告。', encoding='utf-8')
    specification = {'cwd': cwd, 'allowed_write': None, 'allowed_command': None, 'expected': None}
    if scenario == 'R1':
        specification['prompt'] = '仅作概念解释：VCF 中的基因型 0/1 表示什么？不要调用工具、执行命令、交接或创建文件。'
    elif scenario == 'R2':
        selected = cwd / 'brief.txt'
        content = '这是本次全新隔离目录中的合成 canary 说明。\n'
        specification.update(allowed_write=selected, write_content=content,
            prompt=f'请创建一份合成说明文件，内容为 {json.dumps(content, ensure_ascii=False)}。输出名尚未选择 brief.txt 或 detailed.txt；请先真实询问我的选择，等待答复后再交接并使用 write。只允许写本工作目录中的选定文件，不执行shell、不安装。',
            answer='选择 brief.txt；只在当前新隔离目录写这一份文件，允许交接；不授权安装或运行命令。')
    else:
        if tool_info is None:
            raise CanaryError('R3_REGISTERED_TOOL_UNAVAILABLE')
        for name in ('tiny.vcf', 'tiny.fa'):
            shutil.copyfile(fixtures / name, cwd / name)
        executable = tool_info['executable']
        if tool_info['name'] == 'bcftools':
            input_path = cwd / 'tiny.vcf'; rows = cwd / 'observed-genotypes.tsv'; samples = cwd / 'observed-samples.txt'
            command = (shlex.join([executable, 'query', '-f', '%CHROM\t%POS[\t%GT]\n', str(input_path)])
                       + ' > ' + shlex.quote(str(rows)) + ' && '
                       + shlex.join([executable, 'query', '-l', str(input_path)]) + ' > ' + shlex.quote(str(samples)))
            targets = [rows, samples]
        else:
            input_path = cwd / 'tiny.fa'; command = shlex.join([executable, 'faidx', str(input_path)]); targets = [cwd / 'tiny.fa.fai']
        specification.update(allowed_command=command, targets=targets, tool_info=tool_info,
            prompt=('对新目录中的合成夹具做一次最小真实工具检查；先明确交接，随后仅调用一次 run_command，'
                    f'参数 cmd 必须完整为 {json.dumps(command, ensure_ascii=False)}，workdir={json.dumps(str(cwd))}，'
                    f'env_name={json.dumps(tool_info["env_name"])}。等待该 run_ref 的真实结果，读取 '
                    + '、'.join(str(value) for value in targets)
                    + ' 后依据实际回执解释观察结果并引用真实路径，说明这是合成数据；不安装、不把预期当结果、不运行其它命令。'))
    return specification


def approval_allowed(frame, spec):
    proposal = frame.get('payload') or frame.get('proposal') or {}
    payload = proposal.get('payload', {})
    if not proposal.get('request_id') or not proposal.get('action_hash') or not proposal.get('request_scope'):
        return False
    if spec.get('approved_proposals', 0):
        return False
    if payload.get('kind') == 'file_write' and spec.get('allowed_write'):
        args = payload.get('arguments', {})
        return (spec.get('question_answered') is True and payload.get('tool') == 'write_file' and Path(payload.get('path', '')).resolve() == spec['allowed_write']
                and args.get('content') == spec['write_content'] and args.get('expected_sha256', '') == '')
    if payload.get('kind') == 'command' and spec.get('allowed_command'):
        args = payload.get('arguments', {})
        return (payload.get('cwd') == str(spec['cwd']) and args.get('cmd') == spec['allowed_command']
                and set(args) == {'cmd', 'env'} and isinstance(args['env'], dict)
                and set(args['env']) == {'ENV_PREFIX'} and args['env']['ENV_PREFIX'] == spec['tool_info']['prefix'])
    return False


def validate_result(scenario, spec, frames, result, before, after):
    if result.get('runtime_status') != 'completed' or result.get('scientific_success') is not None:
        raise CanaryError('CANARY_RUNTIME_NOT_COMPLETED_OR_FALSE_SCIENCE_STATUS')
    sends = after[scenario] - before[scenario]
    if not 1 <= sends <= 6 or after['total'] > 18 or after[scenario] > 6:
        raise CanaryError('CANARY_SEND_COUNT_INVALID')
    events = [row.get('event', {}) for row in frames if row.get('type') == 'event']
    tools = [row for row in events if row.get('type') == 'tool_execution_start']
    generate = {row.get('data', {}).get('requestId') for row in frames if row.get('type') == 'biomind_state' and row.get('data', {}).get('role') == 'Generate'}
    if scenario == 'R1':
        if tools or generate or not result.get('model_conclusion', '').strip():
            raise CanaryError('R1_NOT_PURE_CONSULT')
    elif scenario == 'R2':
        if sum(row.get('type') == 'question' for row in frames) != 1 or len(generate) != 1:
            raise CanaryError('R2_QUESTION_OR_HANDOFF_COUNT')
        if spec['allowed_write'].read_text() != spec['write_content']:
            raise CanaryError('R2_ACTUAL_FILE_MISMATCH')
        if any(row.get('toolName') in ('run_command', 'install_tool') for row in tools):
            raise CanaryError('R2_UNEXPECTED_EXECUTION_TOOL')
    else:
        if sum(row.get('toolName') == 'run_command' for row in tools) != 1 or any(row.get('toolName') == 'install_tool' for row in tools):
            raise CanaryError('R3_EXECUTION_COUNT_OR_INSTALL')
        facts = result.get('execution_facts', [])
        if not any(row.get('run_ref') and row.get('terminal') is True and row.get('exit_code') == 0 for row in facts):
            raise CanaryError('R3_NO_ACTUAL_SUCCESSFUL_TERMINAL_RECEIPT')
        if spec['tool_info']['name'] == 'bcftools':
            rows = [line.split('\t') for line in spec['targets'][0].read_text().splitlines() if line]
            samples = spec['targets'][1].read_text().splitlines()
            if samples != ['S01', 'S02', 'S03'] or len(rows) != 5 or [int(row[1]) for row in rows] != [10, 20, 30, 40, 50] or any(len(row) != 5 for row in rows):
                raise CanaryError('R3_BCFTOOLS_REFERENCE_MISMATCH')
        else:
            rows = [line.split('\t') for line in spec['targets'][0].read_text().splitlines()]
            if len(rows) != 1 or rows[0][:2] != ['chrSynthetic', '100']:
                raise CanaryError('R3_SAMTOOLS_REFERENCE_MISMATCH')
        if not any(str(target) in result.get('model_conclusion', '') for target in spec['targets']):
            raise CanaryError('R3_MODEL_DID_NOT_REFERENCE_ACTUAL_OUTPUT')
    if sum(row.get('type') == 'agent_settled' for row in events) != 1:
        raise CanaryError('CANARY_SETTLEMENT_COUNT')
    return {'status': 'PASS_SCOPED_CANARY', 'actual_sends': sends, 'scientific_success': None,
            'reference_check': 'synthetic evaluator only, never included in model prompt'}


def run_one(options, directory, scenario, tool_info, identity, lock_fd):
    fixtures = options.delivery / 'request/BioMind_Pi_1-4批完整迁移交接包_20260926/fixtures'
    spec = make_scenario(directory, scenario, tool_info, fixtures)
    cwd = spec['cwd']; config = {'provider': options.provider, 'model': options.model, 'thinkingLevel': options.thinking,
        'python': str(options.python), 'installRoot': str(REPO), 'agentDir': str(cwd / 'agent'), 'dataRoot': str(cwd / 'domain'),
        'registryPath': str(options.registry), 'preferencesPath': str(cwd / 'preference.md'),
        'memoryRoot': str(cwd / 'memory'), 'wikiRoot': str(cwd / 'wiki'), 'archivePath': '', 'historyEnabled': False, 'memoryMode': 'disabled'}
    config_path = cwd / 'nonsecret-config.json'; config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
    # Credentials remain opaque process environment values. No dotenv, auth.json,
    # npmrc, credential read APIs, or environment values enter evidence.
    environment = os.environ.copy()
    environment.update(BIOMIND_PI_CANARY_LEDGER=str(LEDGER), BIOMIND_PI_CANARY_SCENARIO=scenario,
                       BIOMIND_PI_PYTHON=str(options.python), PI_OFFLINE='1')
    environment.pop('BIOMIND_PI_PROVIDER_FIXTURE', None)
    before = ledger_counts()
    command = [str(RUNTIME / '.runtime/node/bin/node'), '--max-old-space-size=1024', str(RUNTIME / 'dist/host.js'),
               '--mode', 'json', '--project-dir', str(cwd), '--config', str(config_path)]
    # Keep the same open-file-description lock alive in the model host if the
    # runner returns while it is still draining. Closing this parent's FD does
    # not unlock the inherited FD; no subsequent runner can mix its counters.
    child = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, pass_fds=(lock_fd,))
    selector = selectors.DefaultSelector(); selector.register(child.stdout, selectors.EVENT_READ, 'stdout'); selector.register(child.stderr, selectors.EVENT_READ, 'stderr')
    pending = b''; stderr_bytes = 0; frames = []; raw_result = None; outcome = {'status': 'FAIL'}; started = time.monotonic(); question_seen = False
    def send(value):
        child.stdin.write((json.dumps(value, ensure_ascii=False) + '\n').encode()); child.stdin.flush()
    try:
        while time.monotonic() - started < options.timeout:
            if child.poll() is not None and not selector.get_map():
                break
            for key, _ in selector.select(timeout=0.25):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj); continue
                if key.data == 'stderr':
                    stderr_bytes += len(chunk); continue
                pending += chunk
                if len(pending) > 4 * 1024 * 1024:
                    raise CanaryError('HOST_FRAME_LIMIT')
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1); frame = json.loads(line); frames.append(safe_frame(frame))
                    if frame.get('type') == 'ready':
                        if frame.get('extension_sha256') != identity['extension_dist_sha256']:
                            raise CanaryError('LOADED_EXTENSION_HASH_MISMATCH')
                        send({'type': 'prompt', 'requestId': scenario, 'text': spec['prompt']})
                    elif frame.get('type') == 'question':
                        if scenario != 'R2' or question_seen:
                            send({'type': 'abort'}); raise CanaryError('UNEXPECTED_CANARY_QUESTION')
                        question_seen = True
                        count = ledger_counts()[scenario]
                        time.sleep(0.2)
                        if ledger_counts()[scenario] != count:
                            send({'type': 'abort'}); raise CanaryError('MODEL_POLLED_DURING_QUESTION')
                        send({'type': 'ui_response', 'id': frame['id'], 'value': spec['answer']})
                        spec['question_answered'] = True
                    elif frame.get('type') == 'approval':
                        allowed = approval_allowed(frame, spec)
                        if allowed:
                            spec['approved_proposals'] = spec.get('approved_proposals', 0) + 1
                        send({'type': 'ui_response', 'id': frame['id'], 'value': {'choice': 'y' if allowed else 'n'}})
                        frames.append({'type': 'canary_approval_fixture', 'allowed': allowed, 'id': frame['id'], 'simulated_user': True})
                        if not allowed:
                            send({'type': 'abort'}); raise CanaryError('UNEXPECTED_APPROVAL_PROPOSAL_DENIED')
                    elif frame.get('type') == 'result':
                        raw_result = frame
                if raw_result is not None:
                    break
            if raw_result is not None:
                break
        if raw_result is None:
            raise CanaryError('CANARY_NO_TERMINAL_RESULT_WITHIN_DRIVER_BOUND')
        outcome = validate_result(scenario, spec, frames, raw_result, before, ledger_counts())
    except (OSError, ValueError, KeyError, TypeError, CanaryError) as error:
        outcome = {'status': 'FAIL', 'reason': str(error) if isinstance(error, CanaryError) else type(error).__name__}
    finally:
        # Closing input preserves any already approved worker through the real
        # domain disconnect policy; no kill-all or implicit scientific cancel.
        try:
            if child.poll() is None:
                send({'type': 'abort'})
            child.stdin.close()
        except (OSError, BrokenPipeError):
            pass
        try:
            exit_code = child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            exit_code = None
            outcome = {'status': 'BLOCKED', 'reason': 'HOST_STILL_DRAINING_NO_FORCED_WORKER_TERMINATION', 'host_pid': child.pid}
        selector.close()
        try:
            after = ledger_counts()
        except (CanaryError, sqlite3.Error):
            after = {'status': 'UNAVAILABLE_NO_REFUND'}
        if exit_code not in (0, None) and outcome.get('status') == 'PASS_SCOPED_CANARY':
            outcome = {'status': 'FAIL', 'reason': 'HOST_NONZERO_EXIT', 'exit_code': exit_code}
        report = {'scenario': scenario, **outcome, 'identity': identity, 'frames': frames,
                  'ledger_before': before, 'ledger_after': after, 'exit_code': exit_code, 'stderr_bytes_not_exported': stderr_bytes,
                  'automated_user_fixture': scenario in ('R2', 'R3'), 'scientific_success': None,
                  'reference_values_only_in_postrun_evaluator': True,
                  'runner_lock_lifetime': 'shared inherited descriptor held until both runner and model host exit; ledger never reset'}
        (directory / (scenario + '-result.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True); action.add_argument('--plan', action='store_true'); action.add_argument('--execute', action='store_true')
    parser.add_argument('--delivery', type=Path, default=DELIVERY)
    parser.add_argument('--registry', type=Path, default=REPO / 'software_registry.json')
    parser.add_argument('--provider'); parser.add_argument('--model'); parser.add_argument('--thinking', choices=('off', 'minimal', 'low', 'medium', 'high'), default='off')
    parser.add_argument('--scenario', choices=(*SCENARIOS, 'all'), default='all')
    parser.add_argument('--python', type=Path, default=Path('/public/home/liusiqi/anaconda3/envs/biomind/bin/python'))
    parser.add_argument('--timeout', type=int, default=300, help='Bound of this canary driver only; does not alter scientific executor policy')
    options = parser.parse_args()
    if not 30 <= options.timeout <= 600:
        parser.error('--timeout must be between 30 and 600 seconds')
    options.delivery = options.delivery.resolve(); options.registry = options.registry.resolve()
    plan = preflight(options)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if plan['status'] != 'READY_FOR_EXPLICIT_EXECUTE':
        return 2
    if options.plan:
        return 0
    try:
        with exclusive_execution() as lock_fd:
            locked_plan = preflight(options)
            if locked_plan['status'] != 'READY_FOR_EXPLICIT_EXECUTE' or locked_plan['identity'] != plan['identity']:
                print(json.dumps({'status': 'BLOCKED', 'reason': 'PREFLIGHT_CHANGED_BEFORE_LOCKED_EXECUTION'}))
                return 2
            return execute_plan(options, locked_plan, lock_fd)
    except CanaryError as error:
        print(json.dumps({'status': 'BLOCKED', 'reason': str(error)}))
        return 2


def execute_plan(options, plan, lock_fd):
    """Caller holds exclusive_execution throughout the complete scenario set."""
    directory = options.delivery / 'evidence' / ('live-canaries-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:8])
    directory.mkdir(mode=0o700)
    (directory / 'preflight.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    for scenario in plan['selected_scenarios']:
        # Recheck frozen production bytes before each new process. The evidence
        # index itself is not rewritten while scenarios run.
        current_plan = preflight(argparse.Namespace(**(vars(options) | {'scenario': scenario})))
        if current_plan['status'] != 'READY_FOR_EXPLICIT_EXECUTE' or current_plan['identity'] != plan['identity']:
            print(json.dumps({'status': 'BLOCKED', 'reason': 'PREFLIGHT_CHANGED', 'evidence': str(directory)})); return 2
        report = run_one(options, directory, scenario, plan['registered_tool'], plan['identity'], lock_fd)
        print(json.dumps({'scenario': scenario, 'status': report['status'], 'evidence': str(directory / (scenario + '-result.json'))}))
        if report['status'] != 'PASS_SCOPED_CANARY':
            return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
