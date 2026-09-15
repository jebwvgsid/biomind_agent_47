"""Reuse the established offline exporter; optional immutable BASE comparison."""
from pathlib import Path
import argparse
import hashlib
import json
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'tests/a1_unified_fix'))
import export_and_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--base-export', type=Path)
    args = parser.parse_args()
    target = args.destination.resolve()
    assert target.is_relative_to(Path('/tmp')) and not target.exists()
    selected = [str(p.relative_to(REPO)) for p in Path(__file__).parent.glob('*.py')]
    selected += [
        'tests/test_planner_resource_protocol_v2.py',
        'tests/test_plan_cancel_fix.py',
        'tests/test_planner_wgs_skill_contracts.py',
    ]
    manifest = export_and_run.export(target, selected)
    if args.base_export:
        source = args.base_export.resolve()
        assert source.is_relative_to(Path('/tmp'))
        old = json.loads((source / 'export_manifest.json').read_text())
        for name in list(manifest['files']):
            if name.startswith('tests/'):
                continue
            assert name in old['files'], 'New production file has no BASE source: ' + name
            body = (source / name).read_bytes()
            assert hashlib.sha256(body).hexdigest() == old['files'][name]
            (target / name).write_bytes(body)
            manifest['files'][name] = old['files'][name]
        manifest['production_source_mode'] = 'immutable_base_safe_snapshot_with_new_test_only_targets'
        manifest['base_production_signature'] = old['source_signature']
    # These are already captured PUBLIC business JSON, not old session state or
    # scientific inputs. They serve optional old/new representation comparisons.
    fixture = REPO / 'tests/plan_live_review_20260915/fixtures'
    for name in ('live_round_1.txt', 'live_round_2.txt', 'live_round_3.txt', 'live_round_4.txt',
                 'live_public_context.json', 'f6_passed_candidate.json'):
        source = fixture / name
        assert source.is_file() and not source.is_symlink()
        relative = str(source.relative_to(REPO))
        dest = target / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        body = source.read_bytes()
        dest.write_bytes(body)
        manifest['files'][relative] = hashlib.sha256(body).hexdigest()
    manifest['source_signature'] = hashlib.sha256(json.dumps(manifest['files'], sort_keys=True).encode()).hexdigest()
    (target / 'export_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'signature': manifest['source_signature'], 'files': len(manifest['files']), 'export': str(target),
                     'source_mode': manifest['production_source_mode']}))


if __name__ == '__main__':
    main()
