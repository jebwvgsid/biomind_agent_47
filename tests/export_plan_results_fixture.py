"""Safe allowlisted exporter for the Plan/result implementation slices."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_cli_v2_fixture as previous

PLAN_FILES = (
    'tests/export_plan_results_fixture.py',
    'tests/test_plan_revision_handoff.py',
    'tests/test_plan_results_evidence.py',
    'tests/test_plan_result_contract_v1.py',
    'tests/test_plan_results_managed.py',
    'tests/test_plan_results_name.py',
    'tests/test_plan_results_purpose.py',
    'tests/test_runtime_recovery_reporting.py',
)
previous.previous.TEST_FILES += PLAN_FILES
previous.previous.RESOURCE_FILES += (
    'skills/genomics/reseq-variant-workflow/SKILL.md',
    'skills/genomics/wgs-short-read-mapping/SKILL.md',
    'skills/genomics/wgs-gatk-snp-indel/SKILL.md',
)

if __name__ == '__main__':
    previous.main()
