"""Explicit offline export of the new short-preflight candidate wrapper."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_plan_results_fixture as previous

previous.previous.previous.TEST_FILES += (
    'tests/export_preflight_feedback_fixture.py', 'tests/test_preflight_feedback.py',
)
previous.previous.previous.RESOURCE_FILES += (
    'skills/genomics/wgs-short-read-mapping/scripts/inspect_fastq.py',
    'skills/genomics/wgs-short-read-mapping/scripts/inspect_fastq_batch.py',
)

if __name__ == '__main__':
    previous.previous.main()
