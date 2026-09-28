#!/usr/bin/env python3
"""Build offline Pi package candidates from current source/dist; never npm install/publish.

The tarballs are npm's standard package/ layout. Skills have one source tree;
release copying is a snapshot, not a loader/compiler or second method registry.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'integrations/pi_runtime'
FORBIDDEN_PARTS = {'node_modules', '__pycache__', '.git', '.biomind', '.pi', '.agents', 'sessions', 'logs'}
MACHINE_PATH = re.compile(rb'/public/home/|/home/liusiqi/|/Users/[^/\s]+/|[A-Za-z]:\\Users\\')
MAX_RESOURCE_BYTES = 5 * 1024 * 1024


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def write_candidate(base: Path, name: str, manifest: dict, resources: dict[str, bytes]) -> dict:
    target = base / name
    target.mkdir()
    resources['package.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()
    modes = {relative: (0o755 if relative.startswith('skills/') and (ROOT / relative).is_file()
                           and (ROOT / relative).stat().st_mode & 0o111 else 0o644) for relative in resources}
    for relative, data in sorted(resources.items()):
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        destination.chmod(modes[relative])
    archive = base / f'{name}-{manifest["version"]}.tgz'
    with tarfile.open(archive, 'w:gz') as tar:
        for relative, data in sorted(resources.items()):
            info = tarfile.TarInfo('package/' + relative)
            info.size = len(data); info.mode = modes[relative]; info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    # Validate the actual archive bytes, not merely the planned file list.
    with tarfile.open(archive) as tar:
        actual = {member.name.removeprefix('package/'): tar.extractfile(member).read()
                  for member in tar if member.isfile()}
    assert actual == resources
    return {'name': name, 'version': manifest['version'], 'archive': str(archive),
            'sha256': digest(archive.read_bytes()), 'files': [
                {'path': rel, 'bytes': len(data), 'sha256': digest(data), 'mode': oct(modes[rel])} for rel, data in sorted(actual.items())],
            'lifecycle_scripts': manifest.get('scripts', {}), 'publish_executed': False,
            'license_status': 'NOT_CONFIRMED_FOR_PUBLIC_DISTRIBUTION', 'private': True}


def skills_resources() -> dict[str, bytes]:
    result = {}
    root = ROOT / 'skills'
    for source in sorted(root.rglob('*')):
        relative = source.relative_to(root)
        if any(part.startswith('.') or part in FORBIDDEN_PARTS for part in relative.parts):
            continue
        if source.is_symlink():
            raise ValueError(f'Symlink is not a portable release resource: skills/{relative}')
        if not source.is_file():
            continue
        if source.suffix in {'.pyc', '.bam', '.sam', '.fastq', '.fq', '.h5ad', '.rds'}:
            raise ValueError(f'Unexpected scientific data/cache: skills/{relative}')
        if source.stat().st_size > MAX_RESOURCE_BYTES:
            raise ValueError(f'Resource requires explicit release review (>5 MiB): skills/{relative}')
        data = source.read_bytes()
        if MACHINE_PATH.search(data):
            raise ValueError(f'Machine path in portable resource: skills/{relative}')
        if source.suffix == '.py' and re.search(rb'(?m)^\s*(?:from\s+agent_core\b|import\s+agent_core\b)', data):
            raise ValueError(f'Private host import in portable script: skills/{relative}')
        result['skills/' + relative.as_posix()] = data
    if not any(name.endswith('/SKILL.md') for name in result):
        raise ValueError('No native SKILL.md resources found')
    return result


def extension_resources() -> dict[str, bytes]:
    dist = RUNTIME / 'dist'
    pending = ['extensions/biomind.js']; result = {}
    imports = re.compile(r'(?:from\s*|import\s*\()\s*[\'\"](\.[^\'\"]+)[\'\"]')
    while pending:
        relative = pending.pop()
        if 'runtime/' + relative in result:
            continue
        source = (dist / relative).resolve()
        if not source.is_relative_to(dist.resolve()) or source.suffix != '.js':
            raise ValueError(f'Unexpected runtime import: {relative}')
        data = source.read_bytes()
        if MACHINE_PATH.search(data):
            raise ValueError(f'Machine path in extension: {relative}')
        # Source maps are build artifacts with source-tree locations; do not ship
        # a dangling URL or source maps containing a developer checkout path.
        data = re.sub(rb'(?m)^//# sourceMappingURL=.*(?:\n|$)', b'', data)
        result['runtime/' + relative] = data
        for imported in imports.findall(data.decode()):
            target = (source.parent / imported).resolve()
            if not target.is_relative_to(dist.resolve()):
                raise ValueError(f'Import escapes runtime: {relative}: {imported}')
            pending.append(target.relative_to(dist.resolve()).as_posix())
    result['extension.js'] = b'''import path from 'node:path';
import { existsSync } from 'node:fs';
import biomind from './runtime/extensions/biomind.js';
// The optional domain service is the independently installed BioMind service,
// not a second Python implementation shipped inside this package.
export default function extension(pi) {
  const root = process.env.BIOMIND_PI_INSTALL_ROOT;
  if (!root || !path.isAbsolute(root)) throw new Error('BIOMIND_PI_INSTALL_ROOT_REQUIRED');
  if (!process.env.BIOMIND_PI_PYTHON || !path.isAbsolute(process.env.BIOMIND_PI_PYTHON))
    throw new Error('BIOMIND_PI_PYTHON_REQUIRED');
  const service = process.env.BIOMIND_PI_DOMAIN_SERVICE ?? path.join(root, 'integrations/pi_runtime/python_service.py');
  if (!existsSync(service)) throw new Error('BIOMIND_PYTHON_DOMAIN_SERVICE_NOT_INSTALLED');
  process.env.BIOMIND_PI_DOMAIN_SERVICE = service;
  return biomind(pi);
}
'''
    result['README.md'] = '''# BioMind Pi extension — release candidate

Not published. License/attribution approval remains required; private=true prevents npm publication.

Pi package resource: one explicit extension.js entry. Internal runtime helpers are
not extension discovery entries. Skills are separately selectable in the Skills package.
The host provides Pi/typebox peers; this candidate contains no node_modules, Pi core,
Python service copy, local registry, credentials, sessions or user memory.

Requires the independently installed current complete BioMind Python host, its
paired current skills source tree, and existing dependencies. Domain compatibility
readers reuse scientific parsers from that same skills tree; they are not duplicated
in this extension archive. Configure BIOMIND_PI_INSTALL_ROOT and BIOMIND_PI_PYTHON with
absolute local paths, optionally BIOMIND_PI_DOMAIN_SERVICE for the same service entry.
Configure local registry/data through existing BioMind variables, not this manifest.
No install hooks, dependency installation, environment creation or model requests occur
when generating this archive.

Validated API baseline: Pi 0.87.1. Peer * follows Pi's resource loading convention;
it is not a compatibility claim for untested releases. Default observation remains
compat and needs the precisely pinned BioMind observer patch in the host. Explicit
BIOMIND_PI_OBSERVABILITY=public uses official hooks with partial reporting and rejects
strict canary operation; it is not equivalent to the compatibility statistics.
Strict canary execution requires the existing BioMind product launcher/guard; this
standalone Pi resource package does not claim to gate arbitrary Pi model execution.

The product application's runtime lock remains separate and unchanged. Review the
source and attribution before installing/publishing; generation does neither.
'''.encode()
    return result


def source_provenance(records: list[dict]) -> dict:
    sources = {}
    for package in records:
        for item in package['files']:
            name = item['path']
            if name.startswith('skills/'):
                source = ROOT / name
            elif name.startswith('runtime/'):
                source = RUNTIME / 'dist' / name.removeprefix('runtime/')
            else:
                continue  # Generated manifest/README/entry: generator identity below.
            data = source.read_bytes()
            packaged = re.sub(rb'(?m)^//# sourceMappingURL=.*(?:\n|$)', b'', data) if name.startswith('runtime/') else data
            if digest(packaged) != item['sha256']:
                raise ValueError(f'Source changed during packaging: {source.relative_to(ROOT)}')
            sources[source.relative_to(ROOT).as_posix()] = digest(data)
    return {'generator_sha256': digest(Path(__file__).read_bytes()), 'source_inputs_sha256': sources,
            'runtime_transform': 'remove sourceMappingURL comments only; maps are not shipped'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--version', default='0.1.0-candidate.20260928')
    parser.add_argument('--only', choices=['skills', 'extension', 'both'], default='both')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    common = {'version': args.version, 'private': True, 'license': 'UNLICENSED', 'keywords': ['pi-package']}
    records = []
    if args.only in {'skills', 'both'}:
        resources = skills_resources()
        resources['README.md'] = '''# Portable bioinformatics Skills — release candidate

Standard SKILL.md files and their referenced scripts/resources, copied from the sole
current source tree. Pi owns discovery, body loading, /skill and reload. No BioMind
Python host or private tool protocol is required to read or execute the methods;
install the documented scientific dependencies separately with your authorization.
Project inputs/outputs and skill resource directories are distinct.

Not published or installed. No lifecycle scripts. Public redistribution license and
attribution are not yet confirmed; private=true and UNLICENSED are deliberate.
'''.encode()
        records.append(write_candidate(output, 'biomind-bioinformatics-skills', {
            **common, 'name': 'biomind-bioinformatics-skills', 'description': 'Portable standard bioinformatics Skills',
            'files': ['skills', 'README.md'], 'pi': {'skills': ['./skills']}}, resources))
    if args.only in {'extension', 'both'}:
        records.append(write_candidate(output, 'biomind-pi-extension', {
            **common, 'name': 'biomind-pi-extension', 'type': 'module', 'description': 'Optional BioMind domain extension for Pi',
            'engines': {'node': '>=22.19.0'}, 'files': ['extension.js', 'runtime', 'README.md'],
            'peerDependencies': {name: '*' for name in ['@earendil-works/pi-ai', '@earendil-works/pi-agent-core', '@earendil-works/pi-coding-agent', 'typebox']},
            'pi': {'extensions': ['./extension.js']}}, extension_resources()))
    record = {'status': 'RELEASE_CANDIDATES_ONLY', 'source_root': str(ROOT), 'packages': records,
              'installation': 'NOT_RUN', 'publication': 'NOT_RUN', 'dependency_update': 'NOT_RUN'}
    write_json(output / 'PACKAGE_CONTENTS.json', record)
    write_json(output / 'SOURCE_INPUTS.json', source_provenance(records))
    print(json.dumps({'status': record['status'], 'output': str(output), 'packages': [p['name'] for p in records]}))


if __name__ == '__main__':
    main()
