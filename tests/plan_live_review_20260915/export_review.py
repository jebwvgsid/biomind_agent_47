"""Safe current-source exporter plus explicit newly owned business fixtures."""
from pathlib import Path
import hashlib
import json
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'tests/a1_unified_fix'))
import export_and_run


def main():
    target = Path(sys.argv[1]).resolve()
    assert target.is_relative_to(Path('/tmp')) and not target.exists()
    selected = [str(p.relative_to(REPO)) for p in Path(__file__).parent.glob('*.py')]
    manifest = export_and_run.export(target, selected)
    manifest['pre_supplement_fixture_signature'] = manifest['source_signature']
    for name in ['live_round_1.txt', 'live_round_2.txt', 'live_round_3.txt', 'live_round_4.txt',
                 'live_public_context.json', 'f6_passed_candidate.json']:
        source = Path(__file__).parent / 'fixtures' / name
        assert source.is_file() and not source.is_symlink()
        relative = str(source.relative_to(REPO))
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        body = source.read_bytes()
        destination.write_bytes(body)
        manifest['files'][relative] = hashlib.sha256(body).hexdigest()
    manifest['source_signature'] = hashlib.sha256(json.dumps(manifest['files'], sort_keys=True).encode()).hexdigest()
    manifest['supplement_fixtures'] = 'Explicit captured public business candidates and public context only; original session/state/scientific inputs excluded'
    (target / 'export_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'source_signature': manifest['source_signature'], 'files': len(manifest['files']), 'export': str(target)}))


if __name__ == '__main__':
    main()
