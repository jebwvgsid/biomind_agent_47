import unittest
import sys
import types

try:
    import langchain_core.messages  # type: ignore  # noqa: F401
except (ImportError, ModuleNotFoundError):
    langchain_core = types.ModuleType("langchain_core")
    messages_module = types.ModuleType("langchain_core.messages")

    class HumanMessage:
        def __init__(self, content: str = ""):
            self.content = content

    messages_module.HumanMessage = HumanMessage
    sys.modules.setdefault("langchain_core", langchain_core)
    sys.modules["langchain_core.messages"] = messages_module

from execution.validator import ResultValidator


class ResultValidatorRuntimeTests(unittest.TestCase):
    def test_swallowed_availability_failure_is_critical(self):
        validator = ResultValidator()
        code = (
            "conda run -n hicexplore Rscript -e \"library(CALDER)\" "
            "2>/dev/null && echo OK || echo 'CALDER not available'"
        )

        result = validator.validate("CALDER not available\n", "", code, 0)

        self.assertFalse(result["passed"])
        self.assertTrue(
            any("Swallowed Availability Failure" in item for item in result["critical_failures"])
        )

    def test_data_qc_echo_fail_is_not_availability_failure(self):
        validator = ResultValidator()
        code = (
            "conda run -n bio_wgs samtools quickcheck -v sample.sort.markdup.bam "
            "&& echo PASS || echo FAIL\n"
            "conda run -n bio_wgs samtools depth sample.sort.markdup.bam > sample.coverage.txt\n"
            "wc -l sample.coverage.txt"
        )
        stdout = "PASS\n54447 sample.coverage.txt\n"

        result = validator.validate(stdout, "", code, 0)

        self.assertTrue(result["passed"], result)
        self.assertFalse(
            any("Swallowed Availability Failure" in item for item in result["critical_failures"])
        )

    def test_swallowed_tool_version_failure_is_still_critical(self):
        validator = ResultValidator()
        code = "gatk --version 2>/dev/null && echo OK || echo 'gatk not found'"

        result = validator.validate("gatk not found\n", "", code, 0)

        self.assertFalse(result["passed"])
        self.assertTrue(
            any("Swallowed Availability Failure" in item for item in result["critical_failures"])
        )

    def test_biomind_shim_error_is_critical(self):
        validator = ResultValidator()

        result = validator.validate(
            "",
            "BioMind shim error: failed to activate conda env 'bio_cooler' for tool 'cooler'.",
            "cooler info sample.mcool::40000",
            1,
        )

        self.assertFalse(result["passed"])
        self.assertTrue(any("biomind shim error" in item.lower() for item in result["critical_failures"]))

    def test_masked_tool_failure_is_critical_even_with_exit_zero(self):
        validator = ResultValidator()
        stdout = (
            "A USER ERROR has occurred: sample-name-map is not a recognized option\n"
            "ERROR conda.cli.main_run:execute(142): `conda run ...` failed.\n"
        )

        result = validator.validate(
            stdout,
            "",
            "gatk CombineGVCFs 2>&1 | grep -iE 'error|exception' | head -20",
            0,
        )

        self.assertFalse(result["passed"])
        self.assertTrue(
            any("Masked Tool Failure" in item for item in result["critical_failures"])
        )

    def test_missing_conda_environment_cannot_pass_with_exit_zero(self):
        validator = ResultValidator()

        result = validator.validate(
            "",
            "EnvironmentNameNotFound: Could not find conda environment: missing_env",
            "conda run -n missing_env samtools --version",
            0,
        )

        self.assertFalse(result["passed"])
        self.assertTrue(any("environmentnamenotfound" in item.lower() for item in result["critical_failures"]))

    def test_shell_unbound_variable_is_critical(self):
        validator = ResultValidator()

        result = validator.validate(
            "",
            "activate-binutils.sh: line 68: ADDR2LINE: unbound variable",
            "conda activate bio_gatk",
            1,
        )

        self.assertFalse(result["passed"])
        self.assertTrue(any("unbound variable" in item.lower() for item in result["critical_failures"]))

    def test_readonly_preview_pipe_nonzero_with_stdout_is_warning_not_failure(self):
        validator = ResultValidator()
        code = (
            "ls -lh genome.fasta ont1.fq.gz && echo '---' && "
            "head -c 500 genome.fasta && echo '' && echo '---' && "
            "zcat ont1.fq.gz 2>/dev/null | head -8"
        )
        stdout = (
            "-rw-r--r-- 1 user group 16M genome.fasta\n"
            "-rw-r--r-- 1 user group 199M ont1.fq.gz\n"
            "---\n>chr1\nACGT\n---\n@read1\nACGT\n+\n!!!!\n"
        )

        result = validator.validate(stdout, "", code, 141)

        self.assertTrue(result["passed"], result)
        self.assertTrue(result["benign_nonzero_readonly_preview"])
        self.assertTrue(any("Read-only preview" in item for item in result["warnings"]))
        self.assertFalse(result["critical_failures"])

    def test_analysis_tool_nonzero_with_stdout_is_not_benign_preview(self):
        validator = ResultValidator()

        result = validator.validate(
            "some aligner banner\n",
            "",
            "minimap2 -ax map-ont ref.fa reads.fq.gz | head -20",
            141,
        )

        self.assertFalse(result["passed"])
        self.assertFalse(result["benign_nonzero_readonly_preview"])
        self.assertTrue(any("error code" in item for item in result["critical_failures"]))

    def test_readonly_preview_with_hard_error_signal_still_fails(self):
        validator = ResultValidator()

        result = validator.validate(
            "A USER ERROR has occurred: bad argument\n",
            "",
            "cat tool.log | head -20",
            141,
        )

        self.assertFalse(result["passed"])
        self.assertTrue(any("Masked Tool Failure" in item for item in result["critical_failures"]))


if __name__ == "__main__":
    unittest.main()
