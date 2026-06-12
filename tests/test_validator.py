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


if __name__ == "__main__":
    unittest.main()
