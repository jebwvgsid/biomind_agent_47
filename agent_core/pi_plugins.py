"""Package-only dispatch: never model/session initialization or dotenv loading."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

COMMANDS = {'plugins', 'install', 'remove', 'uninstall', 'list', 'config', 'update'}


def parser():
    p = argparse.ArgumentParser(description='BioMind pinned Pi package management (server-side)')
    p.add_argument('command', choices=sorted(COMMANDS))
    p.add_argument('source', nargs='?')
    p.add_argument('--json', action='store_true', help='stdout 仅输出 BioMindPluginsV1 JSON；脚本必须显式指定')
    p.add_argument('--verbose', action='store_true', help='显示脱敏子进程诊断（--json 时仅保存日志）')
    p.add_argument('--local', '-l', action='store_true')
    p.add_argument('--extensions', action='store_true')
    p.add_argument('--self', action='store_true')
    p.add_argument('--all', action='store_true')
    p.add_argument('--pi-config', type=Path)
    p.add_argument('--pi-node', type=Path)
    p.add_argument('--project-dir', type=Path)
    p.add_argument('--declaration', help='Complete native PackageSource JSON; preserve resource filters')
    return p


def read_context(args):
    context = {'python': str(Path(sys.executable).resolve()), 'cwd': str(Path.cwd().resolve())}
    if args.pi_config:
        config = args.pi_config.expanduser().resolve()
        # This entry never loads credentials or arbitrary dotenv text.
        if config.suffix != '.json' or any(word in config.name.lower() for word in ('auth', 'secret', 'credential', 'api_key', 'models')):
            raise ValueError('NONSECRET_RUNTIME_CONFIG_REQUIRED')
        with config.open(encoding='utf-8') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError('INVALID_RUNTIME_CONFIG')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('INVALID_RUNTIME_CONFIG')
        if value.get('agentDir') is not None:
            agent = Path(value['agentDir']).expanduser()
            if not agent.is_absolute():
                raise ValueError('ABSOLUTE_AGENT_DIRECTORY_REQUIRED')
            context['agentDir'] = str(agent.resolve())
    if args.project_dir:
        context['cwd'] = str(args.project_dir.expanduser().resolve())
    return context


def make_request(args):
    if args.command == 'plugins':
        if args.self or args.all:
            raise ValueError('PINNED_PI_SELF_UPDATE_REFUSED')
        if not args.json or args.source or args.local or args.extensions or args.declaration:
            raise ValueError('PLUGINS_JSON_REQUEST_REQUIRED')
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError('PLUGIN_REQUEST_TOO_LARGE')
        return json.loads(raw)
    action = 'remove' if args.command == 'uninstall' else args.command
    if args.self or args.all or (action == 'update' and args.source in ('self', 'pi')):
        raise ValueError('PINNED_PI_SELF_UPDATE_REFUSED')
    if action == 'update' and not args.extensions and not args.source:
        raise ValueError('PINNED_PI_SELF_UPDATE_REFUSED')
    if args.extensions and action != 'update':
        raise ValueError('INVALID_PLUGIN_ARGUMENT')
    if action == 'update' and args.local:
        raise ValueError('NATIVE_UPDATE_HAS_NO_SCOPE_OPTION')
    request = {'action': action}
    if action != 'update':
        request['scope'] = 'project' if args.local else 'user'
    if args.source:
        request['source'] = args.source
    if args.declaration:
        request['declaration'] = json.loads(args.declaration)
    if action == 'config' and not args.declaration:
        # Native interactive config invokes resolve() and can install packages;
        # expose explicit filtered edits rather than silently invoking it here.
        raise ValueError('CONFIG_DECLARATION_REQUIRED')
    return request


def main(argv, *, runtime=None):
    from agent_core.pi_entry import RUNTIME, MINIMUM_NODE, PiEntryError, node_executable, node_version
    runtime = (runtime or RUNTIME).resolve()
    args = parser().parse_args(list(argv))
    from agent_core.pi_plugin_output import emit, run_child
    child_started = False
    log_path = None
    try:
        request = make_request(args)
        context = read_context(args)
        node = node_executable(args.pi_node, os.environ, runtime)
        version = subprocess.run([str(node), '--version'], capture_output=True, text=True, timeout=10, check=True)
        parsed = node_version(version.stdout)
        if parsed is None or parsed < MINIMUM_NODE:
            raise ValueError('UNSUPPORTED_NODE_VERSION')
        package = json.loads((runtime / 'node_modules/@earendil-works/pi-coding-agent/package.json').read_text())
        if package.get('version') != '0.87.1':
            raise ValueError('PINNED_PI_VERSION_MISMATCH')
        cli = runtime / 'dist/plugin-cli.js'
        if not cli.is_file():
            raise ValueError('PLUGIN_RUNTIME_BUILD_REQUIRED')
        # Separate result fd: npm/git lifecycle output cannot corrupt JSON or
        # leak into desktop error messages. Temporary file is unlinked/private.
        with tempfile.TemporaryFile() as result_file:
            environment = dict(os.environ)
            environment['BIOMIND_PLUGIN_RESULT_FD'] = str(result_file.fileno())
            environment['BIOMIND_PI_PYTHON'] = context['python']
            child_started = True
            returncode, log_path = run_child([str(node), str(cli)], {'request': request, 'context': context},
                environment, result_file.fileno(), machine=args.json, verbose=args.verbose)
            result_file.seek(0)
            raw = result_file.read(1048577)
            if len(raw) > 1048576:
                raise ValueError('PLUGIN_RESULT_TOO_LARGE')
            result = json.loads(raw)
            if result.get('schema') != 'BioMindPluginsV1':
                raise ValueError('PLUGIN_RESULT_INVALID')
            if result.get('status') == 'ok' and returncode != 0:
                raise ValueError('PACKAGE_CLI_EXIT_OUTCOME_UNKNOWN')
        result['logFile'] = log_path
        result['childExitCode'] = returncode
        emit(result, args.json)
        return 0 if result.get('status') == 'ok' else 3 if result.get('status') == 'BUSY' else 2
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        mutation = isinstance(locals().get('request'), dict) and request.get('action') in ('install', 'remove', 'update', 'repair')
        emit({'schema': 'BioMindPluginsV1', 'status': 'repair_required' if mutation else 'error',
                          'code': ('PACKAGE_CLI_CANCELLED_OUTCOME_UNKNOWN' if isinstance(error, KeyboardInterrupt) else 'PACKAGE_CLI_TIMEOUT_OUTCOME_UNKNOWN') if mutation else 'PLUGIN_CLI_CANCELLED_OR_TIMEOUT',
                          'logFile': getattr(error, 'log_path', log_path), 'childState': 'own_process_group_terminated',
                          'stage': 'child_process', 'outcomeUnknown': mutation, 'repairRequired': mutation,
                          'changed': False, 'reloadRequired': mutation}, args.json)
        return 130 if isinstance(error, KeyboardInterrupt) else 2
    except (OSError, ValueError, PiEntryError, subprocess.SubprocessError) as error:
        code = str(error) if isinstance(error, ValueError) and str(error).replace('_', '').isupper() else 'PLUGIN_OPERATION_FAILED'
        uncertain = child_started and isinstance(locals().get('request'), dict) and request.get('action') in ('install', 'remove', 'update', 'repair')
        emit({'schema': 'BioMindPluginsV1', 'status': 'repair_required' if uncertain else 'BLOCKED' if code == 'PINNED_PI_SELF_UPDATE_REFUSED' else 'error',
                          'code': 'PACKAGE_CLI_RESULT_OUTCOME_UNKNOWN' if uncertain else code,
                          'outcomeUnknown': uncertain, 'changed': False, 'reloadRequired': uncertain, 'repairRequired': uncertain, 'logFile': getattr(error, 'log_path', log_path)}, args.json)
        return 2
