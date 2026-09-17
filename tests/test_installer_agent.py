from pathlib import Path
import unittest

from agent_core.installer import InstallerAgent
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


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


def test_installer_registration_is_parent_owned_after_target_verification():
    source = installer_source()

    assert "registration_requests" in source
    assert "parent_target_verifier" in source
    assert "verification_required" in source
    assert "INSTALLATION_REGISTRATION_TARGET_MISMATCH" in source
    assert "self.pm.register_environment(" not in source
    assert "self.pm.register_existing_tool(" not in source
    assert "self.pm.bind_tool_to_environment(" not in source


def test_installer_uses_step_local_context_after_initial_prompt():
    source = installer_source()

    assert "is_init = not any(isinstance(message, AIMessage) for message in messages)" in source
    assert "def _step_local_messages(" in source
    assert "step_context = self._step_local_messages(messages)" in source
    assert "LLM_STEP_CONTEXT_CHARS" in source
    assert "step context clipped" in source


class InstallerStepContextTests(unittest.TestCase):
    def test_step_context_keeps_work_order_and_latest_evidence(self):
        messages = [
            HumanMessage(content="original install work order"),
            SystemMessage(content="stable installer instructions"),
            AIMessage(content="old install command"),
            HumanMessage(content="<observation>old failure</observation>"),
            AIMessage(content="current install command"),
            HumanMessage(content="<observation>current result</observation>"),
        ]

        visible = InstallerAgent._step_local_messages(messages, max_messages=6, max_chars=4000)
        text = "\n".join(str(message.content) for message in visible)

        self.assertIn("original install work order", text)
        self.assertIn("current install command", text)
        self.assertIn("current result", text)
        self.assertNotIn("old install command", text)
        self.assertLessEqual(len(visible), 6)
        self.assertLessEqual(sum(len(str(message.content)) for message in visible), 4000)
