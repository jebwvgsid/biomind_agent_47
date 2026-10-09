"""The opt-out bypasses static invocation restrictions without rewriting code."""

from copy import deepcopy
import sys

import pytest

from agent_core.skill_access import SkillAccessError
from agent_core.skill_script_invocation import (
    build_skill_script_argv,
    validate_skill_script_invocations,
)


HELPER = "/registered/skill/scripts/helper.py"


def _resource():
    return {
        "handle": "testing/helper#scripts/helper.py",
        "skill_id": "testing/helper",
        "canonical_path": HELPER,
        "snapshot_id": "selected-snapshot",
        "resource_snapshot_id": "selected-resource-snapshot",
        "invocation": {
            "interpreter": sys.executable,
            "argv_prefix": [sys.executable, HELPER],
            "arguments": {"--source": {"nargs": 1, "required": True}},
            "delegates": [],
        },
    }


@pytest.mark.parametrize("action, expected_code", [
    ({"type": "execute", "language": "bash", "code":
      f'SOURCE="/data/ref.fa"\n"{sys.executable}" "{HELPER}" --source "$SOURCE"\n'},
     "SKILL_INVOCATION_UNVERIFIABLE"),
    ({"type": "execute", "language": "bash", "code":
      f'if [ -r /data/ref.fa ]; then\n"{sys.executable}" "{HELPER}" --source /data/ref.fa\nfi\n'},
     "SKILL_INVOCATION_UNVERIFIABLE"),
    ({"type": "execute", "argv": ["/different/python", HELPER, "--source", "/data/ref.fa"]},
     "SKILL_INVOCATION_INTERPRETER_REQUIRED"),
    ({"type": "execute", "argv": [sys.executable, HELPER, "--undeclared", "/data/ref.fa"]},
     "SKILL_INVOCATION_ARGUMENT_INVALID"),
])
def test_explicit_opt_out_preserves_otherwise_rejected_candidate(action, expected_code):
    resources = [_resource()]
    original_action, original_resources = deepcopy(action), deepcopy(resources)

    # Existing direct callers, and an explicit re-enable, keep the old contract.
    for kwargs in ({}, {"enforce": True}):
        with pytest.raises(SkillAccessError) as caught:
            validate_skill_script_invocations(action, resources, **kwargs)
        assert caught.value.code == expected_code

    assert validate_skill_script_invocations(action, resources, enforce=False) is None
    assert action == original_action
    assert resources == original_resources


def test_opt_out_does_not_change_explicit_argv_builder_contract():
    resource = _resource()
    assert build_skill_script_argv(resource, ["--source", "/data/ref.fa"]) == [
        sys.executable, HELPER, "--source", "/data/ref.fa",
    ]
    with pytest.raises(SkillAccessError) as caught:
        build_skill_script_argv(resource, ["--undeclared", "/data/ref.fa"])
    assert caught.value.code == "SKILL_INVOCATION_ARGUMENT_INVALID"
