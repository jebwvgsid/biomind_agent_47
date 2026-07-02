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
from agent_core.skill_manager import SkillManager
from agent_core.workflow_manager import WorkflowManager
from langchain_core.messages import AIMessage


class _FakeSkillManager:
    def __init__(self):
        self.records = []
        self.skills_root = Path(".")

    def get_runtime_requirements(self, skill_id):
        if skill_id.startswith("scrna/"):
            return {
                "skill_id": skill_id,
                "env": "bio_scrna_core",
                "default_env": "bio_scrna_core",
                "env_runner": "conda run -n bio_scrna_core Rscript",
                "language": "r",
                "packages": ["Seurat", "yaml"],
                "capabilities": ["scrna-preprocess"],
                "install_tool": '<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>',
                "install_command": "",
                "install_commands": [],
                "install_fallbacks": [],
                "install_policy": "declared-only",
                "env_file": "",
                "healthcheck": "conda run -n bio_scrna_core Rscript -e \"cat('OK\\n')\"",
                "mode_healthchecks": {},
            }
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
                "install_policy": "declared-only",
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
        agent.skill_manager.skills_root = Path(__file__).resolve().parents[1] / "skills"
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

    def test_runtime_preflight_fails_when_declared_conda_env_is_absent_even_if_help_passes(self):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = _FakeSkillManager()
        agent.pm = _FakePackageManager()
        agent.runtime_status = {}
        agent._verify_conda_env_presence = lambda _env: (False, "EnvironmentLocationNotFound: bio_scrna_core")

        ok, detail, env = agent._verify_skill_runtime(
            "3d-dna/compartment-calling",
            task_id="task_missing_env",
        )

        self.assertFalse(ok)
        self.assertEqual(env, "compartment")
        self.assertIn("EnvironmentLocationNotFound", detail)
        cache = agent.runtime_status["task_missing_env"]["3d-dna/compartment-calling::compartment"]
        self.assertFalse(cache["ok"])
        self.assertFalse(agent.pm.commands)

    def test_relative_skill_healthcheck_is_run_from_repo_root(self):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = _FakeSkillManager()
        agent.skill_manager.skills_root = Path(__file__).resolve().parents[1] / "skills"
        agent.pm = _FakePackageManager()

        resolved = agent._resolved_runtime_healthcheck(
            "conda run -n bio_scrna_core Rscript skills/scrna/env/bootstrap/scripts/check_scrna_env.R"
        )

        self.assertIn("cd ", resolved)
        self.assertIn("Biomind_v1", resolved)
        self.assertIn("skills/scrna/env/bootstrap/scripts/check_scrna_env.R", resolved)

    def test_runtime_block_continues_when_reporting_steps_remain(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            task_id = "runtime_block_reporting"
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = tmpdir
            state = build_plan_state(
                task_id,
                (
                    "- [>] Step 1: Run workflow | Action: EXECUTE | Done: outputs exist\n"
                    "- [ ] Step 2: Generate stage_status.tsv | Action: EXECUTE | Done: blocked status exists\n"
                ),
            )
            save_plan_state(tmpdir, task_id, state)

            progress = agent._mark_plan_current_blocked(task_id, "blocked: missing runtime")
            next_step = agent._next_step_after_runtime_block(task_id)

        self.assertEqual(next_step, "generate")
        self.assertIn("- [!] Step 1: Run workflow", progress)
        self.assertIn("- [ ] Step 2: Generate stage_status.tsv", progress)

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
        self.assertIn("selected skill runtime declares `compartment_pc1_runtime`", note)

    def test_undeclared_package_install_tool_is_sanitized_to_selected_runtime(self):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = _FakeSkillManager()

        defaults = agent._skill_runtime_defaults(["3d-dna/compartment-calling"])
        tool_name, note = agent._sanitize_install_tool_for_selected_runtime(
            "pybedtools",
            ["3d-dna/compartment-calling"],
            defaults,
            "compartment",
        )

        self.assertEqual(tool_name, "compartment_pc1_runtime")
        self.assertIn("selected skill runtime declares `compartment_pc1_runtime`", note)
        self.assertIn("undeclared package names", note)

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

    def test_install_tool_command_is_not_used_as_execute_runner(self):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = _FakeSkillManager()

        violation = agent._selected_skill_env_runner_violation(
            (
                "scrna-env-bootstrap_r <<'REOF'\n"
                "library(Seurat)\n"
                "cat('bad runner')\n"
                "REOF\n"
            ),
            ["scrna/preprocess/basic"],
        )

        self.assertIn("declares install_tool `scrna-env-bootstrap_r`", violation)
        self.assertIn("not for running analysis code", violation)
        self.assertIn("conda run -n bio_scrna_core Rscript", violation)

    def test_structured_action_json_extracts_execute_code(self):
        content = (
            '<action_json>{"action_type":"execute","language":"bash",'
            '"code":"echo ready"}</action_json>'
        )

        action = BioAnalystAgent._extract_structured_action(content)

        self.assertEqual(action["type"], "execute")
        self.assertEqual(action["code"], "echo ready")
        self.assertEqual(action["language"], "bash")

    def test_structured_action_parser_falls_back_to_xml_install_tool(self):
        content = '<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>'

        action = BioAnalystAgent._extract_structured_action(content)

        self.assertEqual(action["type"], "install_tool")
        self.assertEqual(action["tool"], "scrna-env-bootstrap_r")
        self.assertEqual(action["env"], "bio_scrna_core")

    def test_structured_action_parser_does_not_treat_mentions_as_execute(self):
        action = BioAnalystAgent._extract_structured_action("I should output <execute> next.")

        self.assertEqual(action, {})

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

    def test_compartment_install_context_uses_strict_contract(self):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = _FakeSkillManager()

        defaults = agent._skill_runtime_defaults(["3d-dna/compartment-calling"])
        context = BioAnalystAgent._selected_skill_install_command_context(defaults, "compartment")

        self.assertIn("[STRICT SKILL RUNTIME CONTRACT]", context)
        self.assertIn("allowed_install_command: conda create -n compartment", context)
        self.assertNotIn("pybedtools", context)

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

    def test_xml_install_tool_metadata_is_not_double_wrapped(self):
        tag = BioAnalystAgent._install_tool_tag(
            '<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>',
            "fallback_env",
        )

        self.assertEqual(tag, '<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>')
        self.assertEqual(
            BioAnalystAgent._install_tool_name(tag),
            "scrna-env-bootstrap_r",
        )

    def test_runtime_redirect_normalizes_xml_install_tool_and_marks_failed_cache(self):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = None
        agent.runtime_status = {}
        agent._skill_runtime_requirements = lambda _sid: {
            "env": "bio_scrna_core",
            "default_env": "bio_scrna_core",
            "env_runner": "conda run -n bio_scrna_core Rscript",
            "healthcheck": "bash skills/scrna/workflows/ingest-qc/scripts/run_scrna_ingest_qc.sh --help",
            "install_tool": '<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>',
            "install_command": "",
        }
        agent._skill_runtime_defaults = lambda _selected: {"tool_name": "scrna_workflows_ingest_qc_r", "install_tool": "scrna-env-bootstrap_r"}

        redirect, runtime_skills = agent._runtime_install_redirect_from_execution(
            "conda run -n bio_scrna_core Rscript run_scrna_ingest_qc.R",
            "",
            "EnvironmentLocationNotFound: Not a conda environment: bio_scrna_core",
            ["scrna/workflows/ingest-qc"],
            "task_runtime_xml",
        )

        self.assertEqual(runtime_skills, ["scrna/workflows/ingest-qc"])
        self.assertIn('<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>', redirect)
        self.assertNotIn("<install_tool env=\"bio_scrna_core\"><install_tool", redirect)
        cache = agent.runtime_status["task_runtime_xml"]["scrna/workflows/ingest-qc::bio_scrna_core::execution"]
        self.assertFalse(cache["ok"])

    def test_runtime_install_policy_context_blocks_install_when_requested(self):
        self.assertTrue(
            BioAnalystAgent._runtime_install_disallowed_by_context(
                "请使用 scrna/workflows/ingest-qc，环境缺失时 blocked。"
            )
        )
        self.assertTrue(
            BioAnalystAgent._runtime_install_disallowed_by_context(
                "Do not install dependencies; block if the runtime is missing."
            )
        )
        self.assertFalse(
            BioAnalystAgent._runtime_install_disallowed_by_context(
                "If the skill runtime is missing, use the declared install_tool."
            )
        )

    def test_consultant_missing_runtime_block_handoff_goes_to_planner(self):
        class _FakeScrnaSkillManager:
            def match_skills(self, _context, max_results=1):
                return ["scrna/workflows/ingest-qc"][:max_results]

            def normalize_selected_skills(self, raw, max_results=1):
                return list(raw or [])[:max_results]

            def get_runtime_requirements(self, skill_id):
                return {
                    "skill_id": skill_id,
                    "env": "bio_scrna_core",
                    "default_env": "bio_scrna_core",
                    "env_runner": "conda run -n bio_scrna_core Rscript",
                    "healthcheck": "bash skills/scrna/workflows/ingest-qc/scripts/run_scrna_ingest_qc.sh --help",
                    "install_tool": '<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>',
                }

            def get_skill_meta(self, _skill_id):
                return {"_scripts": [], "tags": ["scrna"], "legacy_ids": []}

        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = _FakeScrnaSkillManager()
        agent.runtime_status = {}

        result = agent._consultant_runtime_block_handoff(
            "请使用 scrna/workflows/ingest-qc，环境缺失时 blocked。",
            "conda run -n bio_scrna_core R -e 'library(Seurat)'",
            "",
            "EnvironmentLocationNotFound: Not a conda environment: bio_scrna_core",
            "task_consultant_block",
            max_skills=1,
        )

        self.assertEqual(result["next_step"], "planner")
        self.assertEqual(result["selected_skills"], ["scrna/workflows/ingest-qc"])
        self.assertIn("[SELECTED_SKILLS: scrna/workflows/ingest-qc]", result["requirements_summary"])
        self.assertIn("requires missing runtime environments/dependencies to be treated as blocked", result["requirements_summary"])
        cache = result["runtime_status"]["scrna/workflows/ingest-qc::bio_scrna_core::consultant"]
        self.assertFalse(cache["ok"])

    def test_consultant_execute_loop_guard_is_present(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("execute_actions", source)
        self.assertIn("len(execute_actions) >= 6", source)
        self.assertIn("consultant_execute_count_loop_guard_blocked", source)
        self.assertIn("Repeated Consultant EXECUTE actions reached the loop guard", source)

    def test_consultant_loop_guard_does_not_deterministically_choose_workflow(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertNotIn("def _consultant_auto_workflow_handoff", source)
        self.assertIn("Consultant read-only verification has already run several times", source)
        self.assertIn('"next_step": "ask_user"', source)

    def test_loop_guard_has_no_modality_auto_handoff(self):
        repo_root = Path(__file__).resolve().parents[1]
        source = (repo_root / "bio_agents.py").read_text(encoding="utf-8")

        self.assertNotIn("def _consultant_auto_workflow_handoff", source)

    def test_workflow_exclusion_rules_are_metadata_driven_not_core_modality_hardcoding(self):
        repo_root = Path(__file__).resolve().parents[1]
        source = (repo_root / "bio_agents.py").read_text(encoding="utf-8")
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.workflow_manager = WorkflowManager(str(repo_root / "workflows"))

        self.assertNotIn("_strong_analysis_modalities", source)
        hic_workflow = agent.workflow_manager.get_workflow("hic_tad_analysis")
        reason = agent._workflow_exclusion_reason(
            hic_workflow,
            "matrix.mtx features.tsv barcodes.tsv pbmc3k 单细胞 QC",
        )

        self.assertIn("exclude_when", reason)

    def test_user_explicit_skill_correction_resets_workflow_without_auto_handoff(self):
        repo_root = Path(__file__).resolve().parents[1]
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = SkillManager(str(repo_root / "skills"))
        agent.workflow_manager = WorkflowManager(str(repo_root / "workflows"))
        agent.runtime_status = {
            "task_switch": {
                "3d-dna/tad-calling::hicexplore": {
                    "skill_id": "3d-dna/tad-calling",
                    "env": "hicexplore",
                    "ok": True,
                },
                "scrna/workflows/ingest-qc::bio_scrna_core": {
                    "skill_id": "scrna/workflows/ingest-qc",
                    "env": "bio_scrna_core",
                    "ok": True,
                },
            }
        }

        update = agent._state_update_for_user_skill_correction(
            "请改用 scrna/workflows/ingest-qc 继续，对这个 pbmc3k 10x scRNA 数据做 ingest 和 QC。",
            {
                "active_skills": ["3d-dna/tad-calling"],
                "selected_skills": ["3d-dna/tad-calling"],
                "workflow_id": "hic_tad_analysis",
            },
            "task_switch",
        )

        self.assertEqual(update["next_step"], "planner")
        self.assertEqual(update["workflow_id"], "")
        self.assertEqual(update["active_skills"], ["scrna/workflows/ingest-qc"])
        self.assertEqual(update["workflow_handoff"], {})
        self.assertEqual(update["workflow_stages"], [])
        self.assertIn("scrna/workflows/ingest-qc::bio_scrna_core", update["runtime_status"])
        self.assertNotIn("3d-dna/tad-calling::hicexplore", update["runtime_status"])

    def test_confirmation_timeout_uses_shared_input_timeout(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("self.input_timeout_seconds = resolve_input_timeout()", source)
        self.assertIn("timeout_seconds=self.input_timeout_seconds", source)


if __name__ == "__main__":
    unittest.main()
