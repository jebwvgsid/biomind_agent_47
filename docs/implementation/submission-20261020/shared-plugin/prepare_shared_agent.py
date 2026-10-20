"""Prepare a fresh shared Pi scope; never adopt or modify the legacy scope.

Settings, extensions and tool binaries are copied; existing private auth/model
files are copied with mode 0600 without recording their content or digests.
The sessions symlink retains Pi's native per-project history layout.
"""
from pathlib import Path
import importlib.util
import json
import os
import stat
import sys
import hashlib

B = Path('/public/home/liusiqi/03.毕设')
O = B / 'BioMind_Production_20261020'
OUT = O / 'shared-plugin'
CORE = Path(json.loads((O / 'build-result.json').read_text())['root'])
OLD = B / 'Biomind_v1/integrations/pi_runtime/.biomind/pi-agent'
NEW = B / 'BioMind_Unified_Core_20261009/production/shared-agent-20261020'
PACKAGE = B / 'BioMind_Unified_Core_20261009/plugin-candidates/pi-web-access-20261010/check-20261010T025658571044Z/agent/npm/node_modules/pi-web-access'
PACKAGE_SHA = '66e5c91481b884b41a0a1dd8b253a19451fdb22fd661801ba4f39c2230dc0527'

def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value

def stable_read(path):
    before = path.lstat()
    assert stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
    raw = path.read_bytes()
    after = path.lstat()
    assert (before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_ino, after.st_size, after.st_mtime_ns), 'Source changed'
    return raw, stat.S_IMODE(before.st_mode)

def create(path, raw, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())

def main():
    assert not NEW.exists(), 'Fresh scope required; do not reuse a partial preparation'
    OUT.mkdir(mode=0o700, exist_ok=True)
    project = OUT / 'bootstrap-project'
    project.mkdir(mode=0o700)
    helper = module('bootstrap_shared_resources', B / 'BioMind_Submission_Work_20261020/result/production-review/bootstrap_web_resources.py')
    identity = json.loads((CORE / 'RELEASE.json').read_text())
    receipt = helper.prepare_agent_resources(
        core=CORE, python=identity['python']['path'], node=identity['node']['path'],
        agent_dir=NEW, project_dir=project, settings_source=OLD / 'settings.json',
        package_source=PACKAGE, package_sha256=PACKAGE_SHA,
        web_config=json.loads((B / 'BioMind_Submission_Work_20261020/result/production-review/web-search.json.candidate').read_text()))
    guard = module('shared_guard', CORE / 'agent_core/pi_plugin_guard.py')
    request = {'agentDir': str(NEW), 'cwd': str(project), 'scopes': ['user']}
    plan = guard.prepare(request)
    assert not any(s['legacyUnverified'] or s['adoptionPending'] for s in plan['scopes'])
    fds = []
    copied = []
    try:
        # Gate first, then scope: no new readers may enter during preparation.
        for key in ('gate', 'lock'):
            row = plan['scopes'][0]
            fd = os.open(row[key], os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            fds.append(fd)
            guard.lock_fds({'files': [{'fd': fd, 'path': row[key], 'mode': 'exclusive'}]})
        for name in ('auth.json', 'models-store.json', 'models.json'):
            src = OLD / name
            if not src.exists():
                continue
            raw, mode = stable_read(src)
            assert not mode & 0o077, 'Private source mode required'
            create(NEW / name, raw, 0o600)
            assert (NEW / name).read_bytes() == src.read_bytes()
            copied.append({'name': name, 'private': True, 'byte_equal': True})
        for dirname in ('extensions', 'bin'):
            (NEW / dirname).mkdir(mode=0o700, exist_ok=True)
            for src in sorted((OLD / dirname).rglob('*')):
                assert not src.is_symlink(), 'Review symlink before migrating'
                target = NEW / src.relative_to(OLD)
                if src.is_dir():
                    target.mkdir(mode=0o700, exist_ok=True)
                    continue
                raw, mode = stable_read(src)
                create(target, raw, 0o700 if mode & 0o111 else 0o600)
                copied.append({'name': str(src.relative_to(OLD)), 'sha256': hashlib.sha256(raw).hexdigest()})
        assert (OLD / 'sessions').is_dir()
        (NEW / 'sessions').symlink_to(OLD / 'sessions', target_is_directory=True)
        assert (NEW / 'sessions').resolve() == (OLD / 'sessions').resolve()
    finally:
        for fd in reversed(fds):
            os.close(fd)
    receipt.update(status='PREPARED_SHARED', oldAgentDir=str(OLD),
                   newAgentDir=str(NEW), sessionRoot=str((OLD / 'sessions').resolve()),
                   legacyScopeModified=False, oldProcessesStopped=False,
                   slurmJobsModified=False, credential_copy=True,
                   copied=copied, guard=guard.plan(request))
    (OUT / 'preparation.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': receipt['status'], 'agentDir': str(NEW), 'historyPreserved': True}))

if __name__ == '__main__':
    main()
