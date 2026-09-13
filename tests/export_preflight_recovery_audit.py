"""Safe current-source export for the session_1789266028 audit; no session copy."""
from pathlib import Path
import argparse
import hashlib
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_cli_deployment_fixture
import export_cli_model_progress_fixture as base
base.TEST_FILES += ('tests/export_preflight_recovery_audit.py',
                    'tests/test_preflight_recovery_audit.py')
base.RESOURCE_FILES += (
    'skills/genomics/wgs-short-read-mapping/SKILL.md',
    'skills/genomics/wgs-short-read-mapping/scripts/inspect_fastq.py',
    'skills/genomics/wgs-short-read-mapping/scripts/inspect_fastq_batch.py',
)
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--tree', default='HEAD')
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    manifest = base.export(repository, args.destination, args.tree)
    # Preserve existing worktree semantics only for the five explicitly scoped
    # safe files. This neither stages nor edits those files in the repository.
    overlay = ['prompts/manager.py', 'agent_core/node_context.py',
               'agent_core/planner_context.py', 'agent_core/provider_message_safety.py',
               'agent_core/workflow/managed_stop.py',
               'tests/export_preflight_recovery_audit.py', 'tests/test_preflight_recovery_audit.py']
    for name in overlay:
        source = repository / name
        assert source.is_file() and not source.is_symlink()
        body = source.read_bytes(); target = args.destination / name
        target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(body)
        manifest['files'][name] = hashlib.sha256(body).hexdigest()
    manifest['worktree_overlay'] = overlay
    manifest['source_signature'] = hashlib.sha256(json.dumps(manifest['files'],sort_keys=True).encode()).hexdigest()
    (args.destination / 'export_manifest.json').write_text(json.dumps(manifest,indent=2))
    print(json.dumps({'tree':manifest['source'], 'overlay':overlay,
                      'source_signature':manifest['source_signature']}))
