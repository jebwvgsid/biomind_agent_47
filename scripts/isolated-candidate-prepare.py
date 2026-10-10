#!/usr/bin/env python3
"""Prepare a fresh private Studio candidate; never adopt a legacy resource root.

Reuses configure-service.py and the core's real resource guard. No network,
package installation, model request, process stop, or production write occurs.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import sys
import time

VERSION = 'isolated-server-1.3.0-rc.20261010.1'
CORE_ID = '30aeedb2e667e60c5f7699da16918f325a00d8d0ce7633bb42d30e3da7c012ab'
PLUGIN_SHA = '05eb99b2f5a8f0088dfe3ce3da444004b7f11cb3867d632718ff9f9ccbb656bd'
SENSITIVE = {'auth.json', 'credentials.json', 'credential.json', 'secrets.json', '.npmrc'}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as target:
        json.dump(value, target, ensure_ascii=False, indent=2)
        target.write('\n'); target.flush(); os.fsync(target.fileno())


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def private_dir(path):
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('CANDIDATE_PRIVATE_DIRECTORY_REQUIRED')


def copy_tree(source, target, allowed_root=None, skipped=(), inventory=None):
    """Physically copy code; resolve only links contained in the selected source.

    This does not preserve hardlinks, so any candidate write is independent.
    Explicit skips avoid runtime state and build caches; credential-like files
    are excluded before reading. No copy follows a link out of allowed_root.
    """
    source, target = Path(source), Path(target)
    allowed_root = Path(allowed_root or source).resolve(strict=True)
    resolved = source.resolve(strict=True)
    if resolved != allowed_root and allowed_root not in resolved.parents:
        raise RuntimeError('COPY_SOURCE_LINK_ESCAPES_SELECTED_TREE')
    if source.name.startswith('.env') or source.name in SENSITIVE or source.name in skipped:
        return
    if resolved.is_dir():
        target.mkdir(mode=0o700)
        for child in sorted(resolved.iterdir()):
            copy_tree(child, target / child.name, allowed_root, skipped, inventory)
        return
    if not resolved.is_file():
        raise RuntimeError('COPY_NON_REGULAR_SOURCE')
    shutil.copyfile(resolved, target)
    target.chmod(0o700 if os.access(resolved, os.X_OK) else 0o600)
    a, b = resolved.stat(), target.stat()
    if (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino) or target.is_symlink() or b.st_nlink != 1:
        raise RuntimeError('COPY_NOT_INDEPENDENT')
    actual = digest(target)
    if actual != digest(resolved):
        raise RuntimeError('COPY_CHANGED_DURING_PREPARATION')
    if inventory is not None:
        inventory.append({'source': str(resolved), 'path': str(target), 'sha256': actual, 'bytes': b.st_size})


def runtime_dependencies(source, target, inventory):
    """Copy the installed production dependency closure, not dev/UI packages.

    Entry packages match next.config.mjs serverExternalPackages, Next's runtime
    and React peers. Nested dependency roots remain at their original relative
    path. npm optional build/download tooling is not executed or installed.
    """
    source = source.resolve(strict=True)
    private_dir(target)
    pending = [(source, name) for name in ['next', 'react', 'react-dom', 'undici',
        '@earendil-works/pi-coding-agent', '@earendil-works/pi-agent-core',
        '@earendil-works/pi-ai', '@earendil-works/pi-tui', '@lydell/node-pty']]
    selected = {}
    while pending:
        context, name = pending.pop()
        candidate = context / name
        if not (candidate / 'package.json').is_file():
            candidate = source / name
        if not (candidate / 'package.json').is_file():
            raise RuntimeError('BACKEND_RUNTIME_DEPENDENCY_MISSING:' + name)
        candidate = candidate.resolve(strict=True)
        if source not in candidate.parents:
            raise RuntimeError('BACKEND_DEPENDENCY_OUTSIDE_SELECTED_ROOT')
        if str(candidate) in selected:
            continue
        metadata = json.loads((candidate / 'package.json').read_text())
        selected[str(candidate)] = metadata.get('version')
        for dep in metadata.get('dependencies', {}):
            search = candidate
            while search != source.parent:
                modules = search / 'node_modules'
                if (modules / dep / 'package.json').is_file():
                    pending.append((modules, dep)); break
                search = search.parent
            else:
                if (source / dep / 'package.json').is_file():
                    pending.append((source, dep))
                else:
                    raise RuntimeError('BACKEND_RUNTIME_DEPENDENCY_MISSING:' + dep)
    # Parent copies already include nested dependencies. Avoid overlapping writes.
    roots = [Path(value) for value in selected]
    for root in sorted(roots):
        if any(parent in roots for parent in root.parents):
            continue
        dest = target / root.relative_to(source)
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        copy_tree(root, dest, source, skipped=('.cache',), inventory=inventory)
    return [{'path': name, 'version': version} for name, version in sorted(selected.items())]


def guarded_import(core, agent, project, source_agent, root, node, inventory):
    guard = module('isolated_candidate_guard', core / 'agent_core/pi_plugin_guard.py')
    request = {'agentDir': str(agent), 'cwd': str(project), 'scopes': ['user', 'project']}
    if agent.exists() or (project / '.pi').exists():
        raise RuntimeError('FRESH_PLUGIN_SCOPE_REQUIRED')
    plan = guard.prepare(request)
    gates = []
    try:
        for row in plan['scopes']:
            fd = os.open(row['gate'], os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            gates.append({'fd': fd, 'path': row['gate'], 'mode': 'exclusive'})
        guard.lock_fds({'files': gates})
        initialized = guard.initialize({**request, 'gates': gates})
        if any(row['legacyUnverified'] for row in initialized['scopes']):
            raise RuntimeError('FRESH_GUARD_INITIALIZATION_FAILED')
        copy_tree(source_agent / 'npm', agent / 'npm', inventory=inventory)
        # Preserve exact native package source/version/filter settings. Only the
        # prior check's npm cache/config paths are relocated into this candidate.
        settings = json.loads((source_agent / 'settings.json').read_text())
        permitted = {'packages', 'npmCommand', 'extensions', 'skills', 'prompts', 'themes', 'trustedProjects', 'projectTrust'}
        if set(settings) - permitted:
            raise RuntimeError('UNREVIEWED_PLUGIN_SETTINGS')
        packages = settings.get('packages')
        if packages != ['npm:pi-web-access@file:' + str(source_agent.parent.parent / 'pi-web-access-0.38.0.tgz')]:
            raise RuntimeError('VERIFIED_PLUGIN_SOURCE_CHANGED')
        settings['npmCommand'] = [str(node), str(node.parent.parent / 'lib/node_modules/npm/bin/npm-cli.js'),
            '--ignore-scripts', '--no-audit', '--no-fund', '--userconfig', str(root / 'empty-user.npmrc'),
            '--globalconfig', str(root / 'empty-global.npmrc'), '--cache', str(root / 'npm-cache')]
        write_json(agent / 'settings.json', settings)
        (agent / 'sessions').mkdir(mode=0o700)
        return {'protocol': 'linux_flock_v1', 'origin': 'born-guarded', 'legacyDirectoryAdopted': False,
                'settingsSourceSha256': digest(source_agent / 'settings.json'), 'guardSha256': digest(core / 'agent_core/pi_plugin_guard.py')}
    finally:
        for row in gates:
            os.close(row['fd'])


def prepare(task, target):
    task, target = task.resolve(strict=True), target.absolute()
    if target.exists() or target.is_symlink():
        raise RuntimeError('CANDIDATE_TARGET_ALREADY_EXISTS_NO_OVERWRITE')
    parent = target.parent.resolve(strict=True)
    if parent != task / 'deployments':
        raise RuntimeError('CANDIDATE_MUST_BE_NEW_DEPLOYMENT')
    core = task / 'releases/biomind-30aeedb2e667e60c'
    backend = task / 'studio/backend/pi-studio'
    service_source = task / 'deployments/server-1.3.0-rc.20261010.1/service-v1'
    verified_path = task / 'plugin-candidates/pi-web-access-20261010/VERIFIED-ISOLATED.json'
    verified = json.loads(verified_path.read_text())
    if verified['result'] != 'PASS' or verified['package'] != 'pi-web-access@0.38.0' or verified['piVersion'] != '0.87.1':
        raise RuntimeError('VERIFIED_PLUGIN_RECEIPT_REQUIRED')
    source_agent = Path(verified['agentDir'])
    plugin_archive = source_agent.parent.parent / 'pi-web-access-0.38.0.tgz'
    if digest(plugin_archive) != PLUGIN_SHA:
        raise RuntimeError('PLUGIN_ARCHIVE_IDENTITY_CHANGED')
    source_lock = source_agent / 'npm/package-lock.json'
    lock_record = next(row for row in verified['evidence'] if row['path'] == str(source_lock))
    if digest(source_lock) != lock_record['sha256']:
        raise RuntimeError('PLUGIN_DEPENDENCY_LOCK_CHANGED')
    plugin_manifest = json.loads((source_agent / 'npm/node_modules/pi-web-access/package.json').read_text())
    if plugin_manifest['version'] != '0.38.0':
        raise RuntimeError('PLUGIN_INSTALLED_VERSION_CHANGED')
    manifest = json.loads((core / 'integrations/pi_runtime/dist/build-manifest.json').read_text())
    if manifest['buildId'] != CORE_ID:
        raise RuntimeError('CORE_IDENTITY_CHANGED')
    native = core / 'integrations/pi_runtime/node_modules/@earendil-works/pi-coding-agent/package.json'
    if json.loads(native.read_text())['version'] != '0.87.1':
        raise RuntimeError('PI_VERSION_CHANGED')
    base = task.parent
    python = Path('/public/home/liusiqi/anaconda3/envs/biomind/bin/python')
    node = base / 'Biomind_v1/integrations/pi_runtime/.runtime/node/bin/node'
    private_dir(target)
    dirs = ['private', 'evidence', 'projects', 'projects/acceptance', 'uploads', 'tmp', 'npm-cache',
            'state', 'state/memory', 'state/wiki', 'xdg', 'xdg/config', 'xdg/cache', 'xdg/state']
    for name in dirs:
        private_dir(target / name)
    inventory = []
    print('Copying fixed backend build and dependencies into a private candidate...', flush=True)
    copy_tree(service_source, target / 'service-v1', inventory=inventory)
    backend_target = target / 'backend'
    private_dir(backend_target)
    # Runtime code/build only. No old uploads, .env, credentials, user state or
    # 2.3 GB webpack cache. Dependencies are physical copies, not source links.
    for name in ['package.json', 'next.config.mjs', 'instrumentation.ts', 'lib', 'public', 'packages']:
        copy_tree(backend / name, backend_target / name, backend, inventory=inventory)
    copy_tree(backend / '.next-pkg', backend_target / '.next-pkg', skipped=('cache', 'trace', 'trace-build', 'diagnostics'), inventory=inventory)
    dependencies = (backend / 'node_modules').resolve(strict=True)
    dependency_packages = runtime_dependencies(dependencies, backend_target / 'node_modules', inventory)
    print('Importing verified native plugin resources under the real guard...', flush=True)
    agent, project = target / 'agent', target / 'projects/acceptance'
    for name in ('empty-user.npmrc', 'empty-global.npmrc'):
        (target / name).write_text(''); (target / name).chmod(0o600)
    guard = guarded_import(core, agent, project, source_agent, target, node, inventory)
    # Copy non-secret domain metadata; no auth/model credential file is read.
    for source, dest in [(base / 'agent_shim/software_registry.json', target / 'registry.json'),
                         (base / 'Biomind_v1/user_preferences.md', target / 'preferences.md')]:
        if not source.is_file():
            raise RuntimeError('DOMAIN_METADATA_SOURCE_MISSING')
        copy_tree(source, dest, source.parent, inventory=inventory)
    (project / 'README.md').write_text('Isolated BioMind Studio acceptance project. Production history and data are not imported.\n')
    runtime = {'python': str(python), 'agentDir': str(agent), 'dataRoot': '.biomind/pi-domain',
        'installRoot': str(core), 'provider': 'deepseek', 'model': 'deepseek-v4-flash', 'thinkingLevel': 'off',
        'registryPath': str(target / 'registry.json'), 'preferencesPath': str(target / 'preferences.md'),
        'memoryRoot': str(target / 'state/memory'), 'wikiRoot': str(target / 'state/wiki'), 'sessionMode': 'persistent'}
    write_json(target / 'runtime.json', runtime)
    setup = {'schema': 'BioMindStudioConfigureV1', 'backendRoot': str(backend_target), 'mainRoot': str(core),
        'runtimeConfig': str(target / 'runtime.json'), 'sessionRoot': str(agent / 'sessions'),
        'pythonPath': str(python), 'nodePath': str(node), 'projectRoots': [str(target / 'projects')],
        'dataRoots': [str(target / 'uploads')], 'uploadsRoot': str(target / 'uploads'),
        'mode': 'local', 'serviceId': 'biomind-isolated-20261010'}
    configure = module('isolated_configure', target / 'service-v1/configure-service.py')
    model_source = base / 'BioMind_Studio_Deployment_20260929/private/model-selection.json'
    configure.model_record_metadata(str(model_source), runtime)
    copy_tree(model_source, target / 'model-selection.json', model_source.parent, inventory=inventory)
    setup['modelRecordPath'] = str(target / 'model-selection.json')
    config_path = target / 'private/service.json'
    configured = configure.configure(config_path, {**setup, 'workbenchPassword': secrets.token_urlsafe(32)})
    config = json.loads(config_path.read_text())
    config['environment'].update({'BIOMIND_DISABLE_DOTENV': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        'PI_CODING_AGENT_DIR': str(agent), 'TMPDIR': str(target / 'tmp'),
        'XDG_CONFIG_HOME': str(target / 'xdg/config'), 'XDG_CACHE_HOME': str(target / 'xdg/cache'),
        'XDG_STATE_HOME': str(target / 'xdg/state'), 'PI_WEB_ACCESS_CACHE_ROOT': str(agent)})
    configure.atomic(config_path, config)
    preset = {'schema': 'BioMindStudioSetupPresetV1', 'profile': {'alias': 'yongjiusuo', 'sshPort': 22,
        'localPort': 30144, 'label': 'BioMind isolated 1.3.0-rc.20261010.1',
        'servicePath': str(target / 'service-v1/server-service.py'), 'serviceConfig': str(config_path)}, 'setup': setup}
    write_json(target / 'setup-preset.json', preset)
    inventory.sort(key=lambda row: row['path'])
    write_json(target / 'evidence/isolation-inventory.json', inventory)
    result = {'schema': 'BioMindIsolatedCandidateV1', 'version': VERSION, 'status': 'PREPARED',
        'root': str(target), 'config': str(config_path), 'runtime': str(target / 'runtime.json'),
        'project': str(project), 'agentDir': str(agent), 'coreRoot': str(core),
        'coreBuildId': CORE_ID, 'coreCommit': manifest['sourceCommit'], 'piVersion': '0.87.1',
        'backendBuildId': (backend_target / '.next-pkg/BUILD_ID').read_text().strip(),
        'serverId': configured['serverId'], 'serviceId': configured['serviceId'],
        'plugin': {'package': 'pi-web-access', 'version': '0.38.0', 'source': str(plugin_archive),
                   'archiveSha256': PLUGIN_SHA, 'verificationReceipt': str(verified_path)},
        'backendRuntimePackages': dependency_packages, 'resourceGuard': guard, 'copiedFiles': len(inventory), 'copiedBytes': sum(row['bytes'] for row in inventory),
        'inventorySha256': digest(target / 'evidence/isolation-inventory.json'),
        'configSha256': digest(config_path), 'runtimeSha256': digest(target / 'runtime.json'),
        'started': False, 'actualHttpIdentityVerified': False, 'credentialsCopied': False,
        'productionChanged': False, 'apiCredentialFilesRead': False,
        'checks': {'physicalIndependentCopies': 'PASS', 'isolatedWritableRoots': 'PASS',
                   'nativeGuardInitialization': 'PASS', 'serviceConfiguration': 'PASS',
                   'httpReady': 'NOT_RUN', 'actualMain': 'NOT_RUN', 'realModel': 'NOT_RUN', 'webSearch': 'NOT_RUN', 'windows': 'NOT_RUN'}}
    write_json(target / 'PREPARED.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--target', type=Path)
    args = parser.parse_args()
    try:
        result = prepare(args.task, args.target or args.task / 'deployments' / VERSION)
    except Exception as error:
        print(json.dumps({'status': 'FAILED', 'code': str(error) if isinstance(error, RuntimeError) else type(error).__name__,
                          'productionChanged': False, 'partialCandidatePreserved': True}), flush=True)
        return 2
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
