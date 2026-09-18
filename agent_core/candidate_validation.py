"""Reversible switch for generated-script carrier and helper-call admission.

The current trial defaults these expression checks off. Parsing still needs
one complete executable payload; execution, approval and result checks retain
their own behavior. No environment files are loaded by this module.
"""

import os


CANDIDATE_VALIDATION_ENV = "BIOMIND_CANDIDATE_VALIDATION_ENABLED"


def candidate_validation_enabled() -> bool:
    """Read process configuration, never a model-authored action field."""
    return os.getenv(CANDIDATE_VALIDATION_ENV, "0").strip().lower() not in {
        "", "0", "false", "no", "off",
    }
