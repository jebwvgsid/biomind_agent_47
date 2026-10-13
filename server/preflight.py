#!/usr/bin/env python3
"""Read configuration and inspect paths only. No credentials/network/model startup."""
import argparse
import json
import os
from pathlib import Path
import stat


def inspect(config_path):
    config = json.loads(Path(config_path).read_text())
    rows = []
    def check(name, condition):
        rows.append(dict(check=name, result='PASS' if condition else 'FAIL'))
    def file(name, value, executable=False):
        valid = isinstance(value, str) and value.startswith('/') and '\x00' not in value
        target = Path(value) if valid else None
        check(name, bool(valid and target.is_file() and (not executable or os.access(target, os.X_OK))))
    def directory(name, value):
        check(name, isinstance(value, str) and value.startswith('/') and Path(value).is_dir())
    check('service_schema', config.get('schema') == 'BioMindStudioServiceV1')
    check('configured_mode', config.get('mode') in ('local', 'slurm'))
    environment = config.get('environment', {})
    check('environment_mapping', isinstance(environment, dict))
    if not isinstance(environment, dict):
        environment = {}
    command = config.get('command', [])
    file('launch_executable', command[0] if command else None, True)
    file('launch_script', command[1] if len(command) > 1 else None)
    directory('backend_working_directory', config.get('cwd'))
    studio = environment.get('BIOMIND_STUDIO_ROOT')
    directory('backend_source', studio)
    if isinstance(studio, str) and studio.startswith('/'):
        for part in ('package.json', '.next-pkg/BUILD_ID', '.next-pkg/server/app/api/agent/profile/route.js',
                     '.next-pkg/server/app/api/agent/new/route.js', '.next-pkg/server/app/api/agent/[id]/route.js',
                     '.next-pkg/server/app/api/agent/[id]/events/route.js'):
            file('backend:' + part, str(Path(studio) / part))
    for name in ('BIOMIND_LAUNCHER_PYTHON','BIOMIND_ENTRYPOINT','BIOMIND_PIPE_LAUNCHER','BIOMIND_RUNTIME_CONFIG',
                 'BIOMIND_HISTORY_NODE','BIOMIND_HISTORY_QUERY_SCRIPT','BIOMIND_INSTRUCTIONS_QUERY_SCRIPT'):
        file(name, environment.get(name), name in ('BIOMIND_LAUNCHER_PYTHON','BIOMIND_HISTORY_NODE'))
    directory('native_session_root', environment.get('BIOMIND_SESSION_ROOT'))
    for name in ('BIOMIND_PROJECT_ROOTS_JSON','BIOMIND_DATA_ROOTS_JSON'):
        try:
            roots = json.loads(environment.get(name,''))
            valid = isinstance(roots,list) and bool(roots) and all(isinstance(root,str) and root.startswith('/') and Path(root).is_dir() for root in roots)
        except (ValueError, TypeError):
            valid = False
        check(name,valid)
    directory('upload_root',environment.get('PI_WEB_UPLOADS_DIR'))
    # lstat only: this checker intentionally never opens this credential source.
    private_path=config.get('environmentFile')
    try:
        info=Path(private_path).lstat()
        valid=stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and not info.st_mode & 0o077
    except (OSError,TypeError):
        valid=False
    check('credential_file_permissions_only',valid)
    rows.extend(dict(check=name,result='NOT_RUN') for name in (
        'credential_contents','scheduler_access','http_profile_identity','model_initialization','real_task','windows_application'))
    return dict(schema='BioMindStudioPreflightV1',status='paths_ok' if all(row['result']!='FAIL' for row in rows) else 'configuration_incomplete',
                credentialsRead=False,networkUsed=False,serviceStarted=False,checks=rows)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    args=parser.parse_args()
    try:
        result=inspect(args.config)
    except (OSError,ValueError,TypeError,KeyError):
        result=dict(schema='BioMindStudioPreflightV1',status='configuration_incomplete',code='INVALID_PREFLIGHT_CONFIGURATION',
                    credentialsRead=False,networkUsed=False,serviceStarted=False)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['status']=='paths_ok' else 2)
