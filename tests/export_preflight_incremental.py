"""Export code and named fixtures only; never copy config, sessions or inputs."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_cli_model_progress_fixture as base


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--tree')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    # Existing graph fixtures import one another. This is source code only,
    # not an expansion of the executed test selection.
    base.TEST_FILES += tuple(str(p.relative_to(root)) for p in (root/'tests').glob('*.py'))
    base.RESOURCE_FILES += ('tests/fixtures/preflight_1789734892_candidates.json.gz',
        'tests/fixtures/generate_interaction_1789741049.json.gz',
        'tests/fixtures/context_256k_recovery_1789728208.json.gz',
        'tests/fixtures/preflight_1789734892_receipt.json.gz',
        'skills/genomics/snpeff-annotation/SKILL.md',
        'skills/3d-dna/tad-calling/SKILL.md')
    skill = 'skills/genomics/wgs-short-read-mapping/'
    base.RESOURCE_FILES += tuple(skill + n for n in ['SKILL.md', 'scripts/inspect_wgs_inputs.py',
        'scripts/inspect_fastq.py', 'scripts/inspect_fastq_batch.py', 'scripts/inspect_fasta_header.py'])
    manifest = base.export(root, args.destination, args.tree)
    print(json.dumps({'source':manifest['source'], 'files':len(manifest['files']), 'signature':manifest['source_signature']}))


if __name__ == '__main__':
    main()
