"""Implementation allowlisted current-byte export; never imports production in the host.

Run with the known BioMind interpreter. Config, credentials, old sessions and
scientific data are excluded. Existing guarded bootstrap supplies inert config.
New test process receipts, rather than disk HEAD, identify what was loaded.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'tests'))
import export_cross_layer_implementation as prior

PYTHON = '/public/home/liusiqi/anaconda3/envs/biomind/bin/python'
OWN = 'tests/a1_unified_fix/'
DOC = 'docs/implementation/a1_unified_fix_20260914/'
SAFE_DIRS = ('agent_core', 'execution', 'memory', 'prompts')

def digest(body):
    return hashlib.sha256(body).hexdigest()

def git(*args):
    return prior.base.git(REPO, *args)

def protected_snapshot():
    status = git('status', '--porcelain=v1', '-z', '--untracked-files=all')
    paths = git('ls-files', '-m', '-o', '--exclude-standard', '-z').decode().split('\0')
    safe_hashes = {}
    for name in paths:
        p = Path(name)
        if not name or name.startswith((OWN, DOC)):
            continue
        # Metadata records all other changes; content reads only safe source/tests.
        if p.suffix == '.py' and p.parts[0] in (*SAFE_DIRS, 'tests'):
            source = REPO / p
            if source.is_file() and not source.is_symlink():
                safe_hashes[name] = digest(source.read_bytes())
    return {'head': git('rev-parse', 'HEAD').decode().strip(),
            'tree': git('rev-parse', 'HEAD^{tree}').decode().strip(),
            'branch': git('branch', '--show-current').decode().strip(),
            'status_records': status.decode().split('\0'),
            'index_entries_sha256': digest(git('ls-files', '--stage', '-z')),
            'protected_safe_hashes': safe_hashes,
            'sensitive_config_content': 'NOT_READ; metadata only',
            'interpreter': PYTHON,
            'interpreter_sha256': digest(Path(PYTHON).read_bytes())}

def export(destination, tests):
    destination = destination.resolve()
    assert destination.is_relative_to(Path('/tmp')) and destination != Path('/tmp')
    assert not destination.exists(), 'new export only'
    extra = set(tests)
    extra.update(str(p.relative_to(REPO)) for p in (REPO / OWN).glob('*.py'))
    for name in extra:
        assert name.startswith('tests/') and name.endswith('.py') and '..' not in Path(name).parts
    prior.base.TEST_FILES += tuple(extra)
    # Public Skill declarations/references/scripts are safe source, not user data.
    resources = [str(p.relative_to(REPO)) for p in (REPO / 'skills').rglob('*')
                 if p.is_file() and not p.is_symlink()
                 and (p.suffix in ('.md', '.py')
                      or ('scripts' in p.parts and p.suffix in ('.R', '.sh'))
                      or str(p.relative_to(REPO)) == 'skills/scrna/ingest/data-download/references/example_manifest.yaml')
                 and not any(x.startswith('.') for x in p.relative_to(REPO / 'skills').parts)]
    prior.base.RESOURCE_FILES += tuple(resources)
    snapshot = protected_snapshot()
    manifest = prior.base.export(REPO, destination)
    for name in extra:
        assert name in manifest['files'], 'requested source missing: ' + name
    manifest['baseline'] = snapshot
    manifest['production_source_mode'] = 'current_safe_worktree_bytes_with_protected_overlay'
    manifest['source_head_is_not_loaded_revision'] = True
    manifest['historical_loaded_revision'] = 'UNKNOWN'
    manifest['fixtures'] = {name: {'sha256': sha, 'tracked': bool(git('ls-files', '--', name))}
                            for name, sha in manifest['files'].items() if name.startswith('tests/')}
    manifest['overlay'] = {}
    for name, sha in manifest['files'].items():
        if name.startswith('tests/'):
            continue
        try:
            old = digest(git('show', snapshot['head'] + ':' + name))
        except subprocess.CalledProcessError:
            old = None
        if old != sha:
            manifest['overlay'][name] = {'head_sha256': old, 'export_sha256': sha}
    # Existing loaded-byte regression consumes this spelling. Keep the same
    # exact differences; do not relabel a dirty export as a bare commit.
    manifest['protected_overlays'] = manifest['overlay']
    # Generated runtime cache is inventoried separately; tests build their own registry.
    registry = REPO / 'skills/.skill_runtime_registry.json'
    if registry.is_file() and not registry.is_symlink():
        manifest['disk_registry'] = {'path': str(registry), 'sha256': digest(registry.read_bytes()),
                                     'load_claim': 'NOT_EXPORTED; new registry from exported Skill bytes'}
    (destination / 'export_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return manifest

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path, help='new /tmp run root')
    parser.add_argument('--test', action='append', default=[], help='existing safe Python test dependency')
    parser.add_argument('--run', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    assert args.root.resolve().is_relative_to(Path('/tmp')) and not args.root.exists()
    manifest = export(args.root / 'export', args.test)
    print(json.dumps({'head': manifest['baseline']['head'], 'signature': manifest['source_signature'],
                      'files': len(manifest['files']), 'root': str(args.root)}), flush=True)
    if args.run:
        from cli_model_progress_support import launch
        raise SystemExit(launch(args.root / 'export', args.root / 'results', args.run, installed_cli=True))

if __name__ == '__main__':
    main()
