#!/usr/bin/env python3
"""Install the BioMind user command without installing packages or reading keys.

The existing Pi agent directory is reused through a directory alias. No auth
file is opened, moved or copied. Shell files receive only a bounded PATH block;
their contents are never printed or copied to a report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

PATH_BLOCK = b'''\n# >>> BioMind user command >>>
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) export PATH="$PATH:$HOME/.local/bin" ;;
esac
# <<< BioMind user command <<<
'''


def present(path: Path) -> bool:
    return os.path.lexists(path)


def install(repo: Path, user_home: Path, agent_dir: Path, *, apply: bool) -> dict:
    repo, user_home = repo.resolve(), user_home.absolute()
    launcher = repo/'bin/biomind'
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise ValueError('Executable BioMind launcher is missing')
    command = user_home/'.local/bin/biomind'
    shared = agent_dir.absolute()
    previous = repo/'integrations/pi_runtime/.biomind/pi-agent'
    # Preflight every target before any mutation; never overwrite a command or
    # replace a pre-existing personal credential/configuration directory.
    if present(command) and not (command.is_symlink() and command.resolve() == launcher):
        raise ValueError('An existing biomind command would be replaced')
    if present(shared) and not shared.is_dir():
        raise ValueError('The personal BioMind agent path is not a directory')
    reuse = not present(shared) and previous.is_dir()
    profiles = []
    for filename in ('.bashrc', '.bash_profile'):
        path = user_home/filename
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError('Shell startup file must be a regular file')
        payload = path.read_bytes() if path.exists() else b''
        if b'# >>> BioMind user command >>>' in payload and PATH_BLOCK not in payload:
            raise ValueError('A modified BioMind PATH block needs manual review')
        profiles.append((path, payload, PATH_BLOCK not in payload))
    record = {'mode': 'APPLIED' if apply else 'CHECK_ONLY', 'command': str(command),
              'launcher': str(launcher), 'agent_dir': str(shared),
              'existing_agent_directory_reused': reuse or (shared.is_symlink() and shared.resolve() == previous.resolve()),
              'credential_contents_read': False, 'credential_files_moved_or_copied': False,
              'packages_installed': False, 'model_calls': 0, 'shell_changes': []}
    if apply:
        command.parent.mkdir(parents=True, exist_ok=True)
        if not present(command):
            command.symlink_to(launcher)
        shared.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not present(shared):
            if reuse:
                shared.symlink_to(previous, target_is_directory=True)
            else:
                shared.mkdir(mode=0o700)
    for path, before, append in profiles:
        if apply and append:
            # Append preserves every previous byte, mode and existing setting.
            # Recheck the small source file so concurrent edits are not hidden.
            if (path.read_bytes() if path.exists() else b'') != before:
                raise ValueError('Shell startup file changed during installation')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'ab') as stream:
                stream.write(PATH_BLOCK)
            after = path.read_bytes()
            if after != before + PATH_BLOCK:
                raise ValueError('Shell PATH append was not verified')
        else:
            after = before
        record['shell_changes'].append({'path': str(path), 'path_block_added': apply and append,
                                       'before_sha256': hashlib.sha256(before).hexdigest(),
                                       'after_sha256': hashlib.sha256(after).hexdigest(),
                                       'existing_bytes_preserved': True})
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    phase = 'resolve_pi_directory'
    try:
        runtime = args.repo.resolve()/'integrations/pi_runtime'
        # Ask the installed Pi for its own directory. Do not recreate its HOME,
        # tilde or PI_CODING_AGENT_DIR configuration rules in BioMind.
        config_url = (runtime/'node_modules/@earendil-works/pi-coding-agent/dist/config.js').as_uri()
        code = ('import { getAgentDir } from '+json.dumps(config_url)+'; '
                'process.stdout.write(JSON.stringify(getAgentDir()));')
        query = subprocess.run([str(runtime/'.runtime/node/bin/node'), '--input-type=module', '-e', code],
                               cwd=runtime, capture_output=True, text=True, timeout=15, check=True)
        agent_dir = Path(json.loads(query.stdout))
        phase = 'install_user_entry'
        result = install(args.repo, Path.home(), agent_dir, apply=args.apply)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Never emit file data or an unfiltered operating-system exception.
        print(json.dumps({'status': 'FAILED', 'error_type': type(error).__name__, 'phase': phase,
                          'errno': getattr(error, 'errno', None),
                          'os_message': getattr(error, 'strerror', None),
                          'exit_code': getattr(error, 'returncode', None),
                          'message': 'User launcher setup could not complete; existing configuration was not replaced.'}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
