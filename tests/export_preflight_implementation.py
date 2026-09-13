"""Explicit safe export for the preflight implementation; no original session."""
from pathlib import Path
import argparse
import hashlib
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_preflight_recovery_audit
import export_cli_model_progress_fixture as base
OWNED = (
    'agent_core/inspection_readers.py', 'agent_core/action_runtime.py', 'agent_core/recovery.py',
    'agent_core/workflow/recovery_agent.py', 'execution/preflight.py', 'bio_agents.py',
    'tests/offline_managed_bootstrap.py', 'tests/export_preflight_implementation.py',
    'tests/test_preflight_types.py', 'tests/test_preflight_mapping_commit.py',
    'tests/test_preflight_directory.py', 'tests/test_recovery_public_rejection.py',
    'skills/genomics/wgs-short-read-mapping/SKILL.md',
    'skills/genomics/wgs-short-read-mapping/scripts/inspect_fastq_batch.py',
    'skills/genomics/wgs-short-read-mapping/scripts/inspect_fastq.py',
    'skills/genomics/wgs-short-read-mapping/scripts/inspect_fasta_header.py',
    'skills/genomics/wgs-short-read-mapping/scripts/inspect_wgs_inputs.py',
)
PROTECTED = ('prompts/manager.py', 'agent_core/node_context.py', 'agent_core/planner_context.py',
             'agent_core/provider_message_safety.py', 'agent_core/workflow/managed_stop.py')
base.TEST_FILES += tuple(x for x in OWNED if x.startswith('tests/'))
base.TEST_FILES += ('tests/test_skill_resource_binding.py',)
base.RESOURCE_FILES += tuple(x for x in OWNED if x.startswith('skills/'))
if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('destination', type=Path); p.add_argument('--tree', default='HEAD')
    p.add_argument('--committed',action='store_true',help='Only the protected five overlay; all owned files from the explicit Git tree')
    args = p.parse_args(); repo = Path(__file__).resolve().parents[1]
    manifest = base.export(repo, args.destination, args.tree)
    names = [x for x in (PROTECTED if args.committed else (*PROTECTED, *OWNED)) if (repo/x).is_file()]
    for name in names:
        source = repo/name; assert not source.is_symlink()
        body=source.read_bytes(); target=args.destination/name
        target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(body)
        manifest['files'][name] = hashlib.sha256(body).hexdigest()
    manifest['worktree_overlay']=names
    manifest['source_signature']=hashlib.sha256(json.dumps(manifest['files'],sort_keys=True).encode()).hexdigest()
    (args.destination/'export_manifest.json').write_text(json.dumps(manifest,indent=2))
    print(json.dumps({'tree':manifest['source'],'signature':manifest['source_signature']}))
