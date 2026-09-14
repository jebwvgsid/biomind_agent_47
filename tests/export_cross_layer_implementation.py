"""Export an explicit candidate Git tree, optionally with registered overlays.

No fixed historical tree and no implicit production worktree overlay. Existing
safe test fixtures remain separately identified; they are not deployment code.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_cross_layer_audit as previous
base = previous.base
NEW_TESTS = ('tests/export_cross_layer_implementation.py',
    'tests/test_cross_layer_publication.py', 'tests/test_cross_layer_implementation_graph.py',
    'tests/test_cross_layer_correction.py', 'tests/test_cross_layer_delivery.py')
EXISTING_FIXTURES = ('tests/test_step_effect_runtime.py', 'tests/test_semantic_completion.py',
    'tests/test_semantic_completion_workflow.py', 'tests/test_inspection_terminal_workflow.py')
base.TEST_FILES += NEW_TESTS + EXISTING_FIXTURES


def export(repo, destination, tree, overlays=()):
    destination = destination.resolve()
    assert destination.is_relative_to(Path('/tmp')) and destination != Path('/tmp')
    resolved = base.git(repo, 'rev-parse', '--verify', tree+'^{tree}').decode().strip()
    entries = base.git(repo, 'ls-tree', '-r', '-z', '--full-tree', resolved).split(b'\0')
    names=[]
    for entry in entries:
        if not entry: continue
        info,raw=entry.split(b'\t',1); name=raw.decode()
        if base.allowed(name):
            assert info.split()[0] in (b'100644',b'100755'),name
            names.append(name)
    destination.mkdir(parents=True,exist_ok=False)
    archive=base.git(repo,'archive','--format=tar',resolved,'--',*names)
    files={}
    with tarfile.open(fileobj=io.BytesIO(archive),mode='r:') as stream:
        for member in stream:
            if member.isdir():continue
            assert member.isfile() and member.name in names
            body=stream.extractfile(member).read();target=destination/member.name
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(body)
            files[member.name]=hashlib.sha256(body).hexdigest()
    overlay_receipts={}
    for name in overlays:
        assert name in previous.previous.PROTECTED, name
        path=repo/name;assert path.is_file() and not path.is_symlink()
        body=path.read_bytes();overlay_receipts[name]={'committed_sha256':files[name],
            'loaded_sha256':hashlib.sha256(body).hexdigest(),'source':'explicit protected worktree overlay'}
        (destination/name).write_bytes(body);files[name]=hashlib.sha256(body).hexdigest()
    fixtures={}
    for name in EXISTING_FIXTURES:
        path=repo/name
        if name in files or not path.is_file():continue
        assert not path.is_symlink()
        body=path.read_bytes();target=destination/name
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(body)
        files[name]=hashlib.sha256(body).hexdigest();fixtures[name]=files[name]
    manifest={'source':resolved,'requested_tree':tree,'files':files,
        'source_signature':hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest(),
        'protected_overlays':overlay_receipts,'preserved_test_fixtures':fixtures,
        'production_source_mode':'candidate_tree_plus_explicit_overlay' if overlays else 'bare_candidate_tree',
        'excluded':['config','startup','credentials','sessions','databases','formal data']}
    (destination/'export_manifest.json').write_text(json.dumps(manifest,indent=2))
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination',type=Path)
    parser.add_argument('--tree',required=True,help='Exact candidate commit/tree; no historical default')
    parser.add_argument('--overlay',action='append',default=[],choices=previous.previous.PROTECTED)
    args=parser.parse_args()
    manifest=export(Path(__file__).resolve().parents[1],args.destination,args.tree,args.overlay)
    print(json.dumps({k:manifest[k] for k in ('source','source_signature','production_source_mode')}))
