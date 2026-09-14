"""Export this implementation's current source, with explicitly listed fixtures."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_plan_resource_cycle_audit as audit

FIX_TESTS = ('tests/test_plan_resource_fix.py', 'tests/plan_resource_fix_support.py', 'tests/export_plan_resource_fix.py', 'tests/test_plan_stop_fix.py', 'tests/test_plan_cancel_fix.py', 'tests/test_plan_lifecycle_scope_fix.py', 'tests/test_plan_transport_fix.py', 'tests/test_plan_epoch_reload_fix.py')
audit.base.TEST_FILES += FIX_TESTS + ('tests/test_planner_full_replan_convergence_v2.py', 'tests/test_planner_feedback_boundary.py', 'tests/test_planner_lifecycle_boundary.py', 'tests/test_input_boundary.py', 'tests/test_approval_owner_lifecycle.py')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--tree', help='Optional explicit Git tree; no historical default')
    parser.add_argument('--overlay', action='append', default=[])
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    manifest = (audit.previous.export(repo, args.destination, args.tree, args.overlay)
                if args.tree else audit.base.export(repo, args.destination))
    manifest['worktree_base_head'] = subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    if not args.tree:
        manifest['production_source_mode'] = 'exact_worktree'
        protected = ('prompts/manager.py','agent_core/node_context.py','agent_core/planner_context.py',
                     'agent_core/provider_message_safety.py','agent_core/workflow/managed_stop.py')
        manifest['protected_overlays'] = {}
        for name in protected:
            committed = subprocess.check_output(['git','show',manifest['worktree_base_head']+':'+name],cwd=repo)
            manifest['protected_overlays'][name] = {'committed_sha256':hashlib.sha256(committed).hexdigest(),
                'loaded_sha256':manifest['files'][name], 'source':'preserved worktree overlay'}

    prefix = 'docs/audits/session_1789369423_plan_resource_cycle/'
    for name in [prefix + 'candidate_C_rejected.json', prefix + 'task_declarations.json',
                 *[prefix + f'candidate_{i:02d}.json' for i in range(1, 8)]]:
        body = (repo/name).read_bytes()
        target = args.destination/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        manifest.setdefault('audit_fixtures', {})[name] = hashlib.sha256(body).hexdigest()
    (args.destination/'export_manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(json.dumps({'source': manifest['source'], 'source_signature': manifest['source_signature']}))

if __name__ == '__main__':
    main()
