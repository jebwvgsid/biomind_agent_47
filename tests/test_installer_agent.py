from pathlib import Path


def installer_source() -> str:
    return (Path(__file__).resolve().parents[1] / "agent_core" / "installer.py").read_text(encoding="utf-8")


def test_installer_has_generate_turn_and_repeated_observation_guards():
    source = installer_source()

    assert "MAX_INSTALLER_GENERATE_TURNS = 10" in source
    assert "MAX_REPEATED_OBSERVATIONS = 3" in source
    assert "installer exceeded" in source
    assert "installer repeated the same observation" in source
    assert 'return "end_failure"' in source


def test_installer_preserves_code_review_feedback_on_reject():
    source = installer_source()

    assert "User rejected execution. " in source
    assert "Feedback:" in source
    assert "str(final_code or 'No reason provided.')" in source


def test_installer_converts_llm_provider_rejection_to_failure_state():
    source = installer_source()

    assert "llm_failure_summary" in source
    assert "is_llm_input_rejected" in source
    assert "installer prompt payload" in source
    assert "No installation command was executed after this failure." in source


def test_installer_enforces_strict_skill_runtime_contracts():
    source = installer_source()

    assert "_runtime_contract_violation" in source
    assert "STRICT SKILL RUNTIME CONTRACT" in source
    assert "allowed_install_command" in source
    assert "Refusing undeclared install command" in source
    assert "Runtime contract violation" in source
    assert "Refusing a manual `cd`" in source


def test_installer_runs_bash_inside_source_root():
    source = installer_source()

    assert "import shlex" in source
    assert "source_root = str(self.pm.source_root)" in source
    assert "cd {shlex.quote(source_root)}" in source


def test_installer_contract_healthcheck_registers_declared_runtime():
    source = installer_source()

    assert "_verify_and_register_contract_runtime" in source
    assert "_declared_install_like_command" in source
    assert "Declared runtime healthcheck passed" in source
    assert "Environment '{env}' registered successfully via PackageManager" in source
    assert "Strict runtime healthcheck failed after declared install command" in source
