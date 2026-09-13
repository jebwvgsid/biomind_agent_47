"""Export current committed safe source + the five preserved overlays + new witnesses."""
from pathlib import Path
import argparse
import hashlib
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_preflight_implementation as previous
base = previous.base
OWNED = ('tests/export_cross_layer_audit.py', 'tests/test_cross_layer_audit.py',
         'tests/test_cross_layer_graph_audit.py')
# Existing safe regression fixture is untracked at the audit baseline. Copy its
# unchanged bytes explicitly; do not present it as committed source or stage it.
EXISTING_TEST_FIXTURES = ('tests/test_step_effect_runtime.py',)
base.TEST_FILES += OWNED + EXISTING_TEST_FIXTURES
base.RESOURCE_FILES += ('skills/genomics/wgs-gatk-snp-indel/SKILL.md',
                        'skills/genomics/snpeff-annotation/SKILL.md',
                        'skills/genomics/reseq-variant-workflow/SKILL.md')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--tree', default='HEAD')
    parser.add_argument('--reuse-export', type=Path, help='Reuse verified allowlisted bytes of the same Git tree; new destination only')
    args=parser.parse_args();repo=Path(__file__).resolve().parents[1]
    if args.reuse_export:
        assert args.destination.resolve().is_relative_to(Path('/tmp')) and args.destination.resolve()!=Path('/tmp')
        manifest=json.loads((args.reuse_export/'export_manifest.json').read_text())
        expected=base.git(repo,'rev-parse',f'{args.tree}^{{tree}}').decode().strip()
        assert manifest['source']==expected
        args.destination.mkdir(parents=True,exist_ok=False)
        for name,digest in manifest['files'].items():
            assert base.allowed(name)
            source=args.reuse_export/name;assert not source.is_symlink()
            body=source.read_bytes();assert hashlib.sha256(body).hexdigest()==digest
            target=args.destination/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(body)
        manifest['reused_verified_export']=str(args.reuse_export)
    else:
        manifest=base.export(repo,args.destination,args.tree)
    for name in (*previous.PROTECTED,*OWNED,*EXISTING_TEST_FIXTURES):
        source=repo/name
        assert source.is_file() and not source.is_symlink()
        body=source.read_bytes();target=args.destination/name
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(body)
        manifest['files'][name]=hashlib.sha256(body).hexdigest()
    manifest['worktree_overlay']=[*previous.PROTECTED,*OWNED,*EXISTING_TEST_FIXTURES]
    manifest['preserved_existing_test_fixtures']=list(EXISTING_TEST_FIXTURES)
    manifest['source_signature']=hashlib.sha256(json.dumps(manifest['files'],sort_keys=True).encode()).hexdigest()
    (args.destination/'export_manifest.json').write_text(json.dumps(manifest,indent=2))
    print(json.dumps({'tree':manifest['source'],'signature':manifest['source_signature']}))
