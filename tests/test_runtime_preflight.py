import asyncio
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
    from bio_agents import BioAnalystAgent, _consultant_validation_is_format_repair_error
except Exception as exc:  # pragma: no cover - optional integration deps may be absent.
    BioAnalystAgent = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None

from agent_core.plan_tracker import (
    build_plan_state,
    load_plan_state,
    mark_first_pending_running,
    save_plan_state,
    task_status,
)
from agent_core.skill_manager import SkillManager
from agent_core.completion_gate import GateResult
from agent_core.workflow_manager import WorkflowManager
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from config.config import (
    LLM_MAX_HISTORY_CHARS,
    LLM_MAX_HISTORY_MESSAGES,
    LLM_STEP_CONTEXT_CHARS,
    LLM_STEP_CONTEXT_MESSAGES,
)
from agent_core.llm_safety import is_transient_llm_failure
from agent_core.task_spec import create_task_spec


class _FakeSkillManager:
    def __init__(self):
        self.records = []
        self.skills_root = Path(".")
        self.runtime_records = {}

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
        if skill_id == "sequence/search/local-blast":
            return {
                "skill_id": skill_id,
                "env": "bio_sequence_search_local_blast_runtime",
                "default_env": "bio_sequence_search_local_blast_runtime",
                "env_runner": "conda run -n bio_sequence_search_local_blast_runtime",
                "language": "cli",
                "packages": ["blast"],
                "capabilities": ["local-blast", "makeblastdb", "blastp", "blastn", "blastdbcmd"],
                "install_tool": "sequence_search_local_blast_runtime",
                "install_command": "conda create -y -n bio_sequence_search_local_blast_runtime -c conda-forge -c bioconda blast",
                "install_commands": [
                    "conda create -y -n bio_sequence_search_local_blast_runtime -c conda-forge -c bioconda blast",
                ],
                "install_fallbacks": [],
                "install_policy": "declared-only",
                "env_file": "",
                "healthcheck": "conda run -n bio_sequence_search_local_blast_runtime makeblastdb -version && conda run -n bio_sequence_search_local_blast_runtime blastp -version && conda run -n bio_sequence_search_local_blast_runtime blastn -version && conda run -n bio_sequence_search_local_blast_runtime blastdbcmd -version",
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
        if args:
            skill_id = args[0]
        else:
            skill_id = kwargs.get("skill_id", "")
        if skill_id:
            self.runtime_records[skill_id] = dict(kwargs)

    def get_runtime_record(self, skill_id):
        return dict(self.runtime_records.get(skill_id, {}))

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
        self.registry = {"tools": {}, "environments": self.environments}

    def get_environment(self, env_name):
        return self.environments.get(env_name, {})

    def register_environment(self, env_name, **kwargs):
        self.environments[env_name] = {"env": env_name, **kwargs}
        self.registry["environments"] = self.environments

    def find_matching_environment(self, requirements):
        req_packages = set(requirements.get("packages") or [])
        req_caps = set(requirements.get("capabilities") or [])
        best = None
        best_score = -1
        for env, record in self.environments.items():
            env_packages = set(record.get("packages") or [])
            env_caps = set(record.get("capabilities") or [])
            missing_packages = sorted(req_packages - env_packages)
            missing_caps = sorted(req_caps - env_caps)
            score = len(req_packages & env_packages) * 10 + len(req_caps & env_caps) * 5
            if not missing_packages and not missing_caps:
                score += 100
            if score > best_score:
                best_score = score
                best = {
                    "env": env,
                    "match_type": "exact" if not missing_packages and not missing_caps else "partial",
                    "matched_packages": sorted(req_packages & env_packages),
                    "matched_capabilities": sorted(req_caps & env_caps),
                    "missing_packages": missing_packages,
                    "missing_capabilities": missing_caps,
                    "record": dict(record),
                }
        return best

    def verify_environment(self, env_name, language="", healthcheck=""):
        self.commands.append(healthcheck)
        if "which Rscript" in healthcheck:
            return False, "which: no Rscript in /data0/stu_liusiqi/anaconda3/envs/calder2/bin"
        return True, "OK"


class _NeverReturningLLM:
    async def ainvoke(self, messages):
        await asyncio.sleep(1)


class _ProviderCancelledLLM:
    async def ainvoke(self, messages):
        raise asyncio.CancelledError()


@unittest.skipIf(BioAnalystAgent is None, f"bio_agents import unavailable: {IMPORT_ERROR}")
class RuntimePreflightTests(unittest.TestCase):
    def test_transient_llm_failures_are_classified_for_one_bounded_retry(self):
        self.assertTrue(is_transient_llm_failure(RuntimeError("APIConnectionError: Connection error")))
        self.assertTrue(is_transient_llm_failure(TimeoutError("consultant timed out")))
        self.assertFalse(is_transient_llm_failure(ValueError("invalid workflow JSON")))

    def test_llm_request_has_hard_timeout(self):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.llm = _NeverReturningLLM()
        agent.llm_timeout_seconds = 0.02
        agent.llm_readonly_timeout_seconds = 0.01

        with self.assertRaises(TimeoutError):
            asyncio.run(
                agent._ainvoke_llm(
                    [HumanMessage(content="readonly probe")],
                    stage="generate",
                    task_id="timeout_test",
                    task_mode="readonly",
                )
            )

    def test_provider_cancelled_llm_request_becomes_retryable_timeout(self):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.llm = _ProviderCancelledLLM()
        agent.llm_timeout_seconds = 1
        agent.llm_readonly_timeout_seconds = 1

        with self.assertRaisesRegex(TimeoutError, "cancelled by the provider/client"):
            asyncio.run(
                agent._ainvoke_llm(
                    [HumanMessage(content="plan")],
                    stage="planner",
                    task_id="provider_cancel_test",
                    task_mode="managed",
                )
            )

    def test_external_llm_request_cancellation_still_propagates(self):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.llm = _NeverReturningLLM()
        agent.llm_timeout_seconds = 5
        agent.llm_readonly_timeout_seconds = 5

        async def cancel_outer_request():
            request = asyncio.create_task(
                agent._ainvoke_llm(
                    [HumanMessage(content="plan")],
                    stage="planner",
                    task_id="user_cancel_test",
                    task_mode="managed",
                )
            )
            await asyncio.sleep(0)
            request.cancel()
            await request

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(cancel_outer_request())

    def test_generate_history_is_bounded_and_keeps_original_request(self):
        messages = [HumanMessage(content="original request")]
        messages.extend(
            AIMessage(content="x" * (LLM_MAX_HISTORY_CHARS // 2))
            for _ in range(LLM_MAX_HISTORY_MESSAGES + 3)
        )

        visible = BioAnalystAgent._generate_visible_messages(messages)
        total_chars = sum(len(str(message.content)) for message in visible)

        self.assertLessEqual(len(visible), LLM_MAX_HISTORY_MESSAGES)
        self.assertLessEqual(total_chars, LLM_MAX_HISTORY_CHARS)
        self.assertEqual(str(visible[0].content), "original request")

    def test_step_context_keeps_current_evidence_and_drops_old_plan_history(self):
        messages = [
            HumanMessage(content="original user goal"),
            AIMessage(content="old full planner output that should not be forwarded"),
            AIMessage(content="EXECUTE old-command"),
            SystemMessage(content="<observation>old execution output</observation>"),
            SystemMessage(content="<supervisor_feedback>old retry feedback</supervisor_feedback>"),
            HumanMessage(content="<human_answer stage=generate>use the verified input</human_answer>"),
            AIMessage(content="<execute>current-command</execute>"),
            SystemMessage(content="<observation>current execution output</observation>"),
        ]

        visible = BioAnalystAgent._generate_visible_messages(messages)
        text = "\n".join(str(message.content) for message in visible)

        self.assertIn("original user goal", text)
        self.assertIn("use the verified input", text)
        self.assertIn("current execution output", text)
        self.assertIn("current-command", text)
        self.assertNotIn("old full planner output", text)
        self.assertLessEqual(len(visible), LLM_STEP_CONTEXT_MESSAGES)
        self.assertLessEqual(sum(len(str(message.content)) for message in visible), LLM_STEP_CONTEXT_CHARS)

    def test_current_step_prompt_context_does_not_forward_future_plan_steps(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            task_id = "step_context_task"
            plan = (
                "- [ ] Step 1: Action: EXECUTE preflight\n"
                "  Output contract: preflight.tsv\n"
                "  Success criteria: preflight is non-empty\n"
                "- [ ] Step 2: Action: EXECUTE downstream analysis\n"
                "  Output contract: downstream.tsv\n"
            )
            state = build_plan_state(task_id, plan)
            mark_first_pending_running(state)
            save_plan_state(tmpdir, task_id, state)

            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = tmpdir
            context = agent._current_step_prompt_context(task_id, current_plan=plan)

        self.assertIn("preflight", context)
        self.assertNotIn("downstream analysis", context)

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

    def test_terminal_status_blocks_finalize_internal_error_before_wiki_offer(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            task_id = "finalize_internal_error"
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = tmpdir

            status = agent._terminal_status_from_snapshot(
                task_id,
                SimpleNamespace(
                    values={
                        "consultation_state": {
                            "phase": "blocked",
                            "consultation_status": "blocked",
                            "completion_decision": "blocked",
                            "completion_reason": "FINALIZE_REPAIR_EXHAUSTED",
                            "finalize_protocol": None,
                        },
                        "messages": [
                            SystemMessage(
                                content=(
                                    "<consultant_finalize_internal_error>"
                                    '{"error_code":"FINALIZE_REPAIR_EXHAUSTED"}'
                                    "</consultant_finalize_internal_error>"
                                )
                            )
                        ],
                    }
                ),
            )

        self.assertEqual(status, "blocked")

    def test_empty_terminal_snapshot_never_defaults_to_completed(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            task_id = "terminal_evidence_missing"
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = tmpdir

            status = agent._terminal_status_from_snapshot(
                task_id,
                SimpleNamespace(values={"messages": []}),
            )

        self.assertEqual(status, "blocked")

    def test_active_durable_finalize_overrides_stale_completed_terminal(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            task_id = "stale_completed_active_finalize"
            state = load_plan_state(tmpdir, task_id)
            state.setdefault("task_state", {})["status"] = "completed"
            state["task_state"]["consultation_state"] = {
                "phase": "finalize",
                "finalize_protocol": {
                    "schema_version": 4,
                    "wire_schema_version": 3,
                    "finalize_status": "resolving",
                    "attempts": [{
                        "attempt_id": "attempt-held",
                        "provider_status": "in_flight",
                    }],
                },
            }
            save_plan_state(tmpdir, task_id, state)
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = tmpdir

            status = agent._terminal_status_from_snapshot(
                task_id,
                SimpleNamespace(values={"task_status": "completed"}),
            )

        self.assertEqual(status, "blocked")

    def test_terminal_status_text_does_not_block_quoted_finalize_tag(self):
        self.assertEqual(
            BioAnalystAgent._terminal_status_from_text(
                "<consultant_finalize_internal_error>"
                '{"error_code":"FINALIZE_PROVIDER_FATAL"}'
                "</consultant_finalize_internal_error>"
            ),
            "",
        )

    def test_wiki_offer_wait_is_not_business_task_input(self):
        self.assertFalse(
            BioAnalystAgent._business_waiting_state(
                {"stage": "wiki_ingest_offer", "status": "waiting_user"}
            )
        )

    def test_consultant_explicit_blocked_status_is_durable(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            task_id = "durable_finalize_internal_error"
            save_plan_state(tmpdir, task_id, build_plan_state(task_id, ""))
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = tmpdir

            agent._record_profiler_state_update(
                task_id,
                "consultant",
                {"task_id": task_id},
                {
                    "task_status": "blocked",
                    "next_step": "finish",
                    "consultation_state": {
                        "phase": "blocked",
                        "completion_decision": "blocked",
                    },
                },
            )

            durable = load_plan_state(tmpdir, task_id)

        self.assertEqual(task_status(durable), "blocked")
        self.assertEqual(
            durable["task_state"]["last_transition"]["task_status"],
            "blocked",
        )

    def test_install_result_continues_when_plan_has_pending_steps(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            task_id = "install_continue_task"
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = tmpdir
            state = build_plan_state(
                task_id,
                "- [x] Step 1: Install bio_lumpy\n- [ ] Step 2: Run mapping\n- [ ] Step 3: Write report",
            )
            save_plan_state(tmpdir, task_id, state)

            self.assertEqual(agent._next_step_after_install_result(task_id), "generate")

    def test_install_result_finishes_when_plan_is_terminal(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            task_id = "install_finish_task"
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = tmpdir
            state = build_plan_state(
                task_id,
                "- [x] Step 1: Install bio_lumpy\n- [x] Step 2: Run mapping",
            )
            save_plan_state(tmpdir, task_id, state)

            self.assertEqual(agent._next_step_after_install_result(task_id), "finish")

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
        self.assertIn("[RUNTIME_AUDIT]", summary)
        self.assertIn("3d-dna/calder2-subcompartment: failed", summary)
        self.assertIn("which: no Rscript", summary)
        self.assertIn("decision:", summary)
        self.assertTrue(any("which Rscript" in command for command in agent.pm.commands))

    def test_skill_only_fallback_runs_runtime_preflight_before_planner(self):
        repo_root = Path(__file__).resolve().parents[1]
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = SkillManager(str(repo_root / "skills"))
        agent.pm = _FakePackageManager()
        agent.runtime_status = {}
        agent.data_path = tempfile.gettempdir()
        agent._verify_skill_runtime = lambda *_args, **_kwargs: (True, "healthcheck passed", "bio_gatk")
        agent._consultant_handoff_memory_packet = lambda *_args, **_kwargs: ""

        result = agent._consultant_skill_only_fallback_handoff(
            consultant_context="use genomics/snpeff-annotation",
            fallback_raw=["genomics/snpeff-annotation"],
            task_id="task_snpeff_fallback",
            consultant_memory_retrieval_context="",
            consultant_wiki_context="",
        )

        self.assertEqual(result["next_step"], "planner")
        self.assertEqual(result["runtime_audit"][0]["status"], "ready")
        self.assertFalse(result["runtime_audit"][0]["install_required"])
        self.assertIn("status: ready", result["requirements_summary"])
        self.assertIn("env_runner: bash", result["requirements_summary"])
        self.assertIn("Never synthesize a Conda prefix", result["requirements_summary"])

    def test_planner_does_not_load_global_installed_tool_inventory(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")
        planner_source = source[source.index("async def planner("):source.index("async def plan_review(")]

        self.assertNotIn("list_installed_tools", planner_source)
        self.assertIn("runtime_audit_context", planner_source)

    def test_generate_context_does_not_load_global_installed_tool_inventory(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")
        context_source = source[
            source.index("def _ephemeral_prompt_context_result("):
            source.index("def _bounded_message_history(")
        ]

        self.assertNotIn("_build_tool_context", context_source)
        self.assertIn("build_scoped_runtime_context", context_source)

    def test_runtime_audit_reports_verified_alternate_environment_candidate(self):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = _FakeSkillManager()
        agent.pm = _FakePackageManager()
        agent.runtime_status = {}
        agent.pm.register_environment(
            "shared_scrna",
            packages=["Seurat", "yaml"],
            capabilities=["scrna-preprocess"],
            language="r",
            status="verified",
        )
        agent._verify_conda_env_presence = lambda env: (
            (False, "EnvironmentLocationNotFound: bio_scrna_core")
            if env == "bio_scrna_core" else
            (True, f"Conda environment `{env}` exists.")
        )

        summary = agent._consultant_runtime_preflight_summary(
            ["scrna/preprocess/basic"],
            task_id="task_alt_runtime",
        )

        self.assertIn("[RUNTIME_AUDIT]", summary)
        self.assertIn("declared_env: bio_scrna_core", summary)
        self.assertIn("matching_env_candidate: env=shared_scrna", summary)
        self.assertIn("candidate_healthcheck: passed; env=shared_scrna", summary)
        self.assertIn("alternate_runtime_candidate_verified_needs_user_or_planner_decision", summary)

    def test_runtime_audit_extracts_only_existing_healthcheck_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_prefix = root / "envs" / "runtime"
            jar_path = env_prefix / "share" / "tool.jar"
            jar_path.parent.mkdir(parents=True)
            jar_path.write_text("jar", encoding="utf-8")

            resolved = BioAnalystAgent._verified_runtime_paths(
                "\n".join([
                    f"RESOLVED_ENV_PREFIX={env_prefix}",
                    f"RESOLVED_TOOL_JAR={jar_path}",
                    f"MISSING_TOOL={root / 'missing.jar'}",
                    "ordinary healthcheck prose",
                ])
            )

        self.assertEqual(resolved["RESOLVED_ENV_PREFIX"], str(env_prefix.resolve()))
        self.assertEqual(resolved["RESOLVED_TOOL_JAR"], str(jar_path.resolve()))
        self.assertNotIn("MISSING_TOOL", resolved)

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

        self.assertEqual(next_step, "finish")
        self.assertIn("- [!] Step 1: Run workflow", progress)
        self.assertIn("- [-] Step 2: Generate stage_status.tsv", progress)

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

    def test_skill_script_guard_rejects_unknown_flags_and_undeclared_conda_python(self):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = SkillManager(str(Path(__file__).resolve().parents[1] / "skills"))
        script = (
            Path(__file__).resolve().parents[1]
            / "skills/genomics/snpeff-annotation/scripts/render_snpeff_annotation_scripts.py"
        )

        unknown_flag = agent._selected_skill_env_runner_violation(
            f"{sys.executable} {script} --fasta ref.fa --gtf genes.gtf --gtf22",
            ["genomics/snpeff-annotation"],
        )
        wrong_python = agent._selected_skill_env_runner_violation(
            (
                f"conda run -n bio_gatk python {script} --vcf input.vcf "
                "--reference-fasta ref.fa --annotation-gtf genes.gtf "
                "--genome-version custom --outdir output"
            ),
            ["genomics/snpeff-annotation"],
        )
        valid = agent._selected_skill_env_runner_violation(
            (
                f"{sys.executable} {script} --vcf input.vcf "
                "--reference-fasta ref.fa --annotation-gtf genes.gtf "
                "--genome-version custom --outdir output"
            ),
            ["genomics/snpeff-annotation"],
        )

        self.assertIn("undeclared flag(s)", unknown_flag)
        self.assertIn("--fasta", unknown_flag)
        self.assertIn("undeclared Conda Python", wrong_python)
        self.assertEqual(valid, "")

        card = agent.skill_manager.get_execution_cards(
            ["genomics/snpeff-annotation"],
            legacy_renderer_mode=True,
        )
        self.assertIn("script_runner: controller_python", card)
        self.assertIn("--annotation-gtf", card)
        self.assertIn("--reference-fasta", card)

    def test_structured_action_json_extracts_execute_code(self):
        content = (
            '<action_json>{"action_type":"execute","language":"bash",'
            '"code":"echo ready"}</action_json>'
        )

        action = BioAnalystAgent._extract_structured_action(content)

        self.assertEqual(action["type"], "execute")
        self.assertEqual(action["code"], "echo ready")
        self.assertEqual(action["language"], "bash")

    def test_consultant_json_action_normalizes_to_legacy_command(self):
        execute = BioAnalystAgent._consultant_command_from_json(
            '<action_json>{"action_type":"execute","language":"bash","code":"ls -lh data.tsv"}</action_json>'
        )
        load_skill = BioAnalystAgent._consultant_command_from_json(
            '<action_json>{"action_type":"load_skill_doc","skill_id":"genomics/wgs-short-read-mapping"}</action_json>'
        )
        canonical_load_skill = BioAnalystAgent._consultant_command_from_json(
            '```json\n{"action_type":"load_skill_doc",'
            '"canonical_skill_id":"genomics/snpeff-annotation"}\n```'
        )
        finish = BioAnalystAgent._consultant_command_from_json(
            '<action_json>{"action_type":"finish","selected_skills":["genomics/wgs-short-read-mapping"],"summary":"verified inputs"}</action_json>'
        )

        self.assertTrue(execute.startswith("EXECUTE"))
        self.assertIn("ls -lh data.tsv", execute)
        self.assertEqual(load_skill, "LOAD_SKILL_DOC genomics/wgs-short-read-mapping")
        self.assertEqual(canonical_load_skill, "LOAD_SKILL_DOC genomics/snpeff-annotation")
        self.assertTrue(finish.startswith("FINISH "))
        self.assertIn("[SELECTED_SKILLS: genomics/wgs-short-read-mapping]", finish)
        self.assertIn("verified inputs", finish)

    def test_consultant_command_groups_are_rejected_with_field_specific_repair(self):
        content = (
            '<action_json>{"action_type":"execute","requirement_ids":["runtime:x"],'
            '"purpose":"verify","expected_evidence":{"ok":true},'
            '"command_groups":[{"commands":[{"command":"echo ready"}]}]}</action_json>'
        )

        self.assertEqual(BioAnalystAgent._consultant_command_from_json(content), "")
        payload = BioAnalystAgent._consultant_json_payload(content)
        code, detail = BioAnalystAgent._consultant_action_contract_error(content, payload)

        self.assertEqual(code, "ACTION_FORMAT_INVALID")
        self.assertIn("execute.command_groups is unsupported", detail)
        self.assertIn("execute.code is required", detail)
        self.assertTrue(_consultant_validation_is_format_repair_error(
            f'<consultant_validation_error code="{code}">{detail}</consultant_validation_error>'
        ))

    def test_consultant_tool_result_echo_is_a_format_repair_not_execution_evidence(self):
        content = (
            '<tool_result>{"success":false,"errors":['
            '"execute failed: no execute_call_count configured for this phase"]}</tool_result>'
        )

        code, detail = BioAnalystAgent._consultant_action_contract_error(content)

        self.assertEqual(code, "CONTROLLER_EVIDENCE_ECHO")
        self.assertIn("controller-owned evidence", detail)
        self.assertTrue(_consultant_validation_is_format_repair_error(
            f'<consultant_validation_error code="{code}">{detail}</consultant_validation_error>'
        ))

    def test_verified_loaded_skill_ids_require_successful_controller_evidence(self):
        messages = [
            AIMessage(content="LOAD_SKILL_DOC genomics/snpeff-annotation"),
            SystemMessage(content=(
                '<load_error action="LOAD_SKILL_DOC" skill_id="genomics/wgs-manta-sv">failed</load_error>'
            )),
            SystemMessage(content=(
                '<loaded_skill_doc skill_id="genomics/snpeff-annotation">doc</loaded_skill_doc>'
            )),
            SystemMessage(content=(
                '<loaded_skill_reference skill_id="genomics/snpeff-annotation" path="notes.md">ref</loaded_skill_reference>'
            )),
        ]

        self.assertEqual(
            BioAnalystAgent._consultant_verified_loaded_skill_ids(messages),
            ["genomics/snpeff-annotation"],
        )

    def test_controller_runtime_seed_evidence_is_keyed_by_canonical_skill(self):
        repo_root = Path(__file__).resolve().parents[1]
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = SkillManager(str(repo_root / "skills"))
        agent.pm = _FakePackageManager()
        agent.runtime_status = {}
        agent.data_path = tempfile.gettempdir()
        agent._verify_skill_runtime = lambda *_args, **_kwargs: (
            True,
            "RESOLVED_SNPEFF_JAR=/tmp/snpEff.jar",
            "bio_gatk",
        )

        audits, evidence, _summary = agent._consultant_runtime_seed_evidence(
            ["genomics/snpeff-annotation"],
            "task_runtime_seed",
        )

        self.assertEqual(len(audits), 1)
        self.assertEqual(evidence["genomics/snpeff-annotation"]["status"], "ready")

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

    def test_local_blast_missing_makeblastdb_redirect_has_structured_install_hint(self):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = _FakeSkillManager()
        agent.runtime_status = {}

        redirect, runtime_skills = agent._runtime_install_redirect_from_execution(
            "makeblastdb -in target_proteins.fasta -dbtype prot",
            "",
            "./temp_script.sh: line 6: makeblastdb: command not found",
            ["sequence/search/local-blast"],
            "task_local_blast_runtime",
        )

        self.assertEqual(runtime_skills, ["sequence/search/local-blast"])
        self.assertIn('<install_tool env="bio_sequence_search_local_blast_runtime">sequence_search_local_blast_runtime</install_tool>', redirect)
        self.assertIn("failure_class=missing_executable", redirect)
        self.assertIn("missing_tools=[makeblastdb]", redirect)
        self.assertIn("suggested_package=blast", redirect)
        self.assertIn("conda create -y -n bio_sequence_search_local_blast_runtime", redirect)
        self.assertIn("makeblastdb -version", redirect)
        cache = agent.runtime_status["task_local_blast_runtime"][
            "sequence/search/local-blast::bio_sequence_search_local_blast_runtime::execution"
        ]
        self.assertFalse(cache["ok"])
        self.assertIn("makeblastdb: command not found", cache["detail"])

    def test_runtime_redirect_does_not_install_outside_current_step_skills(self):
        repo_root = Path(__file__).resolve().parents[1]
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = SkillManager(str(repo_root / "skills"))
        agent.runtime_status = {}

        redirect, runtime_skills = agent._runtime_install_redirect_from_execution(
            "lumpyexpress -B 02.mapping/sample.sorted.bam -o 03.sv_calling/sample.lumpy.vcf",
            (
                "Sourcing executables from /envs/bio_lumpy/bin/lumpyexpress.config\n"
                "/envs/bio_lumpy/bin/lumpyexpress: line 15: -n: command not found\n"
                "File \"/envs/bio_lumpy/bin/scripts/bamkit/bamlibs.py\", line 1\n"
                "404: Not Found\n"
                "SyntaxError: invalid syntax"
            ),
            "",
            ["genomics/reseq-variant-workflow"],
            "task_lumpy_router_runtime",
        )

        self.assertEqual(runtime_skills, [])
        self.assertEqual(redirect, "")

    def test_runtime_install_policy_context_blocks_install_when_requested(self):
        blocked_spec = create_task_spec("runtime policy")
        blocked_spec["requirements"] = [{
            "requirement_id": "failure-policy-runtime",
            "semantic_key": "failure_policy.runtime_missing",
            "operator": "on_condition",
            "value": "block",
            "strength": "hard",
        }]
        forbidden_spec = create_task_spec("runtime policy")
        forbidden_spec["requirements"] = [{
            "requirement_id": "forbid-install",
            "semantic_key": "forbidden_behavior.runtime_install",
            "operator": "forbid",
            "value": True,
            "strength": "hard",
        }]
        allowed_spec = create_task_spec("runtime policy")
        self.assertTrue(
            BioAnalystAgent._runtime_install_disallowed_by_task_spec(
                blocked_spec
            )
        )
        self.assertTrue(
            BioAnalystAgent._runtime_install_disallowed_by_task_spec(
                forbidden_spec
            )
        )
        self.assertFalse(
            BioAnalystAgent._runtime_install_disallowed_by_task_spec(
                allowed_spec
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

        self.assertIn("max_total_execute=4", source)
        self.assertIn("preflight_execute(", source)
        self.assertIn("record_execution_observation(", source)
        self.assertIn("evaluate_consultation_completion", source)
        self.assertIn("candidate_skill_ids_for_prompt", source)
        self.assertIn("finalize_execute_forbidden", source)

    def test_consultant_loop_guard_does_not_deterministically_choose_workflow(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertNotIn("def _consultant_auto_workflow_handoff", source)
        self.assertNotIn("semantic_contract_ready", source)
        self.assertIn("merge_compiled_constraints(", source)
        self.assertIn('"next_step": "planner"', source)

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
        self.assertEqual(update["selected_skills"], ["scrna/workflows/ingest-qc"])
        self.assertEqual(update["active_skills"], [])
        self.assertEqual(update["workflow_handoff"], {})
        self.assertEqual(update["workflow_stages"], [])
        self.assertIn("scrna/workflows/ingest-qc::bio_scrna_core", update["runtime_status"])
        self.assertNotIn("3d-dna/tad-calling::hicexplore", update["runtime_status"])

    def test_confirmation_timeout_uses_shared_input_timeout(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("self.input_timeout_seconds = resolve_input_timeout()", source)
        self.assertIn("timeout_seconds=self.input_timeout_seconds", source)

    def test_root_cause_post_processing_is_bounded_and_fail_open(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("analysis = await asyncio.wait_for(", source)
        self.assertIn("asyncio.to_thread(\n                            RootCauseAnalyzer().analyze", source)
        self.assertIn("timeout=ROOT_CAUSE_TIMEOUT_SECONDS", source)
        self.assertIn('"status": "diagnostic_timeout"', source)

    def test_command_trace_does_not_leak_xtrace_state_to_child_processes(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertNotIn('export BASH_XTRACEFD=9', source)
        self.assertNotIn('trace_prefix += "set -x\\n"', source)
        self.assertIn("+BIO_ERR|", source)

    def test_managed_execute_does_not_append_global_shims(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("if shim_path and not managed_mode:", source)
        self.assertIn('shim_path=(str(shim_path or "") if not managed_mode else "")', source)

    @unittest.skipIf(BioAnalystAgent is None, f"bio_agents import unavailable: {IMPORT_ERROR}")
    def test_advisory_warn_on_final_artifacts_does_not_force_retry_loop(self):
        plan_state = {
            "steps": [
                {
                    "status": "running",
                    "title": (
                        "Step 5: Generate final report and reproducibility script | "
                        "Action: EXECUTE | Done: report.md, reproduce.sh, stage_status.tsv exist"
                    ),
                }
            ]
        }
        messages = [
            AIMessage(
                content=(
                    "Verdict: WARN\n"
                    "Reason: report preview looked truncated and the summary TSV is small.\n"
                    "Action: inspect the contents before accepting the results."
                )
            )
        ]

        self.assertTrue(BioAnalystAgent._supervisor_warn_requires_followup(messages))
        self.assertTrue(
            BioAnalystAgent._supervisor_warn_is_acceptably_artifact_bounded(
                messages,
                plan_state,
                GateResult(True),
            )
        )

    @unittest.skipIf(BioAnalystAgent is None, f"bio_agents import unavailable: {IMPORT_ERROR}")
    def test_plan_step_runtime_env_reselects_sniffles_over_unrelated_3d_dna(self):
        import tempfile

        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            task_id = "task_longread_sv"
            plan_file = Path(tmpdir) / "task_outputs" / task_id / "plan.md"
            plan = """
- [ ] Step 1: Verify bio_longread_sv runtime and Sniffles2 healthcheck | Goal: confirm minimap2, samtools, sniffles are available | Action: EXECUTE | Skills: [genomics/long-read-sv-sniffles2] | Stage: longread_sv | Batch: 1 | Done: healthcheck passes
- [ ] Step 2: Run ONT minimap2 mapping | Action: EXECUTE | Skills: [genomics/long-read-sv-sniffles2] | Stage: longread_sv | Batch: 1 | Done: ont1.sort.bam exists
"""
            state = build_plan_state(task_id, plan, plan_file=str(plan_file))
            mark_first_pending_running(state, "started")
            save_plan_state(tmpdir, task_id, state)

            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = tmpdir
            agent.skill_manager = SkillManager(str(repo_root / "skills"))

            update = agent._active_skill_update_for_current_plan_step(
                {
                    "selected_skills": [
                        "3d-dna/loop-calling",
                        "genomics/long-read-sv-cutesv",
                        "genomics/long-read-sv-sniffles2",
                    ],
                    "active_skills": [
                        "3d-dna/loop-calling",
                        "genomics/long-read-sv-cutesv",
                        "genomics/long-read-sv-sniffles2",
                    ],
                },
                task_id,
            )

        self.assertIn("genomics/long-read-sv-sniffles2", update.get("active_skills", []), update)
        self.assertNotIn("3d-dna/loop-calling", update.get("active_skills", []), update)

    @unittest.skipIf(BioAnalystAgent is None, f"bio_agents import unavailable: {IMPORT_ERROR}")
    def test_install_target_is_narrowed_to_current_plan_step(self):
        import tempfile

        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            task_id = "task_install_narrow"
            plan_file = Path(tmpdir) / "task_outputs" / task_id / "plan.md"
            plan = """
- [ ] Step 1: Verify bio_longread_sv runtime, minimap2, samtools, and Sniffles2 | Action: EXECUTE | Skills: [genomics/long-read-sv-sniffles2] | Stage: longread_sv | Batch: 1 | Done: healthcheck passes
"""
            state = build_plan_state(task_id, plan, plan_file=str(plan_file))
            mark_first_pending_running(state, "started")
            save_plan_state(tmpdir, task_id, state)

            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = tmpdir
            agent.skill_manager = SkillManager(str(repo_root / "skills"))

            narrowed = agent._narrow_skills_to_current_plan_step(
                [
                    "3d-dna/loop-calling",
                    "genomics/long-read-sv-cutesv",
                    "genomics/long-read-sv-sniffles2",
                ],
                task_id,
            )

        self.assertIn("genomics/long-read-sv-sniffles2", narrowed)
        self.assertNotIn("3d-dna/loop-calling", narrowed)


if __name__ == "__main__":
    unittest.main()
