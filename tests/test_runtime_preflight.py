import tempfile
import unittest
import sys
import types
from pathlib import Path
from types import SimpleNamespace

try:
    import langgraph.graph  # type: ignore  # noqa: F401
except (ImportError, ModuleNotFoundError):
    langgraph_module = types.ModuleType("langgraph")
    graph_module = types.ModuleType("langgraph.graph")
    graph_message_module = types.ModuleType("langgraph.graph.message")
    checkpoint_module = types.ModuleType("langgraph.checkpoint")
    checkpoint_memory_module = types.ModuleType("langgraph.checkpoint.memory")
    types_module = types.ModuleType("langgraph.types")

    class StateGraph:
        pass

    class MemorySaver:
        pass

    class Command:
        pass

    def interrupt(payload):
        return payload

    def add_messages(left, right):
        return list(left or []) + list(right or [])

    graph_module.StateGraph = StateGraph
    graph_module.START = "START"
    graph_module.END = "END"
    graph_message_module.add_messages = add_messages
    checkpoint_memory_module.MemorySaver = MemorySaver
    types_module.interrupt = interrupt
    types_module.Command = Command
    sys.modules.setdefault("langgraph", langgraph_module)
    sys.modules["langgraph.graph"] = graph_module
    sys.modules["langgraph.graph.message"] = graph_message_module
    sys.modules["langgraph.checkpoint"] = checkpoint_module
    sys.modules["langgraph.checkpoint.memory"] = checkpoint_memory_module
    sys.modules["langgraph.types"] = types_module

try:
    import langchain_core.messages  # type: ignore  # noqa: F401
except (ImportError, ModuleNotFoundError):
    langchain_core = types.ModuleType("langchain_core")
    messages_module = types.ModuleType("langchain_core.messages")
    callbacks_module = types.ModuleType("langchain_core.callbacks")

    class BaseMessage:
        def __init__(self, content: str = ""):
            self.content = content

    class AIMessage(BaseMessage):
        pass

    class HumanMessage(BaseMessage):
        pass

    class SystemMessage(BaseMessage):
        pass

    class BaseCallbackHandler:
        pass

    messages_module.AIMessage = AIMessage
    messages_module.HumanMessage = HumanMessage
    messages_module.SystemMessage = SystemMessage
    messages_module.BaseMessage = BaseMessage
    callbacks_module.BaseCallbackHandler = BaseCallbackHandler
    sys.modules.setdefault("langchain_core", langchain_core)
    sys.modules["langchain_core.messages"] = messages_module
    sys.modules["langchain_core.callbacks"] = callbacks_module

if "langchain_core.callbacks" not in sys.modules:
    callbacks_module = types.ModuleType("langchain_core.callbacks")

    class BaseCallbackHandler:
        pass

    callbacks_module.BaseCallbackHandler = BaseCallbackHandler
    sys.modules["langchain_core.callbacks"] = callbacks_module

try:
    import langchain.chat_models  # type: ignore  # noqa: F401
except (ImportError, ModuleNotFoundError):
    langchain_module = types.ModuleType("langchain")
    chat_models_module = types.ModuleType("langchain.chat_models")

    def init_chat_model(*args, **kwargs):
        return object()

    chat_models_module.init_chat_model = init_chat_model
    sys.modules.setdefault("langchain", langchain_module)
    sys.modules["langchain.chat_models"] = chat_models_module

try:
    from bio_agents import BioAnalystAgent
except Exception as exc:  # pragma: no cover - optional integration deps may be absent.
    BioAnalystAgent = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None

from agent_core.plan_tracker import build_plan_state, save_plan_state
from langchain_core.messages import AIMessage


class _FakeSkillManager:
    def __init__(self):
        self.records = []
        self.skills_root = Path(".")

    def get_runtime_requirements(self, skill_id):
        if skill_id == "3d-dna/compartment-calling":
            return {
                "skill_id": skill_id,
                "env": "compartment",
                "default_env": "compartment",
                "env_runner": "conda run -n compartment python",
                "language": "python",
                "packages": ["cooler", "cooltools", "pandas", "numpy"],
                "capabilities": ["ab-compartment", "pc1-eigenvector"],
                "install_tool": "compartment_pc1_runtime",
                "install_command": "conda create -n compartment -c conda-forge -c bioconda python=3.10 cooler cooltools pandas numpy scipy h5py bedtools -y",
                "install_commands": [
                    "conda create -n compartment -c conda-forge -c bioconda python=3.10 cooler cooltools pandas numpy scipy h5py bedtools -y",
                ],
                "install_fallbacks": [],
                "install_policy": "prefer-declared",
                "env_file": "",
                "healthcheck": "conda run -n compartment python skills/3d-dna/compartment-calling/scripts/call_ab_compartments_pc1.py --help",
                "mode_healthchecks": {},
            }
        return {
            "skill_id": skill_id,
            "env": "calder2",
            "default_env": "calder2",
            "env_runner": "conda run -n calder2 python",
            "language": "python",
            "packages": ["cooler", "pandas", "numpy", "scipy", "h5py"],
            "capabilities": ["compartment-calling", "calder2"],
            "install_tool": "calder_r_runtime",
            "install_command": "conda create -n calder2 -c conda-forge -c bioconda python=3.10 r-calder2 cooler pandas numpy scipy h5py r-data.table -y",
            "install_commands": [
                "conda create -n calder2 -c conda-forge -c bioconda python=3.10 r-calder2 cooler pandas numpy scipy h5py r-data.table -y",
            ],
            "install_fallbacks": [],
            "install_policy": "declared-only",
            "env_file": "",
            "healthcheck": "conda run -n calder2 python skills/3d-dna/calder2-subcompartment/scripts/run_calder_compartments.py --help",
            "mode_healthchecks": {
                "official-r": [
                    "conda run -n calder2 python -c \"import cooler, pandas, numpy, h5py; print('PY_OK')\"",
                    "conda run -n calder2 which Rscript",
                    "conda run -n calder2 Rscript -e \"library(CALDER)\"",
                ]
            },
        }

    def record_environment(self, *args, **kwargs):
        self.records.append((args, kwargs))

    def get_skill_meta(self, skill_id):
        return {
            "_skill_dir": ".",
            "_scripts": [],
            "tags": ["compartment", "CALDER2", "mcool"],
            "legacy_ids": [],
        }


class _FakePackageManager:
    def __init__(self):
        self.commands = []
        self.environments = {}

    def get_environment(self, env_name):
        return self.environments.get(env_name, {})

    def register_environment(self, env_name, **kwargs):
        self.environments[env_name] = {"env": env_name, **kwargs}

    def verify_environment(self, env_name, language="", healthcheck=""):
        self.commands.append(healthcheck)
        if "which Rscript" in healthcheck:
            return False, "which: no Rscript in /data0/stu_liusiqi/anaconda3/envs/calder2/bin"
        return True, "OK"


@unittest.skipIf(BioAnalystAgent is None, f"bio_agents import unavailable: {IMPORT_ERROR}")
class RuntimePreflightTests(unittest.TestCase):
    def test_terminal_status_blocks_incomplete_plan_before_wiki_offer(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            task_id = "blocked_install_task"
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = tmpdir
            state = build_plan_state(
                task_id,
                "- [>] Step 1: Install required runtime\n- [ ] Step 2: Run standard wrapper",
            )
            save_plan_state(tmpdir, task_id, state)

            status = agent._terminal_status_from_snapshot(
                task_id,
                SimpleNamespace(values={"messages": []}),
            )

        self.assertEqual(status, "blocked")

    def test_terminal_status_blocks_failed_install_message_without_plan(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            task_id = "blocked_install_message"
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = tmpdir

            status = agent._terminal_status_from_snapshot(
                task_id,
                SimpleNamespace(
                    values={
                        "messages": [
                            AIMessage(
                                content=(
                                    "<observation>blocked: required runtime installation "
                                    "did not complete.</observation>"
                                )
                            )
                        ]
                    }
                ),
            )

        self.assertEqual(status, "blocked")

    def test_terminal_status_text_does_not_treat_reported_failed_counts_as_failure(self):
        self.assertEqual(BioAnalystAgent._terminal_status_from_text("QC report: 0 failed samples"), "")
        self.assertEqual(BioAnalystAgent._terminal_status_from_text("Error: command failed"), "failed")

    def test_official_r_preflight_blocks_calder_when_rscript_missing(self):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = _FakeSkillManager()
        agent.pm = _FakePackageManager()
        agent.runtime_status = {}

        ready, redirect = agent._ensure_skill_runtime_ready(
            ["3d-dna/calder2-subcompartment"],
            task_id="task_preflight",
            code=(
                "conda run -n calder2 python "
                "skills/3d-dna/calder2-subcompartment/scripts/run_calder_compartments.py "
                "--sample leaf leaf.mcool --resolution-bp 40000 --calder-mode official-r"
            ),
        )

        self.assertFalse(ready)
        self.assertIn("r_base_runtime", redirect)
        self.assertTrue(any("which Rscript" in command for command in agent.pm.commands))
        self.assertFalse(any("Rscript --version" in command for command in agent.pm.commands))

    def test_consultant_runtime_preflight_reports_selected_skill_failures(self):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = _FakeSkillManager()
        agent.pm = _FakePackageManager()
        agent.runtime_status = {}

        summary = agent._consultant_runtime_preflight_summary(
            ["3d-dna/calder2-subcompartment"],
            task_id="task_consultant_preflight",
        )

        self.assertIn("[RUNTIME_PREFLIGHT]", summary)
        self.assertIn("3d-dna/calder2-subcompartment: failed", summary)
        self.assertIn("which: no Rscript", summary)
        self.assertTrue(any("which Rscript" in command for command in agent.pm.commands))

    def test_install_tool_owner_skill_prefers_calder_runtime_when_multiple_skills_active(self):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = _FakeSkillManager()

        owner = agent._runtime_owner_skill_for_install(
            "calder_r_runtime",
            ["3d-dna/compartment-calling", "3d-dna/calder2-subcompartment"],
        )
        defaults = agent._skill_runtime_defaults([owner])

        self.assertEqual(owner, "3d-dna/calder2-subcompartment")
        self.assertEqual(defaults["env"], "calder2")
        self.assertIn("r-calder2", defaults["install_command"])

    def test_direct_r_install_is_redirected_to_calder_runtime_install(self):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = _FakeSkillManager()

        redirect = agent._redirect_install_command(
            (
                "/data0/stu_liusiqi/anaconda3/envs/r/bin/Rscript -e "
                "\"devtools::install_github('jgranja24/CALDER')\""
            ),
            ["3d-dna/calder2-subcompartment"],
        )

        self.assertIn('<install_tool env="calder2">calder_r_runtime</install_tool>', redirect)

    def test_direct_calder_install_is_not_allowed_for_default_compartment_skill(self):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = _FakeSkillManager()

        redirect = agent._redirect_install_command(
            (
                "conda run -n compartment Rscript -e "
                "\"devtools::install_github('jgranja24/CALDER')\""
            ),
            ["3d-dna/compartment-calling"],
        )

        self.assertIn('<install_tool env="compartment">compartment_pc1_runtime</install_tool>', redirect)
        self.assertNotIn("calder_r_runtime", redirect)

    def test_calder_install_tool_is_sanitized_for_default_compartment_skill(self):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = _FakeSkillManager()

        defaults = agent._skill_runtime_defaults(["3d-dna/compartment-calling"])
        tool_name, note = agent._sanitize_install_tool_for_selected_runtime(
            "calder_r_runtime",
            ["3d-dna/compartment-calling"],
            defaults,
            "compartment",
        )

        self.assertEqual(tool_name, "compartment_pc1_runtime")
        self.assertIn("not CALDER2", note)

    def test_compartment_skill_blocks_rscript_from_other_env(self):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = _FakeSkillManager()

        violation = agent._selected_skill_env_runner_violation(
            (
                "/data0/stu_liusiqi/anaconda3/envs/scrna-env-bootstrap_r/bin/Rscript "
                "-e \"library(CALDER)\""
            ),
            ["3d-dna/calder2-subcompartment"],
        )

        self.assertIn("bound to runtime env `calder2`", violation)
        self.assertIn("scrna-env-bootstrap_r", violation)

    def test_curly_quote_calder_missing_maps_to_calder_runtime(self):
        name, hint = BioAnalystAgent._runtime_install_target_from_error(
            "Error in library(CALDER) : there is no package called ‘CALDER’",
            "fallback",
        )

        self.assertEqual(name, "calder_r_runtime")
        self.assertIn("CALDER R package is missing", hint)

    def test_calder_runtime_repair_context_defers_to_skill_install_command(self):
        context = BioAnalystAgent._runtime_repair_context("calder_r_runtime", "calder2")

        self.assertIn("selected skill provides an install_command", context)
        self.assertIn("library(CALDER)", context)
        self.assertNotIn("r-calder2", context)

    def test_selected_skill_install_command_context_passes_metadata_to_installer(self):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = _FakeSkillManager()

        defaults = agent._skill_runtime_defaults(["3d-dna/calder2-subcompartment"])
        context = BioAnalystAgent._selected_skill_install_command_context(defaults, "calder2")

        self.assertIn("[SELECTED SKILL INSTALL COMMAND]", context)
        self.assertIn("[STRICT SKILL RUNTIME CONTRACT]", context)
        self.assertIn("allowed_install_command: conda create -n calder2", context)
        self.assertNotIn("mamba create", context)
        self.assertNotIn("biomind-calder2", context)

    def test_prefer_declared_install_context_does_not_emit_strict_contract(self):
        defaults = {
            "selected": ["demo/ordinary-skill"],
            "install_policy": "prefer-declared",
            "install_command": "conda install -n demo -c conda-forge numpy -y",
            "install_commands": ["conda install -n demo -c conda-forge numpy -y"],
            "install_fallbacks": [],
            "env_file": "",
            "healthcheck": "conda run -n demo python -c \"import numpy\"",
        }

        context = BioAnalystAgent._selected_skill_install_command_context(defaults, "demo")

        self.assertIn("[DECLARED SKILL INSTALL PLAN]", context)
        self.assertIn("declared_install_command: conda install -n demo -c conda-forge numpy -y", context)
        self.assertNotIn("[STRICT SKILL RUNTIME CONTRACT]", context)


if __name__ == "__main__":
    unittest.main()
