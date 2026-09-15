"""Explicit pytest adapter for the old resource suite's synthetic report sink.

Activate only with -p tests.plan_authority_v2_20260915.resource_artifact_plugin.
No production behavior or assertion is replaced; the existing guard confines IO
to the same newly created result directory used by the current test launcher.
"""
import os
from pathlib import Path


def pytest_configure(config):
    destination = Path(os.environ["BIOMIND_GER_ARTIFACTS"]).resolve()
    assert destination.is_relative_to(Path("/tmp")) and destination.is_dir()
    os.environ["BIOMIND_RESOURCE_AUDIT_RESULTS"] = str(destination)
