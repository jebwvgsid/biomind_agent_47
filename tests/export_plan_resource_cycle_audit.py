"""Explicit safe worktree/Git export; no application/config/session imports."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_cross_layer_implementation as previous
base = previous.base
AUDIT = 'docs/audits/session_1789369423_plan_resource_cycle/'
OWN = ('tests/test_plan_resource_cycle_audit.py',)
base.TEST_FILES += OWN + ('tests/test_planner_resource_protocol_v2.py', 'tests/test_planner_agent_loop.py',)
base.ROOT_FILES += ('skill_manager.py',)
base.RESOURCE_FILES += ('skills/genomics/snpeff-annotation/SKILL.md', 'skills/metagenomics/kraken2/SKILL.md')

def main():
    p=argparse.ArgumentParser(); p.add_argument('destination', type=Path); p.add_argument('--tree'); p.add_argument('--overlay',action='append',default=[]); a=p.parse_args()
    repo=Path(__file__).resolve().parents[1]
    manifest=previous.export(repo,a.destination,a.tree,a.overlay) if a.tree else base.export(repo,a.destination)
    # Test fixtures and this audit's formal candidate projections are explicitly separate.
    import hashlib
    fixture_names = list(OWN) + ['tests/test_step_effect_runtime.py']
    fixture_names += [AUDIT+f'candidate_{i:02d}.json' for i in range(1,8)] + [AUDIT+'task_declarations.json']
    for name in fixture_names:
        body=(repo/name).read_bytes(); target=a.destination/name; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(body)
        manifest.setdefault('audit_fixtures',{})[name]=hashlib.sha256(body).hexdigest()
    (a.destination/'export_manifest.json').write_text(json.dumps(manifest,indent=2))
    print(json.dumps({'source':manifest['source'],'signature':manifest['source_signature'],'files':len(manifest['files'])}))

if __name__=='__main__':main()
