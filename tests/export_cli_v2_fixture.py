"""Allowlisted CLI V2 export, with an explicit test-only baseline overlay."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_cli_model_progress_fixture as previous

V2_FILES = (
    'tests/export_cli_v2_fixture.py', 'tests/cli_v2_support.py',
    'tests/test_cli_v2_integration.py', 'tests/replay_cli_v2_pty.py',
    'tests/test_cli_v2_display.py', 'tests/test_cli_v2_output.py','tests/test_cli_v2_business.py', 'tests/test_cli_v2_recovery_route.py', 'tests/test_cli_v2_public_style.py',
)
previous.TEST_FILES += V2_FILES

def export(repository, destination, tree=None, baseline_overlay=False):
    manifest = previous.export(repository, destination, tree)
    if baseline_overlay:
        overlay = {}
        for name in V2_FILES:
            if name not in manifest['files'] and (repository/name).is_file():
                data=(repository/name).read_bytes()
                target=destination/name; target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
                overlay[name]=hashlib.sha256(data).hexdigest()
        manifest['baseline_test_overlay']=overlay
        manifest['files'].update(overlay)
        manifest['source_signature']=hashlib.sha256(json.dumps(manifest['files'],sort_keys=True).encode()).hexdigest()
        (destination/'export_manifest.json').write_text(json.dumps(manifest,indent=2))
    return manifest

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination',type=Path)
    parser.add_argument('--tree')
    parser.add_argument('--baseline-overlay',action='store_true')
    parser.add_argument('--repository',type=Path,default=Path(__file__).resolve().parents[1])
    args=parser.parse_args()
    value=export(args.repository.resolve(),args.destination.resolve(),args.tree,args.baseline_overlay)
    print(json.dumps({'source':value['source'],'signature':value['source_signature'],'files':len(value['files'])}))
if __name__=='__main__':main()
