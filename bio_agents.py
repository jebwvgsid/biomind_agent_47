"""
bio_agents.py  ―  BioAnalyst Agent V4
======================================
新增 / 重构内容：
  • MemoryManager   — 20万token自动压缩+归档
  • SupervisorNode  — 科学性验证+错误纠正+CoT日志
  • OutputRouter    — 统一打印，消除重复输出
  • 详细任务规划流程 — Consultant → Planner(详细) → ReviewUI → Script生成 → Execute → Supervisor
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import logging
import os
import re
import shutil
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from colorama import Fore, Style

# ── 路径设置 ──────────────────────────────────────────────────
current_dir = Path(__file__).parent
project_root = current_dir.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import config.startup

# ── 第三方库 ──────────────────────────────────────────────────
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt, Command
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, BaseMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages
from langchain.chat_models import init_chat_model
from langchain_core.callbacks import BaseCallbackHandler

# ── 本地模块 ──────────────────────────────────────────────────
from prompts.templates import INSTALLER_HANDOVER_TEMPLATE
from execution.confirmation import ConfirmationUI, RiskLevel, ConfirmationManager, ConfirmationConfig, RiskAnalyzer
from config.settings import PathManager
from config.config import (
    RAGFLOW_ADDRESS, RAGFLOW_API_KEY, KB_MAP, RERANK_MODEL_ID,
    LLM_API_KEY, LLM_DEFAULT_MODEL, LLM_BASE_URL, BIO_SOFTWARE_ROOT, BIO_SOURCE_INSTALL_DIR,
    LLM_TIMEOUT_SECONDS, LLM_READONLY_TIMEOUT_SECONDS, LLM_MAX_RETRIES,
    LLM_MAX_HISTORY_MESSAGES, LLM_MAX_HISTORY_CHARS,
    LLM_STEP_CONTEXT_MESSAGES, LLM_STEP_CONTEXT_CHARS, LLM_STEP_PLAN_CHARS,
    LLM_STEP_SKILL_DOC_CHARS, LLM_STEP_EPHEMERAL_CHARS,
    DEFAULT_PROJECT_ID, BIOMIND_MEMORY_DIR, BIOMIND_WIKI_ENABLED, BIOMIND_WIKI_DIR, BIOMIND_WIKI_INGEST_MODE, WIKI_EMBEDDING_ENABLED, WIKI_EMBEDDING_BASE_URL,
    WIKI_EMBEDDING_API_KEY, WIKI_EMBEDDING_MODEL, WIKI_EMBEDDING_DIMENSIONS,
    ENABLE_ROOT_CAUSE_ANALYZER, ENABLE_COMMAND_TRACE, ENABLE_PIPESTATUS_CAPTURE,
    ROOT_CAUSE_TIMEOUT_SECONDS,
)

try:
    from package_manager import PackageManager
except ImportError:
    sys.path.append(str(project_root))
    from package_manager import PackageManager

from agent_core.state import AgentState
from agent_core.human_io import (
    HumanInputUnavailable,
    clear_waiting_state,
    load_waiting_state,
    read_pending_paste_lines,
    resolve_input_timeout,
    restore_terminal_state,
    safe_input,
    safe_multiline_input,
    save_waiting_state,
)
from agent_core.installer import InstallerAgent
from agent_core.retriever import RAGFlowRetriever
from agent_core.router import classify_task_mode
from agent_core.skill_manager import SkillManager
from agent_core.workflow_manager import (
    WorkflowManager,
    parse_workflow_handoff,
    parse_workflow_state,
    repair_workflow_handoff,
    repair_workflow_state,
    strip_workflow_activation,
    validate_workflow_handoff,
    validate_workflow_state,
)
from agent_core.middleware import HITLMiddleware
from agent_core.recovery import (
    ActionSignature,
    ExecutionRecord,
    RecoveryAction,
    RecoveryDecision,
    RecoveryDecisionKind,
    RecoveryPolicy,
    RecoveryFailureOwner,
    RecoveryStateStore,
    ValidationEvidence,
    ValidationStatus,
    deterministic_recovery_decision,
    diagnosis_required,
    evaluate_recovery_trigger,
    failure_log_projection,
    parse_recovery_judge_decision,
    recovery_action_route,
    constrain_recovery_decision,
    recovery_decision_from_diagnosis,
    contextualize_recovery_decision,
    recovery_decision_from_state,
    recovery_route_from_state,
    structured_user_decision_required,
    validate_diagnostic_probe_code,
)
from agent_core.diagnostics import (
    RecoveryContext,
    build_current_step_context,
    build_recovery_context,
    enrich_execution_evidence,
)
from agent_core.context_lineage import ContextLineageStore
from agent_core.knowledge_conflicts import (
    ClaimRegistry,
    ConflictResolver,
    KnowledgeClaim,
    KnowledgeConflict,
    KnowledgeConflictDetector,
    KnowledgeConflictStore,
    PlanRevisionManager,
    feature_enabled,
)
from agent_core.llm_safety import (
    clip_text,
    deterministic_failure_solution,
    is_llm_input_rejected,
    is_transient_llm_failure,
    llm_failure_summary,
)
from agent_core.context_budget import RequiredContextOverflow, compress_tagged_text
from agent_core.formal_artifact_resolver import (
    format_formal_upstream_context,
    resolve_formal_upstream_artifacts,
)
from agent_core.plan_tracker import (
    PLAN_OPERATION_LOCAL_REPLAN,
    PLAN_OPERATION_REPAIR,
    TASK_STATUS_BLOCKED,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_DIAGNOSING,
    TASK_STATUS_FAILED,
    TASK_STATUS_REPAIRING,
    TASK_STATUS_RUNNING,
    TASK_STATUS_WAITING_USER,
    TASK_TERMINAL_STATUSES,
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_SKIPPED_UPSTREAM_BLOCKED,
    advance_plan_after_verified_step,
    apply_recovery_decision,
    attach_execution_evidence_relation,
    apply_artifact_reconciliation,
    apply_plan_patch,
    approve_plan_state,
    bind_plan_to_task_revision,
    block_plan_and_downstream,
    build_plan_repair_request,
    build_plan_state,
    increment_plan_budget,
    carry_forward_plan_lifecycle,
    changed_plan_step_ids,
    collapse_renderer_composite_steps,
    current_plan_step_with_index,
    default_plan_file,
    ensure_plan_state,
    load_plan_state,
    mark_first_pending_running,
    mark_plan_step_blocked,
    mark_plan_step_done,
    mark_renderer_action_completed,
    next_action_title,
    next_renderer_manifest_action,
    normalize_renderer_manifest,
    plan_contract_violations,
    plan_approval_is_current,
    plan_state_path,
    provenance_debug_dump,
    record_current_step_evidence,
    record_provenance_event,
    render_structured_plan,
    render_plan_progress,
    renderer_manifest_complete,
    register_renderer_manifest,
    resolve_renderer_action_script,
    reopen_last_done_step,
    restore_step_status_authority,
    reset_plan_state,
    save_plan_state,
    set_task_status,
    step_artifact_contracts,
    validate_renderer_action_artifacts,
    parse_plan_patch,
    sync_plan_state_from_stage_status,
    task_status,
    update_plan_markdown_status,
    update_task_state_projection,
    validate_plan_for_generate,
)
from agent_core.task_spec import (
    apply_user_feedback,
    classify_user_feedback_change,
    create_task_spec,
    evaluate_requirements,
    effective_task_contract,
    ensure_task_spec,
    format_effective_user_facts_context,
    merge_task_spec_input_audit,
    project_effective_user_facts,
    project_task_spec_to_renderer,
    resolve_effective_user_facts,
    task_spec_constraints,
    task_spec_revision,
)
from agent_core.completion_gate import CompletionGate
from execution.executor import (
    BASH_SCRIPT_PROLOGUE,
    detect_script_language,
    normalize_script_code,
    run_bash_script,
    run_python_repl,
    terminate_active_processes,
    get_last_execution_metadata,
)
from execution.profiling import (
    ProfilingCallbackHandler,
    RunProfiler,
    benchmark_metadata_for_task,
    generic_metadata_for_prompt,
    timestamp_now,
)
from execution.validator import ResultValidator
from execution.monitoring import ExecutionMonitor, LogCleaner, ErrorRecoverySystem
from agent_core.middleware import error_recovery_middleware
from agent_core.root_cause import RootCauseAnalyzer
from memory.persistence import PersistentStateManager
from memory.core_store import CoreMemoryStore
from memory.session_archive import SessionArchive
from memory.task_semantics import infer_task_semantics, merge_task_semantics
from memory.task_summary import TaskSummaryRenderer
from memory.wiki_memory import NullWikiMemoryManager, WikiMemoryManager
from memory.artifact_index import ArtifactIndex
from prompts.manager import PromptManager

# ── 新增模块 ──────────────────────────────────────────────────
from memory_manager import MemoryManager
from supervisor import SupervisorNode, log_plan_to_cot


CONSULTANT_PARSE_REPAIR_LIMIT = 3
CONSULTANT_INTERNAL_VALIDATION_ERROR_LIMIT = 3
CONSULTANT_LOAD_ACTION_LOOP_LIMIT = 6
CONSULTANT_LOAD_ERROR_LOOP_LIMIT = 3
CONSULTANT_PROMPT_COMPONENT_BUDGETS = {
    "workflow_catalog": 800,
    "skill_tree": 400,
    "skill_catalog": 1400,
    "skills_summary": 1800,
    "runtime_evidence": 700,
    "memory_context": 400,
    "input_resolution": 1200,
    "rag_history": 300,
    "user_preferences": 300,
}
CONSULTANT_PROMPT_WARNING_CHARS = 12000


def _llm_extra_body(model: str, base_url: str) -> dict[str, Any]:
    model_lower = (model or "").lower()
    base_url_lower = (base_url or "").lower()

    if "deepseek" in base_url_lower or model_lower.startswith("deepseek-"):
        thinking_enabled = os.getenv("DEEPSEEK_THINKING", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
            "enabled",
        }
        return {"thinking": {"type": "enabled" if thinking_enabled else "disabled"}}

    if "dashscope.aliyuncs.com" in base_url_lower or "qwen" in model_lower:
        return {"enable_thinking": False}

    return {}


def _consultant_validation_is_format_repair_error(text: str) -> bool:
    lower = str(text or "").lower()
    return any(
        marker in lower
        for marker in (
            "response did not use any supported command",
            "reply with exactly one command",
            "workflow handoff is invalid",
            "finish must include either a workflow handoff",
            "selected invalid skills",
            "raw selected items",
        )
    )

logger = logging.getLogger("BioMind.Agent")

SUPERVISOR_RETRY_FIELDS = (
    "retry_reason",
    "failed_step_id",
    "failure_fingerprint",
    "failure_owner",
    "required_next_action",
    "recovery_scope",
    "evidence_status",
)


def missing_supervisor_retry_fields(state: Dict[str, Any]) -> List[str]:
    """Return missing fields from the Supervisor -> Recovery contract."""
    if str((state or {}).get("supervisor_verdict") or "").upper() != "RETRY":
        return []
    return [key for key in SUPERVISOR_RETRY_FIELDS if not str((state or {}).get(key) or "").strip()]


# ════════════════════════════════════════════════════════════════
#  1.  统一输出路由器（消除重复打印）
# ════════════════════════════════════════════════════════════════

class OutputRouter:
    """
    所有终端打印通过此类，通过 _seen 集合去重，
    避免同一内容在 streaming 和事件循环中被打印两次。
    """
    def __init__(self):
        self._seen: set = set()
        self._last_node: str = ""
        self.pager_line_threshold = int(os.getenv("BIOMIND_PAGER_LINES", "35"))
        self.preview_lines = int(os.getenv("BIOMIND_PREVIEW_LINES", "20"))
        self.viewer_height = int(os.getenv("BIOMIND_VIEWER_HEIGHT", "15"))
        self.viewer_mode = os.getenv("BIOMIND_VIEWER_MODE", "inline").lower()

    def _pager_enabled(self) -> bool:
        if os.getenv("BIOMIND_PAGER", "1").lower() in {"0", "false", "no", "off"}:
            return False
        return bool(sys.stdin.isatty() and sys.stdout.isatty())

    def _inline_window_viewer(self, title: str, text: str) -> bool:
        """Show a fixed-height scroll window directly in the current terminal page."""
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return False

        lines = text.splitlines() or [""]
        height = max(5, self.viewer_height)
        width = shutil.get_terminal_size((100, 24)).columns
        width = max(40, width)
        top = 0
        previous_block_lines = 0

        def visible_width_slice(s: str, limit: int) -> str:
            # Good enough for terminal logs: keep ANSI-less content stable.
            return s[:limit]

        def render() -> int:
            nonlocal top
            content_h = height
            max_top = max(0, len(lines) - content_h)
            top = min(max(top, 0), max_top)
            body_width = width - 2
            header = f" {title} " if title else " Scroll View "
            status = (
                f" {top + 1}-{min(top + content_h, len(lines))}/{len(lines)}  "
                "VIEW ONLY: n/j next  p/k prev  q/Enter return (approval follows) "
            )

            block = []
            block.append("┌" + "─" * body_width + "┐")
            block.append("│" + visible_width_slice(header, body_width).ljust(body_width) + "│")
            block.append("├" + "─" * body_width + "┤")
            for idx in range(content_h):
                line = lines[top + idx] if top + idx < len(lines) else ""
                block.append("│" + visible_width_slice(line, body_width).ljust(body_width) + "│")
            block.append("├" + "─" * body_width + "┤")
            block.append("│" + visible_width_slice(status, body_width).ljust(body_width) + "│")
            block.append("└" + "─" * body_width + "┘")
            print("\n".join(block))
            return len(block)

        try:
            while True:
                if previous_block_lines:
                    # Move cursor back above the previous block + prompt, then clear.
                    print(f"\x1b[{previous_block_lines + 1}A\x1b[J", end="")
                previous_block_lines = render()
                # A previous subprocess or terminal UI can leave ISIG/ICANON
                # disabled. Restore normal TTY semantics before waiting so
                # Ctrl-C/Ctrl-Z are signals instead of literal ``^C``/``^Z``.
                restore_terminal_state()
                cmd = safe_input(
                    "Scroll only (Enter/q/y=continue to approval) > ",
                    timeout_seconds=resolve_input_timeout(300),
                    history=False,
                ).strip().lower()
                if cmd in {"", "q", "quit", "exit", "y", "yes"}:
                    break
                if cmd in {"n", "next", "j", "down", "d", " "}: 
                    top += height
                elif cmd in {"p", "prev", "previous", "k", "up", "u"}:
                    top -= height
                elif cmd in {"g", "home"}:
                    top = 0
                elif cmd in {"end", "bottom"}:
                    top = max(0, len(lines) - height)
                else:
                    try:
                        top = int(cmd) - 1
                    except ValueError:
                        pass
            return True
        except KeyboardInterrupt:
            restore_terminal_state()
            raise
        except (EOFError, HumanInputUnavailable):
            restore_terminal_state()
            return True

    def _preview_text(self, title: str, text: str, color: str = ""):
        lines = text.splitlines()
        shown = "\n".join(lines[:self.preview_lines])
        suffix = ""
        if len(lines) > self.preview_lines:
            suffix = f"\n... [{len(lines) - self.preview_lines} more lines; set BIOMIND_PAGER=1 in a TTY to scroll]"
        header = f"\n{title}\n" if title else ""
        body = f"{header}{shown}{suffix}"
        print(f"{color}{body}{Style.RESET_ALL}" if color else body)

    def page_text(self, title: str, text: str, color: str = "", force: bool = False):
        """Display long text in a scrollable terminal pager when available."""
        text = text.rstrip("\n")
        line_count = len(text.splitlines())
        if not text:
            return
        if not force and line_count <= self.pager_line_threshold:
            header = f"\n{title}\n" if title else ""
            body = header + text
            print(f"{color}{body}{Style.RESET_ALL}" if color else body)
            return
        if not self._pager_enabled():
            self._preview_text(title, text, color)
            return

        if self.viewer_mode == "inline" and self._inline_window_viewer(title, text):
            return

        if not shutil.which("less"):
            self._preview_text(title, text, color)
            return

        fd, path = tempfile.mkstemp(prefix="biomind_view_", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                if title:
                    f.write(title.rstrip() + "\n")
                    f.write("=" * min(80, max(20, len(title))) + "\n\n")
                f.write(text)
                f.write("\n")
            print(f"{Fore.CYAN}↕ Opening scroll view ({line_count} lines). Use ↑/↓/PgUp/PgDn, q to return.{Style.RESET_ALL}")
            subprocess.run(["less", "-R", "-S", path])
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def print_node_header(self, node: str):
        if node != self._last_node:
            self._last_node = node
            icons = {
                "triage": "🔀", "direct_chat": "💬",
                "consultant": "🧠", "planner": "📋", "plan_review": "👁️",
                "generate": "⚙️ ", "execute": "▶️ ", "supervisor": "🔬",
                "install_subagent": "🔧", "finish": "🏁",
            }
            icon = icons.get(node, "•")
            print(f"\n{Fore.CYAN}{'─'*55}")
            print(f"  {icon}  NODE: {node.upper()}")
            print(f"{'─'*55}{Style.RESET_ALL}")

    def print_once(self, key: str, text: str, color: str = ""):
        """相同 key 只打印一次"""
        if key in self._seen:
            return
        self._seen.add(key)
        print(f"{color}{text}{Style.RESET_ALL}" if color else text)

    def print_ai(self, content: str):
        """打印 AI 消息（过滤代码块和标签）"""
        # 不打印纯代码块（由 execute 节点负责展示）
        if "<execute>" in content or "<install_tool>" in content:
            return
        clean = str(content or "")
        # Drop known machine-only payloads, but preserve the text enclosed by
        # status wrappers such as <llm_retryable_pause>.  Removing every
        # generic tagged block made real timeout/failure messages invisible.
        for tag in ("observation", "validation_result", "runtime_status"):
            clean = re.sub(
                rf"<{tag}\b[^>]*>.*?</{tag}>",
                "",
                clean,
                flags=re.IGNORECASE | re.DOTALL,
            )
        clean = re.sub(r"</?[^>]+>", "", clean).strip()
        clean = re.sub(r"```.*?```", "[code block]", clean, flags=re.DOTALL)
        if clean:
            key = clean[:80]
            if key not in self._seen:
                self._seen.add(key)
                self.page_text("🤖 AI Message", clean, Fore.WHITE)

    def print_observation(self, content: str):
        """打印执行结果（只打印一次）"""
        clean = content.replace("<observation>", "").replace("</observation>", "").strip()
        key = f"obs:{clean[:60]}"
        if key not in self._seen:
            self._seen.add(key)
            color = Fore.GREEN if "✅" in clean else Fore.RED
            print(f"\n{color}{'─'*50}")
            self.page_text("Execution Output", clean, color)
            print(f"{'─'*50}{Style.RESET_ALL}")

    def reset_for_task(self):
        self._seen.clear()
        self._last_node = ""


output = OutputRouter()


# ════════════════════════════════════════════════════════════════
#  2.  思考流式处理器
# ════════════════════════════════════════════════════════════════

class VibeThinkingHandler(BaseCallbackHandler):
    def __init__(self):
        self.is_thinking = False

    def on_llm_new_token(self, token: str, **kwargs) -> None:
        chunk = kwargs.get("chunk")
        reasoning = ""
        if chunk and hasattr(chunk, "additional_kwargs"):
            reasoning = chunk.additional_kwargs.get("reasoning_content", "")

        if reasoning:
            if not self.is_thinking:
                print(f"\n{Fore.LIGHTBLACK_EX}🧠 [Thinking...]{Style.RESET_ALL}", end="")
                self.is_thinking = True
            print(f"{Fore.LIGHTBLACK_EX}{reasoning}{Style.RESET_ALL}", end="", flush=True)
        elif token:
            if self.is_thinking:
                print(f"\n{Fore.GREEN}💡 [Done thinking]{Style.RESET_ALL}\n", end="")
                self.is_thinking = False
            # 不在这里打印，由 OutputRouter 统一处理


# ════════════════════════════════════════════════════════════════
#  3.  BioAnalystAgent 主类
# ════════════════════════════════════════════════════════════════

class BioAnalystAgent:
    @staticmethod
    def _resolve_runtime_memory_dir(data_path: str) -> Path:
        configured = Path(BIOMIND_MEMORY_DIR).expanduser() if BIOMIND_MEMORY_DIR else None
        if configured:
            if not configured.is_absolute():
                configured = PathManager.SYSTEM_ROOT / configured
            return configured.resolve()
        return (PathManager.SYSTEM_ROOT / ".biomind" / "memory").resolve()

    @staticmethod
    def _copy_runtime_memory_if_missing(source_dir: str | Path, target_dir: str | Path) -> int:
        source = Path(source_dir).resolve()
        target = Path(target_dir).resolve()
        if source == target or not source.exists() or not source.is_dir():
            return 0
        copied = 0
        for source_file in source.rglob("*"):
            if not source_file.is_file():
                continue
            dest = target / source_file.relative_to(source)
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, dest)
            copied += 1
        return copied

    def __init__(
        self,
        data_path: str = str(PathManager.WORK_DIR),
        llm_model: str = LLM_DEFAULT_MODEL,
        model_provider: str = "openai",
        enable_persistence: bool = True,
        max_memory_tokens: int = 200_000,
        use_llm_supervisor: bool = True,
    ):
        self.data_path = data_path
        self.profiler = RunProfiler(output_root=Path(data_path) / "task_outputs")
        self.input_timeout_seconds = resolve_input_timeout()
        self.llm_timeout_seconds = LLM_TIMEOUT_SECONDS
        self.llm_readonly_timeout_seconds = min(
            LLM_READONLY_TIMEOUT_SECONDS,
            self.llm_timeout_seconds,
        )
        self.llm_max_retries = LLM_MAX_RETRIES

        # ── PackageManager ────────────────────────────────────
        try:
            self.pm = PackageManager(work_dir=data_path)
        except ImportError:
            logger.warning("PackageManager not found.")
            self.pm = None

        # ── LLM ──────────────────────────────────────────────
        llm_kwargs: dict[str, Any] = {
            "model": llm_model,
            "model_provider": model_provider,
            "base_url": LLM_BASE_URL,
            "streaming": True,
            "api_key": LLM_API_KEY,
            "callbacks": [VibeThinkingHandler(), ProfilingCallbackHandler(self.profiler)],
            "temperature": 0.7,
            "timeout": self.llm_timeout_seconds,
            "max_retries": self.llm_max_retries,
        }
        extra_body = _llm_extra_body(llm_model, LLM_BASE_URL)
        if extra_body:
            llm_kwargs["extra_body"] = extra_body
        self.llm = init_chat_model(**llm_kwargs)
        logger.info(
            "LLM request policy: timeout=%ss readonly_timeout=%ss max_retries=%s",
            self.llm_timeout_seconds,
            self.llm_readonly_timeout_seconds,
            self.llm_max_retries,
        )

        # ── 验证 & 确认 ───────────────────────────────────────
        self.validator = ResultValidator()
        self.confirmation_manager = ConfirmationManager(
            ConfirmationConfig(
                auto_approve_safe=True,
                show_code_preview=True,
                timeout_seconds=self.input_timeout_seconds,
            )
        )
        self.middleware = HITLMiddleware(auto_approve_safe=True)

        # ── 持久化 ────────────────────────────────────────────
        self.state_manager = None
        if enable_persistence:
            self.state_manager = PersistentStateManager(
                db_path=str(PathManager.SYSTEM_DB_PATH),
                output_dir=str(PathManager.DEFAULT_OUTPUT_DIR),
            )
        self.artifact_index = ArtifactIndex(PathManager.SYSTEM_DB_PATH) if enable_persistence else None

        # ── RAG ───────────────────────────────────────────────
        self.retriever = None
        if RAGFLOW_API_KEY:
            try:
                self.retriever = RAGFlowRetriever(
                    api_key=RAGFLOW_API_KEY,
                    base_url=RAGFLOW_ADDRESS,
                    dataset_ids=[],
                    rerank_id=RERANK_MODEL_ID,
                )
            except Exception as e:
                logger.warning(f"RAG init failed: {e}")

        # ── MemoryManager（新）────────────────────────────────
        self.runtime_memory_dir = self._resolve_runtime_memory_dir(data_path)
        self.runtime_root = self.runtime_memory_dir.parent
        migrated = WikiMemoryManager.migrate_legacy_runtime_memory(
            Path(data_path) / "memory",
            self.runtime_memory_dir,
        )
        if migrated:
            logger.info("Migrated legacy runtime memory files into %s", self.runtime_memory_dir)
        migrated_current_memory = self._copy_runtime_memory_if_missing(
            Path(data_path) / ".biomind" / "memory",
            self.runtime_memory_dir,
        )
        if migrated_current_memory:
            logger.info(
                "Copied %s runtime memory file(s) from working directory into fixed memory dir %s",
                migrated_current_memory,
                self.runtime_memory_dir,
            )
        self.wiki_enabled = bool(BIOMIND_WIKI_ENABLED)
        self.configured_wiki_dir: Optional[Path] = None
        if self.wiki_enabled and BIOMIND_WIKI_DIR:
            configured = Path(BIOMIND_WIKI_DIR).expanduser()
            if not configured.is_absolute():
                configured = self.runtime_memory_dir / configured
            self.configured_wiki_dir = configured.resolve()
            migrated_wiki = WikiMemoryManager.migrate_wiki_pages(
                self.runtime_memory_dir / "wiki",
                self.configured_wiki_dir,
            )
            if migrated_wiki:
                logger.info(
                    "Migrated %s wiki file(s) into configured wiki dir %s",
                    len(migrated_wiki),
                    self.configured_wiki_dir,
                )

        archive_dir = self.runtime_root / "memory_archives"
        self.memory_manager = MemoryManager(
            llm=self.llm,
            archive_dir=str(archive_dir),
            max_tokens=max_memory_tokens,
        )
        self.default_project_id = DEFAULT_PROJECT_ID
        self.core_memory_store = CoreMemoryStore(
            memory_dir=self.runtime_memory_dir,
            default_project_id=self.default_project_id,
        )
        self.session_archive = SessionArchive(
            db_path=self.runtime_memory_dir / "session_archive.sqlite3",
        )
        self._stable_prompt_cache: Dict[str, Dict[str, str]] = {}
        self._stable_prompt_dir = self.runtime_memory_dir / "session_prompts"
        self._latest_llm_call_refs: Dict[str, List[Dict[str, Any]]] = {}
        if self.wiki_enabled:
            self.wiki_memory_manager = WikiMemoryManager(
                llm=self.llm,
                memory_dir=self.runtime_memory_dir,
                default_project_id=self.default_project_id,
                wiki_dir=self.configured_wiki_dir,
                embedding_config={
                    "enabled": WIKI_EMBEDDING_ENABLED,
                    "base_url": WIKI_EMBEDDING_BASE_URL,
                    "api_key": WIKI_EMBEDDING_API_KEY,
                    "model": WIKI_EMBEDDING_MODEL,
                    "dimensions": WIKI_EMBEDDING_DIMENSIONS,
                },
            )
        else:
            self.wiki_memory_manager = NullWikiMemoryManager(default_project_id=self.default_project_id)
        self.task_summary_renderer = TaskSummaryRenderer(
            memory_dir=self.runtime_memory_dir,
            session_archive=self.session_archive,
            wiki_memory_manager=self.wiki_memory_manager,
        )
        self._wiki_digest_cache: Dict[str, Tuple[float, str]] = {}
        self.wiki_ingest_mode = BIOMIND_WIKI_INGEST_MODE if self.wiki_enabled else "disabled"
        self._wiki_ingest_offer_done: set[str] = set()
        self._wiki_auto_ingest_done: set[str] = set()

        # ── SupervisorNode（新）───────────────────────────────
        self.supervisor_node = SupervisorNode(
            llm=self.llm,
            output_dir=os.path.join(data_path, "task_outputs"),
            max_retries=2,
            use_llm_review=use_llm_supervisor,
        )
        self._install_env_choices: Dict[str, str] = {}
        self._install_attempt_counts: Dict[str, int] = {}
        self._install_scope_attempt_counts: Dict[str, int] = {}
        self.runtime_status: Dict[str, Dict[str, Dict[str, Any]]] = {}

        # ── 子智能体 ──────────────────────────────────────────
        _installer = InstallerAgent(self.llm, work_dir=data_path, package_manager=self.pm)
        self.installer_app = _installer.get_graph()

        # ── SkillManager ──────────────────────────────────────
        try:
            from config.config import SKILLS_DIR
            self.skill_manager = SkillManager(SKILLS_DIR)
        except Exception:
            self.skill_manager = None
        self._sync_runtime_registries_at_startup()
        self.workflow_manager = WorkflowManager(str(Path(__file__).resolve().parent / "workflows"))

        # ── 图 ────────────────────────────────────────────────
        self.checkpointer = MemorySaver()
        self.app = self._build_graph()

    # ────────────────────────────────────────────────────────────
    #  工具方法
    # ────────────────────────────────────────────────────────────

    def _auto_save_results(self, task_id, step_index, code, stdout):
        if not self.state_manager:
            return
        patterns = [r'-O\s+([^\s]+)', r'-o\s+([^\s]+)', r'>\s+([^\s]+)', r"to_csv\(['\"](.*?)['\"]"]
        detected = []
        for p in patterns:
            detected.extend(re.findall(p, code))
        for fpath in detected:
            fpath = fpath.strip()
            if os.path.exists(fpath):
                self.state_manager.save_artifact(
                    task_id, step_index, 'file',
                    os.path.basename(fpath), str(fpath), str(fpath),
                )

    def _auto_task_id_for_prompt(self, prompt: Any, fallback_prefix: str = "task") -> str:
        if isinstance(prompt, Command):
            return ""
        text = str(prompt or "").strip()
        if not text:
            return ""
        try:
            semantics = infer_task_semantics(
                task_id="",
                stage="start",
                messages=[HumanMessage(content=text)],
                payload={"user_query": text},
            )
        except Exception as exc:
            logger.debug("Automatic task id inference failed: %s", exc)
            semantics = {}
        task_type = str(semantics.get("task_type") or "general")
        analysis_type = str(semantics.get("analysis_type") or "general_analysis")
        if task_type == "general" and analysis_type == "general_analysis":
            return ""
        title = str(semantics.get("task_title") or analysis_type or fallback_prefix).strip()
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", title).strip("._-").lower()
        if not slug:
            slug = f"{fallback_prefix}_{datetime.now().strftime('%Y%m%d')}"
        slug = slug[:90].strip("._-") or fallback_prefix
        return f"{slug}_{datetime.now().strftime('%H%M%S')}"

    def _record_initial_profiler_metadata(self, task_id: str, input_data: Any) -> None:
        if isinstance(input_data, Command):
            return
        prompt = str(input_data or "").strip()
        generic_meta = generic_metadata_for_prompt(task_id, prompt, repo_root=current_dir)
        benchmark_meta = benchmark_metadata_for_task(task_id, repo_root=current_dir)
        metadata = {**generic_meta, **benchmark_meta} if benchmark_meta else generic_meta
        try:
            self.profiler.record_task_metadata(
                task_id,
                test_content=metadata.get("test_content", ""),
                test_prompt=metadata.get("test_prompt", ""),
                test_data=metadata.get("test_data", []),
                data_size_bytes=metadata.get("data_size_bytes", 0),
                benchmark=metadata.get("benchmark", {}),
                extra={"semantics": metadata.get("semantics", {})},
            )
        except Exception as exc:
            logger.debug("Profiler initial metadata recording failed: %s", exc)

    def _should_auto_rename_session(self, task_id: str) -> bool:
        if not re.fullmatch(r"session_\d+", str(task_id or "")):
            return False
        try:
            return not self.task_summary_renderer.task_exists(task_id)
        except Exception:
            return True

    async def _observe_session_memory(
        self,
        task_id: str,
        stage: str,
        messages: Optional[List[BaseMessage]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        messages = messages or []
        payload = dict(payload or {})
        event_type = str(payload.get("event_type") or f"{stage}_event")
        project_id = str(payload.get("project_id") or self.default_project_id)
        try:
            inferred = infer_task_semantics(
                task_id=task_id,
                stage=stage,
                messages=messages,
                payload=payload,
            )
            task_semantics = merge_task_semantics(inferred, payload.get("task_semantics"))
            if task_semantics:
                payload["task_semantics"] = task_semantics
            recent = messages[-4:] if messages else []
            content_parts = [
                f"{type(msg).__name__}: {str(getattr(msg, 'content', msg))[:2000]}"
                for msg in recent
            ]
            if payload:
                content_parts.append("payload: " + json.dumps(payload, ensure_ascii=False, default=str)[:4000])
            content = "\n\n".join(content_parts) or stage
            summary = str(payload.get("summary") or content[:1200])
            returncode = payload.get("returncode")
            self.session_archive.append_event(
                task_id=task_id,
                session_id=task_id,
                project_id=project_id,
                stage=stage,
                event_type=event_type,
                content=content,
                summary=summary,
                metadata=payload,
                paths=self._archive_paths_from_payload(payload, task_semantics),
                tags=[str(payload.get("task_mode") or ""), str(payload.get("workflow_id") or "")],
                returncode=returncode if isinstance(returncode, int) else None,
                task_semantics=task_semantics,
                source_refs=[f"task:{task_id}"],
            )
        except Exception as e:
            logger.warning(f"SessionArchive observe failed at {stage}: {e}")

    async def _finalize_task_memory(
        self,
        task_id: str,
        status: str,
        messages: Optional[List[BaseMessage]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload = dict(payload or {})
        payload["status"] = status
        payload["event_type"] = "task_finalized"
        output_result = (
            payload.get("solution")
            or payload.get("result")
            or payload.get("final_result")
            or payload.get("reason")
            or payload.get("decision")
            or ""
        )
        try:
            inferred = infer_task_semantics(
                task_id=task_id,
                stage="finalize",
                messages=messages or [],
                payload=payload,
            )
            output_artifacts = self._archive_paths_from_payload(payload, inferred)
        except Exception:
            output_artifacts = []
        self.profiler.record_final_result(
            task_id,
            status=status,
            output_result=str(output_result or ""),
            output_artifacts=output_artifacts,
            node="finalize",
        )
        try:
            await self._observe_session_memory(
                task_id=task_id,
                stage="finalize",
                messages=messages or [],
                payload=payload,
            )
            self.task_summary_renderer.write_task_summary(task_id, fallback_status=status)
            await self._maybe_auto_wiki_ingest_after_finalize(task_id, status)
        except Exception as e:
            logger.warning(f"Task memory finalize failed for {task_id}: {e}")

    async def _maybe_auto_wiki_ingest_after_finalize(self, task_id: str, status: str) -> None:
        if not self.wiki_enabled or self.wiki_ingest_mode != "auto" or status != "completed":
            return
        project_id = self.default_project_id
        key = f"{project_id}:{task_id}"
        if key in self._wiki_auto_ingest_done:
            return
        self._wiki_auto_ingest_done.add(key)
        try:
            pending_count = self.session_archive.pending_for_wiki_count(
                project_id=project_id,
                task_id=task_id,
            )
            if pending_count <= 0:
                return
            result = await self.wiki_memory_manager.ingest_from_archive(
                self.session_archive,
                project_id=project_id,
                task_id=task_id,
                limit=max(1, pending_count),
            )
            logger.info("Auto wiki ingest after %s: %s", task_id, result)
        except Exception as e:
            logger.warning(f"Auto wiki ingest failed for {task_id}: {e}")

    async def _maybe_offer_wiki_ingest(self, task_id: str, project_id: Optional[str] = None) -> None:
        if not self.wiki_enabled or self.wiki_ingest_mode != "ask":
            return
        project_id = project_id or self.default_project_id
        key = f"{project_id}:{task_id}"
        if key in self._wiki_ingest_offer_done:
            return
        self._wiki_ingest_offer_done.add(key)
        try:
            task_pending = self.session_archive.pending_for_wiki_count(
                project_id=project_id,
                task_id=task_id,
            )
            if task_pending <= 0:
                return
            project_pending = self.session_archive.pending_for_wiki_count(project_id=project_id)
        except Exception as e:
            logger.warning(f"Wiki ingest pending check failed for {task_id}: {e}")
            return

        prompt = (
            f"\n{Fore.CYAN}Wiki memory: {task_pending} pending event(s) from this task "
            f"({project_pending} pending in project). Ingest this task into Wiki now? [y/N] > "
            f"{Style.RESET_ALL}"
        )
        payload = {
            "type": "wiki_ingest_offer",
            "task_id": task_id,
            "project_id": project_id,
            "task_pending": task_pending,
            "project_pending": project_pending,
            "mode": self.wiki_ingest_mode,
        }
        wait_started = time.perf_counter()
        wait_started_at = timestamp_now()
        try:
            response = await self._aprompt_user(
                task_id,
                "wiki_ingest_offer",
                prompt,
                payload=payload,
                started_at=wait_started_at,
            )
        except HumanInputUnavailable as exc:
            self._record_human_wait(
                task_id,
                "wiki_ingest_offer",
                wait_started,
                f"<waiting_user:{type(exc).__name__}>",
                payload,
                started_at=wait_started_at,
            )
            return
        self._record_human_wait(
            task_id,
            "wiki_ingest_offer",
            wait_started,
            response,
            payload,
            started_at=wait_started_at,
        )
        if response.strip().lower() not in {"y", "yes"}:
            print(f"{Fore.CYAN}Wiki ingest skipped. Run /wiki ingest later, or set BIOMIND_WIKI_INGEST_MODE=manual/auto.{Style.RESET_ALL}")
            return
        result = await self.wiki_memory_manager.ingest_from_archive(
            self.session_archive,
            project_id=project_id,
            task_id=task_id,
            limit=max(1, task_pending),
        )
        output.page_text("Wiki Ingest", result, Fore.CYAN)

    async def _task_summary_prompt_context(self, task_id: str, query: str) -> Dict[str, str]:
        try:
            return await self.task_summary_renderer.build_prompt_context_async(task_id, query)
        except Exception as e:
            logger.warning(f"Task summary retrieval failed for {task_id}: {e}")
            return {"long_term_memory": "", "task_memory": "", "related_memory": ""}

    def _task_summary_resume_context(self, task_id: str) -> str:
        try:
            task_memory = self.task_summary_renderer.read_task_summary(task_id)
        except Exception as e:
            logger.warning(f"Task summary resume read failed for {task_id}: {e}")
            return ""
        if not task_memory.strip():
            return ""
        return (
            "**Task Summary (derived resume context):**\n"
            f"{task_memory}\n\n"
            "Use this task summary first, then use execution records below as supporting evidence.\n"
        )

    @staticmethod
    def _archive_paths_from_payload(payload: Dict[str, Any], task_semantics: Dict[str, Any]) -> List[str]:
        paths: List[str] = []
        for key in ("paths", "path", "output_path", "output_paths", "output_file", "artifact", "artifacts", "plan_file", "script_file"):
            value = payload.get(key)
            if isinstance(value, (list, tuple, set)):
                paths.extend(str(item) for item in value if str(item).strip())
            elif str(value or "").strip():
                paths.append(str(value))
        paths.extend(str(item) for item in task_semantics.get("output_artifacts", []) if str(item).strip())
        seen: set[str] = set()
        ordered: List[str] = []
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            ordered.append(path)
        return ordered

    def _load_agents_md(self, work_dir: str) -> str:
        try:
            start = Path(work_dir).expanduser().resolve()
        except Exception:
            start = Path(work_dir).expanduser()
        if start.is_file():
            start = start.parent

        candidates: List[Path] = []
        for folder in [start, *start.parents]:
            path = folder / "AGENTS.md"
            if path.exists() and path.is_file():
                candidates.append(path)

        if not candidates:
            return ""

        blocks = []
        for path in reversed(candidates):
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            blocks.append(f"### {path}\n{content.strip()}")
        return self._clip_prompt_text("\n\n".join(blocks), 20_000)

    def _user_preferences_path(self) -> Path:
        configured = os.getenv("BIOMIND_USER_PREFERENCES", "").strip()
        if configured:
            path = Path(configured).expanduser()
            if not path.is_absolute():
                path = current_dir / path
            return path.resolve()
        return (current_dir / "user_preferences.md").resolve()

    def _load_user_preferences(self) -> str:
        path = self._user_preferences_path()
        if not path.exists() or not path.is_file():
            return ""
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        content = content.strip()
        if not content:
            return ""
        return self._clip_prompt_text(f"### {path}\n{content}", 12_000)

    def _get_or_create_stable_prompt(self, session_id: str, project_id: Optional[str] = None) -> Dict[str, str]:
        project_id = project_id or self.default_project_id
        cache_key = f"{session_id}::{project_id}"
        cached = self._stable_prompt_cache.get(cache_key)
        if cached:
            return cached

        prompt_path, meta_path = self._stable_prompt_paths(session_id, project_id)
        if prompt_path.exists():
            prompt = prompt_path.read_text(encoding="utf-8")
            meta = self._read_json_file(meta_path)
            record = {
                "prompt": prompt,
                "session_id": session_id,
                "project_id": str(meta.get("project_id") or project_id),
                "core_memory_loaded_at": str(meta.get("core_memory_loaded_at") or "unknown"),
                "prompt_path": str(prompt_path),
            }
            self._stable_prompt_cache[cache_key] = record
            return record

        work_dir = str(self.pm.work_dir if self.pm else self.data_path)
        snapshot = self.core_memory_store.load_snapshot(project_id, snapshot_id=session_id)
        core_memory = snapshot.prompt_text()
        agents_md = self._load_agents_md(work_dir)
        user_preferences = self._load_user_preferences()
        prompt = PromptManager.build_stable_system_prompt(
            package_manager=self.pm,
            work_dir=work_dir,
            core_memory=core_memory,
            agents_md_context=agents_md,
            user_preferences_context=user_preferences,
        )
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        meta = {
            "session_id": session_id,
            "project_id": project_id,
            "core_memory_loaded_at": snapshot.loaded_at,
            "work_dir": work_dir,
            "user_preferences_path": str(self._user_preferences_path()),
            "user_preferences_loaded": bool(user_preferences),
            "prompt_path": str(prompt_path),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        record = {
            "prompt": prompt,
            "session_id": session_id,
            "project_id": project_id,
            "core_memory_loaded_at": snapshot.loaded_at,
            "prompt_path": str(prompt_path),
        }
        self._stable_prompt_cache[cache_key] = record
        return record

    def _archive_prompt_context(
        self,
        task_id: Optional[str],
        query: str,
        project_id: Optional[str] = None,
        limit: int = 4,
    ) -> str:
        if not query.strip():
            return ""
        try:
            hits = self.session_archive.search(
                query,
                task_id=task_id or None,
                project_id=project_id,
                limit=limit,
            )
            return self.session_archive.format_hits(hits, title="Retrieved Session Archive")
        except Exception as e:
            logger.warning(f"Session archive retrieval failed for {task_id or 'project'}: {e}")
            return ""

    def _pending_archive_buffer_context(
        self,
        task_id: Optional[str],
        query: str,
        project_id: Optional[str] = None,
        limit: int = 4,
    ) -> str:
        if not self.wiki_enabled or not query.strip():
            return ""
        project_id = project_id or self.default_project_id
        try:
            hits = self.session_archive.search_pending_for_wiki(
                query,
                task_id=task_id or None,
                project_id=project_id,
                limit=limit,
            )
            if len(hits) < limit:
                seen_ids = {hit.id for hit in hits}
                for hit in self.session_archive.search_pending_for_wiki(
                    query,
                    project_id=project_id,
                    limit=limit * 2,
                ):
                    if hit.id in seen_ids:
                        continue
                    hits.append(hit)
                    seen_ids.add(hit.id)
                    if len(hits) >= limit:
                        break
            if not hits:
                return ""
            context = self.session_archive.format_hits(
                hits,
                title="Pending SessionArchive Buffer (not yet Wiki-ingested)",
            )
            return (
                "<session_archive_buffer_policy>\n"
                "- This buffer is only for the current task or recent events waiting for Wiki curation.\n"
                "- Prefer Wiki claims for durable facts; use these raw events only to avoid losing just-finished context.\n"
                "- Do not treat TaskSummary reports as factual sources.\n"
                "</session_archive_buffer_policy>\n\n"
                f"{self._clip_prompt_text(context, 2400)}"
            )
        except Exception as e:
            logger.warning(f"Pending SessionArchive buffer retrieval failed for {task_id or 'project'}: {e}")
            return ""

    def _artifact_index_context(self, task_id: Optional[str], query: str, limit: int = 6) -> str:
        if not self.artifact_index or not query.strip():
            return ""
        try:
            hits = self.artifact_index.search_artifacts(query, task_id=task_id or None, limit=limit)
            if len(hits) < limit:
                seen_ids = {hit.id for hit in hits}
                for hit in self.artifact_index.search_artifacts(query, limit=limit * 2):
                    if hit.id in seen_ids:
                        continue
                    hits.append(hit)
                    seen_ids.add(hit.id)
                    if len(hits) >= limit:
                        break
            if not hits:
                return ""
            return (
                "<artifact_index_policy>\n"
                "- This artifact index is retrieved only when Generate appears to be missing an input or output path.\n"
                "- Verify paths on disk before reusing them; artifact records may be stale.\n"
                "</artifact_index_policy>\n\n"
                f"{self._clip_prompt_text(self.artifact_index.format_hits(hits, title='Retrieved Artifact Index'), 2400)}"
            )
        except Exception as e:
            logger.warning(f"Artifact index retrieval failed for {task_id or 'project'}: {e}")
            return ""

    @staticmethod
    def _empty_task_summary_context() -> Dict[str, str]:
        return {"long_term_memory": "", "task_memory": "", "related_memory": ""}

    @staticmethod
    def _recent_observation_text(messages: List[BaseMessage], max_chars: int = 2400) -> str:
        parts: List[str] = []
        for msg in reversed(messages[-10:]):
            content = str(getattr(msg, "content", msg) or "")
            if (
                "<observation>" in content
                or "<supervisor_feedback" in content
                or "supervisor" in content.lower()
                or "❌" in content
            ):
                parts.append(content)
            if len(parts) >= 3:
                break
        return "\n\n".join(reversed(parts))[-max_chars:]

    @staticmethod
    def _latest_human_text(messages: List[BaseMessage], max_chars: int = 1600) -> str:
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                return str(getattr(msg, "content", "") or "")[-max_chars:]
        return ""

    @staticmethod
    def _looks_like_failed_or_retry_context(text: str) -> bool:
        lower = str(text or "").lower()
        return "❌" in str(text or "") or any(
            term in lower
            for term in (
                "failed",
                "failure",
                "traceback",
                "exception",
                "error:",
                "returncode",
                "failed validation",
                "retry",
                "warn",
                "报错",
                "失败",
                "错误",
            )
        )

    @staticmethod
    def _supervisor_warn_requires_followup(messages: List[BaseMessage]) -> bool:
        """Detect WARN reviews that are actually unfinished-step instructions."""
        text = "\n".join(str(getattr(msg, "content", msg) or "") for msg in messages)
        lower = text.lower()
        if "verdict" not in lower or "warn" not in lower:
            return False
        followup_action = re.search(
            r"\baction\s*:\s*(?:execute|run|rerun|re-run|inspect|verify|check|proceed|continue|repair)\b",
            lower,
        )
        unfinished_signal = any(
            phrase in lower
            for phrase in (
                "only created",
                "without running",
                "no actual",
                "not performed",
                "not generated",
                "incomplete",
                "truncated",
                "before accepting",
                "before treating",
                "proceed to the next planned analysis step",
                "execute the generated",
                "run the preprocess",
                "inspect the output",
                "validate its scientific outputs",
                "未真正执行",
                "没有真正执行",
                "尚未执行",
                "继续执行",
                "执行生成的",
            )
        )
        return bool(followup_action and unfinished_signal)

    @staticmethod
    def _supervisor_warn_is_acceptably_artifact_bounded(
        messages: List[BaseMessage],
        plan_state: Dict[str, Any],
        step_gate: Any,
    ) -> bool:
        """Allow artifact-bounded WARNs when deterministic gates already passed.

        Some LLM reviews ask for another inspection because a terminal preview
        was visually truncated or a compact TSV is small. If the active step is
        the final/status/report artifact step and CompletionGate already verified
        the step contract, do not convert that advisory WARN into an endless
        RETRY loop.
        """
        if not getattr(step_gate, "passed", False):
            return False
        step = BioAnalystAgent._running_plan_step(plan_state)
        title = str(step.get("title") or "").lower()
        if not re.search(r"report|reproduce|stage_status|status|summary|final|artifact|output|报告|状态|可复现|总结", title):
            return False
        text = "\n".join(str(getattr(msg, "content", msg) or "") for msg in messages).lower()
        advisory_terms = ("inspect", "verify", "check", "before accepting", "truncated", "incomplete", "empty")
        hard_terms = ("exit code", "traceback", "no such file", "does not exist", "command not found", "a user error")
        return any(term in text for term in advisory_terms) and not any(term in text for term in hard_terms)

    @staticmethod
    def _looks_like_missing_input_context(text: str) -> bool:
        lower = str(text or "").lower()
        missing_terms = (
            "no such file",
            "does not exist",
            "cannot find",
            "cannot access",
            "not found",
            "missing input",
            "missing path",
            "找不到",
            "不存在",
            "缺少输入",
            "路径缺失",
        )
        path_terms = (
            "path",
            "file",
            "input",
            "artifact",
            "output",
            "rds",
            "fastq",
            "bam",
            "vcf",
            "csv",
            "tsv",
            "路径",
            "文件",
            "输入",
            "产物",
        )
        return any(term in lower for term in missing_terms) and any(term in lower for term in path_terms)

    @staticmethod
    def _looks_like_evidence_query(text: str) -> bool:
        lower = str(text or "").lower()
        return any(
            term in lower
            for term in (
                "evidence",
                "source",
                "citation",
                "claim",
                "why",
                "basis",
                "依据",
                "证据",
                "来源",
                "引用",
                "为什么",
                "解释",
            )
        )

    async def _generate_on_demand_memory_contexts(
        self,
        state: AgentState,
        messages: List[BaseMessage],
        task_id: str,
        project_id: str,
        query: str,
        memory_packet_context: str,
        supervisor_verdict: str,
    ) -> Dict[str, Any]:
        """Return only memory contexts required by the current Generate turn."""
        contexts: Dict[str, Any] = {
            "task_summary_context": self._empty_task_summary_context(),
            "archive_context": "",
            "wiki_context": "",
            "wiki_digest": "",
        }
        recent_observation = self._recent_observation_text(messages)
        latest_human = self._latest_human_text(messages)
        trigger_text = "\n\n".join(
            part for part in [query, recent_observation, latest_human] if str(part).strip()
        )

        recovery_trigger = (
            supervisor_verdict in {"RETRY", "WARN"}
            or self._looks_like_failed_or_retry_context(recent_observation)
        )
        missing_input = self._looks_like_missing_input_context(trigger_text)
        evidence_query = self._looks_like_evidence_query(latest_human)

        archive_parts: List[str] = []
        if recovery_trigger:
            recovery_query = "\n".join(
                part
                for part in [
                    "similar error history recovery fix",
                    supervisor_verdict,
                    trigger_text,
                ]
                if str(part).strip()
            )
            archive_context = self._archive_prompt_context(
                None,
                recovery_query,
                project_id=project_id,
                limit=4,
            )
            if archive_context.strip():
                archive_parts.append(
                    "<error_recovery_memory>\n"
                    f"{self._clip_prompt_text(archive_context, 2600)}\n"
                    "</error_recovery_memory>"
                )

        if missing_input:
            artifact_context = self._artifact_index_context(task_id, trigger_text, limit=6)
            if artifact_context.strip():
                archive_parts.append(f"<artifact_lookup>\n{artifact_context}\n</artifact_lookup>")
            pending_context = self._pending_archive_buffer_context(
                task_id=task_id,
                project_id=project_id,
                query=trigger_text,
                limit=4,
            )
            if pending_context.strip():
                archive_parts.append(
                    "<missing_input_archive_lookup>\n"
                    f"{self._clip_prompt_text(pending_context, 2400)}\n"
                    "</missing_input_archive_lookup>"
                )

        if archive_parts:
            contexts["archive_context"] = (
                "<on_demand_runtime_memory_policy>\n"
                "- Generate normally receives only step-local context.\n"
                "- The sections below were retrieved because this turn matched an error-recovery or missing-input trigger.\n"
                "- Use them only to repair the current step; do not treat raw events as durable facts without verification.\n"
                "</on_demand_runtime_memory_policy>\n\n"
                + "\n\n".join(archive_parts)
            )

        if self.wiki_enabled and evidence_query:
            wiki_context = self._wiki_prompt_context(task_id, project_id, trigger_text)
            if wiki_context.strip():
                contexts["wiki_context"] = wiki_context
            contexts["wiki_digest"] = self._wiki_digest_prompt_context(project_id, max_chars=2000)
        elif self.wiki_enabled and not memory_packet_context.strip() and state.get("wiki_memory_context"):
            contexts["wiki_context"] = str(state.get("wiki_memory_context") or "")

        return contexts

    def _archive_resume_context(self, task_id: str) -> str:
        try:
            hits = self.session_archive.resume_events(task_id=task_id, limit=8)
            if not hits:
                return ""
            return self.session_archive.format_hits(hits, title="Retrieved Session Archive for Resume")
        except Exception as e:
            logger.warning(f"Session archive resume retrieval failed for {task_id}: {e}")
            return ""

    def _build_resume_prompt(self, task_id: str, user_prompt: Any = "") -> str:
        parts = []
        task_resume = self._task_summary_resume_context(task_id)
        if task_resume.strip():
            parts.append(task_resume.strip())
        if self.state_manager:
            state_resume = self.state_manager.resume_task(task_id)
            if str(state_resume).strip():
                parts.append(str(state_resume).strip())
        archive_resume = self._archive_resume_context(task_id)
        if archive_resume.strip():
            parts.append(archive_resume.strip())
        if str(user_prompt).strip():
            parts.append(str(user_prompt).strip())
        return "\n\n".join(parts)

    def _persistent_task_record(self, task_id: str) -> Dict[str, Any]:
        if not self.state_manager:
            return {}
        try:
            context = self.state_manager.get_task_context(task_id)
        except Exception:
            return {}
        task_info = context.get("task_info") if isinstance(context, dict) else {}
        return dict(task_info) if isinstance(task_info, dict) else {}

    @staticmethod
    def _business_waiting_state(waiting: Dict[str, Any]) -> bool:
        return bool(waiting and str(waiting.get("stage") or "") not in {"", "interactive_prompt"})

    @staticmethod
    def _recovery_state_is_resolved(recovery_state: Dict[str, Any]) -> bool:
        """Return whether durable recovery state supersedes stale checkpoint retry data."""
        phase = str((recovery_state or {}).get("phase") or "").strip().lower()
        status = str((recovery_state or {}).get("status") or "").strip().lower()
        return phase in {"resolved", "success", "completed", "cleared"} or status in {
            "resolved", "success", "completed", "cleared",
        }

    def _resolve_resume_node(self, task_id: str, values: Optional[Dict[str, Any]] = None) -> Tuple[str, Dict[str, Any]]:
        """Resolve recovery from durable business state, never from failure_stage alone."""
        values = dict(values or {})
        plan_state = load_plan_state(self.data_path, task_id)
        task_state = dict(plan_state.get("task_state") or {})
        recovery_store = RecoveryStateStore(self.data_path, task_id)
        persisted_recovery = recovery_store.load_phase()
        recovery_state = dict(task_state.get("recovery_state") or {})
        if persisted_recovery:
            recovery_state.update(persisted_recovery)
        task_record = self._persistent_task_record(task_id)
        task_spec = ensure_task_spec(
            values.get("task_spec") or task_state.get("task_spec") or plan_state.get("task_spec"),
            str(task_record.get("user_query") or ""),
            task_id=task_id,
        )
        task_revision = max(
            task_spec_revision(task_spec),
            int(plan_state.get("generated_from_task_revision") or 0),
        )
        selected_skills = list(
            values.get("selected_skills")
            or plan_state.get("selected_skill_ids")
            or [
                skill_id
                for step in plan_state.get("steps", []) or []
                if isinstance(step, dict)
                for skill_id in step.get("skill_ids", []) or []
                if str(skill_id).strip()
            ]
        )
        selected_skills = list(dict.fromkeys(str(item) for item in selected_skills if str(item).strip()))
        waiting = load_waiting_state(self.data_path, task_id)
        pending_question = values.get("pending_human_question") or task_state.get("pending_question") or {}
        pending_action = values.get("pending_action") or task_state.get("pending_action") or {}
        recovery_resolved = self._recovery_state_is_resolved(recovery_state)
        pending_recovery_action = (
            {}
            if recovery_resolved
            else values.get("pending_recovery_action") or recovery_state.get("pending_action") or {}
        )
        validation = validate_plan_for_generate(plan_state, task_revision)
        steps = [step for step in plan_state.get("steps", []) or [] if isinstance(step, dict)]
        statuses = {str(step.get("status") or "").lower() for step in steps}
        durable_task_status = task_status(plan_state)
        recovery_phase = str(recovery_state.get("phase") or "").lower()
        recovery_action_type = str(pending_recovery_action.get("type") or "").lower() if isinstance(pending_recovery_action, dict) else ""
        active_or_pending = bool(statuses & {
            "pending", "ready", "running", "diagnosing", "repairing", "retry_pending"
        })

        if durable_task_status in {TASK_STATUS_BLOCKED, TASK_STATUS_FAILED, TASK_STATUS_CANCELLED}:
            node = "finish"
            reason = "legacy_terminal_state" if active_or_pending else f"task_{durable_task_status}"
        elif recovery_phase in {"blocked", "exhausted"} or recovery_action_type == "blocked":
            node = "finish"
            reason = "legacy_terminal_recovery_with_active_steps" if active_or_pending else "recovery_terminal_without_new_evidence"
        elif durable_task_status == TASK_STATUS_COMPLETED:
            node = "finish"
            reason = "task_already_terminal"
        elif pending_action and not self._validate_pending_action_contract(
            pending_action,
            {**values, "task_spec": task_spec, "task_revision": task_revision},
            task_id,
        ):
            node = "execute"
            reason = "fresh_pending_action"
        elif pending_question:
            node = "ask_user"
            reason = "pending_user_question"
        elif self._business_waiting_state(waiting):
            node = "waiting_user"
            reason = "saved_human_input_required"
        elif str(recovery_state.get("status") or "") == "PROMPT_ASSEMBLY_FAILURE":
            node = "finish"
            reason = "prompt_assembly_failure_blocked"
        elif recovery_phase in {
            "capture", "unwrap", "diagnose", "probe", "plan_repair", "apply_repair", "rerun", "verify", "rollback"
        } or pending_recovery_action:
            node = "generate"
            reason = "resume_persisted_local_recovery"
        elif str(task_record.get("status") or "") == "completed":
            node = "finish"
            reason = "task_already_terminal"
        elif steps and statuses and statuses <= {"done", "passed", "skipped", "skipped_upstream_blocked", "cancelled"}:
            node = "finish"
            reason = "task_already_terminal"
        elif validation.get("valid"):
            node = "generate"
            reason = "approved_valid_plan"
        elif steps and validation.get("reason_code") == "plan_not_approved":
            node = "plan_review"
            reason = "plan_requires_approval"
        elif str(task_spec.get("goal") or "").strip() and selected_skills:
            node = "planner"
            reason = "task_spec_and_skills_without_valid_plan"
        else:
            node = "consultant"
            reason = "task_requires_consultation"

        failure = plan_state.get("failure") if isinstance(plan_state.get("failure"), dict) else {}
        current_step_index, current_step = current_plan_step_with_index(plan_state)
        audit = {
            "task_id": task_id,
            "resume_mode": "business_state",
            "task_status": (
                TASK_STATUS_BLOCKED
                if recovery_phase in {"blocked", "exhausted"} or recovery_action_type == "blocked"
                else durable_task_status
            ),
            "task_revision": task_revision,
            "plan_revision": int(plan_state.get("plan_revision") or 0),
            "plan_status": str(plan_state.get("plan_status") or ""),
            "approved_plan_hash": str(plan_state.get("approved_plan_hash") or ""),
            "current_step_id": str(current_step.get("step_id") or ""),
            "current_step_index": current_step_index,
            "selected_skill_ids": selected_skills,
            "resolved_input_count": len(task_spec.get("inputs", []) or []),
            "failure_stage": str(failure.get("failure_stage") or values.get("failure_stage") or ""),
            "recovery_phase": str(recovery_state.get("phase") or ""),
            "recovery_status": str(recovery_state.get("status") or ""),
            "resolved_resume_node": node,
            "resume_reason": reason,
            "plan_validation": validation,
        }
        return node, audit

    def _build_resume_runner_state(
        self,
        task_id: str,
        user_prompt: Any = "",
        checkpoint_values: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        values = dict(checkpoint_values or {})
        plan_state = load_plan_state(self.data_path, task_id)
        task_state = dict(plan_state.get("task_state") or {})
        recovery_store = RecoveryStateStore(self.data_path, task_id)
        persisted_recovery = recovery_store.load_phase()
        recovery_state = dict(task_state.get("recovery_state") or {})
        if persisted_recovery:
            recovery_state.update(persisted_recovery)
        recovery_resolved = self._recovery_state_is_resolved(recovery_state)
        task_record = self._persistent_task_record(task_id)
        original_query = str(task_record.get("user_query") or "")
        task_spec = ensure_task_spec(
            values.get("task_spec") or task_state.get("task_spec") or plan_state.get("task_spec"),
            original_query,
            task_id=task_id,
        )
        task_revision = max(task_spec_revision(task_spec), int(plan_state.get("generated_from_task_revision") or 0))
        selected_skills = list(values.get("selected_skills") or task_state.get("selected_skills") or plan_state.get("selected_skill_ids") or [])
        if not selected_skills:
            selected_skills = list(dict.fromkeys(
                str(skill_id)
                for step in plan_state.get("steps", []) or []
                if isinstance(step, dict)
                for skill_id in step.get("skill_ids", []) or []
                if str(skill_id).strip()
            ))
        plan_file = Path(str(plan_state.get("plan_file") or default_plan_file(self.data_path, task_id)))
        try:
            current_plan = plan_file.read_text(encoding="utf-8")
        except OSError:
            current_plan = str(values.get("current_plan") or "")
        node, audit = self._resolve_resume_node(
            task_id,
            {
                **values,
                "task_spec": task_spec,
                "task_revision": task_revision,
                "selected_skills": selected_skills,
            },
        )
        project_id = str(values.get("project_id") or self.default_project_id)
        stable_record = self._get_or_create_stable_prompt(task_id, project_id)
        resume_message = str(user_prompt or "").strip()
        messages = [] if values.get("messages") else list(values.get("messages") or [])
        if not messages:
            if not values.get("messages"):
                messages = [HumanMessage(content=str(task_spec.get("goal") or original_query or resume_message))]
        messages.append(SystemMessage(content=f"<resume_audit>\n{json.dumps(audit, ensure_ascii=False)}\n</resume_audit>"))
        logger.info("Resume state resolved: %s", json.dumps(audit, ensure_ascii=False, sort_keys=True))
        runner_state = {
            **values,
            "messages": messages,
            "task_id": task_id,
            "session_id": task_id,
            "project_id": project_id,
            "stable_system_prompt": stable_record["prompt"],
            "core_memory_loaded_at": stable_record["core_memory_loaded_at"],
            "task_mode": str(values.get("task_mode") or "managed"),
            "task_spec": task_spec,
            "task_revision": task_revision,
            "current_plan": current_plan,
            "plan_progress": render_plan_progress(plan_state),
            "selected_skills": selected_skills,
            "selected_skills_raw": list(values.get("selected_skills_raw") or selected_skills),
            "active_skills": list(values.get("active_skills") or []),
            "runtime_status": values.get("runtime_status") or self._runtime_status_for_task(task_id),
            "runtime_audit": values.get("runtime_audit") or task_state.get("runtime_audit") or [],
            "input_audit": values.get("input_audit") or task_state.get("input_audit") or {},
            "pending_action": values.get("pending_action") or {},
            "pending_human_question": values.get("pending_human_question") or task_state.get("pending_question") or {},
            "pending_recovery_action": (
                {}
                if recovery_resolved
                else values.get("pending_recovery_action") or recovery_state.get("pending_action") or {}
            ),
            "recovery_phase": (
                None if recovery_resolved else recovery_state.get("phase") or values.get("recovery_phase")
            ),
            "recovery_context": (
                {} if recovery_resolved else values.get("recovery_context") or recovery_store.load_context()
            ),
            "diagnostic_summary": (
                {} if recovery_resolved else values.get("diagnostic_summary") or recovery_store.load_diagnostic_summary()
            ),
            "resume_mode": "business_state",
            "resolved_resume_node": node,
            "resume_audit": audit,
            "recovery_status": "resuming" if recovery_resolved else recovery_state.get("status") or "resuming",
            "next_step": node,
        }
        if recovery_resolved and audit.get("resume_reason") == "approved_valid_plan":
            runner_state.update({
                "supervisor_verdict": "",
                "last_supervisor_feedback": {},
                "retry_count": 0,
                "recovery_decision": {},
                "forbidden_recovery_actions": [],
                "observation": "",
                "execution_evidence": {},
                "failure_stage": "",
                "failure_owner": "",
                "error_analysis": "",
            })
            for field in SUPERVISOR_RETRY_FIELDS:
                runner_state[field] = ""
        return runner_state

    def _continue_control_state(self, task_id: str) -> Dict[str, Any]:
        """Classify plain 'continue' without turning it into task content."""
        task_record = self._persistent_task_record(task_id)
        has_plan = plan_state_path(self.data_path, task_id).exists()
        if not task_record and not has_plan:
            return {"action": "new_task"}
        waiting = load_waiting_state(self.data_path, task_id)
        if self._business_waiting_state(waiting):
            return {"action": "waiting_user", "waiting": waiting}
        resume_node, audit = self._resolve_resume_node(task_id)
        if str(task_record.get("status") or "") == "completed" or resume_node == "finish":
            return {"action": "completed", "resume_audit": audit}
        return {"action": "resume", "resume_audit": audit}

    def _wiki_disabled_message(self) -> str:
        return (
            "Wiki memory is disabled (BIOMIND_WIKI_ENABLED=false). "
            "BioMind will use SessionArchive, TaskSummary, Core memory, and verified filesystem evidence instead."
        )

    def _wiki_prompt_context(self, task_id: str, project_id: str, query: str) -> str:
        if not self.wiki_enabled or not query.strip():
            return ""
        try:
            return self.wiki_memory_manager.build_prompt_context(
                task_id=task_id,
                project_id=project_id,
                query=query,
            )
        except Exception as e:
            logger.warning(f"Wiki memory retrieval failed for {task_id}: {e}")
            return ""

    def _wiki_memory_stage_context(
        self,
        task_id: str,
        project_id: str,
        query: str,
        digest_chars: int = 2_000,
        context_chars: int = 4_000,
    ) -> str:
        if not self.wiki_enabled:
            return ""
        parts = []
        digest = self._wiki_digest_prompt_context(project_id, max_chars=digest_chars)
        if digest:
            parts.append(f"<wiki_digest>\n{digest}\n</wiki_digest>")
        wiki_context = self._wiki_prompt_context(task_id, project_id, query)
        if wiki_context:
            parts.append(f"<wiki_context>\n{self._clip_prompt_text(wiki_context, context_chars)}\n</wiki_context>")
        if not parts:
            return ""
        policy = (
            "<wiki_memory_policy>\n"
            "- wiki_purpose is direction-setting context, not factual evidence.\n"
            "- Wiki claims with evidence are preferred for project facts, dataset paths, methods, parameters, and conclusions.\n"
            "- If Wiki and Core memory conflict, verify filesystem/evidence and prefer current input/latest observations over stale memory.\n"
            "- This context is read-only; do not compile or write wiki pages during consultant/planner/generate.\n"
            "</wiki_memory_policy>"
        )
        return policy + "\n\n" + "\n\n".join(parts)

    async def _consultant_memory_retrieval_context(
        self,
        task_id: str,
        project_id: str,
        query: str,
        wiki_context: str,
    ) -> str:
        """Build the full retrieval bundle shown only to Consultant."""
        sections: List[str] = [
            "<memory_broker_policy>\n"
            "- Consultant is the only broad automatic memory retrieval stage for managed tasks.\n"
            "- Curate relevant facts into the FINISH handoff; do not copy all retrieved text forward.\n"
            "- Planner and Generate should consume the curated handoff packet and request targeted retrieval only when information is missing.\n"
            "</memory_broker_policy>"
        ]
        if wiki_context.strip():
            sections.append(f"<wiki_memory_retrieval>\n{wiki_context}\n</wiki_memory_retrieval>")

        try:
            task_context = await self._task_summary_prompt_context(task_id, query)
        except Exception as exc:
            logger.warning("Consultant task memory retrieval failed for %s: %s", task_id, exc)
            task_context = {"long_term_memory": "", "task_memory": "", "related_memory": ""}

        task_memory = str(task_context.get("task_memory") or "").strip()
        related_memory = str(task_context.get("related_memory") or "").strip()
        if task_memory:
            sections.append(
                "<task_summary_retrieval>\n"
                f"{self._clip_prompt_text(task_memory, 1800)}\n"
                "</task_summary_retrieval>"
            )
        if related_memory:
            sections.append(
                "<task_related_archive_retrieval>\n"
                f"{self._clip_prompt_text(related_memory, 1800)}\n"
                "</task_related_archive_retrieval>"
            )

        archive_context = self._archive_prompt_context(
            None,
            query,
            project_id=project_id,
            limit=4,
        )
        if archive_context.strip():
            sections.append(
                "<project_session_archive_retrieval>\n"
                f"{self._clip_prompt_text(archive_context, 2400)}\n"
                "</project_session_archive_retrieval>"
            )

        return self._clip_prompt_text("\n\n".join(sections), 10_000)

    def _consultant_handoff_memory_packet(
        self,
        consultant_summary: str,
        retrieval_context: str,
    ) -> str:
        """Create the compact cross-stage memory packet after Consultant curates retrieval."""
        source_refs = self._memory_source_refs(retrieval_context)
        ref_lines = "\n".join(f"- {ref}" for ref in source_refs) if source_refs else "- none"
        return (
            "<consultant_memory_packet>\n"
            "<memory_packet_policy>\n"
            "- This packet is the primary memory handoff from Consultant to Planner and Generate.\n"
            "- It is curated primarily from Wiki and source SessionArchive evidence; TaskSummary is report/cache only.\n"
            "- If details are missing, request targeted retrieval instead of repeating broad automatic recall.\n"
            "</memory_packet_policy>\n\n"
            "<consultant_curated_handoff>\n"
            f"{self._clip_prompt_text(consultant_summary, 5000)}\n"
            "</consultant_curated_handoff>\n\n"
            "<memory_source_refs>\n"
            f"{ref_lines}\n"
            "</memory_source_refs>\n"
            "</consultant_memory_packet>"
        )

    @staticmethod
    def _memory_source_refs(text: str, limit: int = 24) -> List[str]:
        patterns = [
            r"\b(?:session_archive|task|artifact|raw):[A-Za-z0-9_.:/\\-]+",
            r"\[([A-Za-z0-9_.:/\\-]+\.md)\]",
        ]
        refs: List[str] = []
        seen: set[str] = set()
        for pattern in patterns:
            for match in re.finditer(pattern, str(text or "")):
                ref = match.group(1) if match.groups() else match.group(0)
                ref = ref.strip()
                if not ref or ref in seen:
                    continue
                seen.add(ref)
                refs.append(ref)
                if len(refs) >= limit:
                    return refs
        return refs

    def _wiki_digest_prompt_context(self, project_id: str, max_chars: int = 4_000) -> str:
        if not self.wiki_enabled:
            return ""
        try:
            digest_path = self.wiki_memory_manager.digest_path(project_id)
            if not digest_path.exists():
                return ""
            mtime = digest_path.stat().st_mtime
            cache_key = str(digest_path)
            cached = self._wiki_digest_cache.get(cache_key)
            if cached and cached[0] == mtime:
                return cached[1]

            data = json.loads(digest_path.read_text(encoding="utf-8"))
            text = self._format_wiki_digest(data)
            clipped = self._clip_prompt_text(text, max_chars)
            self._wiki_digest_cache[cache_key] = (mtime, clipped)
            return clipped
        except Exception as e:
            logger.warning(f"Wiki digest load failed for project {project_id}: {e}")
            return ""

    @staticmethod
    def _format_wiki_digest(data: Dict[str, Any]) -> str:
        page_types = data.get("page_types") if isinstance(data.get("page_types"), dict) else {}
        claims = data.get("high_confidence_claims")
        if not isinstance(claims, list):
            claims = []
        lines = [
            f"Project: {data.get('project_id', 'unknown')}",
            f"Updated: {data.get('updated_at', 'unknown')}",
            f"Pages: {data.get('page_count', 0)} | Claims: {data.get('claim_count', 0)} | Review items: {data.get('review_count', 0)}",
            f"Entities: {data.get('entity_count', 0)} | Concepts: {data.get('concept_count', 0)} | Research queries: {data.get('research_query_count', 0)}",
            "Page types: " + (", ".join(f"{key}={value}" for key, value in sorted(page_types.items())) or "none"),
        ]
        knowledge_pages = data.get("knowledge_pages")
        if isinstance(knowledge_pages, list) and knowledge_pages:
            lines.append("Entity/concept pages:")
            for page in knowledge_pages[:6]:
                if not isinstance(page, dict):
                    continue
                lines.append(f"- {page.get('path', '')}: {page.get('title', '')} ({page.get('type', '')})")
        lines.append("High-confidence claims:")
        if claims:
            for claim in claims[:8]:
                evidence = claim.get("evidence") if isinstance(claim, dict) else []
                if not isinstance(evidence, list):
                    evidence = []
                evidence_text = ", ".join(str(item) for item in evidence[:3]) or "none"
                lines.append(
                    f"- {claim.get('id', 'claim')}: {claim.get('text', '')} "
                    f"(page: {claim.get('page', 'unknown')}; evidence: {evidence_text})"
                )
        else:
            lines.append("- None compiled yet.")
        lines.append("Use wiki search/page retrieval for evidence; this digest is only a compact index.")
        return "\n".join(lines)

    def _ephemeral_prompt_context(
        self,
        state: AgentState,
        messages: List[BaseMessage],
        current_plan: str,
        loaded_skills: str,
        task_summary_context: Dict[str, str],
        memory_packet_context: str,
        wiki_context: str,
        wiki_digest: str,
        archive_context: str,
        supervisor_context: str,
        recovery_context: str = "",
        plan_progress: str = "",
    ) -> str:
        text, _ = self._ephemeral_prompt_context_result(
            state=state,
            messages=messages,
            current_plan=current_plan,
            loaded_skills=loaded_skills,
            task_summary_context=task_summary_context,
            memory_packet_context=memory_packet_context,
            wiki_context=wiki_context,
            wiki_digest=wiki_digest,
            archive_context=archive_context,
            supervisor_context=supervisor_context,
            recovery_context=recovery_context,
            plan_progress=plan_progress,
        )
        return text

    def _ephemeral_prompt_context_result(
        self,
        state: AgentState,
        messages: List[BaseMessage],
        current_plan: str,
        loaded_skills: str,
        task_summary_context: Dict[str, str],
        memory_packet_context: str,
        wiki_context: str,
        wiki_digest: str,
        archive_context: str,
        supervisor_context: str,
        recovery_context: str = "",
        plan_progress: str = "",
    ) -> Tuple[str, Dict[str, Any]]:
        rag_history_list = state.get("rag_history", []) or []
        inherited_rag = str(rag_history_list[-1]) if rag_history_list else ""
        work_dir = str(self.pm.work_dir if self.pm else self.data_path)
        runtime_audit_context = self._format_runtime_audit(
            list(state.get("runtime_audit", []) or [])
        )
        dynamic_tool_context = PromptManager.build_scoped_runtime_context(
            self._clip_prompt_text(runtime_audit_context, 3000),
            self.pm,
            work_dir,
        )
        return PromptManager.build_ephemeral_context_result(
            task_spec_context=self._generate_task_spec_projection(state, messages),
            rag_context=inherited_rag,
            history=messages,
            dynamic_tool_context=dynamic_tool_context,
            loaded_skills_instructions=loaded_skills,
            long_term_memory=task_summary_context.get("long_term_memory", ""),
            task_memory=task_summary_context.get("task_memory", ""),
            related_memory=task_summary_context.get("related_memory", ""),
            memory_packet_context=memory_packet_context,
            wiki_context=wiki_context,
            wiki_digest=wiki_digest,
            archive_context=archive_context,
            supervisor_context=supervisor_context,
            recovery_context=recovery_context,
            current_plan=current_plan,
            plan_progress=plan_progress,
            current_action_context=self._current_action_contract(state, str(state.get("task_id") or "default")),
            max_chars=LLM_STEP_EPHEMERAL_CHARS,
        )

    def _compact_generate_context_result(
        self,
        *,
        state: AgentState,
        messages: List[BaseMessage],
        current_plan: str,
        loaded_skills: str,
        supervisor_context: str,
        recovery_context: str = "",
    ) -> Tuple[str, Dict[str, Any]]:
        """Rebuild Generate from required business state after packing failure."""
        runtime_context = self._format_runtime_audit(list(state.get("runtime_audit", []) or []))
        text, report = PromptManager.build_ephemeral_context_result(
            task_spec_context=self._generate_task_spec_projection(state, messages),
            history=[],
            dynamic_tool_context=runtime_context or "No selected-skill runtime audit is available.",
            loaded_skills_instructions=loaded_skills,
            supervisor_context=supervisor_context,
            recovery_context=recovery_context,
            current_plan=current_plan,
            plan_progress="PlanTracker owns status; use the canonical CurrentStepContext above.",
            current_action_context=self._current_action_contract(state, str(state.get("task_id") or "default")),
            max_chars=LLM_STEP_EPHEMERAL_CHARS,
        )
        report = dict(report)
        report["projection_mode"] = "compact_required_state_retry"
        return text, report

    def _generate_task_spec_projection(
        self,
        state: AgentState,
        messages: List[BaseMessage],
    ) -> str:
        """Project user-owned facts needed to generate only the current step."""
        spec = self._task_spec_for_state(dict(state), messages)
        task_id = str(state.get("task_id") or "default")
        plan_state = load_plan_state(self.data_path, task_id)
        input_audit = state.get("input_audit") or (plan_state.get("task_state") or {}).get("input_audit") or {}
        if isinstance(input_audit, dict) and input_audit:
            spec = merge_task_spec_input_audit(spec, input_audit)
        _, current_step = current_plan_step_with_index(plan_state)
        payload = project_effective_user_facts(
            resolve_effective_user_facts(spec),
            audience="generate",
            step=current_step or {},
            max_chars=6000,
        )
        payload.pop("projection_text", None)
        contract = effective_task_contract(spec)
        # Compatibility aliases remain derived from the same resolved facts;
        # Generate no longer walks user_directives or a separate constraint list.
        payload["user_constraints"] = [
            str(item.get("text") or "")
            for item in payload.get("active_constraints", [])
            if str(item.get("text") or "").strip()
        ]
        payload["resolved_inputs"] = contract["inputs"]
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def _bounded_message_history(
        messages: List[BaseMessage],
        *,
        max_messages: int = LLM_MAX_HISTORY_MESSAGES,
        max_chars: int = LLM_MAX_HISTORY_CHARS,
    ) -> List[BaseMessage]:
        """Keep the initial request and recent evidence within a fixed budget."""
        if not messages:
            return []
        selected = messages[-max_messages:]
        if messages[0] not in selected and max_messages > 1:
            selected = [messages[0]] + selected[-(max_messages - 1):]

        per_message_limit = max(1200, max_chars // max(1, len(selected)))
        bounded: List[BaseMessage] = []
        for message in selected:
            content = str(getattr(message, "content", message) or "")
            if len(content) <= per_message_limit:
                bounded.append(message)
                continue
            clipped = compress_tagged_text(content, per_message_limit)
            try:
                bounded.append(message.model_copy(update={"content": clipped}))
            except AttributeError:
                if isinstance(message, HumanMessage):
                    bounded.append(HumanMessage(content=clipped))
                elif isinstance(message, AIMessage):
                    bounded.append(AIMessage(content=clipped))
                elif isinstance(message, SystemMessage):
                    bounded.append(SystemMessage(content=clipped))
                else:
                    bounded.append(HumanMessage(content=clipped))
        return bounded

    @staticmethod
    def _user_goal_messages(messages: List[BaseMessage]) -> List[BaseMessage]:
        """Keep the original goal and explicit later user answers only."""
        if not messages:
            return []
        selected: List[BaseMessage] = []
        first_human = next(
            (message for message in messages if isinstance(message, HumanMessage)),
            None,
        )
        if first_human is not None:
            selected.append(first_human)
        later_humans = [
            message
            for message in messages
            if isinstance(message, HumanMessage)
            and message is not first_human
            and "<observation>" not in str(getattr(message, "content", ""))
        ]
        if later_humans:
            selected.append(later_humans[-1])
        return selected

    @staticmethod
    def _task_spec_for_state(
        state: Optional[Dict[str, Any]],
        messages: Optional[List[BaseMessage]] = None,
    ) -> Dict[str, Any]:
        mapping = state if isinstance(state, dict) else {}
        fallback = "\n".join(
            str(getattr(message, "content", "")).strip()
            for message in BioAnalystAgent._user_goal_messages(list(messages or mapping.get("messages", []) or []))
            if str(getattr(message, "content", "")).strip()
        )
        return ensure_task_spec(
            mapping.get("task_spec"),
            fallback,
            task_id=str(mapping.get("task_id") or mapping.get("session_id") or ""),
            project_root=project_root,
        )

    @classmethod
    def _task_revision_for_state(cls, state: Optional[Dict[str, Any]]) -> int:
        mapping = state if isinstance(state, dict) else {}
        try:
            explicit_revision = int(mapping.get("task_revision") or 0)
        except (TypeError, ValueError):
            explicit_revision = 0
        return max(
            task_spec_revision(mapping.get("task_spec")),
            explicit_revision,
        )

    def _stale_plan_task_revision_reason(self, state: AgentState, task_id: str) -> str:
        plan_state = load_plan_state(self.data_path, task_id)
        if not plan_state.get("steps"):
            return ""
        task_revision = self._task_revision_for_state(dict(state))
        try:
            plan_revision = int(plan_state.get("generated_from_task_revision") or 0)
        except (TypeError, ValueError):
            plan_revision = 0
        if task_revision <= 0 or task_revision == plan_revision:
            return ""
        return (
            f"The effective task is revision {task_revision}, but the approved plan was generated from "
            f"task revision {plan_revision}. Re-plan before generating or executing another action."
        )

    def _supervisor_feedback_snapshot(
        self,
        update: Dict[str, Any],
        state: AgentState,
        task_id: str,
    ) -> Dict[str, Any]:
        relevant = [
            str(getattr(message, "content", message) or "")
            for message in list(update.get("messages", []) or [])
            if any(
                marker in str(getattr(message, "content", message) or "")
                for marker in (
                    "<supervisor_feedback",
                    "Step completion validation failed",
                    "Final completion validation failed",
                    "Knowledge conflict",
                )
            )
        ]
        plan_state = load_plan_state(self.data_path, task_id)
        structured = update.get("supervisor_feedback") or {}
        evidence = update.get("execution_evidence") or state.get("execution_evidence") or {}
        return {
            "verdict": str(structured.get("verdict") or update.get("supervisor_verdict") or ""),
            "text": self._clip_observation_text("\n\n".join(relevant[-4:]), 4000),
            "step_id": str(
                evidence.get("step_id")
                or (self._running_plan_step(plan_state) or {}).get("step_id")
                or ""
            ),
            "plan_version": str(plan_state.get("plan_version") or "plan_v2"),
            "task_revision": self._task_revision_for_state(dict(state)),
            "execution_id": str(evidence.get("execution_id") or evidence.get("action_id") or ""),
            "failed_checks": [
                {
                    "rule": str(item.get("rule") or "")[:160],
                    "severity": str(item.get("severity") or "")[:40],
                    "message": self._clip_observation_text(item.get("message"), 700),
                }
                for item in list(structured.get("failed_checks") or [])[-8:]
                if isinstance(item, dict)
            ],
            "required_fix": [
                self._clip_observation_text(item, 700)
                for item in list(structured.get("required_fix") or [])[-8:]
            ],
            "retryable": bool(structured.get("retryable")),
            "retry_reason": self._clip_observation_text(update.get("retry_reason"), 1200),
            "failed_step_id": str(update.get("failed_step_id") or ""),
            "failure_fingerprint": str(update.get("failure_fingerprint") or ""),
            "required_next_action": str(update.get("required_next_action") or ""),
            "recovery_scope": str(update.get("recovery_scope") or ""),
            "evidence_status": str(update.get("evidence_status") or ""),
        }

    async def _adjudicate_post_review_recovery(
        self,
        state: AgentState,
        update: Dict[str, Any],
        task_id: str,
    ) -> Dict[str, Any]:
        """Use the shared trigger/context schema for Supervisor/Gate conflicts."""
        merged = {**dict(state), **dict(update or {})}
        evidence = merged.get("execution_evidence") or state.get("execution_evidence") or {}
        if not isinstance(evidence, dict) or not evidence:
            return update
        store = RecoveryStateStore(self.data_path, task_id)
        history = store.load_attempts()
        current_execution_id = str(evidence.get("execution_id") or "")
        history.extend(
            item for item in store.load_execution_records()
            if str(item.get("execution_id") or "") != current_execution_id
        )
        trigger = evaluate_recovery_trigger(merged, evidence, recovery_history=history)
        update["recovery_trigger"] = trigger.to_dict()
        if not trigger.required:
            return update

        plan_state = load_plan_state(self.data_path, task_id)
        active_skills = [str(item) for item in merged.get("active_skills") or [] if str(item).strip()]
        skill_context = ""
        if active_skills:
            try:
                skill_context = self.skill_manager.get_execution_cards(active_skills)
            except Exception as exc:  # noqa: BLE001 - diagnostic context is best effort.
                skill_context = f"active skill policy unavailable: {exc}"
        context = build_recovery_context(
            merged,
            evidence,
            plan_state=plan_state,
            task_root=Path(self.data_path) / "task_outputs" / task_id,
            skill_policy={
                "skill_ids": active_skills,
                "relevant_context": self._clip_observation_text(skill_context, 3000),
            },
            recovery_history=history,
        )
        prompt_context = context.prompt_view()
        diagnosis = dict(prompt_context.get("diagnosis") or {})
        try:
            evidence_returncode = int(
                evidence.get("returncode")
                if evidence.get("returncode") is not None
                else evidence.get("exit_code")
            )
        except (TypeError, ValueError):
            evidence_returncode = None
        structured_execution_failed = (
            evidence_returncode not in {None, 0}
            or str(evidence.get("execution_status") or "").lower() == "failed"
        )
        judge_needed = diagnosis_required(trigger, evidence, merged)
        if diagnosis.get("collection_complete") and not judge_needed and (
            structured_execution_failed or diagnosis.get("evidence_sufficient")
        ):
            decision = recovery_decision_from_diagnosis(diagnosis)
        elif judge_needed:
            cached_judge = store.load_llm_result(context.context_id, "recovery_judge")
            cached_judge_valid = False
            if cached_judge.get("parsed_result"):
                try:
                    decision = RecoveryDecision.from_mapping(cached_judge["parsed_result"])
                    cached_judge_valid = True
                except (TypeError, ValueError):
                    cached_judge_valid = False
            if not cached_judge:
                increment_plan_budget(plan_state, "recovery_judge_count")
                save_plan_state(self.data_path, task_id, plan_state)
                prompt = PromptManager.get_recovery_judge_prompt(prompt_context)
                try:
                    response = await self._ainvoke_llm(
                        [HumanMessage(content=prompt)],
                        stage="recovery_judge",
                        task_id=task_id,
                        task_mode=str(state.get("task_mode") or "managed"),
                        trace_metadata={
                            "call_kind": "recovery_judge",
                            "attempt_id": str(evidence.get("attempt_id") or ""),
                            "action_id": str(evidence.get("action_id") or ""),
                            "step_id": str(evidence.get("step_id") or ""),
                            "recovery_context_ref": str(store.root / "recovery_context.json"),
                        },
                    )
                    raw_response = str(getattr(response, "content", response) or "")
                    decision = parse_recovery_judge_decision(raw_response)
                    store.save_llm_result(
                        context.context_id,
                        "recovery_judge",
                        raw_response=raw_response,
                        parsed_result=decision.to_dict(),
                    )
                except Exception as exc:
                    store.save_llm_result(
                        context.context_id,
                        "recovery_judge",
                        raw_response=str(getattr(locals().get("response"), "content", "") or ""),
                        parsed_result={},
                    )
                    logger.warning(
                        "RecoveryJudge unavailable after review; preserving an internal protocol error: %s",
                        exc,
                    )
                    decision = recovery_decision_from_diagnosis({
                        "root_cause": "RecoveryJudge was unavailable after review; preserved evidence requires internal escalation.",
                        "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                        "failure_type": "recovery_judge_unavailable",
                        "evidence": [*trigger.reasons, self._clip_observation_text(exc, 300)],
                        "confidence": 1.0,
                        "missing_evidence": [],
                        "repair_strategy": "Repair or resume the internal recovery protocol without asking for a scientific decision.",
                        "next_action": RecoveryAction.ESCALATE.value,
                        "requires_user_input": False,
                        "evidence_sufficient": True,
                    })
            elif not cached_judge_valid:
                decision = recovery_decision_from_diagnosis({
                    "root_cause": "The cached RecoveryJudge result is invalid for this unchanged recovery context.",
                    "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                    "failure_type": "recovery_judge_schema_error",
                    "evidence": [*trigger.reasons, f"recovery_context_id:{context.context_id}"],
                    "confidence": 1.0,
                    "missing_evidence": [],
                    "repair_strategy": "Repair the internal decision protocol without another LLM call.",
                    "next_action": RecoveryAction.ESCALATE.value,
                    "requires_user_input": False,
                    "evidence_sufficient": True,
                })
        else:
            decision = deterministic_recovery_decision(trigger, evidence)

        decision = constrain_recovery_decision(
            decision,
            diagnosis,
            user_decision_supported=structured_user_decision_required(merged),
        )
        decision = contextualize_recovery_decision(
            decision,
            action_id=str(evidence.get("action_id") or evidence.get("execution_id") or ""),
            step_id=str(evidence.get("step_id") or state.get("current_step_id") or ""),
            attempt_id=str(evidence.get("attempt_id") or evidence.get("execution_id") or ""),
            source_node="supervisor",
        )
        decision = RecoveryPolicy().constrain_with_progress(
            decision,
            dict(merged.get("recovery_progress") or {}),
            dict((plan_state.get("task_state") or {}).get("budgets") or {}),
        )
        decision = contextualize_recovery_decision(decision, source_node="supervisor")
        application = apply_recovery_decision(plan_state, decision)
        save_plan_state(self.data_path, task_id, plan_state)

        store.save_diagnostic_context(context)
        store.save_recovery_decision(decision)
        store.append_trace("RECOVERY_JUDGE", {
            "trigger": trigger.to_dict(),
            "decision": decision.to_dict(),
            "source": "post_review",
        })
        route = recovery_action_route(
            decision.recommended_action,
            interaction_allowed=not bool(state.get("non_interactive") or state.get("disable_user_questions")),
            failure_owner=decision.failure_owner,
            requires_user_input=decision.requires_user_input,
        )
        pending = {
            "type": route["pending_type"],
            "reason": decision.reason or decision.root_cause,
            "step_id": str(evidence.get("step_id") or state.get("current_step_id") or ""),
            "failure_class": decision.failure_class,
            "failure_owner": decision.failure_owner,
            "recommended_action": decision.recommended_action,
            "evidence": list(decision.evidence),
            "failure_type": decision.failure_type,
            "missing_evidence": list(decision.missing_evidence),
            "repair_strategy": decision.repair_strategy,
            "requires_user_input": decision.requires_user_input,
            "diagnostic_context_path": str(store.root / "recovery_context.json"),
        }
        update.update({
            "diagnostic_context": prompt_context,
            "recovery_context": prompt_context,
            "recovery_context_digest": context.context_id,
            "current_step_context": dict(prompt_context.get("current_step") or {}),
            "progress_evidence": dict(prompt_context.get("progress") or {}),
            "recovery_decision": decision.to_dict(),
            "diagnostic_summary": diagnosis,
            "structured_diagnosis": diagnosis,
            "failure_owner": decision.failure_owner,
            "recovery_history": history[-3:],
            "pending_recovery_action": pending,
            "required_next_action": route["required_next_action"],
            "recovery_scope": route["recovery_scope"],
            "evidence_status": "sufficient" if decision.evidence_sufficient else "insufficient",
            "retry_reason": decision.reason or decision.root_cause,
            "recovery_phase": "diagnose",
            "recovery_status": decision.recommended_action,
            "recovery_application": application,
            "current_step_id": str(application.get("current_step_id") or ""),
            "current_stage_id": str(application.get("current_stage_id") or ""),
        })
        if decision.next_route == "ask_user":
            question = "BioMind needs your decision before recovery can continue: " + (decision.reason or decision.root_cause)
            pending["question"] = question
            update["pending_human_question"] = {
                "type": "recovery_user_decision",
                "stage": "recovery",
                "question": question,
                "resume_step": "generate",
            }
        return update

    def _commit_legacy_recovery_projection(
        self,
        state: AgentState,
        update: Dict[str, Any],
        task_id: str,
        *,
        source_node: str,
    ) -> Dict[str, Any]:
        """Translate compatibility verdict fields into one committed policy decision."""
        if not isinstance(update, dict):
            return update
        existing = update.get("recovery_decision")
        if isinstance(existing, dict) and existing:
            try:
                decision = RecoveryDecision.from_mapping(existing)
            except ValueError:
                decision = None
            if decision is not None and str(decision.source_node or "") == source_node:
                return update

        merged = {**dict(state or {}), **update}
        verdict = str(update.get("supervisor_verdict") or "").strip().upper()
        required = str(update.get("required_next_action") or "").strip().upper()
        pending = update.get("pending_recovery_action")
        pending = dict(pending) if isinstance(pending, dict) else {}
        pending_type = str(pending.get("type") or "").strip().lower()
        if verdict not in {"RETRY", "ESCALATE"} and required not in {
            "ACTION_FORMAT_REPAIR", "FORMAT_REPAIR", "PROBE", "DIAGNOSTIC_PROBE",
            "PLAN_PATCH", "PATCH_PLAN", "BLOCKED",
        } and pending_type not in {"blocked", "action_format_repair", "diagnostic_probe", "plan_patch"}:
            return update

        plan_state = load_plan_state(self.data_path, task_id)
        _, step = current_plan_step_with_index(plan_state)
        step_id = str((step or {}).get("step_id") or state.get("current_step_id") or "task")
        evidence = dict(merged.get("execution_evidence") or {})
        action_id = str(evidence.get("action_id") or evidence.get("execution_id") or f"{source_node}:{step_id}")
        attempt_id = str(evidence.get("attempt_id") or evidence.get("execution_id") or "")
        if not attempt_id:
            attempt_id = hashlib.sha256(json.dumps({
                "verdict": verdict,
                "required": required,
                "pending_type": pending_type,
                "step_id": step_id,
                "feedback": update.get("completion_gate_result") or update.get("retry_reason") or "",
            }, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:20]
        budgets = dict((plan_state.get("task_state") or {}).get("budgets") or {})
        policy = RecoveryPolicy()

        if required in {"ACTION_FORMAT_REPAIR", "FORMAT_REPAIR"} or pending_type == "action_format_repair":
            decision = policy.decide(
                pre_action_evidence={
                    "valid": False,
                    "repairable": True,
                    "violations": [str(pending.get("reason") or update.get("retry_reason") or required)],
                },
                action_id=action_id,
                step_id=step_id,
                attempt_id=attempt_id,
                budgets=budgets,
            )
        elif required in {"PLAN_PATCH", "PATCH_PLAN"} or pending_type == "plan_patch":
            decision = policy.decide(
                validation_evidence={
                    "status": "failed",
                    "failure_owner": RecoveryFailureOwner.PLAN.value,
                    "contract_violations": [str(pending.get("reason") or update.get("retry_reason") or "plan structural error")],
                },
                action_id=action_id,
                step_id=step_id,
                attempt_id=attempt_id,
                budgets=budgets,
            )
        elif required in {"PROBE", "DIAGNOSTIC_PROBE"} or pending_type == "diagnostic_probe":
            decision = policy.decide(
                validation_evidence={"status": "unknown", "recoverability_hints": ["probe"]},
                progress_snapshot=dict(merged.get("recovery_progress") or {}),
                action_id=action_id,
                step_id=step_id,
                attempt_id=attempt_id,
                budgets=budgets,
            )
        elif required == "BLOCKED" or pending_type == "blocked":
            reason = str(pending.get("reason") or update.get("retry_reason") or "structured blocked state")
            decision = policy.decide(
                pre_action_evidence={
                    "valid": False,
                    "repairable": False,
                    "missing_bindings": ["required_recovery_dependency"],
                    "blocked_evidence": [reason],
                },
                action_id=action_id,
                step_id=step_id,
                attempt_id=attempt_id,
                budgets=budgets,
            )
        else:
            gate = dict(update.get("completion_gate_result") or {})
            validation_errors = []
            if gate.get("passed") is False:
                validation_errors = [
                    str(gate.get("message") or gate.get("feedback") or "completion contract failed")
                ]
            if not evidence:
                evidence = {"returncode": 0, "command_role": "primary"}
            decision = policy.decide(
                execution_evidence=evidence,
                validation_evidence={
                    "status": "failed" if verdict == "RETRY" else "escalate",
                    "errors": validation_errors,
                    "warnings": [str(update.get("retry_reason") or "")] if verdict == "ESCALATE" else [],
                },
                action_id=action_id,
                step_id=step_id,
                attempt_id=attempt_id,
                budgets=budgets,
            )
        decision = contextualize_recovery_decision(decision, source_node=source_node)
        application = apply_recovery_decision(plan_state, decision)
        save_plan_state(self.data_path, task_id, plan_state)
        update["recovery_decision"] = decision.to_dict()
        update["recovery_application"] = application
        update["current_step_id"] = str(application.get("current_step_id") or "")
        update["current_stage_id"] = str(application.get("current_stage_id") or "")
        update["next_step"] = decision.next_route
        update["recovery_status"] = decision.kind
        update["retry_count"] = int(
            (((plan_state.get("task_state") or {}).get("budgets") or {}).get("execution_retry_count"))
            or 0
        )
        return update

    def _fresh_supervisor_feedback(self, state: AgentState, task_id: str) -> Dict[str, Any]:
        """Return feedback only when it belongs to the current execution and plan step."""
        feedback = state.get("last_supervisor_feedback") or {}
        if not isinstance(feedback, dict) or not feedback:
            return {}
        plan_state = load_plan_state(self.data_path, task_id)
        current_step = self._running_plan_step(plan_state) or {}
        evidence = state.get("execution_evidence") or {}
        expected = {
            "step_id": str(current_step.get("step_id") or ""),
            "plan_version": str(plan_state.get("plan_version") or "plan_v2"),
            "task_revision": str(self._task_revision_for_state(dict(state))),
            "execution_id": str(evidence.get("execution_id") or evidence.get("action_id") or ""),
        }
        for key, value in expected.items():
            recorded = str(feedback.get(key) or "")
            if recorded and value and recorded != value:
                return {}
        return dict(feedback)

    def _renderer_action_protocol(
        self,
        state: AgentState,
        task_id: str,
    ) -> Dict[str, Any]:
        """Return the controller-owned Renderer contract for the current step."""
        plan_state = load_plan_state(self.data_path, task_id)
        step = self._running_plan_step(plan_state) or {}
        renderer_execution = dict(step.get("renderer_execution") or {})
        manifest_path = str(renderer_execution.get("manifest_path") or "").strip()
        manifest_status = "absent"
        if manifest_path:
            try:
                manifest_status = "registered" if Path(manifest_path).is_file() and Path(manifest_path).stat().st_size > 0 else "registered_missing"
            except OSError:
                manifest_status = "registered_missing"

        skill_ids = self._selected_skill_ids(step.get("skill_ids") or state.get("active_skills") or [])
        skill_manager = getattr(self, "skill_manager", None)
        records = [
            skill_manager.get_renderer_record(skill_id)
            for skill_id in skill_ids
        ] if skill_manager else []
        declared = [record for record in records if record.get("status") == "declared"]
        requested_id = str(step.get("renderer_id") or "").strip().lower()
        if requested_id:
            declared = [
                record for record in declared
                if requested_id in {
                    str(alias).strip().lower()
                    for alias in record.get("renderer_aliases") or []
                    if str(alias).strip()
                }
            ]
        renderer = declared[0] if len(declared) == 1 else {}
        renderer_composite = bool(
            step.get("renderer_composite")
            or str(step.get("step_type") or "").strip().lower() == "renderer_composite"
            or (
                renderer
                and (
                    str(step.get("tool") or "").strip().lower() == "renderer"
                    or "renderer-backed" in str(step.get("title") or "").strip().lower()
                )
            )
        )
        input_audit = state.get("input_audit") or (plan_state.get("task_state") or {}).get("input_audit") or {}
        input_audit = input_audit if isinstance(input_audit, dict) else {}
        resolved_inputs = [
            item for item in input_audit.get("resolved_inputs", []) or []
            if isinstance(item, dict)
        ][:24]
        missing_roles = [str(item) for item in input_audit.get("missing_roles", []) or [] if str(item).strip()]
        ambiguous = input_audit.get("ambiguous_roles", []) or []
        ambiguous_roles = [
            str(item.get("role") or item) if isinstance(item, dict) else str(item)
            for item in ambiguous
            if str(item).strip()
        ]
        argument_schema = dict((renderer.get("invocation") or {}).get("arguments") or {})
        task_spec = merge_task_spec_input_audit(
            self._task_spec_for_state(dict(state), list(state.get("messages", []) or [])),
            input_audit,
        )
        argument_projection = project_task_spec_to_renderer(
            task_spec,
            renderer.get("invocation") or {},
            task_id=task_id,
        ) if renderer else {
            "arguments": {},
            "outdir": "",
            "missing_bindings": [],
            "conflicts": [],
            "unverified_bindings": [],
            "complete": False,
        }
        if renderer_composite and manifest_status != "registered":
            allowed_actions = ["RENDERER_INVOCATION", "BLOCKED_WITH_DETERMINISTIC_MISSING_EVIDENCE"]
        elif renderer_composite:
            allowed_actions = ["MANIFEST_RUNNER"]
        else:
            allowed_actions = ["EXECUTE", "STEP_COMPLETION_CANDIDATE", "PLAN_PATCH", "BLOCKED"]
        return {
            "renderer_composite": renderer_composite,
            "renderer": renderer,
            "renderer_id": str(renderer.get("renderer_id") or step.get("renderer_id") or ""),
            "argument_schema": argument_schema,
            "argument_projection": argument_projection,
            "manifest_path": manifest_path,
            "manifest_status": manifest_status,
            "resolved_inputs": resolved_inputs,
            "missing_roles": missing_roles,
            "ambiguous_roles": ambiguous_roles,
            "allowed_actions": allowed_actions,
        }

    @staticmethod
    def _is_readonly_capability_probe(code: str) -> bool:
        """Classify source/help inspection without inferring a business action."""
        text = str(code or "").strip()
        if not text:
            return False
        if re.search(r"(?:^|\s)(?:--help|-h)(?:\s|$)", text, re.IGNORECASE):
            return True
        if re.search(r"(?:^|[;&|]\s*)(?:head|grep|rg|sed|cat|tail|awk|less|more)\b", text, re.IGNORECASE):
            writes_file = bool(
                re.search(r"(?:^|\s)(?:>>?|tee\s+|cp\s+|mv\s+)", text)
                and not re.search(r"\d?>&\d", text)
            )
            return not writes_file
        return False

    def _match_legacy_renderer_invocation(self, code: str, renderer: Dict[str, Any]) -> Dict[str, str]:
        """Accept only one complete, exact legacy Renderer launch command."""
        if self._is_readonly_capability_probe(code):
            return {}
        text = str(code or "").strip()
        if not text or any(token in text for token in ("\n", "|", ";", "&&", "||", ">", "<")):
            return {}
        try:
            argv = shlex.split(text)
        except ValueError:
            return {}
        if len(argv) < 3:
            return {}
        entrypoint = str(renderer.get("resolved_entrypoint") or "")
        if not entrypoint:
            return {}
        runner = Path(argv[0]).name.lower()
        if runner not in {"python", "python3", Path(sys.executable).name.lower()}:
            return {}
        try:
            invoked_entrypoint = str(Path(argv[1]).expanduser().resolve())
        except OSError:
            return {}
        if invoked_entrypoint != str(Path(entrypoint).expanduser().resolve()):
            return {}

        invocation = dict(renderer.get("invocation") or {})
        argument_specs = invocation.get("arguments") or {}
        flags = argv[2:]

        def has_value(flag: str) -> bool:
            for index, item in enumerate(flags):
                if item.startswith(flag + "=") and item.split("=", 1)[1]:
                    return True
                if item == flag and index + 1 < len(flags) and not flags[index + 1].startswith("-"):
                    return True
            return False

        for name, raw_spec in argument_specs.items():
            spec = dict(raw_spec or {}) if isinstance(raw_spec, dict) else {}
            if not spec.get("required"):
                continue
            accepted = [str(spec.get("flag") or "--" + str(name).replace("_", "-"))]
            accepted.extend(
                str(alias) if str(alias).startswith("-") else "--" + str(alias).replace("_", "-")
                for alias in spec.get("aliases") or []
            )
            if not any(has_value(flag) for flag in accepted):
                return {}
        outdir = ""
        for index, item in enumerate(flags):
            if item == "--outdir" and index + 1 < len(flags):
                outdir = flags[index + 1]
                break
            if item.startswith("--outdir="):
                outdir = item.split("=", 1)[1]
                break
        if not outdir:
            return {}
        return {"outdir": outdir}

    @staticmethod
    def _renderer_blocked_has_deterministic_evidence(
        action: Dict[str, Any],
        protocol: Dict[str, Any],
    ) -> bool:
        known = {
            str(item).strip().lower()
            for item in [
                *(protocol.get("missing_roles") or []),
                *(protocol.get("ambiguous_roles") or []),
            ]
            if str(item).strip()
        }
        provided = {
            str(item).strip().lower()
            for item in action.get("missing_evidence", []) or []
            if str(item).strip()
        }
        reason = str(action.get("reason") or "").lower()
        return bool(known and (known & provided or any(item in reason for item in known)))

    def _action_format_repair_feedback(
        self,
        state: AgentState,
        task_id: str,
        *,
        invalid_hash: str,
        invalid_excerpt: str,
        contract_error: Dict[str, Any] | None = None,
    ) -> str:
        protocol = self._renderer_action_protocol(state, task_id)
        plan_state = load_plan_state(self.data_path, task_id)
        _, step = current_plan_step_with_index(plan_state)
        lines = [
            "<action_format_repair>",
            "Your previous response did not satisfy the current action contract.",
            f"Current step: {str((step or {}).get('step_id') or 'unknown')}",
            f"Invalid response hash: {invalid_hash}",
            f"Invalid response excerpt: {str(invalid_excerpt or '').strip()[:500]}",
        ]
        if contract_error:
            lines.append("Contract error: " + json.dumps(contract_error, ensure_ascii=False, sort_keys=True))
        if protocol.get("renderer_composite"):
            lines.extend([
                f"canonical_renderer_id: {protocol.get('renderer_id') or '<missing>'}",
                "renderer_argument_schema: " + json.dumps(protocol.get("argument_schema") or {}, ensure_ascii=False, sort_keys=True),
                "formal_verified_inputs: " + json.dumps(protocol.get("resolved_inputs") or [], ensure_ascii=False, sort_keys=True),
                "projected_renderer_arguments: " + json.dumps(protocol.get("argument_projection") or {}, ensure_ascii=False, sort_keys=True),
                f"manifest_status: {protocol.get('manifest_status')}",
            ])
        lines.append("Allowed actions:")
        lines.extend(f"- {item}" for item in protocol.get("allowed_actions") or [])
        lines.extend([
            "Return exactly one <action_json> block with one structured action.",
            "Do not provide a narrative summary or ask whether to continue.",
            "</action_format_repair>",
        ])
        return "\n".join(lines)

    def _bind_pending_action(self, action: Dict[str, Any], state: AgentState, task_id: str) -> Dict[str, Any]:
        """Bind one generated action to the exact task revision and plan step."""
        if not isinstance(action, dict) or not action:
            return {}
        raw_action = dict(action)
        bound = dict(action)
        plan_state = load_plan_state(self.data_path, task_id)
        step = self._running_plan_step(plan_state) or {}
        task_revision = self._task_revision_for_state(dict(state))
        plan_version = str(plan_state.get("plan_version") or "plan_v2")
        step_id = str(step.get("step_id") or "")
        purpose = self._pending_action_purpose(bound, step)
        skill_manager = getattr(self, "skill_manager", None)
        protocol = self._renderer_action_protocol(state, task_id)
        if (
            str(bound.get("type") or "").lower() == "execute"
            and skill_manager
            and protocol.get("renderer_composite")
            and purpose not in {"probe", "validation"}
        ):
            renderer_records = [
                skill_manager.get_renderer_record(skill_id)
                for skill_id in self._selected_skill_ids(step.get("skill_ids") or state.get("active_skills") or [])
            ]
            renderer_records = [record for record in renderer_records if record.get("status") == "declared"]
            code = str(bound.get("code") or "")
            matches = [
                (record, self._match_legacy_renderer_invocation(code, record))
                for record in renderer_records
            ]
            matched = [(record, details) for record, details in matches if details]
            if len(matched) == 1:
                renderer, details = matched[0]
                outdir = details["outdir"]
                bound.update({
                    "command_role": "renderer_invocation",
                    "action_source": "renderer_compat_adapter",
                    "renderer_id": str(renderer.get("renderer_id") or ""),
                    "manifest_path": str(
                        (Path(outdir).expanduser() / str(renderer.get("manifest_filename") or "manifest.json")).resolve()
                    ) if outdir else "",
                })
        fingerprint_input = json.dumps(
            {
                "type": bound.get("type"),
                "code": bound.get("code"),
                "tool": bound.get("tool"),
                "question": bound.get("question"),
                "step_id": step_id,
                "task_revision": task_revision,
                "plan_version": plan_version,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        explicit_role = str(bound.get("command_role") or "").strip().lower()
        if explicit_role not in {
            "primary", "diagnostic", "required_validation", "optional_probe", "fallback", "recovery",
            "renderer_invocation",
        }:
            explicit_role = ""
        command_role = explicit_role or {
            "probe": "diagnostic",
            "validation": "required_validation",
            "fallback": "fallback",
        }.get(purpose, "primary")
        bound.update(
            {
                "action_id": "action_" + hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()[:16],
                "step_id": step_id,
                "task_revision": task_revision,
                "plan_version": plan_version,
                "purpose": purpose,
                "command_role": command_role,
                "runtime_bindings": self._runtime_bindings_from_state(state),
                "expected_outputs": list(bound.get("expected_outputs") or step.get("outputs") or []),
                "expected_evidence": list(bound.get("success_checks") or step.get("success_criteria") or []),
            }
        )
        action_record = record_provenance_event(plan_state, "action_bound", {
            "task_id": task_id,
            "action": bound,
            "raw_action": raw_action,
        })
        if action_record.get("manifest_id"):
            bound["manifest_id"] = str(action_record["manifest_id"])
        if action_record.get("stage_id"):
            bound["stage_id"] = str(action_record["stage_id"])
        save_plan_state(self.data_path, task_id, plan_state)
        return bound

    @staticmethod
    def _renderer_cli_arguments(arguments: Any) -> List[str]:
        """Build renderer argv without letting leading-dash values become options."""
        if isinstance(arguments, list):
            return [str(item) for item in arguments]
        if not isinstance(arguments, dict):
            return []
        argv: List[str] = []
        for raw_key, raw_value in arguments.items():
            key = str(raw_key or "").strip()
            if not key:
                continue
            flag = key if key.startswith("-") else "--" + key.replace("_", "-")
            values = raw_value if isinstance(raw_value, list) else [raw_value]
            for value in values:
                if value is False or value is None:
                    continue
                if value is True:
                    argv.append(flag)
                    continue
                text = str(value)
                if text.startswith("-") and flag.startswith("--"):
                    argv.append(f"{flag}={text}")
                else:
                    argv.extend([flag, text])
        return argv

    @staticmethod
    def _renderer_argument_key(value: Any) -> str:
        """Normalize one renderer argument name without guessing its meaning."""
        return str(value or "").strip().lstrip("-").replace("-", "_").lower()

    def _normalize_renderer_invocation_arguments(
        self,
        action: Dict[str, Any],
        renderer: Dict[str, Any],
        state: Dict[str, Any],
    ) -> Tuple[List[str], str, Dict[str, Any]]:
        """Validate and normalize an invocation from renderer-owned metadata.

        The controller understands the protocol shape, while each renderer owns
        its argument names, aliases, routed arguments, and runtime bindings.
        """
        raw_arguments = action.get("arguments") or {}
        outdir = str(action.get("outdir") or "").strip()
        if isinstance(raw_arguments, list):
            return self._renderer_cli_arguments(raw_arguments), outdir, {}
        if not isinstance(raw_arguments, dict):
            return [], outdir, {
                "error_type": "RENDERER_INVOCATION_SCHEMA_ERROR",
                "reason": "renderer arguments must be an object or argv list",
            }

        invocation = dict(renderer.get("invocation") or {})
        argument_specs = invocation.get("arguments") or {}
        if not isinstance(argument_specs, dict) or not argument_specs:
            return self._renderer_cli_arguments(raw_arguments), outdir, {}

        specs: Dict[str, Dict[str, Any]] = {}
        aliases: Dict[str, str] = {}
        for raw_name, raw_spec in argument_specs.items():
            canonical = self._renderer_argument_key(raw_name)
            spec = dict(raw_spec or {}) if isinstance(raw_spec, dict) else {"flag": str(raw_spec or "")}
            specs[canonical] = spec
            names = [raw_name, spec.get("flag"), *(spec.get("aliases") or [])]
            for name in names:
                normalized = self._renderer_argument_key(name)
                if normalized:
                    aliases[normalized] = canonical

        values: Dict[str, Any] = {}
        duplicate_fields: List[str] = []
        for raw_name, value in raw_arguments.items():
            normalized = self._renderer_argument_key(raw_name)
            if normalized in values:
                duplicate_fields.append(str(raw_name))
            values[normalized] = value

        route_errors: List[Dict[str, Any]] = []
        routed_arguments = invocation.get("routed_arguments") or {}
        if isinstance(routed_arguments, dict):
            for raw_source, raw_route in routed_arguments.items():
                source = self._renderer_argument_key(raw_source)
                if source not in values:
                    continue
                route = dict(raw_route or {})
                selector = self._renderer_argument_key(route.get("selector"))
                selector_value = str(values.get(selector) or "").strip()
                routes = {str(key): value for key, value in (route.get("routes") or {}).items()}
                target = self._renderer_argument_key(routes.get(selector_value))
                if not target:
                    route_errors.append({
                        "field": raw_source,
                        "selector": route.get("selector"),
                        "value": selector_value,
                        "allowed_values": sorted(routes),
                    })
                    continue
                if target in values and values[target] != values[source]:
                    duplicate_fields.append(str(raw_source))
                else:
                    values[target] = values[source]
                values.pop(source, None)
                values.pop(selector, None)

        ignored_fields: List[str] = []
        for raw_name in [
            *(invocation.get("runtime_only_arguments") or []),
            *(invocation.get("controller_owned_arguments") or []),
        ]:
            key = self._renderer_argument_key(raw_name)
            if key in values:
                ignored_fields.append(key)
                values.pop(key, None)

        runtime_bindings = self._runtime_bindings_from_state(state)
        for canonical, spec in specs.items():
            binding = str(spec.get("runtime_binding") or "").strip()
            if binding and runtime_bindings.get(binding):
                values[canonical] = runtime_bindings[binding]

        normalized_values: Dict[str, Any] = {}
        unknown_fields: List[str] = []
        for raw_name, value in values.items():
            canonical = aliases.get(raw_name)
            if not canonical:
                unknown_fields.append(raw_name)
                continue
            if canonical in normalized_values and normalized_values[canonical] != value:
                duplicate_fields.append(raw_name)
                continue
            normalized_values[canonical] = value

        argv: List[str] = []
        seen: set = set()
        for canonical, value in normalized_values.items():
            spec = specs[canonical]
            seen.add(canonical)
            if bool(spec.get("top_level")):
                candidate = str(value or "").strip()
                if outdir and candidate and Path(outdir).expanduser() != Path(candidate).expanduser():
                    duplicate_fields.append(canonical)
                elif candidate:
                    outdir = candidate
                continue
            flag = str(spec.get("flag") or "--" + canonical.replace("_", "-")).strip()
            argv.extend(self._renderer_cli_arguments({flag: value}))

        missing_fields = [
            canonical
            for canonical, spec in specs.items()
            if bool(spec.get("required"))
            and canonical not in seen
            and not (bool(spec.get("top_level")) and outdir)
        ]
        if unknown_fields or missing_fields or duplicate_fields or route_errors:
            return [], outdir, {
                "error_type": "RENDERER_INVOCATION_SCHEMA_ERROR",
                "renderer_id": str(renderer.get("renderer_id") or ""),
                "unknown_fields": sorted(set(unknown_fields)),
                "missing_fields": sorted(set(missing_fields)),
                "conflicting_fields": sorted(set(duplicate_fields)),
                "route_errors": route_errors,
                "ignored_runtime_or_controller_fields": sorted(set(ignored_fields)),
                "allowed_fields": sorted(specs),
            }
        return argv, outdir, {}

    def _materialize_renderer_invocation(
        self,
        action: Dict[str, Any],
        state: AgentState,
        task_id: str,
    ) -> Dict[str, Any]:
        plan_state = load_plan_state(self.data_path, task_id)
        step = self._running_plan_step(plan_state) or {}
        skill_ids = self._selected_skill_ids(step.get("skill_ids") or state.get("active_skills") or [])
        records = [self.skill_manager.get_renderer_record(skill_id) for skill_id in skill_ids] if self.skill_manager else []
        requested_id = str(action.get("renderer_id") or "").strip()
        candidates = [
            record for record in records
            if record.get("status") == "declared"
            and (
                not requested_id
                or requested_id.lower() in {
                    str(alias).strip().lower()
                    for alias in (record.get("renderer_aliases") or [])
                    if str(alias).strip()
                }
            )
        ]
        if len(candidates) != 1:
            declared = [record for record in records if record.get("status") == "declared"]
            return {
                "type": "renderer_contract_error",
                "source": "controller",
                "contract_error": {
                    "error_type": "RENDERER_INVOCATION_SCHEMA_ERROR",
                    "reason": "unknown_renderer_id" if requested_id else "ambiguous_renderer_id",
                    "requested_renderer_id": requested_id,
                    "allowed_renderer_ids": [str(record.get("renderer_id") or "") for record in declared],
                    "accepted_aliases": {
                        str(record.get("renderer_id") or ""): list(record.get("renderer_aliases") or [])
                        for record in declared
                    },
                },
            }
        renderer = candidates[0]
        argv, outdir, contract_error = self._normalize_renderer_invocation_arguments(action, renderer, state)
        if contract_error:
            return {
                "type": "renderer_contract_error",
                "source": "controller",
                "contract_error": contract_error,
            }
        step_id = str(step.get("step_id") or "")
        if "--producer-step-id" not in argv and not any(item.startswith("--producer-step-id=") for item in argv):
            argv.extend(["--producer-step-id", step_id])
        has_outdir_arg = "--outdir" in argv or any(item.startswith("--outdir=") for item in argv)
        if outdir and not has_outdir_arg:
            argv.extend(["--outdir", outdir])
        if not outdir:
            for index, item in enumerate(argv):
                if item == "--outdir" and index + 1 < len(argv):
                    outdir = argv[index + 1]
                    break
                if item.startswith("--outdir="):
                    outdir = item.split("=", 1)[1]
                    break
        if not outdir:
            return {
                "type": "renderer_contract_error",
                "source": "controller",
                "contract_error": {
                    "error_type": "RENDERER_INVOCATION_SCHEMA_ERROR",
                    "renderer_id": str(renderer.get("renderer_id") or ""),
                    "missing_fields": ["outdir"],
                },
            }
        entrypoint = str(renderer.get("resolved_entrypoint") or "")
        if not entrypoint:
            return {
                "type": "renderer_contract_error",
                "source": "controller",
                "contract_error": {
                    "error_type": "RENDERER_REGISTRY_ERROR",
                    "renderer_id": str(renderer.get("renderer_id") or ""),
                    "reason": "declared renderer entrypoint could not be resolved",
                },
            }
        command_argv = [sys.executable, entrypoint, *argv]
        manifest_path = str((Path(outdir).expanduser() / str(renderer.get("manifest_filename") or "manifest.json")).resolve())
        return {
            "type": "execute",
            "language": "bash",
            "code": shlex.join(command_argv),
            "purpose": "analysis",
            "command_role": "renderer_invocation",
            "action_source": "renderer",
            "renderer_id": str(renderer.get("renderer_id") or ""),
            "manifest_path": manifest_path,
            "expected_outputs": [manifest_path],
            "success_checks": ["renderer_exit_zero", "manifest_v3_valid", "manifest_provenance_matches"],
        }

    def _projected_renderer_action_update(
        self,
        state: AgentState,
        task_id: str,
    ) -> Dict[str, Any]:
        """Materialize a complete TaskSpec projection through the existing renderer path."""
        protocol = self._renderer_action_protocol(state, task_id)
        projection = dict(protocol.get("argument_projection") or {})
        if (
            not protocol.get("renderer_composite")
            or protocol.get("manifest_status") != "absent"
            or not protocol.get("renderer_id")
            or not projection.get("complete")
            or projection.get("unverified_bindings")
        ):
            return {}
        structured_action = {
            "type": "renderer_invocation",
            "renderer_id": protocol["renderer_id"],
            "arguments": dict(projection.get("arguments") or {}),
            "outdir": str(projection.get("outdir") or ""),
            "argument_projection": projection,
            "action_source": "task_spec_projection",
        }
        action = self._materialize_renderer_invocation(structured_action, state, task_id)
        if action.get("type") != "execute":
            return {
                "messages": [SystemMessage(content=(
                    "TaskSpec renderer argument projection could not be materialized. "
                    + json.dumps(action.get("contract_error") or {}, ensure_ascii=False, default=str)
                ))],
                "pending_action": {},
                "renderer_argument_projection": projection,
                "generate_result": "NEEDS_RECOVERY",
                "generate_exit_reason": "renderer_projection_contract_error",
                "next_step": "generate",
            }
        bound = self._bind_pending_action(action, state, task_id)
        return {
            "messages": [SystemMessage(content=(
                "Renderer invocation was projected from verified TaskSpec bindings and the declared renderer schema."
            ))],
            "pending_action": bound,
            "renderer_argument_projection": projection,
            "generate_result": "GENERATED_ACTION",
            "generate_exit_reason": "renderer_invocation_projected",
            "next_step": "execute",
        }

    def _registered_renderer_action_update(
        self,
        state: AgentState,
        task_id: str,
    ) -> Dict[str, Any]:
        plan_state = load_plan_state(self.data_path, task_id)
        _, step = current_plan_step_with_index(plan_state)
        step = step or {}
        execution = dict(step.get("renderer_execution") or {})
        manifest_path = str(execution.get("manifest_path") or "")
        if not manifest_path:
            return {}
        try:
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "messages": [SystemMessage(content=f"Registered renderer manifest is unreadable: {manifest_path}")],
                "pending_action": {},
                "failure_owner": RecoveryFailureOwner.ARTIFACT_CONTRACT.value,
                "required_next_action": "RECONCILE_ARTIFACTS",
                "next_step": "supervisor",
                "generate_result": "NEEDS_RECOVERY",
                "generate_exit_reason": "renderer_manifest_unreadable",
            }
        normalized = normalize_renderer_manifest(manifest, manifest_path)
        if renderer_manifest_complete(normalized, execution):
            evidence_ids = [
                str(item.get("execution_id") or item.get("action_id") or "")
                for item in execution.get("action_evidence", []) or []
                if str(item.get("execution_id") or item.get("action_id") or "")
            ]
            return {
                "messages": [SystemMessage(content="All required renderer manifest actions have completed; requesting step-level validation.")],
                "pending_action": {},
                "step_completion_candidate": {
                    "type": "step_completion_candidate",
                    "step_id": str(step.get("step_id") or ""),
                    "reason": "All required actions in the registered renderer manifest completed.",
                    "evidence_ids": evidence_ids or [str(execution.get("producer_action_id") or "renderer_manifest")],
                },
                "generate_result": "GENERATED_ACTION",
                "generate_exit_reason": "renderer_step_completion_candidate",
                "next_step": "generate",
            }
        action = next_renderer_manifest_action(normalized, execution)
        if not action:
            return {
                "messages": [SystemMessage(content="Renderer manifest has no runnable action; dependency or contract reconciliation is required.")],
                "pending_action": {},
                "failure_owner": RecoveryFailureOwner.ARTIFACT_CONTRACT.value,
                "required_next_action": "RECONCILE_ARTIFACTS",
                "next_step": "supervisor",
                "generate_result": "NEEDS_RECOVERY",
                "generate_exit_reason": "renderer_manifest_stalled",
            }
        try:
            script_path = resolve_renderer_action_script(normalized, action)
            manifest_script_hash = hashlib.sha256(script_path.read_bytes()).hexdigest()
        except (OSError, ValueError) as exc:
            return {
                "messages": [SystemMessage(content=str(exc))],
                "pending_action": {},
                "failure_owner": RecoveryFailureOwner.ARTIFACT_CONTRACT.value,
                "required_next_action": "RECONCILE_ARTIFACTS",
                "next_step": "supervisor",
                "generate_result": "NEEDS_RECOVERY",
                "generate_exit_reason": "renderer_script_contract_error",
            }
        artifact_by_id = {
            str(item.get("artifact_id") or ""): item
            for item in normalized.get("artifacts", []) or []
        }
        expected_outputs = [
            str(artifact_by_id[item].get("path") or "")
            for item in action.get("expected_artifact_ids", []) or []
            if item in artifact_by_id
        ]
        pending = self._bind_pending_action({
            "type": "execute",
            "language": "bash",
            "code": shlex.join(["bash", str(script_path)]),
            "purpose": "validation" if action.get("kind") == "verification" else "analysis",
            "command_role": "required_validation" if action.get("kind") == "verification" else "primary",
            "action_source": "manifest_runner",
            "renderer_id": str(execution.get("renderer_id") or ""),
            "manifest_path": manifest_path,
            "manifest_fingerprint": str(execution.get("manifest_fingerprint") or ""),
            "manifest_action_id": str(action.get("id") or ""),
            "manifest_script_hash": manifest_script_hash,
            "expected_outputs": expected_outputs,
            "success_checks": list(action.get("success_checks") or []),
        }, state, task_id)
        execution["current_action_id"] = str(action.get("id") or "")
        step["renderer_execution"] = execution
        save_plan_state(self.data_path, task_id, plan_state)
        return {
            "messages": [SystemMessage(content=f"Manifest Runner selected action {action.get('id')}: {script_path.name}")],
            "pending_action": pending,
            "generate_result": "GENERATED_ACTION",
            "generate_exit_reason": "manifest_action_selected",
            "next_step": "execute",
        }

    @staticmethod
    def _runtime_bindings_from_state(state: Dict[str, Any]) -> Dict[str, str]:
        """Return verified runtime paths as safe environment bindings.

        Runtime audit remains the sole path resolver. Execute only transports
        bindings that already passed the audit's existing-path validation.
        """
        bindings: Dict[str, str] = {}
        for audit in state.get("runtime_audit", []) or []:
            if not isinstance(audit, dict) or str(audit.get("status") or "").lower() != "ready":
                continue
            for key, value in (audit.get("resolved_runtime_paths") or {}).items():
                name = str(key or "").strip()
                path = Path(str(value or "")).expanduser()
                if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", name):
                    continue
                try:
                    valid = path.is_dir() or (path.is_file() and path.stat().st_size > 0)
                except OSError:
                    valid = False
                if not valid:
                    continue
                resolved = str(path.resolve())
                bindings[name] = resolved
                if name.startswith("RESOLVED_"):
                    bindings.setdefault(name[len("RESOLVED_"):], resolved)
        return bindings

    @staticmethod
    def _pending_action_purpose(action: Dict[str, Any], step: Dict[str, Any]) -> str:
        """Classify action intent so helper success cannot complete analysis work."""
        explicit = str(action.get("purpose") or "").strip().lower()
        action_type = str(action.get("type") or "").strip().lower()
        if action_type == "install_tool":
            return "install"
        if action_type == "ask_user":
            return "user_input"
        code = str(action.get("code") or "")
        lower = code.lower()
        if re.search(r"(?:blocked|stage_blocked|skipped).{0,80}(?:stage_status|report|\.txt)|(?:stage_status|report|\.txt).{0,80}(?:blocked|stage_blocked|skipped)", lower, re.DOTALL):
            return "record_failure"
        read_only = bool(
            BioAnalystAgent._is_readonly_capability_probe(code)
            or re.search(
                r"\b(?:ls|find|grep|rg|head|tail|cat|sed|awk|less|more|stat|file|test|command\s+-v|which|type)\b",
                lower,
            )
        )
        writes_output = bool(re.search(r"(?:^|\s)(?:>|>>|tee\s|cp\s|mv\s)|\s-o\s+\S+", lower))
        if read_only and not writes_output:
            step_purpose = str(step.get("purpose") or "").strip().lower()
            return "validation" if explicit == "validation" or step_purpose == "validation" else "probe"
        if explicit in {"analysis", "probe", "validation", "record_failure", "install", "user_input"}:
            return explicit
        step_purpose = str(step.get("purpose") or "").strip().lower()
        if step_purpose in {"analysis", "probe", "validation", "record_failure", "install", "user_input"}:
            return step_purpose
        return "analysis"

    def _validate_pending_action_contract(
        self,
        action: Dict[str, Any],
        state: AgentState,
        task_id: str,
        *,
        expected_type: str = "execute",
    ) -> str:
        """Validate one durable action before a router may enter Execute."""
        plan_state = load_plan_state(self.data_path, task_id)
        if task_status(plan_state) in TASK_TERMINAL_STATUSES:
            return f"Task is terminal ({task_status(plan_state)}); no further action may execute."
        if not isinstance(action, dict) or not action:
            return "No structured pending action is available."
        if str(action.get("type") or "").strip().lower() != expected_type:
            return f"Pending action type is not {expected_type}."
        if not str(action.get("action_id") or "").strip():
            return "Pending action has no action_id."
        stale = self._pending_action_stale_reason(action, state, task_id)
        if stale:
            return stale
        if expected_type == "execute":
            code = str(action.get("code") or "").strip()
            if not code:
                return "Pending execute action has no command body."
            executable_lines = [
                line.strip() for line in code.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            if not executable_lines:
                return "Pending execute action contains comments only."
            protocol = self._renderer_action_protocol(state, task_id)
            if protocol.get("renderer_composite") and protocol.get("manifest_status") != "registered":
                source = str(action.get("action_source") or "")
                role = str(action.get("command_role") or "")
                if source not in {"renderer", "renderer_compat_adapter"} or role != "renderer_invocation":
                    return (
                        "The current Renderer-backed composite step has no registered manifest. "
                        "Only a structured RENDERER_INVOCATION may execute; ordinary EXECUTE, probes, "
                        "and STEP_COMPLETION_CANDIDATE are rejected before execution."
                    )
            if str(action.get("action_source") or "") == "manifest_runner":
                _, step = current_plan_step_with_index(plan_state)
                step = step or {}
                renderer_execution = dict(step.get("renderer_execution") or {})
                if str(action.get("manifest_fingerprint") or "") != str(renderer_execution.get("manifest_fingerprint") or ""):
                    return "Pending manifest action does not match the registered manifest fingerprint."
                manifest_path = str(renderer_execution.get("manifest_path") or "")
                try:
                    manifest = normalize_renderer_manifest(
                        json.loads(Path(manifest_path).read_text(encoding="utf-8")),
                        manifest_path,
                    )
                    manifest_action = next(
                        item for item in manifest.get("actions", [])
                        if str(item.get("id") or "") == str(action.get("manifest_action_id") or "")
                    )
                    declared_script = resolve_renderer_action_script(manifest, manifest_action)
                    declared_script_hash = hashlib.sha256(declared_script.read_bytes()).hexdigest()
                except (OSError, json.JSONDecodeError, StopIteration, ValueError) as exc:
                    return f"Manifest action contract is invalid: {exc}"
                if str(declared_script) not in code:
                    return (
                        f"Pending command does not execute the manifest-declared script {declared_script}; "
                        f"requested action={action.get('manifest_action_id') or '<missing>'}."
                    )
                if str(action.get("manifest_script_hash") or "") != declared_script_hash:
                    return "Pending manifest action script changed after it was selected; reschedule it from the manifest."
            else:
                _, step = current_plan_step_with_index(plan_state)
                renderer_execution = dict((step or {}).get("renderer_execution") or {})
                if renderer_execution.get("manifest_path") and str(action.get("command_role") or "") not in {
                    "diagnostic", "recovery",
                }:
                    return (
                        "A renderer manifest is already registered for the current composite step. "
                        "Only the controller-selected manifest action may execute; free-form Generate commands are rejected."
                    )
        records = RecoveryStateStore(self.data_path, task_id).load_execution_records()
        same = [row for row in records if str(row.get("action_id") or row.get("execution_id") or "") == str(action.get("action_id"))]
        script_hash = str(action.get("manifest_script_hash") or "")
        if script_hash:
            same = [row for row in same if str(row.get("manifest_script_hash") or "") == script_hash]
        if any(str(row.get("execution_status") or "").lower() == "succeeded" or row.get("exit_code") == 0 for row in same):
            return "Pending action was already executed successfully."
        if len(same) >= 2:
            return "Pending action exhausted its duplicate execution budget."
        return ""

    def _increment_durable_budget(
        self,
        task_id: str,
        bucket: str,
        key: str,
        *,
        limit: int,
    ) -> Tuple[int, int]:
        """Increment an existing checkpoint budget so /resume keeps the count."""
        plan_state = load_plan_state(self.data_path, task_id)
        task_state = dict(plan_state.get("task_state") or {})
        budgets = dict(task_state.get("budgets") or {})
        counters = dict(budgets.get(bucket) or {})
        counter_key = str(key or "task")
        count = int(counters.get(counter_key) or 0) + 1
        counters[counter_key] = count
        budgets[bucket] = counters
        budgets[f"remaining_{bucket}"] = max(0, int(limit) - count)
        task_state["budgets"] = budgets
        plan_state["task_state"] = task_state
        save_plan_state(self.data_path, task_id, plan_state)
        return count, max(0, int(limit) - count)

    def _pending_action_stale_reason(self, action: Dict[str, Any], state: AgentState, task_id: str) -> str:
        if not isinstance(action, dict) or not action:
            return "No structured pending action is available."
        plan_state = load_plan_state(self.data_path, task_id)
        step = self._running_plan_step(plan_state) or {}
        expected = {
            "step_id": str(step.get("step_id") or ""),
            "task_revision": str(self._task_revision_for_state(dict(state))),
            "plan_version": str(plan_state.get("plan_version") or "plan_v2"),
        }
        missing = [key for key in expected if str(action.get(key) or "") == ""]
        if missing:
            return "Pending action is missing revision metadata: " + ", ".join(missing)
        mismatches = [
            f"{key}={action.get(key)!r} (expected {value!r})"
            for key, value in expected.items()
            if str(action.get(key)) != value
        ]
        return "Pending action is stale: " + "; ".join(mismatches) if mismatches else ""

    def _validate_generate_context(self, state: AgentState, task_id: str) -> Dict[str, Any]:
        """Use the canonical Plan validator for every managed Generate entry."""
        if str(state.get("task_mode") or "managed").lower() != "managed":
            return {"valid": True, "reason_code": "lightweight_task", "missing_fields": []}
        plan_state = load_plan_state(self.data_path, task_id)
        validation = validate_plan_for_generate(
            plan_state,
            self._task_revision_for_state(dict(state)),
        )
        task_spec = state.get("task_spec") or plan_state.get("task_spec") or {}
        if validation.get("valid") and (
            not isinstance(task_spec, dict) or not str(task_spec.get("goal") or "").strip()
        ):
            validation = {
                **validation,
                "valid": False,
                "reason_code": "missing_task_spec_goal",
                "missing_fields": ["task_spec.goal"],
            }
        return validation

    def _generate_plan_redirect(
        self,
        state: AgentState,
        task_id: str,
        validation: Dict[str, Any],
        *,
        message: str = "",
    ) -> Dict[str, Any]:
        """Persist and log one observable Generate -> Planner decision."""
        plan_state = load_plan_state(self.data_path, task_id)
        steps = [step for step in plan_state.get("steps", []) or [] if isinstance(step, dict)]
        current_index = int(validation.get("current_step_index", -1))
        if current_index < 0:
            current_index, _ = current_plan_step_with_index(plan_state)
        current_step = steps[current_index] if 0 <= current_index < len(steps) else {}
        audit = {
            "redirect_reason": str(validation.get("reason_code") or "invalid_plan"),
            "validation_function": str(validation.get("validation_function") or "validate_plan_for_generate"),
            "task_id": task_id,
            "task_revision": self._task_revision_for_state(dict(state)),
            "plan_revision": int(plan_state.get("plan_revision") or 0),
            "plan_status": str(plan_state.get("plan_status") or ""),
            "current_step_id": str(validation.get("current_step_id") or current_step.get("step_id") or ""),
            "current_step_index": current_index,
            "missing_fields": list(validation.get("missing_fields") or []),
            "plan_source": "plan_state.json" if plan_state.get("steps") else "none",
            "plan_state_path": str(plan_state_path(self.data_path, task_id)),
            "migration_applied": list(plan_state.get("migrations_applied") or []),
        }
        logger.warning("Generate redirected to Planner: %s", json.dumps(audit, ensure_ascii=False, sort_keys=True))
        detail = json.dumps(audit, ensure_ascii=False, sort_keys=True)
        plan_progress = self._record_plan_current_evidence(task_id, "generate_plan_redirect", detail)
        user_message = message or (
            f"Generate cannot continue ({audit['redirect_reason']}). "
            f"Missing fields: {', '.join(audit['missing_fields']) or 'none'}. "
            "Planner must repair the canonical current-step contract."
        )
        return {
            "messages": [SystemMessage(content=user_message)],
            "plan_progress": plan_progress,
            "pending_action": {},
            "resume_audit": audit,
            "required_next_action": "PLAN_PATCH",
            "failure_owner": RecoveryFailureOwner.PLAN.value,
            "recovery_scope": "CURRENT_STEP",
            "recovery_phase": "plan_repair",
            "next_step": "planner",
        }

    def _pause_retryable_llm_failure(
        self,
        state: AgentState,
        task_id: str,
        stage: str,
        exc: Exception,
    ) -> Dict[str, Any]:
        """Persist transient LLM failure metadata without invalidating valid business state."""
        summary = llm_failure_summary(exc, stage, task_id)
        logger.error(summary)
        failure_id = "failure_" + hashlib.sha256(
            f"{task_id}|{stage}|{type(exc).__name__}|{summary}".encode("utf-8")
        ).hexdigest()[:16]
        plan_state = load_plan_state(self.data_path, task_id)
        prior_failure = plan_state.get("failure") if isinstance(plan_state.get("failure"), dict) else {}
        retry_count = int(prior_failure.get("retry_count") or 0) + 1
        plan_state["failure"] = {
            "failure_type": type(exc).__name__,
            "failure_stage": stage,
            "failure_id": failure_id,
            "retry_count": retry_count,
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
            "task_revision": self._task_revision_for_state(dict(state)),
            "plan_revision": int(plan_state.get("plan_revision") or 0),
            "plan_status": str(plan_state.get("plan_status") or ""),
            "current_step_id": str((self._running_plan_step(plan_state) or {}).get("step_id") or ""),
            "summary": summary,
        }
        save_plan_state(self.data_path, task_id, plan_state)
        if self.state_manager:
            try:
                self.state_manager.update_task_status(task_id, "paused_retryable")
            except Exception:
                pass
        return {
            "messages": [
                SystemMessage(
                    content=(
                        "<llm_retryable_pause>\n"
                        f"{summary}\n"
                        "The task is paused_retryable. Existing plan, approval, current step, skills and inputs were preserved.\n"
                        "</llm_retryable_pause>"
                    )
                )
            ],
            "failure_type": type(exc).__name__,
            "failure_stage": stage,
            "failure_id": failure_id,
            "recovery_status": "paused_retryable",
            "next_step": "finish",
        }

    def _current_action_contract(self, state: AgentState, task_id: str) -> str:
        plan_state = load_plan_state(self.data_path, task_id)
        feedback = self._fresh_supervisor_feedback(state, task_id)
        fixes = [str(item) for item in feedback.get("required_fix", []) if str(item).strip()]
        structured_recovery = bool(state.get("recovery_context") or state.get("recovery_decision"))
        if structured_recovery:
            fixes = []
        protocol = self._renderer_action_protocol(state, task_id)
        lines = [
            f"task_revision: {self._task_revision_for_state(dict(state))}",
            f"plan_version: {str(plan_state.get('plan_version') or 'plan_v2')}",
            "Generate exactly one structured action for the canonical current step; do not execute later steps.",
            "The plan is approved. Do not ask whether to continue; HITL handles execution approval.",
            "Allowed actions and manifest status are authoritative in CurrentStepContext.",
        ]
        if protocol.get("renderer_composite"):
            lines.extend([
                f"canonical_renderer_id: {protocol.get('renderer_id') or '<missing>'}",
                "renderer_argument_schema: " + json.dumps(
                    protocol.get("argument_schema") or {}, ensure_ascii=False, sort_keys=True
                ),
                "formal_verified_inputs: " + json.dumps(
                    protocol.get("resolved_inputs") or [], ensure_ascii=False, sort_keys=True
                ),
                "projected_renderer_arguments: " + json.dumps(
                    protocol.get("argument_projection") or {}, ensure_ascii=False, sort_keys=True
                ),
                (
                    "Only the actions listed above are valid until the Renderer manifest is registered."
                    if protocol.get("manifest_status") != "registered"
                    else "The Manifest Runner owns the next action; do not generate a free-form command."
                ),
            ])
        records = [] if structured_recovery else RecoveryStateStore(self.data_path, task_id).load_execution_records()
        eligible = [
            record
            for record in records
            if isinstance(record, dict)
            and str(record.get("execution_status") or "").lower() == "succeeded"
            and (record.get("exit_code") in (None, "", 0, "0"))
        ][-3:]
        if eligible:
            lines.append("eligible_execution_evidence:")
            for record in eligible:
                evidence_id = str(
                    record.get("execution_id")
                    or record.get("action_id")
                    or record.get("attempt_id")
                    or ""
                )
                lines.append(
                    "- id={}; source_step={}; purpose={}".format(
                        evidence_id,
                        str(record.get("step_id") or ""),
                        str(record.get("purpose") or ""),
                    )
                )
            lines.append(
                "If this evidence already satisfies every success criterion for the current step, return "
                "STEP_COMPLETION_CANDIDATE with the current step_id and cited evidence_ids."
            )
        if fixes:
            lines.append("required_fix: " + " | ".join(fixes[:3]))
        return "\n".join(lines)

    @staticmethod
    def _step_local_messages(
        messages: List[BaseMessage],
        *,
        max_messages: int = LLM_STEP_CONTEXT_MESSAGES,
        max_chars: int = LLM_STEP_CONTEXT_CHARS,
        structured_recovery: bool = False,
    ) -> List[BaseMessage]:
        """Select evidence for the current step without mutating audit history.

        The graph keeps the complete message list for persistence and recovery.
        LLM calls receive only the original goal, explicit user answer, latest
        observation, latest control/recovery feedback, and latest action.
        """
        if not messages:
            return []

        selected_indexes: set[int] = set()

        def add(index: int) -> None:
            if 0 <= index < len(messages):
                selected_indexes.add(index)

        first_human_index = next(
            (index for index, message in enumerate(messages) if isinstance(message, HumanMessage)),
            None,
        )
        if first_human_index is not None:
            add(first_human_index)

        for index in range(len(messages) - 1, -1, -1):
            content = str(getattr(messages[index], "content", messages[index]) or "")
            if isinstance(messages[index], HumanMessage) and "<human_answer" in content:
                add(index)
                break

        if structured_recovery:
            ordered = [messages[index] for index in sorted(selected_indexes)]
            return BioAnalystAgent._bounded_message_history(
                ordered,
                max_messages=min(2, max_messages),
                max_chars=min(4000, max_chars),
            )

        for index in range(len(messages) - 1, -1, -1):
            content = str(getattr(messages[index], "content", messages[index]) or "")
            if "<observation>" in content:
                add(index)
                break

        control_markers = (
            "<supervisor_feedback",
            "<consultant_validation_error>",
            "<recovery_",
            "<action_format_repair>",
            "<step_completion_candidate_rejected>",
            "<knowledge_conflict",
            "step_completion_gate_failed",
            "final_completion_validation_failed",
            "llm_transient_failure",
            "llm_transient_retry",
        )
        for index in range(len(messages) - 1, -1, -1):
            content = str(getattr(messages[index], "content", messages[index]) or "")
            lower = content.lower()
            if any(marker in lower for marker in control_markers):
                add(index)
                break

        action_prefixes = (
            "EXECUTE",
            "SEARCH",
            "LOAD_",
            "ASK",
            "FINISH",
            "<action_json>",
        )
        for index in range(len(messages) - 1, -1, -1):
            content = str(getattr(messages[index], "content", messages[index]) or "").strip()
            if isinstance(messages[index], AIMessage) and (
                content.startswith(action_prefixes)
                or "<execute>" in content
                or "<install_tool" in content
                or "<activate_batch" in content
            ):
                add(index)
                break

        if not selected_indexes:
            add(len(messages) - 1)
        ordered = [messages[index] for index in sorted(selected_indexes)]
        return BioAnalystAgent._bounded_message_history(
            ordered,
            max_messages=max_messages,
            max_chars=max_chars,
        )

    @staticmethod
    def _generate_visible_messages(
        messages: List[BaseMessage],
        *,
        structured_recovery: bool = False,
    ) -> List[BaseMessage]:
        """Drop Consultant internals, then keep only current-step evidence."""
        filtered: List[BaseMessage] = []
        internal_prefixes = ("LOAD_WORKFLOW_TEMPLATE", "LOAD_SKILL_DOC", "LOAD_REFERENCE", "LOAD_SCRIPT")
        internal_tags = (
            "<loaded_workflow_template",
            "<loaded_skill_doc",
            "<loaded_skill_reference",
            "<loaded_skill_script",
            "<load_error action=\"LOAD_",
            "<consultant_validation_error>",
        )
        for message in messages:
            content = str(getattr(message, "content", "")).strip()
            if isinstance(message, AIMessage) and content.startswith(internal_prefixes):
                continue
            if isinstance(message, SystemMessage) and any(tag in content for tag in internal_tags):
                continue
            filtered.append(message)
        return BioAnalystAgent._step_local_messages(
            filtered,
            structured_recovery=structured_recovery,
        )

    def _current_step_prompt_context(
        self,
        task_id: str,
        current_plan: str = "",
        plan_progress: str = "",
        state: Optional[AgentState] = None,
    ) -> str:
        """Serialize the controller-owned CurrentStepContext exactly once."""
        plan_state = load_plan_state(self.data_path, task_id) if task_id else {}
        state_map = dict(state or {})
        protocol = self._renderer_action_protocol(state_map, task_id) if state is not None else {}
        current_step = build_current_step_context(
            state_map,
            plan_state,
            allowed_actions=protocol.get("allowed_actions") or None,
        ).to_dict()
        if not current_step.get("step_id"):
            next_match = re.search(
                r"^Next step:\s*(.+)$",
                str(plan_progress or ""),
                re.MULTILINE | re.IGNORECASE,
            )
            current_step["title"] = (
                next_match.group(1).strip() if next_match else "No pending step."
            )
        return (
            "<current_step_context>\n"
            + json.dumps(current_step, ensure_ascii=False, sort_keys=True, default=str)
            + "\n</current_step_context>"
        )

    @staticmethod
    def _format_workflow_handoff_for_prompt(handoff: Dict[str, Any]) -> str:
        if not handoff:
            return ""
        lines = []
        workflow_id = handoff.get("workflow_id") or "none"
        lines.append(f"workflow_id: {workflow_id}")
        if handoff.get("active_batch_id"):
            lines.append(f"active_batch_id: {handoff.get('active_batch_id')}")
        stages = handoff.get("workflow_stages") or []
        if stages:
            lines.append("workflow_stages:")
            for stage in stages:
                lines.append(
                    "- "
                    f"stage={stage.get('stage', '')}; "
                    f"status={stage.get('status', '')}; "
                    f"skill={stage.get('skill', 'none')}; "
                    f"batch_id={stage.get('batch_id', '')}; "
                    f"reason={stage.get('reason', '')}"
                )
        return "\n".join(lines)

    def _activate_workflow_batch(self, state: AgentState, batch_id: str) -> Dict[str, Any]:
        handoff = self._workflow_state_from_mapping(state)
        stages = [dict(stage) for stage in handoff.get("workflow_stages", state.get("workflow_stages", [])) or []]
        if not stages:
            return {
                "messages": [HumanMessage(content="<observation>❌ No workflow stages are available to activate.</observation>")],
                "next_step": "generate",
            }
        target_batch = str(batch_id or "").strip()
        active_skills: List[str] = []
        future_skills: List[str] = []
        for stage in stages:
            stage_batch = str(stage.get("batch_id") or "").strip()
            is_active = bool(target_batch and stage_batch == target_batch)
            stage["active_now"] = is_active
            if stage.get("status") == "selected_skill":
                skill = str(stage.get("skill") or "").strip()
                normalized = self.skill_manager.normalize_selected_skills([skill], max_results=1) if self.skill_manager else [skill]
                skill = normalized[0] if normalized else ""
                if is_active and skill and skill not in active_skills:
                    active_skills.append(skill)
                elif skill and skill not in future_skills:
                    future_skills.append(skill)
        if not active_skills:
            return {
                "messages": [HumanMessage(content=f"<observation>❌ Batch {target_batch} has no selected skills.</observation>")],
                "next_step": "generate",
            }
        handoff["workflow_stages"] = stages
        handoff["active_batch_id"] = target_batch
        handoff["active_skills"] = active_skills
        handoff["future_skills"] = future_skills
        if not handoff.get("required_skills_all"):
            ordered_all: List[str] = []
            for skill in active_skills + future_skills:
                if skill not in ordered_all:
                    ordered_all.append(skill)
            handoff["required_skills_all"] = ordered_all
        result = validate_workflow_handoff(
            handoff,
            self.skill_manager,
            self.workflow_manager,
            max_active_skills=SkillManager.MAX_ACTIVE_SKILLS,
        )
        if result["errors"]:
            obs = "<observation>❌ Workflow batch activation failed:\n" + "\n".join(result["errors"]) + "\n</observation>"
            return {"messages": [HumanMessage(content=obs)], "next_step": "generate"}
        handoff = result["handoff"]
        obs = (
            f"<observation>✅ Activated workflow batch {target_batch}. "
            f"Active skills: {', '.join(handoff.get('active_skills', []))}</observation>"
        )
        return {
            "messages": [HumanMessage(content=obs)],
            "workflow_state": handoff,
            "workflow_handoff": handoff,
            "workflow_stages": handoff.get("workflow_stages", []),
            "active_batch_id": handoff.get("active_batch_id", target_batch),
            "active_skills": handoff.get("active_skills", []),
            "future_skills": handoff.get("future_skills", []),
            "required_skills_all": handoff.get("required_skills_all", []),
            "selected_skills": (state.get("selected_skills", []) or handoff.get("required_skills_all", [])),
            "next_step": "generate",
        }

    def _current_plan_step_skill_ids(self, task_id: str) -> Tuple[List[str], bool, Dict[str, Any]]:
        plan_state = load_plan_state(self.data_path, task_id)
        step = self._running_plan_step(plan_state)
        if not step:
            step = next(
                (
                    item for item in plan_state.get("steps", []) or []
                    if isinstance(item, dict) and str(item.get("status") or "").lower() == "pending"
                ),
                {},
            )
        raw = [str(item).strip() for item in step.get("skill_ids", []) or [] if str(item).strip()]
        # A persisted non-empty machine mapping is authoritative even if an
        # older PlanPatch lost the derived skills_declared marker.
        declared = bool(step.get("skills_declared") or raw)
        normalized = (
            self.skill_manager.normalize_selected_skills(raw, max_results=SkillManager.MAX_SELECTED_SKILLS)
            if self.skill_manager and raw else raw
        )
        if self.skill_manager:
            step = dict(step)
            step["unresolved_skill_ids"] = [
                item for item in raw
                if not self.skill_manager.normalize_selected_skills([item], max_results=1)
            ]
        return normalized, declared, step

    def _plan_step_skill_violations(self, plan_state: Dict[str, Any], selected_skills: List[str]) -> List[str]:
        selected = set(self._selected_skill_ids(selected_skills))
        violations: List[str] = []
        steps = [step for step in plan_state.get("steps", []) or [] if isinstance(step, dict)]
        if not steps:
            return ["Plan must include a machine-trackable Execution Checklist with explicit `Skills:` fields."]
        for step in steps:
            step_id = str(step.get("step_id") or "unknown")
            if not step.get("skills_declared"):
                violations.append(f"{step_id} must declare `Skills: [...]` explicitly.")
                continue
            raw = [str(item).strip() for item in step.get("skill_ids", []) or [] if str(item).strip()]
            normalized = (
                self.skill_manager.normalize_selected_skills(raw, max_results=SkillManager.MAX_SELECTED_SKILLS)
                if self.skill_manager and raw else raw
            )
            unknown = [
                item for item in raw
                if self.skill_manager and not self.skill_manager.normalize_selected_skills([item], max_results=1)
            ]
            if unknown:
                violations.append(f"{step_id} declares unknown skill IDs: {', '.join(unknown)}.")
            if len(normalized) > SkillManager.MAX_ACTIVE_SKILLS:
                violations.append(
                    f"{step_id} declares {len(normalized)} skills; split it so each step has at most "
                    f"{SkillManager.MAX_ACTIVE_SKILLS}."
                )
            outside = [skill_id for skill_id in normalized if skill_id not in selected]
            if outside:
                violations.append(
                    f"{step_id} uses skills outside Consultant SELECTED_SKILLS: {', '.join(outside)}."
                )
            if not unknown and not outside and len(normalized) <= SkillManager.MAX_ACTIVE_SKILLS:
                step["skill_ids"] = normalized
        return violations

    def _active_skill_update_for_current_plan_step(self, state: AgentState, task_id: str) -> Dict[str, Any]:
        selected_pool = self._selected_skill_ids(state.get("selected_skills", []) or [])
        active, declared, step = self._current_plan_step_skill_ids(task_id)
        if not declared:
            return {
                "active_skills": [],
                "future_skills": [],
                "skill_activation_error_field": "current_step.skills_declared",
                "skill_activation_error": (
                    f"Plan step {step.get('step_id', 'unknown')} has no explicit Skills field; "
                    "Planner must repair the step-to-skill mapping."
                ),
            }
        outside = [skill_id for skill_id in active if skill_id not in selected_pool]
        unresolved = list(step.get("unresolved_skill_ids") or [])
        if unresolved or outside or len(active) > SkillManager.MAX_ACTIVE_SKILLS:
            return {
                "active_skills": [],
                "future_skills": [],
                "skill_activation_error_field": "current_step.skill_ids",
                "skill_activation_error": (
                    f"Plan step {step.get('step_id', 'unknown')} has invalid skill mapping: "
                    f"unknown={unresolved}, outside_selected={outside}, count={len(active)}."
                ),
            }
        current_active = self._selected_skill_ids(state.get("active_skills", []) or [])
        current_future = self._selected_skill_ids(state.get("future_skills", []) or [])
        current_stage = str((step.get("workflow_stage_ids") or [""])[0] or "")
        current_batch = str(step.get("batch_id") or "")
        if (
            current_active == active
            and str(state.get("current_stage_id") or "") == current_stage
            and str(state.get("active_batch_id") or "") == current_batch
            and not current_future
            and not str(state.get("skill_activation_error") or "").strip()
        ):
            return {}
        return {
            "active_skills": active,
            "future_skills": [],
            "selected_skills": selected_pool,
            "current_stage_id": current_stage,
            "active_batch_id": current_batch,
            "skill_selection_source": "plan_step_declared_skills",
            "skill_activation_error": "",
            "skill_activation_error_field": "",
        }

    def _narrow_skills_to_current_plan_step(self, skill_ids: List[str], task_id: str) -> List[str]:
        active, declared, _ = self._current_plan_step_skill_ids(task_id)
        if not declared:
            return []
        allowed = set(self._selected_skill_ids(skill_ids or []))
        return [skill_id for skill_id in active if skill_id in allowed][: SkillManager.MAX_ACTIVE_SKILLS]

    @staticmethod
    def _workflow_batch_sort_key(batch_id: str) -> Tuple[int, str]:
        text = str(batch_id or "").strip()
        if text.isdigit():
            return (int(text), text)
        return (10**9, text)

    def _next_workflow_batch_id(self, state: AgentState) -> str:
        handoff = self._workflow_state_from_mapping(state)
        stages = [dict(stage) for stage in handoff.get("workflow_stages", (state or {}).get("workflow_stages", [])) or []]
        active_batch_id = str((state or {}).get("active_batch_id") or handoff.get("active_batch_id") or "").strip()
        future_skills = set(self._selected_skill_ids((state or {}).get("future_skills", []) or handoff.get("future_skills", []) or []))
        if not stages or not active_batch_id:
            return ""

        candidates: List[str] = []
        for stage in stages:
            if stage.get("status") != "selected_skill":
                continue
            batch_id = str(stage.get("batch_id") or "").strip()
            if not batch_id or batch_id == active_batch_id:
                continue
            skill = str(stage.get("skill") or "").strip()
            canonical = self._selected_skill_ids([skill])
            skill_id = canonical[0] if canonical else skill
            if future_skills and skill_id not in future_skills:
                continue
            if self._workflow_batch_sort_key(batch_id) <= self._workflow_batch_sort_key(active_batch_id):
                continue
            if batch_id not in candidates:
                candidates.append(batch_id)
        if not candidates:
            return ""
        return sorted(candidates, key=self._workflow_batch_sort_key)[0]

    def _activate_next_workflow_batch_after_plan_completion(
        self,
        state: AgentState,
        task_id: str,
    ) -> Optional[Dict[str, Any]]:
        next_batch_id = self._next_workflow_batch_id(state)
        if not next_batch_id:
            return None
        activation = self._activate_workflow_batch(state, next_batch_id)
        if not activation.get("active_skills"):
            return None
        reset_plan_state(self.data_path, task_id)
        activation["next_step"] = "planner"
        activation["current_plan"] = ""
        activation["plan_progress"] = ""
        activation.setdefault("messages", []).append(
            SystemMessage(
                content=(
                    f"Previous workflow batch completed. Activated batch {next_batch_id}; "
                    "create a new plan for the newly active skills. Do not finish the overall task yet."
                )
            )
        )
        return activation

    def _stable_prompt_paths(self, session_id: str, project_id: str) -> Tuple[Path, Path]:
        stem = self._safe_prompt_stem(f"{session_id}__{project_id}")
        return (
            self._stable_prompt_dir / f"{stem}.prompt.md",
            self._stable_prompt_dir / f"{stem}.meta.json",
        )

    @staticmethod
    def _read_json_file(path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _safe_prompt_stem(value: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip())
        return safe[:180] or "session"

    @staticmethod
    def _clip_prompt_text(text: str, max_chars: int) -> str:
        text = str(text)
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n...[truncated]"

    @staticmethod
    def _clip_observation_text(text: str, max_chars: int) -> str:
        text = str(text or "")
        if len(text) <= max_chars:
            return text
        if max_chars < 80:
            return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"
        head = max_chars // 2
        tail = max_chars - head
        omitted = len(text) - max_chars
        return text[:head] + f"\n...[truncated {omitted} chars]...\n" + text[-tail:]

    @staticmethod
    def _consultant_unbounded_output_violation(code: str) -> str:
        """Reject raw whole-file viewers in Consultant's read-only checks.

        Consultant needs filesystem evidence, not full biological data in the
        terminal.  The rule is deliberately format-agnostic and still permits
        a bounded ``cat file | head -n N`` preview when that is useful.
        """
        sequence_path = re.compile(r"\.(?:fa|fasta|fna|fq|fastq)(?:\.gz)?(?:$|[\s'\"`|;&])", re.IGNORECASE)
        for line in str(code or "").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not re.search(r"(?:^|[;&|]\s*)(?:cat|zcat|bzcat|xzcat)\s+", stripped):
                continue
            if re.search(r"\|\s*(?:head|tail)\b", stripped):
                if sequence_path.search(stripped) and not re.search(r"\b(?:head|tail)\s+-c\s+\d+\b", stripped):
                    return (
                        "Consultant read-only output guard blocked a line-bounded preview of a sequence file. "
                        "FASTA/FASTQ may contain one very long sequence line; use `head -c 2048` or a targeted "
                        "header/count query instead of `head -n`/`tail -n`."
                    )
                continue
            if sequence_path.search(stripped) and re.search(r"\b(?:head|tail)(?:\s|$)", stripped):
                return (
                    "Consultant read-only output guard blocked an unbounded sequence preview. "
                    "Use `head -c 2048` or a targeted header/count query."
                )
            return (
                "Consultant read-only output guard blocked a whole-file viewer. "
                "Use a bounded preview such as `head -n 20 <file>`, a targeted header/query, "
                "or file size/path verification; do not print an entire input file."
            )
        return ""

    def _ensure_plan_tracker(self, task_id: str, current_plan: Optional[str] = None) -> Dict[str, Any]:
        plan_file = default_plan_file(self.data_path, task_id)
        return ensure_plan_state(
            self.data_path,
            task_id,
            plan_text=current_plan or "",
            plan_file=plan_file,
        )

    def _persist_plan_tracker(self, task_id: str, state: Dict[str, Any]) -> str:
        save_plan_state(self.data_path, task_id, state)
        plan_file = state.get("plan_file") or str(default_plan_file(self.data_path, task_id))
        update_plan_markdown_status(plan_file, state)
        return render_plan_progress(state)

    def _stage_status_candidates_from_text(self, *texts: str) -> List[Path]:
        candidates: List[Path] = []
        seen: set[str] = set()
        bases = [Path(self.data_path), project_root, Path.cwd()]

        def add(path_text: str) -> None:
            cleaned = str(path_text or "").strip().strip("`'\".,;:()[]{}")
            if not cleaned:
                return
            raw = Path(cleaned)
            paths = [raw] if raw.is_absolute() else [base / raw for base in bases]
            for path in paths:
                try:
                    resolved = path.resolve()
                except OSError:
                    resolved = path
                key = str(resolved)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(resolved)

        for text in texts:
            source = str(text or "")
            for match in re.finditer(r"((?:/|[A-Za-z0-9_.+\-]+/)[^\s`'\"<>|]*stage_status\.tsv)", source):
                add(match.group(1))
            for line in source.splitlines():
                if not re.search(r"\b(output\s+dir(?:ectory)?|输出目录)\b", line, re.IGNORECASE):
                    continue
                for match in re.finditer(r"((?:/|[A-Za-z0-9_.+\-]+/)[^\s`'\"<>|]+)", line):
                    token = match.group(1).strip("`'\".,;:()[]{}")
                    if token:
                        add(str(Path(token) / "stage_status.tsv"))
        return candidates

    def _sync_plan_tracker_from_stage_status(
        self,
        task_id: str,
        state: Dict[str, Any],
        *texts: str,
    ) -> bool:
        evidence_text = json.dumps(state, ensure_ascii=False)[:20000]
        changed = 0
        for path in self._stage_status_candidates_from_text(*texts, evidence_text):
            changed += sync_plan_state_from_stage_status(
                state,
                path,
                evidence=f"terminal stage_status.tsv observed at {path}",
            )
        if changed:
            self._persist_plan_tracker(task_id, state)
            return True
        return False

    def _reconcile_completion_candidate_plan(
        self,
        task_id: str,
        state: Dict[str, Any],
    ) -> Tuple[bool, str]:
        """Reconcile a final claim from formal status evidence without trusting Generate."""
        plan_state = load_plan_state(self.data_path, task_id)
        candidate = state.get("completion_candidate") if isinstance(state.get("completion_candidate"), dict) else {}
        changed = self._sync_plan_tracker_from_stage_status(
            task_id,
            plan_state,
            str(state.get("current_plan") or ""),
            str(candidate.get("text") or ""),
            json.dumps(state.get("task_spec") or {}, ensure_ascii=False),
        )
        return changed, render_plan_progress(load_plan_state(self.data_path, task_id))

    def _start_or_load_plan_progress(self, task_id: str, current_plan: Optional[str], evidence: str = "") -> str:
        state = self._ensure_plan_tracker(task_id, current_plan)
        if state.get("steps"):
            mark_first_pending_running(state, evidence=evidence or "generate_started")
            return self._persist_plan_tracker(task_id, state)
        return render_plan_progress(state)

    def _mark_plan_current_done(self, task_id: str, evidence: str, *, source_node: str = "controller") -> str:
        state = load_plan_state(self.data_path, task_id)
        if state.get("steps"):
            _, step = current_plan_step_with_index(state)
            decision = RecoveryPolicy().decide(
                completion_evidence={
                    "completion_satisfied": True,
                    "task_complete": False,
                    "evidence": [str(evidence or "controller_verified")],
                },
                action_id=str((state.get("task_state") or {}).get("current_action_id") or ""),
                step_id=str((step or {}).get("step_id") or ""),
                attempt_id=f"complete:{str((step or {}).get('step_id') or 'step')}",
                budgets=dict((state.get("task_state") or {}).get("budgets") or {}),
            )
            apply_recovery_decision(state, contextualize_recovery_decision(decision, source_node=source_node))
            return self._persist_plan_tracker(task_id, state)
        return render_plan_progress(state)

    def _validate_and_advance_step_candidate(
        self,
        task_id: str,
        candidate: Dict[str, Any],
        *,
        source_node: str = "controller",
    ) -> Tuple[bool, Any, str]:
        """Let CompletionGate validate a Generate claim, then advance in Controller."""
        plan_state = load_plan_state(self.data_path, task_id)
        records = RecoveryStateStore(self.data_path, task_id).load_execution_records()
        gate = CompletionGate.validate_step_completion_candidate(
            self.data_path,
            task_id,
            plan_state,
            candidate,
            records,
        )
        if not gate.passed:
            return False, gate, render_plan_progress(plan_state)

        record = dict((gate.reconciliation or {}).get("record") or {})
        step_id = str(candidate.get("step_id") or "")
        attached = attach_execution_evidence_relation(
            plan_state,
            step_id,
            record,
            reason=str(candidate.get("reason") or ""),
        )
        completion_reason = (
            "controller_verified_step_completion_candidate: "
            + ", ".join(str(item) for item in candidate.get("evidence_ids", []) or [])
        )
        decision = RecoveryPolicy().decide(
            completion_evidence={
                "completion_satisfied": bool(attached),
                "task_complete": False,
                "evidence": [completion_reason],
            },
            action_id=str(record.get("action_id") or record.get("execution_id") or ""),
            step_id=step_id,
            attempt_id=str(record.get("attempt_id") or record.get("execution_id") or ""),
            budgets=dict((plan_state.get("task_state") or {}).get("budgets") or {}),
        )
        if not attached or decision.kind != RecoveryDecisionKind.COMPLETE_STEP.value:
            gate.passed = False
            gate.message = "Controller could not apply the verified step transition."
            gate.feedback = "The current step changed while its completion candidate was being validated. Regenerate for the new step."
            gate.classification = "step_candidate_transition_stale"
            return False, gate, render_plan_progress(plan_state)
        apply_recovery_decision(
            plan_state,
            contextualize_recovery_decision(decision, source_node=source_node),
        )
        return True, gate, self._persist_plan_tracker(task_id, plan_state)

    def _advance_satisfied_validation_steps(
        self,
        task_id: str,
        execution_evidence: Dict[str, Any],
        *,
        max_steps: int = 4,
        source_node: str = "controller",
    ) -> Tuple[int, str]:
        """Converge consecutive validation steps already covered by one execution."""
        evidence_id = str(
            execution_evidence.get("execution_id")
            or execution_evidence.get("action_id")
            or execution_evidence.get("attempt_id")
            or ""
        )
        if not evidence_id:
            return 0, render_plan_progress(load_plan_state(self.data_path, task_id))
        advanced = 0
        progress = render_plan_progress(load_plan_state(self.data_path, task_id))
        for _ in range(max_steps):
            plan_state = load_plan_state(self.data_path, task_id)
            _, step = current_plan_step_with_index(plan_state)
            if not step or str(step.get("purpose") or "").strip().lower() != "validation":
                break
            candidate = {
                "step_id": str(step.get("step_id") or ""),
                "reason": "Persisted execution evidence already satisfies this consecutive validation step.",
                "evidence_ids": [evidence_id],
            }
            passed, _, progress = self._validate_and_advance_step_candidate(
                task_id,
                candidate,
                source_node=source_node,
            )
            if not passed:
                break
            advanced += 1
        return advanced, progress

    def _mark_plan_current_blocked(self, task_id: str, evidence: str) -> str:
        return self._finalize_blocked_state(task_id, evidence)

    def _plan_has_pending_after_block(self, task_id: str) -> bool:
        state = load_plan_state(self.data_path, task_id)
        for step in state.get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            if str(step.get("status") or "").strip().lower() == "pending":
                return True
        return False

    def _next_step_after_runtime_block(self, task_id: str) -> str:
        return "finish"

    def _record_plan_current_evidence(self, task_id: str, event: str, detail: str = "") -> str:
        state = load_plan_state(self.data_path, task_id)
        if state.get("steps"):
            record_current_step_evidence(state, event, detail)
            return self._persist_plan_tracker(task_id, state)
        return render_plan_progress(state)

    def _record_generate_action(self, task_id: str, step_id: str) -> None:
        plan_state = load_plan_state(self.data_path, task_id)
        increment_plan_budget(plan_state, "generate_action_count")
        task_state = dict(plan_state.get("task_state") or {})
        budgets = dict(task_state.get("budgets") or {})
        by_step = dict(budgets.get("generate_action_count_by_step") or {})
        by_step[str(step_id or "lightweight")] = int(by_step.get(str(step_id or "lightweight")) or 0) + 1
        budgets["generate_action_count_by_step"] = by_step
        task_state["budgets"] = budgets
        plan_state["task_state"] = task_state
        save_plan_state(self.data_path, task_id, plan_state)

    def _record_action_format_failure(
        self,
        task_id: str,
        step_id: str,
        response_text: str,
        schema_error: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, int, str]:
        response_hash = hashlib.sha256(
            str(response_text or "").strip().encode("utf-8", errors="replace")
        ).hexdigest()[:20]
        plan_state = load_plan_state(self.data_path, task_id)
        task_state = dict(plan_state.get("task_state") or {})
        budgets = dict(task_state.get("budgets") or {})
        step_key = str(step_id or "lightweight")
        current_count = int((budgets.get("action_format_repair_by_step") or {}).get(step_key) or 0)
        prior_duplicate_count = int(
            ((budgets.get("invalid_action_hashes_by_step") or {}).get(step_key) or {}).get(response_hash)
            or 0
        )
        scoped_budgets = {**budgets, "action_format_repair_count": current_count}
        policy = RecoveryPolicy(max_format_repairs=1)
        decision = policy.decide(
            pre_action_evidence={
                "valid": False,
                "repairable": True,
                "violations": [
                    "renderer_invocation_contract_error" if schema_error else "unrecognized_action",
                    f"invalid_response_hash={response_hash}",
                ],
            },
            action_id=f"generate:{step_key}",
            step_id=step_key,
            attempt_id=f"format-{current_count + 1}:{response_hash}",
            budgets=scoped_budgets,
        )
        decision = contextualize_recovery_decision(
            decision,
            action_id=f"generate:{step_key}",
            step_id=step_key,
            attempt_id=f"format-{current_count + 1}:{response_hash}",
            source_node="generate",
        )
        apply_recovery_decision(plan_state, decision)
        save_plan_state(self.data_path, task_id, plan_state)
        persisted_budgets = dict((plan_state.get("task_state") or {}).get("budgets") or {})
        count = int((persisted_budgets.get("action_format_repair_by_step") or {}).get(step_key) or 0)
        duplicate_count = int(
            ((persisted_budgets.get("invalid_action_hashes_by_step") or {}).get(step_key) or {}).get(response_hash)
            or 0
        )
        if duplicate_count <= prior_duplicate_count:
            hashes_by_step = dict(persisted_budgets.get("invalid_action_hashes_by_step") or {})
            hashes = dict(hashes_by_step.get(step_key) or {})
            duplicate_count = prior_duplicate_count + 1
            hashes[response_hash] = duplicate_count
            hashes_by_step[step_key] = hashes
            persisted_budgets["invalid_action_hashes_by_step"] = hashes_by_step
            task_state = dict(plan_state.get("task_state") or {})
            task_state["budgets"] = persisted_budgets
            plan_state["task_state"] = task_state
            save_plan_state(self.data_path, task_id, plan_state)
        protocol_dir = Path(self.data_path) / "task_outputs" / str(task_id or "default") / "action_protocol"
        protocol_dir.mkdir(parents=True, exist_ok=True)
        raw_path = protocol_dir / (
            f"{str(step_id or 'lightweight')}.invalid.{count}.{duplicate_count}.{response_hash}.txt"
        )
        raw_path.write_text(str(response_text or ""), encoding="utf-8")
        diagnosis_path = protocol_dir / (
            f"{str(step_id or 'lightweight')}.invalid.{count}.{duplicate_count}.{response_hash}.json"
        )
        schema_error = dict(schema_error or {})
        diagnosis_path.write_text(json.dumps({
            "root_cause": (
                "Generate returned a renderer invocation that failed its declared contract."
                if schema_error
                else "Generate returned no recognized action for the approved current step."
            ),
            "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
            "failure_type": (
                str(schema_error.get("error_type") or "renderer_invocation_schema_error").lower()
                if schema_error
                else "action_schema_error"
            ),
            "evidence": [
                f"raw_response:{raw_path}",
                f"response_hash:{response_hash}",
                *([json.dumps(schema_error, ensure_ascii=False, sort_keys=True)] if schema_error else []),
            ],
            "confidence": 1.0,
            "missing_evidence": [],
            "repair_strategy": (
                "Use the canonical renderer ID and declared invocation fields from the contract error."
                if schema_error
                else "Apply the deterministic action-format repair schema for the unchanged step."
            ),
            "next_action": "ACTION_FORMAT_REPAIR",
            "requires_user_input": False,
            "schema_error": schema_error,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return count, duplicate_count, response_hash

    def _commit_preaction_policy_decision(
        self,
        task_id: str,
        step_id: str,
        violation: str,
        *,
        attempt_id: str,
        source_node: str = "generate",
    ) -> RecoveryDecision:
        plan_state = load_plan_state(self.data_path, task_id)
        budgets = dict((plan_state.get("task_state") or {}).get("budgets") or {})
        decision = RecoveryPolicy().decide(
            pre_action_evidence={
                "valid": False,
                "repairable": True,
                "violations": [str(violation)],
            },
            action_id=f"{source_node}:{step_id or 'task'}",
            step_id=step_id or "task",
            attempt_id=attempt_id,
            budgets=budgets,
        )
        decision = contextualize_recovery_decision(decision, source_node=source_node)
        apply_recovery_decision(plan_state, decision)
        save_plan_state(self.data_path, task_id, plan_state)
        return decision

    def _knowledge_conflict_root(self, task_id: str) -> Path:
        return Path(self.data_path) / "task_outputs" / str(task_id or "default")

    @staticmethod
    def _knowledge_plan_section_lines(plan_text: str, names: Tuple[str, ...]) -> List[str]:
        lines: List[str] = []
        active = False
        level = 0
        wanted = tuple(name.lower() for name in names)
        for raw in str(plan_text or "").splitlines():
            heading = re.match(r"^\s*(#{2,6})\s+(.+?)\s*$", raw)
            if heading:
                current_level = len(heading.group(1))
                title = heading.group(2).lower()
                if any(name in title for name in wanted):
                    active = True
                    level = current_level
                    continue
                if active and current_level <= level:
                    active = False
            if active and raw.strip():
                lines.append(raw.strip())
        return lines

    def _knowledge_claim_registry(
        self,
        task_id: str,
        state: Optional[Dict[str, Any]] = None,
        plan_state: Optional[Dict[str, Any]] = None,
        extra_claims: Optional[Iterable[Any]] = None,
    ) -> ClaimRegistry:
        """Build the shared claim view from existing state without trusting prose as evidence."""
        state = state or {}
        root = self._knowledge_conflict_root(task_id)
        registry = ClaimRegistry.load(root)
        for item in state.get("knowledge_claims", []) or []:
            if isinstance(item, dict):
                registry.register(item)

        for constraint in state.get("user_constraints", []) or []:
            text = str(constraint or "").strip()
            if text:
                registry.register_user_constraint(text, scope="task", scope_id=str(task_id))

        # Consultant prose is a handoff summary, not one atomic fact. Register
        # only user-owned TaskSpec fields so warnings such as "missing candidate"
        # cannot contradict an otherwise valid plan through keyword overlap.
        task_spec = ensure_task_spec(state.get("task_spec") or {}, task_id=task_id)
        for field in (
            "inputs", "parameters", "output_contracts", "must_do", "must_not_do",
            "stop_conditions", "acceptance_criteria", "confirmed_decisions",
        ):
            value = task_spec.get(field)
            items = list(value) if isinstance(value, list) else ([value] if value not in (None, "", {}) else [])
            if isinstance(value, dict):
                items = [{"name": name, "value": item} for name, item in value.items()]
            for index, item in enumerate(items):
                proposition = (
                    json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                    if isinstance(item, dict) else str(item or "").strip()
                )
                if not proposition:
                    continue
                registry.register_proposition(
                    proposition,
                    "task_contract",
                    strength="required",
                    scope="task",
                    scope_id=str(task_id),
                    reliability=0.95,
                    confidence=0.95,
                    attributes={
                        "fact_key": f"task_spec.{field}.{index}",
                        "value": proposition,
                        "task_revision": task_spec_revision(task_spec),
                    },
                )

        plan_state = plan_state or load_plan_state(self.data_path, task_id)
        plan_text = str(state.get("current_plan") or "")
        plan_version = str(plan_state.get("plan_version") or "plan_v1")
        for step in plan_state.get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            step_id = str(step.get("step_id") or "")
            title = str(step.get("title") or "").strip()
            if title:
                registry.register_proposition(
                    title,
                    "plan",
                    strength="required",
                    scope="stage",
                    scope_id=step_id or str(task_id),
                    reliability=0.55,
                    confidence=0.7,
                    attributes={"step_ids": [step_id] if step_id else [], "plan_version": plan_version},
                )
            for key, source_type, strength in (
                ("assumptions", "plan_assumption", "assumption"),
                ("required_constraints", "plan", "required"),
                ("required_inputs", "plan", "required"),
                ("expected_outputs", "plan", "required"),
                ("input_contracts", "plan", "required"),
                ("output_contracts", "plan", "required"),
                ("environment_contract", "plan", "required"),
                ("expected_tool_versions", "plan", "required"),
            ):
                values = step.get(key, [])
                if isinstance(values, str):
                    values = [values]
                elif isinstance(values, dict):
                    values = [f"{name}={value}" for name, value in values.items()]
                for value in values if isinstance(values, list) else []:
                    if isinstance(value, dict):
                        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
                    else:
                        text = str(value or "").strip()
                    if not text:
                        continue
                    registry.register_proposition(
                        text,
                        source_type,
                        strength=strength,
                        scope="stage",
                        scope_id=step_id or str(task_id),
                        reliability=0.5 if source_type == "plan_assumption" else 0.6,
                        confidence=0.65,
                        attributes={"step_ids": [step_id] if step_id else [], "plan_version": plan_version},
                    )
            required_environment = str(step.get("required_environment") or "").strip()
            if required_environment:
                registry.register_proposition(
                    f"Required environment: {required_environment}",
                    "plan",
                    strength="required",
                    scope="environment",
                    scope_id=required_environment,
                    reliability=0.6,
                    confidence=0.7,
                    attributes={"step_ids": [step_id] if step_id else [], "fact_key": "environment", "value": required_environment, "plan_version": plan_version},
                )

        # Legacy markdown plans do not have structured assumptions yet. Only
        # claim lines from explicit assumption/contract sections so ordinary
        # explanatory prose cannot become a false runtime constraint.
        for line in self._knowledge_plan_section_lines(plan_text, ("assumption", "contract", "success criteria")):
            cleaned = re.sub(r"^[-*]\s+", "", line).strip()
            if cleaned:
                registry.register_plan_assumption(
                    cleaned,
                    scope="workflow",
                    scope_id=str(task_id),
                    reliability=0.45,
                    confidence=0.6,
                    attributes={"plan_version": plan_version},
                )

        evidence = state.get("execution_evidence") or {}
        if isinstance(evidence, dict):
            command = str(evidence.get("command") or evidence.get("generated_code") or "").strip()
            current_step = self._running_plan_step(plan_state)
            step_id = str(current_step.get("step_id") or "")
            if command:
                registry.register_observation(
                    f"Runtime command: {command[:3000]}",
                    "runtime_argv",
                    strength="observation",
                    scope="command",
                    scope_id=step_id or str(task_id),
                    confidence=0.95,
                    reliability=0.95,
                    evidence_ids=[f"execute:{task_id}"],
                    attributes={"fact_key": "execution_command", "value": command[:3000], "step_ids": [step_id] if step_id else []},
                )
            if evidence.get("returncode") is not None:
                returncode = str(evidence.get("returncode"))
                registry.register_observation(
                    f"Runtime return code: {returncode}",
                    "runtime_output",
                    strength="observation",
                    scope="command",
                    scope_id=step_id or str(task_id),
                    confidence=0.98,
                    reliability=0.98,
                    evidence_ids=[f"execute:{task_id}"],
                    attributes={"fact_key": "returncode", "value": returncode, "step_ids": [step_id] if step_id else []},
                )

        runtime_status = state.get("runtime_status") or {}
        if isinstance(runtime_status, dict):
            environment = str(runtime_status.get("env") or runtime_status.get("environment") or "").strip()
            if environment:
                registry.register_observation(
                    f"Verified runtime environment: {environment}",
                    "environment_probe",
                    strength="observation",
                    scope="environment",
                    scope_id=environment,
                    confidence=0.85,
                    reliability=0.9,
                    attributes={"fact_key": "environment", "value": environment},
                )

        for claim in extra_claims or []:
            registry.register(claim)
        return registry

    @staticmethod
    def _load_knowledge_conflicts(root: Path) -> List[KnowledgeConflict]:
        path = root / "conflicts" / "conflicts.jsonl"
        conflicts: List[KnowledgeConflict] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    conflicts.append(KnowledgeConflict.from_mapping(json.loads(line)))
        except (OSError, json.JSONDecodeError):
            return []
        return conflicts

    def _knowledge_conflict_update(
        self,
        task_id: str,
        state: Optional[Dict[str, Any]] = None,
        *,
        phase: str = "",
        plan_state: Optional[Dict[str, Any]] = None,
        extra_claims: Optional[Iterable[Any]] = None,
    ) -> Dict[str, Any]:
        if not feature_enabled("ENABLE_KNOWLEDGE_CONFLICT_DETECTOR", True):
            return {}
        state = dict(state or {})
        root = self._knowledge_conflict_root(task_id)
        registry = self._knowledge_claim_registry(task_id, state, plan_state, extra_claims)
        attempt = int(state.get("retry_count") or 0)
        conflicts = KnowledgeConflictDetector.detect(registry, attempt=attempt)
        conflicts.extend(KnowledgeConflictDetector.detect_stale_plan(registry, attempt=attempt))
        existing = self._load_knowledge_conflicts(root)
        by_fp: Dict[str, KnowledgeConflict] = {item.fingerprint: item for item in existing}
        new_conflicts: List[KnowledgeConflict] = []
        updated_conflicts: List[KnowledgeConflict] = []
        current_fingerprints = {item.fingerprint for item in conflicts}
        for conflict in conflicts:
            if conflict.fingerprint in by_fp:
                prior = by_fp[conflict.fingerprint]
                # Preserve an unresolved status; repeated detection is not
                # progress. A conflict that was resolved and then reappears is
                # active again and must be audited as new evidence.
                if prior.status in {"resolved", "accepted_exception", "superseded"}:
                    conflict.resolution_id = None
                    new_conflicts.append(conflict)
                else:
                    conflict.status = prior.status
                    conflict.resolution_id = prior.resolution_id
                by_fp[conflict.fingerprint] = conflict
            else:
                by_fp[conflict.fingerprint] = conflict
                new_conflicts.append(conflict)
        # Current detection is authoritative. Keep historical conflicts in the
        # audit log, but stop blocking once the same active claims are no longer
        # judged contradictory (or one side was superseded by newer evidence).
        for fingerprint, prior in by_fp.items():
            if fingerprint in current_fingerprints or prior.status in {"resolved", "accepted_exception", "superseded"}:
                continue
            prior.status = "resolved"
            prior.resolved_at_attempt = attempt
            prior.resolution_id = f"revalidated_{prior.conflict_id}_{attempt}"
            updated_conflicts.append(prior)
        all_conflicts = list(by_fp.values())
        resolutions = [ConflictResolver.resolve(item, registry) for item in all_conflicts if item.status not in {"resolved", "accepted_exception", "superseded"}]
        current_plan_state = plan_state or load_plan_state(self.data_path, task_id)
        stale = set(current_plan_state.get("stale_step_ids", []))
        revalidation = set(current_plan_state.get("revalidation_required_step_ids", []))
        for decision in resolutions:
            if decision.requires_plan_revision:
                stale.update(decision.affected_step_ids)
                revalidation.update(decision.affected_step_ids)
                revalidation.update(decision.affected_downstream_step_ids)
        if stale or revalidation:
            current_plan_state["stale_step_ids"] = sorted(stale)
            current_plan_state["revalidation_required_step_ids"] = sorted(revalidation)
            save_plan_state(self.data_path, task_id, current_plan_state)
        store = KnowledgeConflictStore(root, task_id)
        store.save_registry(registry)
        store.append("conflicts.jsonl", [*new_conflicts, *updated_conflicts])
        if resolutions:
            store.append("conflict_resolutions.jsonl", resolutions)
        store.save_summary(
            conflicts=all_conflicts,
            resolutions=resolutions,
            plan_state=current_plan_state,
            user_hard_constraints=state.get("user_constraints", []),
        )
        blocking = [item for item in all_conflicts if item.blocking]
        summary = {
            "phase": phase,
            "unresolved_count": len([item for item in all_conflicts if item.status not in {"resolved", "accepted_exception", "superseded"}]),
            "blocking_conflicts": [item.conflict_id for item in blocking],
            "requires_probe": [item.conflict_id for item in all_conflicts if item.status == "needs_probe"],
            "decisions": [item.to_dict() for item in resolutions],
            "active_plan_version": current_plan_state.get("plan_version", "plan_v1"),
            "stale_steps": sorted(stale),
            "revalidation_required_steps": sorted(revalidation),
            "progress_events": [
                {
                    "event": "knowledge_conflict_detected",
                    "conflict_id": item.conflict_id,
                    "conflict_type": item.conflict_type,
                    "phase": phase,
                }
                for item in new_conflicts
            ],
        }
        return {
            "knowledge_claims": [item.to_dict() for item in registry.all()],
            "knowledge_conflicts": [item.to_dict() for item in all_conflicts],
            "knowledge_conflict_summary": summary,
            "knowledge_progress_events": summary["progress_events"],
            "conflict_resolution": resolutions[-1].to_dict() if resolutions else {},
            "plan_revision": (
                current_plan_state.get("revision_history", [])[-1]
                if current_plan_state.get("revision_history")
                else {"plan_version": current_plan_state.get("plan_version", "plan_v1"), "status": "initial"}
            ),
            "plan_version": current_plan_state.get("plan_version", "plan_v1"),
            "plan_stale_steps": sorted(stale),
            "plan_revalidation_steps": sorted(revalidation),
        }

    def _knowledge_conflict_execution_guard(self, task_id: str, state: Dict[str, Any]) -> Tuple[str, str]:
        """Return (action, reason); no command is run while a critical conflict is unresolved."""
        if not feature_enabled("ENABLE_KNOWLEDGE_CONFLICT_DETECTOR", True):
            return "allow", ""
        summary = state.get("knowledge_conflict_summary") or {}
        blocking = list(summary.get("blocking_conflicts", [])) if isinstance(summary, dict) else []
        if not blocking:
            plan_state = load_plan_state(self.data_path, task_id)
            current = self._running_plan_step(plan_state)
            stale = {str(item) for item in plan_state.get("stale_step_ids", [])}
            required = {str(item) for item in plan_state.get("revalidation_required_step_ids", [])}
            current_id = str(current.get("step_id") or "")
            if current_id and (current_id in stale or current_id in required):
                return "probe", f"Current plan step {current_id} requires revalidation before execution."
            return "allow", ""
        conflicts = state.get("knowledge_conflicts") or []
        details = []
        user_decision = False
        for item in conflicts:
            if not isinstance(item, dict) or item.get("conflict_id") not in blocking:
                continue
            details.append(f"{item.get('conflict_type')}: {item.get('description')}")
            decision = str((state.get("conflict_resolution") or {}).get("decision") or "")
            user_decision = user_decision or decision == "request_user_decision"
        return ("ask_user" if user_decision else "probe", "Unresolved knowledge conflict prevents execution: " + " | ".join(details[:3]))

    @staticmethod
    def _running_plan_step(state: Dict[str, Any]) -> Dict[str, Any]:
        steps = [step for step in state.get("steps", []) or [] if isinstance(step, dict)]
        cursor = str(state.get("current_step_id") or "")
        if cursor:
            current = next(
                (step for step in steps if str(step.get("step_id") or "") == cursor),
                None,
            )
            if current and str(current.get("status") or "").strip().lower() in {
                "running", "retry_pending", "repairing", "diagnosing", "ready", "pending",
            }:
                return current
        for status in ("running", "retry_pending", "repairing", "diagnosing", "ready", "pending"):
            for step in steps:
                if str(step.get("status") or "").strip().lower() == status:
                    return step
        return {}

    @staticmethod
    def _plan_step_is_activation_only(step: Dict[str, Any]) -> bool:
        title = str(step.get("title") or "")
        lower = title.lower()
        if "activate_batch" not in lower and "action: activate" not in lower:
            return False
        if re.search(r"\b(run|execute|generate|create|write|cluster|marker|annotat|de analysis|report)\b", lower):
            return False
        if any(term in title for term in ("运行", "执行", "生成", "聚类", "注释", "报告", "差异")):
            return False
        return not re.search(r"\.(?:rds|tsv|csv|txt|md|png|pdf|sh|html?)\b", lower)

    @staticmethod
    def _plan_step_action_is(step: Dict[str, Any], action: str) -> bool:
        wanted = str(action or "").strip().lower()
        declared = str(step.get("action") or "").strip().lower()
        if declared:
            return declared == wanted
        title = str(step.get("title") or "").lower()
        return bool(wanted and re.search(rf"\baction:\s*{re.escape(wanted)}\b", title))

    @staticmethod
    def _execution_purpose_completes_step(step: Dict[str, Any], execution_purpose: str) -> bool:
        """Return whether helper evidence is the declared business result."""
        purpose = str(execution_purpose or "analysis").strip().lower()
        if purpose not in {"probe", "validation"}:
            return True
        declared = str(step.get("purpose") or "").strip().lower()
        return declared in {"probe", "validation"}

    def _record_activate_batch_progress(self, task_id: str, batch_id: str, update_text: str) -> str:
        state = load_plan_state(self.data_path, task_id)
        if not state.get("steps"):
            return render_plan_progress(state)
        if "✅" in str(update_text or ""):
            step = self._running_plan_step(state)
            if self._plan_step_is_activation_only(step):
                mark_plan_step_done(state, evidence=f"activate batch success: {batch_id}")
            else:
                record_current_step_evidence(state, "activate_batch_success", update_text[:1000])
        else:
            record_current_step_evidence(state, "activate_batch_failed", update_text[:1000])
        return self._persist_plan_tracker(task_id, state)

    @staticmethod
    def _current_step_can_emit_final_solution(plan_state: Dict[str, Any]) -> bool:
        running = BioAnalystAgent._running_plan_step(plan_state)
        if not running:
            return True
        unfinished = [
            step for step in plan_state.get("steps", []) or []
            if isinstance(step, dict)
            and str(step.get("status") or "").strip().lower()
            not in {"done", "passed", "blocked", "failed", "skipped", "skipped_upstream_blocked", "cancelled"}
        ]
        title = str(running.get("title") or "").lower()
        if len(unfinished) == 1:
            return True
        return any(term in title for term in ("final", "report", "summary", "solution", "收尾", "报告", "总结"))

    @staticmethod
    def _plan_has_unfinished_steps(plan_state: Dict[str, Any]) -> bool:
        terminal_statuses = {"done", "passed", "blocked", "failed", "skipped", "skipped_upstream_blocked", "cancelled"}
        for step in plan_state.get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            status = str(step.get("status") or "").strip().lower()
            if status not in terminal_statuses:
                return True
        return False

    def _next_step_after_install_result(self, task_id: str) -> str:
        """Continue managed plans after install/register unless all planned work is terminal."""
        try:
            plan_state = load_plan_state(self.data_path, task_id)
        except Exception:
            plan_state = {}
        if plan_state.get("steps") and self._plan_has_unfinished_steps(plan_state):
            return "generate"
        return "finish"

    def _plan_has_blocking_evidence(self, task_id: str) -> bool:
        state = load_plan_state(self.data_path, task_id)
        for step in state.get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            if str(step.get("status") or "").strip().lower() == "blocked":
                return True
            for item in step.get("evidence", []) or []:
                if not isinstance(item, dict):
                    continue
                event = str(item.get("event") or "").strip().lower()
                detail = str(item.get("detail") or "")
                detail_lower = detail.lower()
                if event in {
                    "execute_failed",
                    "runtime_missing_after_execute",
                    "runtime_not_ready",
                    "skill_env_runner_guard",
                    "future_skill_guard",
                    "pre_execution_boundary_gate_failed",
                    "activate_batch_failed",
                }:
                    return True
                if event in {"blocked", "install_failed"}:
                    return True
                if event == "supervisor_verdict" and "ESCALATE" in detail:
                    return True
                if (
                    "blocked:" in detail_lower
                    or "runtime missing" in detail_lower
                    or "command failed" in detail_lower
                    or "failed healthcheck" in detail_lower
                ):
                    return True
        return False

    def _prepare_final_solution_progress(
        self,
        task_id: str,
        solution_text: str,
        current_plan: str,
    ) -> Tuple[bool, str, str, str]:
        plan_state = load_plan_state(self.data_path, task_id)
        if not plan_state.get("steps"):
            return True, render_plan_progress(plan_state), "completed", ""
        self._sync_plan_tracker_from_stage_status(
            task_id,
            plan_state,
            current_plan,
            solution_text,
        )
        plan_state = load_plan_state(self.data_path, task_id)

        terminal_status = self._terminal_status_from_text(solution_text)
        if terminal_status == "blocked":
            if self._plan_has_blocking_evidence(task_id):
                progress = self._mark_plan_current_blocked(
                    task_id,
                    f"final solution blocked: {solution_text[:500]}",
                )
                return True, progress, "blocked", ""
            progress = self._record_plan_current_evidence(
                task_id,
                "final_solution_blocked_without_evidence",
                solution_text[:1000],
            )
            return (
                False,
                progress,
                "blocked",
                "The final solution claims the task is blocked, but no concrete blocking evidence "
                "was recorded in the plan. Continue execution or record the real dependency/command failure.",
            )

        if self._current_step_can_emit_final_solution(plan_state):
            mark_plan_step_done(plan_state, evidence=f"final solution generated: {solution_text[:500]}")
        else:
            record_current_step_evidence(
                plan_state,
                "premature_final_solution",
                solution_text[:1000],
            )
        progress = self._persist_plan_tracker(task_id, plan_state)
        final_state = load_plan_state(self.data_path, task_id)
        final_gate = CompletionGate.validate_final(
            self.data_path,
            task_id,
            final_state,
            current_plan,
            require_execution_evidence=True,
            require_supervisor_evidence=True,
        )
        if final_gate.passed:
            return True, progress, "completed", ""

        reopened_state = load_plan_state(self.data_path, task_id)
        reopen_last_done_step(
            reopened_state,
            f"final_solution_gate_failed: {final_gate.message}",
        )
        progress = self._persist_plan_tracker(task_id, reopened_state)
        return False, progress, "completed", final_gate.feedback

    def _planner_declares_completed_task(self, plan_content: str) -> bool:
        text = str(plan_content or "").lower()
        completion_markers = (
            "already fully complete",
            "task is already complete",
            "task is complete",
            "task status\n**complete**",
            "task status\ncomplete",
            "**complete** - no blocked",
            "all stages have been successfully executed",
            "all output artifacts are verified",
            "all output artifacts are verified and present",
            "all artifacts delivered and validated",
        )
        return any(marker in text for marker in completion_markers)

    def _planner_proposes_gate_workaround(self, plan_content: str) -> bool:
        text = str(plan_content or "").lower()
        gate_markers = (
            "validation gate",
            "completion gate",
            "gate-checked path",
            "gate checked path",
            "satisfy the completion check",
        )
        workaround_markers = (
            "symlink",
            "symbolic link",
            "copy of",
            "cp ",
            "ln -s",
            "accessible at the gate",
            "fix the path accessibility",
        )
        return any(marker in text for marker in gate_markers) and any(marker in text for marker in workaround_markers)

    def _finish_only_plan_from_completed_plan(self, plan_content: str) -> str:
        return (
            "## Section 1: Requirement Analysis\n"
            "The Planner determined that the existing task is already complete: required outputs have been generated, "
            "validated, and documented. No additional filesystem mutation is needed.\n\n"
            "## Section 2: Step-by-Step Execution Plan\n"
            "Step 1: Finalize already completed task\n"
            "  - Tool: BioMind final response\n"
            "  - Execution guidance: Do not execute shell commands or create compatibility copies/symlinks. "
            "Summarize the verified artifacts and any limitations from the existing evidence.\n"
            "  - Input: Existing verified artifacts and reports\n"
            "  - Output: Final completion summary\n"
            "  - Success criteria: The user receives a concise final summary; no extra files are modified.\n"
            "  - Cautions: Never work around validation gates by copying or symlinking artifacts into unrelated directories.\n"
            "  - Risk: LOW - final reporting only.\n\n"
            "## Execution Checklist\n"
            "- [ ] Step 1: Finalize already completed task | Goal: Report verified completion without modifying outputs | Action: FINISH | Done: final answer summarizes verified artifacts and limitations\n\n"
            "## Section 4: Validation Checklist\n"
            "- Use the existing verified outputs and status files. Do not require bigWig files unless the selected skill documents them.\n"
            "- Do not create compatibility copies or symlinks to satisfy an old validation path.\n\n"
            "## Completion Evidence Summary\n"
            "The original Planner response stated that the task was already fully complete and that output artifacts had been verified. "
            "That completion claim is preserved here without copying old artifact-path lists into new validation requirements.\n"
        )

    def _sanitize_completed_planner_plan(self, plan_content: str) -> str:
        if self._planner_declares_completed_task(plan_content):
            return self._finish_only_plan_from_completed_plan(plan_content)
        return plan_content

    def _replace_plan_tracker_with_plan(self, task_id: str, plan_content: str, reason: str = "") -> str:
        plan_file = default_plan_file(self.data_path, task_id)
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        previous = load_plan_state(self.data_path, task_id)
        plan_state = build_plan_state(task_id, plan_content, plan_file=plan_file)
        task_revision = int(previous.get("generated_from_task_revision") or 0)
        bind_plan_to_task_revision(plan_state, task_revision)
        plan_state["task_spec"] = previous.get("task_spec") or {}
        plan_state["selected_skill_ids"] = list(previous.get("selected_skill_ids") or [])
        plan_state = carry_forward_plan_lifecycle(previous, plan_state)
        approve_plan_state(plan_state, task_revision)
        if reason and plan_state.get("steps"):
            record_current_step_evidence(plan_state, "plan_replaced", reason)
        save_plan_state(self.data_path, task_id, plan_state)
        plan_file.write_text(
            "# Task Plan\n"
            f"Generated: {datetime.now().isoformat()}\n"
            "Plan Source: sanitized completed-task resume\n\n"
            f"{plan_content}",
            encoding="utf-8",
        )
        update_plan_markdown_status(plan_file, plan_state)
        return render_plan_progress(plan_state)

    def _resume_plan_material(self, task_id: str, current_plan: str) -> str:
        parts = [str(current_plan or "")]
        state = load_plan_state(self.data_path, task_id)
        title = next_action_title(state)
        if title:
            parts.append(title)
        plan_file = str(state.get("plan_file") or "").strip()
        if plan_file:
            try:
                path = Path(plan_file)
                if path.exists() and path.is_file():
                    parts.append(path.read_text(encoding="utf-8")[:12000])
            except OSError:
                pass
        return "\n\n".join(part for part in parts if part and part.strip())

    def _current_plan_step_requests_finish(self, task_id: str) -> bool:
        state = load_plan_state(self.data_path, task_id)
        title = next_action_title(state).lower()
        return "action: finish" in title or "| finish" in title

    def _finish_solution_from_plan(self, current_plan: str) -> str:
        return (
            "Task is complete based on the existing verified outputs and status evidence. "
            "No additional shell commands or compatibility file copies are needed. "
            "The analysis outputs, report, reproducibility script, and stage status should be treated as the deliverables; "
            "any prior validation-gate path mismatch should be handled by the generalized gate logic rather than by duplicating artifacts."
        )

    def _plan_tracker_is_complete(self, task_id: str) -> bool:
        state = load_plan_state(self.data_path, task_id)
        steps = [step for step in state.get("steps", []) or [] if isinstance(step, dict)]
        return bool(steps) and all(str(step.get("status")) in {"done", "passed", "skipped"} for step in steps)

    def _llm_failure_response(self, stage: str, exc: BaseException, task_id: str) -> AIMessage:
        summary = llm_failure_summary(exc, stage, task_id)
        logger.error(summary)
        return AIMessage(content=deterministic_failure_solution(exc, stage, task_id))

    async def _ainvoke_llm(
        self,
        messages: List[BaseMessage],
        *,
        stage: str,
        task_id: str = "",
        task_mode: str = "",
        trace_metadata: Optional[Dict[str, Any]] = None,
    ) -> BaseMessage:
        """Invoke the shared model with a hard wall-clock bound.

        The provider/client timeout covers network operations, while this
        wrapper also bounds model-side waits and callback stalls.  The model
        call runs in its own task so a provider-side cancellation cannot escape
        as ``CancelledError`` and silently bypass node ``except Exception``
        handlers.  Cancellation of this outer task still propagates normally,
        preserving Ctrl+C and application shutdown behavior.
        """
        mode = str(task_mode or "").strip().lower()
        timeout = (
            self.llm_readonly_timeout_seconds
            if mode in {"readonly", "simple_execute"}
            else self.llm_timeout_seconds
        )
        prompt_chars = sum(
            len(str(getattr(message, "content", message) or ""))
            for message in messages
        )
        started = time.monotonic()
        logger.info(
            "LLM request started: stage=%s task=%s messages=%s chars=%s timeout=%ss",
            stage,
            task_id or "-",
            len(messages),
            prompt_chars,
            timeout,
        )
        trace_store = ContextLineageStore(self.data_path, task_id) if task_id else None
        snapshot: Dict[str, Any] = {}
        if trace_store:
            metadata = dict(trace_metadata or {})
            call_kind = str(metadata.get("call_kind") or {
                "planner": "planner_initial",
                "planner_repair": "planner_repair",
            }.get(stage, stage))
            plan_state = load_plan_state(self.data_path, task_id)
            _, current_step = current_plan_step_with_index(plan_state)
            current_step = current_step or {}
            metadata.setdefault("node_name", stage)
            metadata.setdefault("call_kind", call_kind)
            metadata.setdefault("task_revision", plan_state.get("generated_from_task_revision"))
            metadata.setdefault("task_spec_revision", metadata.get("task_revision"))
            metadata.setdefault("plan_revision", plan_state.get("plan_revision"))
            metadata.setdefault("step_id", current_step.get("step_id"))
            metadata.setdefault("stage_id", (current_step.get("renderer_execution") or {}).get("current_stage_id"))
            metadata.setdefault("action_id", (current_step.get("renderer_execution") or {}).get("current_action_id"))
            metadata.setdefault("selected_skill_ids", current_step.get("skill_ids") or [])
            if not metadata.get("call_id"):
                ordinal = len(trace_store.list_snapshots()) + 1
                identity = json.dumps({
                    "task": task_id,
                    "kind": call_kind,
                    "ordinal": ordinal,
                    "step": metadata.get("step_id"),
                    "attempt": metadata.get("attempt_id"),
                }, ensure_ascii=False, sort_keys=True)
                metadata["call_id"] = (
                    f"{call_kind}-{ordinal:04d}-"
                    f"{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:12]}"
                )
            snapshot = trace_store.start_call(messages, metadata)
        invoke_task = asyncio.create_task(self.llm.ainvoke(messages))
        try:
            done, _ = await asyncio.wait({invoke_task}, timeout=timeout)
            if not done:
                invoke_task.cancel()
                raise TimeoutError(
                    f"{stage} LLM request timed out after {timeout:.0f}s"
                )
            try:
                response = invoke_task.result()
                if trace_store and snapshot:
                    snapshot = trace_store.finish_call(snapshot["call_id"], response)
                    refs = getattr(self, "_latest_llm_call_refs", None)
                    if refs is None:
                        self._latest_llm_call_refs = {}
                        refs = self._latest_llm_call_refs
                    refs.setdefault(task_id, []).append({
                        key: snapshot.get(key)
                        for key in (
                            "call_id", "snapshot_ref", "request_hash", "response_hash",
                            "task_revision", "plan_revision", "step_id", "attempt_id", "call_kind",
                        )
                    })
                    refs[task_id] = refs[task_id][-50:]
                return response
            except asyncio.CancelledError as exc:
                raise TimeoutError(
                    f"{stage} LLM request was cancelled by the provider/client"
                ) from exc
        except BaseException as exc:
            if trace_store and snapshot:
                try:
                    trace_store.fail_call(snapshot["call_id"], exc)
                except Exception as trace_exc:  # noqa: BLE001 - preserve original provider failure.
                    logger.warning("Failed to persist LLM failure snapshot %s: %s", snapshot.get("call_id"), trace_exc)
            raise
        finally:
            if not invoke_task.done():
                invoke_task.cancel()
            logger.info(
                "LLM request finished: stage=%s task=%s elapsed=%.1fs",
                stage,
                task_id or "-",
                time.monotonic() - started,
            )

    def _lightweight_task_policy(self, task_mode: str) -> str:
        mode = str(task_mode or "").strip().lower()
        if mode == "readonly":
            intent = (
                "The request is read-only. Output exactly one <execute> block with the minimal "
                "inspection command(s) needed to answer it, then stop. Do not modify files, "
                "install tools, start workflows, or perform scientific analysis."
            )
        elif mode == "simple_execute":
            intent = (
                "The request is a simple execution. Output exactly one <execute> block for the "
                "explicit command or existing script requested by the user, then stop. Do not "
                "install tools, expand it into a workflow, or perform scientific review."
            )
        else:
            return ""
        return (
            f'<lightweight_task_policy mode="{mode}">\n'
            f"- {intent}\n"
            "- If the request turns out to require installation, multiple analysis steps, output validation, "
            "or unclear parameters, do not improvise a workflow; ask the user or finish with a concise "
            "explanation that this needs managed mode.\n"
            "- Allowed actions: <execute> for one lightweight command, <ask_user> for one missing detail, "
            "or <solution> for a direct non-execution answer.\n"
            "- Disallowed actions: <search>, <install_tool>, <activate_batch>, workflow planning.\n"
            "</lightweight_task_policy>"
        )

    def _execution_validation_level(self, state: AgentState, code: str) -> str:
        explicit = str(state.get("validation_level") or "").strip().lower()
        if explicit in {"none", "basic", "artifact", "scientific"}:
            return explicit
        if state.get("active_skills") or state.get("selected_skills") or state.get("workflow_id"):
            return "scientific"
        plan_text = str(state.get("current_plan") or state.get("plan_progress") or "").lower()
        code_text = str(code or "").lower()
        analysis_terms = (
            "analysis", "workflow", "pipeline", "qc", "align", "mapping",
            "cluster", "differential", "tad", "variant", "fastqc", "seurat",
        )
        if any(term in plan_text or term in code_text for term in analysis_terms):
            return "artifact"
        return "basic"

    def _validate_execution_result(
        self,
        state: AgentState,
        code: str,
        stdout: str,
        stderr: str,
        returncode: int,
    ) -> tuple:
        level = self._execution_validation_level(state, code)
        if level == "none" or not self.validator:
            return {}, "", level
        validation = self.validator.validate(stdout, stderr, code, returncode)
        status = "failed"
        if validation.get("passed"):
            status = "warning" if validation.get("warnings") else "passed"
        report = self.validator.format_report(validation)
        report = str(report or "").strip()[:1800]
        block = (
            f'\n<validation_result level="{level}" status="{status}">\n'
            f"{report}\n"
            "</validation_result>"
        )
        return validation, block, level

    def _skill_execution_context(self, skill_ids: List[str]) -> str:
        if not self.skill_manager or not skill_ids:
            return ""
        cards = self.skill_manager.get_execution_cards(skill_ids)
        return f"\n[MANDATORY SKILL EXECUTION CARDS]\n{cards}\n" if cards else ""

    def _sync_runtime_registries_at_startup(self):
        if not self.skill_manager:
            return
        try:
            if self.pm:
                self.skill_manager.sync_with_package_manager(self.pm)
            paths = self.skill_manager.runtime_registry_file_candidates(self.pm)
            self.skill_manager.import_runtime_registry_files(paths, package_manager=self.pm)
            if self.pm:
                self.skill_manager.sync_with_package_manager(self.pm)
        except Exception as e:
            logger.warning("Runtime registry startup sync failed: %s", e)

    def _runtime_status_for_task(
        self,
        task_id: str,
        selected_skills: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        records = dict(self.runtime_status.get(task_id or "default", {}))
        if selected_skills is None:
            return records
        selected = set(self._selected_skill_ids(selected_skills))
        if not selected:
            return {}
        return {
            key: record
            for key, record in records.items()
            if str(record.get("skill_id") or "") in selected
        }

    def _runtime_cache_key(self, skill_id: str, env: str, mode: str = "") -> str:
        suffix = f"::{mode}" if mode else ""
        return f"{skill_id}::{env}{suffix}"

    def _cache_runtime_status(
        self,
        task_id: str,
        skill_id: str,
        env: str,
        ok: bool,
        detail: str,
        healthcheck: str,
        env_runner: str,
        mode: str = "",
    ):
        if not hasattr(self, "runtime_status") or self.runtime_status is None:
            self.runtime_status = {}
        bucket = self.runtime_status.setdefault(task_id or "default", {})
        bucket[self._runtime_cache_key(skill_id, env, mode)] = {
            "skill_id": skill_id,
            "env": env,
            "mode": mode or "",
            "ok": bool(ok),
            "detail": detail or "",
            "healthcheck": healthcheck or "",
            "env_runner": env_runner or "",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }

    @staticmethod
    def _requires_calder_official_r_preflight(code: str) -> bool:
        text = str(code or "").lower()
        if "run_calder_compartments.py" not in text:
            return False
        if "--stats-only" in text or "--calder-mode patched-cli" in text:
            return False
        return "--calder-mode official-r" in text or "--calder-mode auto" in text or "--calder-mode" not in text

    def _mode_healthchecks_for_code(
        self,
        skill_id: str,
        requirements: Dict[str, Any],
        code: str = "",
    ) -> Tuple[str, List[str]]:
        if not self._requires_calder_official_r_preflight(code):
            return "", []
        env = str(requirements.get("env") or requirements.get("default_env") or "").strip()
        checks_by_mode = requirements.get("mode_healthchecks") or {}
        checks = []
        if isinstance(checks_by_mode, dict):
            checks = checks_by_mode.get("official-r") or checks_by_mode.get("official_r") or []
        if isinstance(checks, str):
            checks = [checks]
        checks = [str(item).replace("{env}", env).strip() for item in checks if str(item).strip()]
        if not checks:
            checks = [
                f"conda run -n {env} which Rscript",
                f"conda run -n {env} Rscript --version",
                (
                    f"conda run -n {env} Rscript -e "
                    "\"suppressPackageStartupMessages(library(CALDER)); "
                    "suppressPackageStartupMessages(library(data.table)); cat('OK\\n')\""
                ),
            ]
        return "official-r", checks

    def _verify_skill_mode_runtime(
        self,
        skill_id: str,
        env: str,
        mode: str,
        healthchecks: List[str],
        task_id: str = "default",
        use_cache: bool = True,
    ) -> Tuple[bool, str, str]:
        for index, command in enumerate(healthchecks, start=1):
            cache_mode = f"{mode}:{index}"
            cached = self.runtime_status.get(task_id or "default", {}).get(
                self._runtime_cache_key(skill_id, env, cache_mode)
            )
            if use_cache and cached and cached.get("ok"):
                continue
            ok, detail = self.pm.verify_environment(env_name=env, language="", healthcheck=command)
            self._cache_runtime_status(
                task_id,
                skill_id,
                env,
                ok,
                detail or ("OK" if ok else "Healthcheck failed."),
                command,
                f"conda run -n {env}",
                mode=cache_mode,
            )
            if not ok:
                return False, (
                    f"Mode runtime preflight failed for {skill_id} ({mode}) at `{command}`. "
                    f"{detail or 'Healthcheck failed.'}"
                ), env
        return True, f"Mode runtime preflight passed for {skill_id} ({mode}).", env

    def _verify_conda_env_presence(self, env: str) -> Tuple[bool, str]:
        env_name = str(env or "").strip()
        if not env_name or env_name in {"base", "default"}:
            return True, ""
        if not self.pm or not hasattr(self.pm, "conda_exe"):
            return True, ""
        if hasattr(self.pm, "environment_runtime_available"):
            if self.pm.environment_runtime_available(env_name):
                return True, f"Conda environment `{env_name}` exists."
            if getattr(self.pm, "conda_root", None):
                return False, f"Conda environment `{env_name}` is not available under the configured root."
        command = f"conda run -n {shlex.quote(env_name)} true"
        if hasattr(self.pm, "_command_with_configured_manager"):
            command = self.pm._command_with_configured_manager(command)
        try:
            proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=45)
        except Exception as exc:
            return False, f"Conda environment presence check failed for `{env_name}`: {exc}"
        if proc.returncode == 0:
            return True, f"Conda environment `{env_name}` exists."
        detail = "\n".join(part.strip() for part in [proc.stdout, proc.stderr] if part and part.strip()).strip()
        return False, detail or f"Conda environment `{env_name}` is not available."

    def _resolved_runtime_healthcheck(self, healthcheck: str) -> str:
        command = str(healthcheck or "").strip()
        if not command:
            return ""
        if not self.skill_manager:
            return command
        if not re.search(r"(^|\s)(?:bash\s+|Rscript\s+|python\s+)?skills/", command):
            return command
        repo_root = Path(getattr(self.skill_manager, "skills_root", "") or "").parent
        if not repo_root or not repo_root.exists():
            return command
        resolved = command
        if hasattr(self.pm, "_command_with_configured_manager") and re.match(r"^(conda|mamba)(?:\s|$)", resolved):
            resolved = self.pm._command_with_configured_manager(resolved)
        return f"cd {shlex.quote(str(repo_root))} && {resolved}"

    def _verify_skill_runtime(self, skill_id: str, task_id: str = "default", use_cache: bool = True, code: str = "") -> Tuple[bool, str, str]:
        if not self.skill_manager or not self.pm:
            return True, "", ""
        requirements = self._skill_runtime_requirements(skill_id)
        env = requirements.get("env") or requirements.get("default_env", "")
        if not env:
            return True, "", ""
        mode, mode_healthchecks = self._mode_healthchecks_for_code(skill_id, requirements, code)
        if not self.pm.get_environment(env):
            self.pm.register_environment(
                env_name=env,
                purpose=f"Runtime for skill {skill_id}",
                packages=requirements.get("packages", []),
                skill_id=skill_id,
                notes="Auto-registered from skill runtime metadata before healthcheck.",
                language=requirements.get("language", ""),
                capabilities=requirements.get("capabilities", []),
                healthcheck=self._resolved_runtime_healthcheck(requirements.get("healthcheck", "")),
                status="registered",
            )
        cache_key = self._runtime_cache_key(skill_id, env)
        cached = self.runtime_status.get(task_id or "default", {}).get(cache_key)
        env_ok, env_detail = self._verify_conda_env_presence(env)
        if not env_ok:
            self.skill_manager.record_environment(
                skill_id,
                env=env,
                packages=requirements.get("packages", []),
                install_command=requirements.get("install_command", ""),
                status="broken",
                notes=env_detail,
                language=requirements.get("language", ""),
                capabilities=requirements.get("capabilities", []),
                healthcheck=self._resolved_runtime_healthcheck(requirements.get("healthcheck", "")),
            )
            self._cache_runtime_status(
                task_id,
                skill_id,
                env,
                False,
                env_detail,
                f"conda run -n {env} true",
                requirements.get("env_runner", ""),
            )
            return False, env_detail, env
        if use_cache and cached and cached.get("ok") and not mode_healthchecks:
            return True, str(cached.get("detail") or "Cached runtime healthcheck passed."), env
        if use_cache and cached and cached.get("ok"):
            ok, detail = True, str(cached.get("detail") or "Cached runtime healthcheck passed.")
        else:
            resolved_healthcheck = self._resolved_runtime_healthcheck(requirements.get("healthcheck", ""))
            recent_detail = None
            if use_cache and hasattr(self.pm, "recent_environment_healthcheck"):
                recent_detail = self.pm.recent_environment_healthcheck(env, resolved_healthcheck)
            if recent_detail is not None:
                ok, detail = True, recent_detail
            else:
                ok, detail = self.pm.verify_environment(
                    env_name=env,
                    language=requirements.get("language", ""),
                    healthcheck=resolved_healthcheck,
                )
            status = "verified" if ok else "broken"
            self.skill_manager.record_environment(
                skill_id,
                env=env,
                packages=requirements.get("packages", []),
                install_command=requirements.get("install_command", ""),
                status=status,
                notes=detail or ("Environment healthcheck passed." if ok else "Environment healthcheck failed."),
                language=requirements.get("language", ""),
                capabilities=requirements.get("capabilities", []),
                healthcheck=resolved_healthcheck,
            )
            self._cache_runtime_status(
                task_id,
                skill_id,
                env,
                ok,
                detail or ("OK" if ok else "Healthcheck failed."),
                resolved_healthcheck,
                requirements.get("env_runner", ""),
            )
        if not ok:
            return ok, detail, env
        if mode_healthchecks:
            return self._verify_skill_mode_runtime(
                skill_id,
                env,
                mode,
                mode_healthchecks,
                task_id=task_id,
                use_cache=use_cache,
            )
        return ok, detail, env

    def _consultant_runtime_code_hint(self, skill_id: str) -> str:
        """Return a representative command fragment for mode-specific preflight."""
        sid = str(skill_id or "")
        if sid == "3d-dna/calder2-subcompartment":
            return (
                "conda run -n calder2 python "
                "skills/3d-dna/calder2-subcompartment/scripts/run_calder_compartments.py "
                "--calder-mode official-r"
            )
        return ""

    @staticmethod
    def _brief_runtime_value(value: Any, limit: int = 500) -> str:
        text = str(value or "").replace("\n", " ").strip()
        return text[:limit] if text else "none"

    @staticmethod
    def _verified_runtime_paths(detail: str) -> Dict[str, str]:
        """Extract healthcheck-emitted KEY=/absolute/path bindings.

        Only existing, non-empty paths are retained. This keeps arbitrary
        healthcheck prose from becoming trusted execution configuration.
        """
        resolved: Dict[str, str] = {}
        for raw_line in str(detail or "").splitlines():
            match = re.fullmatch(r"\s*([A-Z][A-Z0-9_]{1,63})=(/[^\r\n]+?)\s*", raw_line)
            if not match:
                continue
            key, raw_path = match.groups()
            path = Path(raw_path).expanduser()
            try:
                valid = path.is_dir() or (path.is_file() and path.stat().st_size > 0)
            except OSError:
                valid = False
            if valid:
                resolved[key] = str(path.resolve())
        return resolved

    def _controlled_runtime_discovery_hints(self, query: str, task_id: str) -> Dict[str, Any]:
        if not self.skill_manager or not hasattr(self.skill_manager, "controlled_rg_hints"):
            return {}
        roots: List[str] = []
        for path in (
            getattr(self.pm, "root_dir", None),
            getattr(self.pm, "source_root", None),
            self.skill_manager.skills_root.parent / "docs",
            self.skill_manager.skills_root.parent / "prompts",
            Path(self.data_path) / "task_outputs" / str(task_id or "default"),
        ):
            if path and Path(path).exists():
                roots.append(str(Path(path)))
        return self.skill_manager.controlled_rg_hints(query, search_roots=roots)

    def _runtime_healthcheck_for_env(self, requirements: Dict[str, Any], env: str) -> str:
        declared_env = str(requirements.get("env") or requirements.get("default_env") or "").strip()
        target_env = str(env or "").strip()
        command = str(requirements.get("healthcheck") or "").strip()
        if command and target_env and declared_env and target_env != declared_env:
            command = command.replace("{env}", target_env)
            command = re.sub(
                rf"(?m)(\bconda\s+run\s+-n\s+){re.escape(declared_env)}(\b)",
                rf"\1{target_env}\2",
                command,
            )
        return self._resolved_runtime_healthcheck(command)

    def _registered_runtime_tools(self, requirements: Dict[str, Any]) -> List[Dict[str, str]]:
        if not self.pm:
            return []
        registry = getattr(self.pm, "registry", {}) or {}
        tools = registry.get("tools") or {}
        wanted = set()
        for item in requirements.get("tools") or []:
            token = str(item or "").strip()
            if token:
                wanted.add(token.lower())
        for item in requirements.get("packages") or []:
            token = re.split(r"[<>=!~\s]", str(item or ""))[0].strip()
            if token:
                wanted.add(token.lower())
        for item in requirements.get("capabilities") or []:
            text = str(item or "").strip()
            if text.lower().startswith("tool:"):
                wanted.add(text.split(":", 1)[1].strip().lower())
        found: List[Dict[str, str]] = []
        for name, info in tools.items():
            lower = str(name or "").lower()
            if wanted and lower not in wanted:
                continue
            found.append({
                "tool": str(name),
                "env": str((info or {}).get("env") or (info or {}).get("env_name") or ""),
                "status": str((info or {}).get("status") or "registered"),
                "shim_path": str((info or {}).get("shim_path") or ""),
            })
        return found

    def _runtime_audit_for_skill(self, skill_id: str, task_id: str) -> Dict[str, Any]:
        requirements = self._skill_runtime_requirements(skill_id)
        declared_env = str(requirements.get("env") or requirements.get("default_env") or "").strip()
        healthcheck = self._runtime_healthcheck_for_env(requirements, declared_env)
        audit: Dict[str, Any] = {
            "skill_id": skill_id,
            "declared_env": declared_env,
            "env_runner": str(requirements.get("env_runner") or ""),
            "declared_healthcheck": healthcheck,
            "tools": list(requirements.get("tools") or []),
            "packages": list(requirements.get("packages") or []),
            "capabilities": list(requirements.get("capabilities") or []),
            "metadata_present": bool(requirements.get("metadata_present", True)),
            "skill_registry": {},
            "package_manager_env": {},
            "registered_tools": [],
            "matching_env_candidate": {},
            "candidate_healthcheck": {},
            "declared_ok": True,
            "declared_detail": "",
            "checked_env": declared_env,
            "decision": "no_runtime_declared",
        }

        def finalize() -> Dict[str, Any]:
            decision = str(audit.get("decision") or "no_runtime_declared")
            declared_ok = bool(audit.get("declared_ok"))
            if decision == "runtime_ready" and declared_ok:
                status = "ready"
                install_required = False
                reason = str(audit.get("declared_detail") or "Healthcheck passed.")
            elif decision == "runtime_missing_or_broken":
                status = "missing"
                install_required = True
                reason = str(audit.get("declared_detail") or "Declared runtime is missing or broken.")
            elif decision == "no_runtime_declared":
                status = "unknown"
                install_required = False
                reason = "Skill runtime metadata is missing; discovery hints require verification."
            else:
                status = "healthcheck_failed" if not declared_ok else "unknown"
                install_required = False
                reason = str(audit.get("declared_detail") or decision)
            audit.update({
                "skill": skill_id,
                "env": str(audit.get("checked_env") or declared_env),
                "status": status,
                "healthcheck": str(audit.get("declared_healthcheck") or ""),
                "healthcheck_status": "passed" if declared_ok and status == "ready" else ("not_run" if not healthcheck else "failed"),
                "install_required": install_required,
                "reason": reason,
            })
            resolved_paths = self._verified_runtime_paths(
                str(audit.get("declared_detail") or "")
            )
            checked_env = str(audit.get("checked_env") or declared_env).strip()
            conda_root = Path(str(getattr(self.pm, "conda_root", "") or ""))
            if checked_env and str(conda_root) not in {"", "."}:
                env_prefix = conda_root / "envs" / checked_env
                if env_prefix.is_dir():
                    resolved_paths.setdefault(
                        "RESOLVED_ENV_PREFIX",
                        str(env_prefix.resolve()),
                    )
            audit["resolved_runtime_paths"] = resolved_paths
            return audit

        if not declared_env or not requirements.get("metadata_present", True):
            audit["declared_ok"] = False
            audit["discovery_hints"] = self._controlled_runtime_discovery_hints(skill_id, task_id)
            return finalize()

        if self.skill_manager and hasattr(self.skill_manager, "get_runtime_record"):
            try:
                audit["skill_registry"] = dict(self.skill_manager.get_runtime_record(skill_id) or {})
            except Exception as exc:
                audit["skill_registry"] = {"error": str(exc)}
        if self.pm and hasattr(self.pm, "get_environment"):
            try:
                audit["package_manager_env"] = dict(self.pm.get_environment(declared_env) or {})
            except Exception as exc:
                audit["package_manager_env"] = {"error": str(exc)}
        audit["registered_tools"] = self._registered_runtime_tools(requirements)

        code_hint = self._consultant_runtime_code_hint(skill_id)
        ok, detail, checked_env = self._verify_skill_runtime(
            skill_id,
            task_id=task_id,
            use_cache=True,
            code=code_hint,
        )
        audit["declared_ok"] = bool(ok)
        audit["declared_detail"] = detail or ("OK" if ok else "Healthcheck failed.")
        audit["checked_env"] = checked_env or declared_env
        if ok:
            audit["decision"] = "runtime_ready"
            return finalize()

        match = None
        if self.pm and hasattr(self.pm, "find_matching_environment"):
            try:
                match = self.pm.find_matching_environment(requirements)
            except Exception as exc:
                match = {"error": str(exc)}
        if isinstance(match, dict) and match.get("env") and match.get("env") != declared_env:
            record = dict(match.get("record") or {})
            audit["matching_env_candidate"] = {
                "env": str(match.get("env") or ""),
                "match_type": str(match.get("match_type") or ""),
                "matched_packages": list(match.get("matched_packages") or []),
                "matched_capabilities": list(match.get("matched_capabilities") or []),
                "missing_packages": list(match.get("missing_packages") or []),
                "missing_capabilities": list(match.get("missing_capabilities") or []),
                "status": str(record.get("status") or ""),
                "bound_tools": list(record.get("bound_tools") or []),
            }
            candidate_env = str(match.get("env") or "")
            candidate_healthcheck = self._runtime_healthcheck_for_env(requirements, candidate_env)
            if candidate_healthcheck and self.pm and hasattr(self.pm, "verify_environment"):
                cand_ok, cand_detail = self.pm.verify_environment(
                    env_name=candidate_env,
                    language=str(requirements.get("language") or ""),
                    healthcheck=candidate_healthcheck,
                )
                audit["candidate_healthcheck"] = {
                    "ok": bool(cand_ok),
                    "env": candidate_env,
                    "healthcheck": candidate_healthcheck,
                    "detail": cand_detail or ("OK" if cand_ok else "Healthcheck failed."),
                }
                self._cache_runtime_status(
                    task_id,
                    skill_id,
                    candidate_env,
                    bool(cand_ok),
                    cand_detail or ("OK" if cand_ok else "Healthcheck failed."),
                    candidate_healthcheck,
                    str(requirements.get("env_runner") or ""),
                    mode="candidate",
                )
                audit["decision"] = (
                    "alternate_runtime_candidate_verified_needs_user_or_planner_decision"
                    if cand_ok else
                    "alternate_runtime_candidate_failed"
                )
            else:
                audit["candidate_healthcheck"] = {"ok": False, "env": candidate_env, "detail": "not_run"}
                audit["decision"] = "alternate_runtime_candidate_unverified"
        elif audit["registered_tools"]:
            audit["decision"] = "registered_tools_available_but_declared_env_failed"
        else:
            missing_runtime_evidence = bool(re.search(
                r"Environment(?:Name|Location)NotFound|not a conda environment|"
                r"environment\s+[^\n]{0,120}\s+(?:missing|not found|does not exist)",
                str(audit.get("declared_detail") or ""),
                re.IGNORECASE,
            ))
            audit["decision"] = (
                "runtime_missing_or_broken"
                if missing_runtime_evidence else
                "declared_runtime_healthcheck_failed"
            )
        return finalize()

    def _format_runtime_audit(self, audits: List[Dict[str, Any]]) -> str:
        if not audits:
            return ""
        lines = [
            "[RUNTIME_AUDIT]",
            "- Consultant checked skill runtime declarations, registered environments, registered tool shims, and healthchecks before Planner handoff.",
            "- Do not use bare `which tool` in the current shell as proof that a skill runtime exists. Use declared healthcheck, registered env/shim evidence, or ask/register/install.",
            "- Use the declared env_runner for analysis commands. Never synthesize a Conda prefix from the project or benchmark root; resolve runtime paths inside the verified environment.",
            f"- controller_python: {sys.executable}",
            "- Use controller_python only when an active skill explicitly declares a renderer/helper as a current-Python or controller-side utility.",
        ]
        for audit in audits:
            tools = audit.get("registered_tools") or []
            match = audit.get("matching_env_candidate") or {}
            cand = audit.get("candidate_healthcheck") or {}
            registered_tool_text = ", ".join(
                f"{item.get('tool')}@{item.get('env') or 'unknown'}:{item.get('status') or 'registered'}"
                for item in tools
            ) or "none"
            declared_tools = ",".join(audit.get("tools") or audit.get("packages") or []) or "none"
            lines.extend([
                f"skill: {audit.get('skill', audit.get('skill_id', ''))}",
                f"env: {audit.get('env', audit.get('declared_env', '')) or 'none'}",
                f"declared_env: {audit.get('declared_env', '') or 'none'}",
                f"env_runner: {audit.get('env_runner', '') or 'none'}",
                f"tools: {declared_tools}",
                f"status: {audit.get('status', 'unknown')}",
                f"healthcheck: {audit.get('healthcheck_status', 'not_run')}; command={self._brief_runtime_value(audit.get('healthcheck') or audit.get('declared_healthcheck'), 300)}",
                f"install_required: {'true' if audit.get('install_required') else 'false'}",
                f"reason: {self._brief_runtime_value(audit.get('reason'), 500)}",
                f"decision: {audit.get('decision', '')}",
                f"registered_tools: {registered_tool_text}",
            ])
            if match:
                lines.append(
                    "matching_env_candidate: "
                    f"env={match.get('env', '')}; type={match.get('match_type', '')}; "
                    f"missing_packages={','.join(match.get('missing_packages', []) or []) or 'none'}; "
                    f"missing_capabilities={','.join(match.get('missing_capabilities', []) or []) or 'none'}; "
                    f"bound_tools={','.join(match.get('bound_tools', []) or []) or 'none'}"
                )
            if cand:
                lines.append(
                    "candidate_healthcheck: "
                    f"{'passed' if cand.get('ok') else 'failed'}; env={cand.get('env', '')}; "
                    f"command={self._brief_runtime_value(cand.get('healthcheck'), 300)}; "
                    f"detail={self._brief_runtime_value(cand.get('detail'), 500)}"
                )
            resolved_paths = audit.get("resolved_runtime_paths") or {}
            if resolved_paths:
                lines.append(
                    "resolved_runtime_paths: "
                    + "; ".join(
                        f"{key}={value}"
                        for key, value in sorted(resolved_paths.items())
                    )
                )
                lines.append(
                    "resolved_path_policy: use these exact verified values; "
                    "do not rebuild them from project_root, cwd, or benchmark_root"
                )
            hints = audit.get("discovery_hints") or {}
            if hints:
                lines.append(
                    "discovery_hints: "
                    f"candidate_envs={','.join(hints.get('candidate_envs', []) or []) or 'none'}; "
                    f"candidate_tools={','.join(hints.get('candidate_tools', []) or []) or 'none'}; "
                    f"evidence_files={','.join(hints.get('evidence_files', []) or []) or 'none'}; "
                    "evidence_only=true"
                )
            lines.append("---")
        lines.append("[/RUNTIME_AUDIT]")
        return "\n".join(lines)

    def _consultant_runtime_preflight(
        self,
        selected_skills: Optional[List[str]],
        task_id: str,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """Verify selected runtimes and return structured plus prompt-safe audit evidence."""
        if not getattr(self, "skill_manager", None) or not getattr(self, "pm", None):
            return [], ""
        selected = self._selected_skill_ids(selected_skills)
        if not selected:
            return [], ""

        rows: List[str] = []
        audits: List[Dict[str, Any]] = []
        for sid in selected:
            requirements = self._skill_runtime_requirements(sid)
            env = str(requirements.get("env") or requirements.get("default_env") or "").strip()
            audit = self._runtime_audit_for_skill(sid, task_id)
            audits.append(audit)
            ok = bool(audit.get("declared_ok"))
            detail = str(audit.get("reason") or audit.get("declared_detail") or "")
            checked_env = str(audit.get("checked_env") or env)
            status = "passed" if ok else "failed"
            row = (
                f"- {sid}: {status}; env={checked_env or env}; "
                f"healthcheck={audit.get('declared_healthcheck', '') or requirements.get('healthcheck', '') or 'metadata package check'}; "
                f"detail={str(detail or '').strip()[:500] or 'OK'}"
            )
            match = audit.get("matching_env_candidate") or {}
            if not ok and match.get("env"):
                row += (
                    f"; registered_env_candidate={match.get('env')} "
                    f"({match.get('match_type', 'unknown')}; "
                    f"missing_packages={','.join(match.get('missing_packages', []) or []) or 'none'}; "
                    f"missing_capabilities={','.join(match.get('missing_capabilities', []) or []) or 'none'})"
                )
            if not ok and audit.get("decision"):
                row += f"; decision={audit.get('decision')}"
            rows.append(row)

        if not rows:
            return audits, ""
        runtime_audit = self._format_runtime_audit(audits)
        summary = (
            "[RUNTIME_PREFLIGHT]\n"
            "- Consultant checked the selected active skill runtime(s) before Planner handoff.\n"
            "- A tool registered in a different environment is not automatically available inside the active skill runtime; Planner/Generate must use the skill env_runner, a registered command shim, or a documented fallback.\n"
            + "\n".join(rows)
            + "\n[/RUNTIME_PREFLIGHT]"
            + ("\n\n" + runtime_audit if runtime_audit else "")
        )
        return audits, summary

    def _consultant_runtime_preflight_summary(self, selected_skills: Optional[List[str]], task_id: str) -> str:
        """Backward-compatible textual view for callers and tests."""
        _audits, summary = self._consultant_runtime_preflight(selected_skills, task_id)
        return summary

    def _consultant_runtime_context(self, candidate_skill_ids: Optional[List[str]]) -> str:
        """Render only runtime metadata relevant to Consultant candidates.

        The full package-manager inventory is useful to execution nodes but is
        noisy and easy to misread during routing.  Consultant only needs the
        declared runtime plus any registered status for the few candidate
        skills it may select.
        """
        if not self.skill_manager or not candidate_skill_ids:
            return "<runtime_candidates>\nSelect a canonical skill before resolving its runtime.\n</runtime_candidates>"

        lines = ["<runtime_candidates>"]
        for skill_id in self.skill_manager.normalize_selected_skills(
            candidate_skill_ids,
            max_results=self._selection_max_skills(" ".join(candidate_skill_ids)),
        ):
            requirements = self._skill_runtime_requirements(skill_id)
            record = self.skill_manager.get_runtime_record(skill_id) or {}
            env = str(
                requirements.get("env")
                or requirements.get("default_env")
                or record.get("env")
                or "unspecified"
            ).strip()
            healthcheck = str(requirements.get("healthcheck") or record.get("healthcheck") or "declared by skill").strip()
            status = str(record.get("status") or "unknown").strip()
            lines.append(
                f"- skill={skill_id}; env={env}; registered_status={status}; "
                f"healthcheck={self._brief_runtime_value(healthcheck, 220)}"
            )
        lines.append("</runtime_candidates>")
        return "\n".join(lines)

    def _format_runtime_status_for_observation(
        self,
        task_id: str,
        selected_skills: Optional[List[str]] = None,
    ) -> str:
        records = self._runtime_status_for_task(task_id, selected_skills)
        if not records:
            return ""
        lines = ["<runtime_status>"]
        for record in records.values():
            lines.append(
                "- "
                f"skill_id={record.get('skill_id', '')}; "
                f"env={record.get('env', '')}; "
                f"mode={record.get('mode', '')}; "
                f"ok={record.get('ok', False)}; "
                f"env_runner={record.get('env_runner', '')}; "
                f"checked_at={record.get('checked_at', '')}"
            )
        lines.append("</runtime_status>")
        return "\n" + "\n".join(lines)

    def _selected_skill_env_runner_violation(self, code: str, selected_skills: Optional[List[str]]) -> str:
        if not self.skill_manager or not selected_skills:
            return ""
        code_text = str(code or "")
        normalized_code = code_text.replace("\\", "/")
        code_lines = [
            line.strip()
            for line in code_text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for sid in self._selected_skill_ids(selected_skills):
            requirements = self._skill_runtime_requirements(sid)
            runner = str(requirements.get("env_runner") or "").strip()
            if not runner:
                continue
            env = str(requirements.get("env") or requirements.get("default_env") or "").strip()
            install_tool_name = self._install_tool_name(requirements.get("install_tool") or "")
            if install_tool_name and re.search(rf"(?m)^\s*{re.escape(install_tool_name)}\b", code_text):
                install_tag = self._install_tool_tag(install_tool_name, env)
                return (
                    f"Selected skill `{sid}` declares install_tool `{install_tool_name}`, but the generated "
                    f"<execute> command invokes it like a script/interpreter. `{install_tool_name}` is only for "
                    f"installation delegation via `{install_tag}`, not for running analysis code. "
                    f"If the runtime is already available, regenerate the command with env_runner `{runner}`. "
                    f"If the runtime healthcheck fails, emit `{install_tag}` outside <execute>."
                )
            meta = self.skill_manager.get_skill_meta(sid)
            skill_dir = Path(meta.get("_skill_dir", self.skill_manager.skills_root / sid)).resolve()
            runner_present = runner in code_text or (env and f"conda run -n {env}" in code_text)
            logical_code = re.sub(r"\\\s*\n", " ", code_text)
            for script in meta.get("_scripts", []) or []:
                script_name = Path(script).name
                rel_script = str(script).replace("\\", "/")
                abs_script = str((skill_dir / script).resolve()).replace("\\", "/")
                mentions_script = (
                    script_name in code_text
                    or rel_script in normalized_code
                    or abs_script in normalized_code
                )
                if not mentions_script:
                    continue
                script_path = (skill_dir / script).resolve()
                if script_path.suffix.lower() == ".py":
                    try:
                        script_source = script_path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        script_source = ""
                    declared_flags = set(re.findall(r"['\"](--[A-Za-z0-9][A-Za-z0-9-]*)['\"]", script_source))
                    command_tail = ""
                    for command_line in logical_code.splitlines():
                        token = next(
                            (
                                item for item in (abs_script, rel_script, script_name)
                                if item and item in command_line
                            ),
                            "",
                        )
                        if token:
                            command_tail = command_line.split(token, 1)[1]
                            break
                    used_flags = set(re.findall(r"(?<![A-Za-z0-9-])--[A-Za-z0-9][A-Za-z0-9-]*", command_tail))
                    unknown_flags = sorted(used_flags - declared_flags - {"--help"})
                    if declared_flags and unknown_flags:
                        return (
                            f"Selected skill `{sid}` script `{script_name}` was called with undeclared "
                            f"flag(s): {', '.join(unknown_flags)}. Use only the script's actual CLI "
                            "arguments from its --help/source and active skill documentation."
                        )
                    undeclared_conda_python = bool(re.search(
                        rf"\bconda\s+run\b[^\n]*\bpython(?:\d+(?:\.\d+)*)?\b[^\n]*{re.escape(script_name)}",
                        logical_code,
                    ))
                    if runner == "bash" and undeclared_conda_python:
                        return (
                            f"Selected skill `{sid}` declares env_runner `bash`, but `{script_name}` was "
                            f"wrapped with an undeclared Conda Python. Use controller Python `{sys.executable}` "
                            "when the skill identifies this as a current-Python renderer/helper; keep the "
                            "declared analysis environment only for its tools."
                        )
                if runner == "bash":
                    continue
                if mentions_script and not runner_present:
                    return (
                        f"Selected skill `{sid}` declares env_runner `{runner}` but the generated command "
                        f"mentions `{script_name}` without that runner. Regenerate the command as "
                        f"`{runner} {rel_script} ...` and do not use bare `python {rel_script}`."
                    )
            skill_terms = " ".join([
                " ".join(map(str, requirements.get("packages", []) or [])),
                " ".join(map(str, requirements.get("capabilities", []) or [])),
                " ".join(map(str, meta.get("tags", []) or [])),
            ]).lower()
            managed_tools = set()
            if any(term in skill_terms for term in ["cooler", "cool", "mcool"]):
                managed_tools.add("cooler")
            if any(term in skill_terms for term in ["hicexplorer", "hicfindtads", "hicdifferentialtad", "hi-c"]):
                managed_tools.update({
                    "hicFindTADs",
                    "hicDifferentialTAD",
                    "hicPlotTADs",
                    "hicConvertFormat",
                    "hicTransform",
                })
            for tool in sorted(managed_tools):
                bare_tool_re = re.compile(rf"^(?:env\s+[^;&|]+\s+)?{re.escape(tool)}(?:\s|$)")
                if any(bare_tool_re.search(line) for line in code_lines) and not runner_present:
                    env_prefix = f"conda run -n {env}" if env else runner.rsplit(" ", 1)[0]
                    return (
                        f"Selected skill `{sid}` uses managed runtime `{env or runner}` but the generated command "
                        f"calls bare `{tool}`. Regenerate it as `{env_prefix} {tool} ...` "
                        f"or use `{runner} -c \"...\"` for Python-based preflight. Do not ask for code review "
                        f"on bare `{tool}` commands."
                    )
            if sid == "3d-dna/calder2-subcompartment" and env and re.search(r"\bRscript\b|library\s*\(\s*CALDER", code_text, re.IGNORECASE):
                wrong_envs = []
                wrong_envs.extend(
                    match
                    for match in re.findall(r"\bconda\s+run\s+-n\s+([A-Za-z0-9_.-]+)\s+Rscript\b", code_text, re.IGNORECASE)
                    if match != env
                )
                wrong_envs.extend(
                    match
                    for match in re.findall(r"/envs/([^/\s]+)/bin/Rscript\b", code_text)
                    if match != env
                )
                wrong_envs.extend(
                    match
                    for match in re.findall(r"\bconda\s+activate\s+([A-Za-z0-9_.-]+)", code_text, re.IGNORECASE)
                    if match != env
                )
                if wrong_envs:
                    wrong = ", ".join(list(dict.fromkeys(wrong_envs)))
                    return (
                        f"Selected skill `{sid}` is bound to runtime env `{env}`, but the generated code "
                        f"uses Rscript/R from another environment: {wrong}. Repair `{env}` instead: "
                        f"install/register the selected skill runtime dependencies in `{env}` via install_subagent, then rerun "
                        f"`conda run -n {env} which Rscript` and `conda run -n {env} Rscript -e \"library(CALDER)\"`."
                    )
        return ""

    def _skill_command_names(self, skill_id: str) -> set[str]:
        if not self.skill_manager:
            return set()
        meta = self.skill_manager.get_skill_meta(skill_id)
        requirements = self._skill_runtime_requirements(skill_id)
        terms = " ".join([
            " ".join(map(str, requirements.get("packages", []) or [])),
            " ".join(map(str, requirements.get("capabilities", []) or [])),
            " ".join(map(str, meta.get("tags", []) or [])),
            " ".join(map(str, meta.get("legacy_ids", []) or [])),
        ]).lower()
        commands: set[str] = set()
        if "cooler" in terms or "mcool" in terms:
            commands.add("cooler")
        if "hicfindtads" in terms:
            commands.add("hicFindTADs")
        if "hicdifferentialtad" in terms or "differential-tad" in terms:
            commands.add("hicDifferentialTAD")
        if "hicplottads" in terms:
            commands.add("hicPlotTADs")
        if "hicconvertformat" in terms:
            commands.add("hicConvertFormat")
        if "hictransform" in terms:
            commands.add("hicTransform")
        return commands

    def _workflow_future_skill_violation(
        self,
        code: str,
        active_skills: Optional[List[str]],
        future_skills: Optional[List[str]],
    ) -> str:
        if not self.skill_manager or not future_skills:
            return ""
        code_text = str(code or "")
        normalized_code = code_text.replace("\\", "/")
        code_lines = [
            line.strip()
            for line in code_text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        active_set = set(self._selected_skill_ids(active_skills or []))
        active_commands = set()
        for sid in active_set:
            active_commands.update(self._skill_command_names(sid))

        for sid in self._selected_skill_ids(future_skills):
            if sid in active_set:
                continue
            meta = self.skill_manager.get_skill_meta(sid)
            skill_dir = Path(meta.get("_skill_dir", self.skill_manager.skills_root / sid)).resolve()
            for script in meta.get("_scripts", []) or []:
                script_name = Path(script).name
                rel_script = str(script).replace("\\", "/")
                abs_script = str((skill_dir / script).resolve()).replace("\\", "/")
                if script_name in code_text or rel_script in normalized_code or abs_script in normalized_code:
                    return (
                        f"Generated command mentions future-stage skill `{sid}` script `{script_name}` "
                        "before that workflow batch is active. Finish the current active batch, then "
                        "output the appropriate <activate_batch ... /> action before using this script."
                    )
            for command in sorted(self._skill_command_names(sid) - active_commands):
                command_re = re.compile(rf"^(?:env\s+[^;&|]+\s+)?(?:conda\s+run\s+-n\s+\S+\s+)?{re.escape(command)}(?:\s|$)")
                if any(command_re.search(line) for line in code_lines):
                    return (
                        f"Generated command calls `{command}` from future-stage skill `{sid}` before "
                        "that workflow batch is active. Finish the current active batch and activate "
                        "the next batch before running it."
                    )
        return ""

    def _reuse_registered_runtime_for_install(
        self,
        selected_skills: Optional[List[str]],
        specified_env: Optional[str],
        task_id: str,
    ) -> Dict[str, Any]:
        if not self.skill_manager or not self.pm:
            return {}
        for sid in self._selected_skill_ids(selected_skills):
            requirements = self._skill_runtime_requirements(sid)
            env = requirements.get("env") or requirements.get("default_env", "")
            if not env:
                continue
            if specified_env and specified_env not in {env, "default"}:
                continue
            runtime_record = self.skill_manager.get_runtime_record(sid)
            pm_record = self.pm.get_environment(env)
            if not runtime_record and not pm_record:
                continue
            ok, detail, verified_env = self._verify_skill_runtime(sid, task_id=task_id)
            if ok:
                final_res = (
                    f"✅ Reusing registered environment '{verified_env}' for selected skill '{sid}'. "
                    "Healthcheck passed; installation is not needed."
                )
                return {"ok": True, "env": verified_env, "detail": detail, "message": final_res, "skill_id": sid}
            return {
                "ok": False,
                "env": verified_env or env,
                "detail": detail or "Healthcheck failed.",
                "skill_id": sid,
            }
        return {}

    def _skill_plan_violations(
        self,
        plan: str,
        selected_skills: List[str],
        user_msg: str = "",
        workflow_handoff: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        violations = self.skill_manager.validate_plan(plan, selected_skills) if self.skill_manager and selected_skills else []
        plan_l = str(plan or "").lower()
        request_text = str(user_msg or "")

        if workflow_handoff:
            template_by_stage: Dict[str, Dict[str, Any]] = {}
            workflow_id = str(workflow_handoff.get("workflow_id") or "").strip()
            if self.workflow_manager and workflow_id:
                template = self.workflow_manager.get_workflow(workflow_id)
                template_by_stage = {
                    str(stage.get("stage") or "").strip(): dict(stage)
                    for stage in template.get("stages", []) or []
                    if str(stage.get("stage") or "").strip()
                }
            for stage in workflow_handoff.get("workflow_stages", []) or []:
                if not isinstance(stage, dict):
                    continue
                stage_name = str(stage.get("stage") or "").strip()
                if not stage_name:
                    continue
                template_stage = template_by_stage.get(stage_name, {})
                output_terms = (
                    [str(item) for item in self.workflow_manager._ensure_list(template_stage.get("outputs"))]
                    if self.workflow_manager else []
                )
                terms = [
                    stage_name,
                    stage_name.replace("_", " "),
                    str(stage.get("skill") or template_stage.get("default_skill") or ""),
                    *output_terms,
                ]
                if not any(term and term.lower() in plan_l for term in terms):
                    violations.append(
                        f"Plan coverage missing workflow handoff stage `{stage_name}`. "
                        "Include it as an executable step with `Stage:` and `Skills:` fields, an ASK_USER decision, or a stage_blocked item."
                    )

        expected_match = re.search(
            r"(?:预期产物|expected outputs?|final outputs?)\s*[:：]\s*(.*?)(?=\n\s*(?:通用要求|允许|禁止|依赖规则|停止条件|输入文件|输出目录|$))",
            request_text,
            re.IGNORECASE | re.DOTALL,
        )
        if expected_match:
            for artifact in re.findall(
                r"([A-Za-z0-9_.+\-/]+\.(?:tsv|csv|txt|md|html?|json|ya?ml|rds|png|pdf|svg|sh|R|r|py))\b",
                expected_match.group(1),
                flags=re.IGNORECASE,
            ):
                artifact_name = Path(artifact).name.lower()
                if artifact_name and artifact_name not in plan_l:
                    violations.append(f"Plan coverage missing user-expected output `{artifact_name}`.")

        return violations

    def _repair_consultant_skill_summary(
        self,
        summary: str,
        repaired_skills: List[str],
        raw_selected: List[str],
        normalized_selected: List[str],
        issues: List[str],
    ) -> str:
        """Replace an invalid Consultant skill marker with canonical IDs and keep an audit note."""
        marker = "[SELECTED_SKILLS: " + ", ".join(repaired_skills) + "]"
        repaired_summary = re.sub(
            r"\[SELECTED_SKILLS\s*:\s*.*?\]",
            marker,
            summary or "",
            count=1,
            flags=re.DOTALL,
        )
        if repaired_summary == (summary or "") and marker not in repaired_summary:
            repaired_summary = marker + "\n" + repaired_summary

        audit_note = (
            "[CONSULTANT_SKILL_REPAIR]\n"
            "- Deterministically replaced invalid Consultant-selected skills before Planner handoff.\n"
            f"- raw_selected: {', '.join(raw_selected) if raw_selected else 'None'}\n"
            f"- normalized_selected_before_repair: {', '.join(normalized_selected) if normalized_selected else 'None'}\n"
            f"- validation_issues: {'; '.join(issues) if issues else 'None'}\n"
            f"- repair_selected: {', '.join(repaired_skills) if repaired_skills else 'None'}\n"
            "[/CONSULTANT_SKILL_REPAIR]"
        )
        return repaired_summary.strip() + "\n" + audit_note

    def _selection_max_skills(self, text: str) -> int:
        """Return the task-wide selection bound; Planner owns per-step narrowing."""
        _ = text
        return self.skill_manager.MAX_SELECTED_SKILLS if self.skill_manager else 1

    def _redirect_install_command(self, code: str, selected_skills: Optional[List[str]] = None) -> Optional[str]:
        install_re = re.search(
            r"(?:^|\n)\s*(?:python(?:3)?\s+-m\s+)?pip\s+install\s+([^\n]+)|(?:^|\n)\s*(?:conda|mamba)\s+(?:install|create)\b([^\n]+)",
            code,
            re.IGNORECASE,
        )
        if not install_re:
            r_install_re = re.search(
                r"\bRscript\b.*(?:install\.packages|install_github)\s*\(",
                code,
                re.IGNORECASE | re.DOTALL,
            )
            if r_install_re:
                defaults = self._skill_runtime_defaults(selected_skills)
                env_match = re.search(r"\bconda\s+run\s+-n\s+([A-Za-z0-9_.-]+)\s+Rscript\b", code, re.IGNORECASE)
                env = defaults.get("env") or (env_match.group(1) if env_match else "") or "bio_r_runtime"
                if re.search(r"\bcalder\b", code, re.IGNORECASE) and self._calder_runtime_allowed(selected_skills, env):
                    tool_name = "calder_r_runtime"
                else:
                    tool_name = defaults.get("install_tool") or defaults.get("tool_name") or "r_package_runtime"
                return (
                    self._install_tool_tag(tool_name, env)
                    + "<!-- R package installation must be performed by install_subagent and rechecked after install. -->"
                )
            return None
        payload = install_re.group(1) or install_re.group(2) or ""
        raw_tokens = [t for t in re.split(r"\s+", payload.strip()) if t]
        tokens = []
        skip_next = False
        requested_env = ""
        for token in raw_tokens:
            if skip_next:
                skip_next = False
                continue
            if token in {"-n", "--name"} and not requested_env:
                skip_next = True
                continue
            if token in {"-n", "--name", "-p", "--prefix"}:
                skip_next = True
                continue
            if token in {"-c", "--channel"}:
                skip_next = True
                continue
            if token.startswith("-"):
                continue
            tokens.append(token)
        env_match = re.search(r"(?:^|\s)(?:-n|--name)\s+([A-Za-z0-9_.-]+)", payload)
        if env_match:
            requested_env = env_match.group(1)
        package = next((t for t in tokens if not re.match(r"^(?:python=|env=)", t, re.I)), "python-package")
        package = re.split(r"[<>=!~]", package)[0]

        defaults = self._skill_runtime_defaults(selected_skills)
        env = defaults.get("env", "")
        if not env:
            env = requested_env or f"bio_{package.lower().replace('-', '_')}"

        tool_name = defaults.get("tool_name") if defaults.get("selected") else package
        return self._install_tool_tag(tool_name, env)

    def _infer_runtime_skills_from_code(
        self,
        code: str,
        selected_skills: Optional[List[str]] = None,
    ) -> List[str]:
        skills = list(dict.fromkeys(
            sid for sid in self._selected_skill_ids(selected_skills)
            if not (
                self.skill_manager
                and hasattr(self.skill_manager, "is_workflow_router_skill")
                and self.skill_manager.is_workflow_router_skill(sid)
            )
        ))
        if not self.skill_manager:
            return skills

        text = str(code or "")
        lower = text.lower()
        candidates = []
        if (
            "run_rrblup_package.r" in lower
            or "crop-gs-rrblup-gblup" in lower
            or re.search(r"\brrblup\b", text, re.IGNORECASE)
            or re.search(r"\br-rrblup\b", text, re.IGNORECASE)
            or re.search(r"\bconda\s+run\s+-n\s+crop_gp_pro\b", text, re.IGNORECASE)
        ):
            candidates.append("crop-breeding/crop-gs-rrblup-gblup")

        if hasattr(self.skill_manager, "find_runtime_skills_for_tool"):
            for tool in self._runtime_tool_candidates_from_text(text):
                for sid in self.skill_manager.find_runtime_skills_for_tool(
                    tool,
                    selected_skill_ids=selected_skills or [],
                    max_results=3,
                ):
                    if sid not in candidates:
                        candidates.append(sid)

        for candidate in candidates:
            normalized = self.skill_manager.normalize_selected_skills([candidate], max_results=1)
            for sid in normalized:
                if sid in set(self._selected_skill_ids(selected_skills or [])) and sid not in skills:
                    skills.insert(0, sid)
        return skills

    @staticmethod
    def _looks_like_missing_runtime_dependency(text: str) -> bool:
        lower = str(text or "").lower()
        patterns = (
            r"there is no package called ['\"]?rrblup",
            r"there is no package called ['\"]?calder",
            r"there is no package called .{0,4}calder",
            r"cran package rrblup is required",
            r"\brrblup\b.*\bnot found\b",
            r"\bcalder2?\b.*\bnot available\b",
            r"\bpackage\b.*\brrblup\b.*\brequired\b",
            r"\brscript:\s*command not found\b",
            r"\bno rscript\b",
            r"environmentlocationnotfound",
            r"could not find conda environment",
            r"commandnotfounderror",
            r"\b(?:makeblastdb|blastp|blastn|blastdbcmd|hmmsearch|hmmbuild|diamond)\s*:\s*command not found\b",
            r"\b(?:no|which:\s*no)\s+(?:makeblastdb|blastp|blastn|blastdbcmd|hmmsearch|hmmbuild|diamond)\b",
            r"\bsyntaxerror:\s*invalid syntax\b",
            r"\b404:\s*not found\b",
            r"\bcorrupt(?:ed)?\s+(?:runtime|environment|installation|dependency)\b",
            r"\b(?:runtime|environment|installation|dependency)\s+corrupt(?:ed)?\b",
        )
        return any(re.search(pattern, lower, re.IGNORECASE) for pattern in patterns)

    @staticmethod
    def _missing_executables_from_error(text: str) -> List[str]:
        tools: List[str] = []
        seen: set[str] = set()

        def add(tool: str) -> None:
            cleaned = str(tool or "").strip()
            if not cleaned or "/" in cleaned or cleaned.startswith("-") or cleaned in seen:
                return
            seen.add(cleaned)
            tools.append(cleaned)

        for match in re.finditer(r"\b([A-Za-z0-9_.+-]+)\s*:\s*command not found\b", str(text or ""), re.IGNORECASE):
            add(match.group(1))
        for match in re.finditer(r"\b(?:which:\s*)?no\s+([A-Za-z0-9_.+-]+)\s+(?:in|found)\b", str(text or ""), re.IGNORECASE):
            add(match.group(1))
        for match in re.finditer(r"/bin/([A-Za-z0-9_.+-]+):\s+No such file or directory\b", str(text or ""), re.IGNORECASE):
            add(match.group(1))
        return tools

    @staticmethod
    def _shell_command_candidates_from_text(text: str) -> List[str]:
        ignored = {
            "cd", "cat", "chmod", "conda", "cut", "echo", "env", "false", "find",
            "grep", "head", "ls", "mkdir", "printf", "run", "sed", "sh", "sort",
            "tail", "test", "tr", "true", "uniq", "awk", "bash",
        }
        tools: List[str] = []

        def add(token: str) -> None:
            cleaned = str(token or "").strip().strip('"\'')
            cleaned = cleaned.split("/")[-1]
            if not cleaned or cleaned.startswith("-"):
                return
            lower = cleaned.lower()
            if lower in ignored or re.fullmatch(r"[A-Z_][A-Z0-9_]*", cleaned):
                return
            if lower not in tools:
                tools.append(lower)

        for match in re.finditer(r"\bconda\s+run\s+-n\s+\S+\s+(?:bash\s+-c\s+)?([A-Za-z0-9_.+/-]+)", str(text or ""), re.IGNORECASE):
            add(match.group(1))
        for segment in re.split(r"(?:&&|\|\||\||;|\n)", str(text or "")):
            stripped = segment.strip()
            if not stripped or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", stripped):
                continue
            try:
                parts = shlex.split(stripped)
            except ValueError:
                parts = stripped.split()
            while parts and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", parts[0]):
                parts.pop(0)
            if not parts:
                continue
            if parts[:3] == ["conda", "run", "-n"] and len(parts) >= 5:
                add(parts[4])
            elif parts[:2] == ["conda", "run"] and len(parts) >= 4:
                add(parts[3])
            else:
                add(parts[0])
        return tools

    def _runtime_tool_candidates_from_text(self, text: str) -> List[str]:
        tools: List[str] = []
        for tool in self._missing_executables_from_error(text) + self._shell_command_candidates_from_text(text):
            if tool not in tools:
                tools.append(tool)
        return tools

    @staticmethod
    def _install_tool_parts(value: str, fallback_env: str = "") -> Tuple[str, str]:
        text = str(value or "").strip()
        if not text:
            return "", str(fallback_env or "").strip()
        match = re.fullmatch(
            r"<install_tool(?:\s+env=[\"'](?P<env>.*?)[\"'])?>(?P<name>.*?)</install_tool>",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            return match.group("name").strip(), (match.group("env") or fallback_env or "").strip()
        return text, str(fallback_env or "").strip()

    @classmethod
    def _install_tool_name(cls, value: str) -> str:
        name, _env = cls._install_tool_parts(value)
        return name

    @classmethod
    def _install_tool_tag(cls, value: str, env: str) -> str:
        name, declared_env = cls._install_tool_parts(value, env)
        return f'<install_tool env="{declared_env or env}">{name or "skill_runtime"}</install_tool>'

    @staticmethod
    def _runtime_install_disallowed_by_context(text: str) -> bool:
        haystack = str(text or "")
        patterns = (
            r"(?:环境|运行环境|依赖|runtime|env|environment|dependency).{0,40}(?:缺失|missing|not found|不存在|unavailable).{0,40}(?:blocked|block|阻塞|终止|停止)",
            r"(?:缺失|missing|not found|不存在|unavailable).{0,40}(?:环境|运行环境|依赖|runtime|env|environment|dependency).{0,40}(?:blocked|block|阻塞|终止|停止)",
            r"(?:不要|不得|禁止|不允许).{0,30}(?:安装|install)",
            r"(?:do not|don't|never).{0,30}(?:install|installation)",
            r"\bno\s+(?:runtime\s+)?install(?:ation)?\b(?!\s+required)",
        )
        return any(re.search(pattern, haystack, re.IGNORECASE) for pattern in patterns)

    @staticmethod
    def _runtime_policy_context_from_state(state: Dict[str, Any]) -> str:
        messages = list((state or {}).get("messages", []) or [])
        sampled = messages[:6] + messages[-6:]
        parts = [
            str((state or {}).get("requirements_summary") or ""),
            str((state or {}).get("current_plan") or ""),
        ]
        parts.extend(str(getattr(message, "content", message)) for message in sampled)
        return "\n".join(part for part in parts if part)

    def _runtime_block_message(self, redirect: str, selected_skills: Optional[List[str]], evidence: str = "") -> str:
        env = ""
        tool_name = ""
        match = re.search(r"<install_tool(?:\s+env=[\"'](.*?)[\"'])?>(.*?)</install_tool>", str(redirect or ""), re.DOTALL)
        if match:
            env = (match.group(1) or "").strip()
            tool_name = self._install_tool_name(match.group(2))
        skills = ", ".join(self._selected_skill_ids(selected_skills)) or "selected skill runtime"
        detail = str(evidence or redirect or "").strip().replace("\n", " ")[:600]
        return (
            "blocked: required runtime dependency is missing, and the current task explicitly "
            "requires missing environments/dependencies to be treated as blocked instead of "
            f"delegating installation. skills={skills}; env={env or 'unknown'}; "
            f"install_tool={tool_name or 'skill_runtime'}; evidence={detail}"
        )

    def _consultant_runtime_block_handoff(
        self,
        consultant_context: str,
        code: str,
        stdout: str,
        stderr: str,
        task_id: str,
        max_skills: int = 1,
    ) -> Dict[str, Any]:
        selected: List[str] = []
        raw_selected: List[str] = []
        if self.skill_manager:
            raw_selected = self.skill_manager.match_skills(consultant_context, max_results=max_skills)
            selected = self.skill_manager.normalize_selected_skills(raw_selected, max_results=max_skills)
        if not selected:
            selected = []
        runtime_skills = self._infer_runtime_skills_from_code(code, selected)
        evidence = (stderr or stdout or "Consultant runtime verification failed.").strip()
        env_match = re.search(r"\bconda\s+run\s+-n\s+([A-Za-z0-9_.-]+)", str(code or ""), re.IGNORECASE)
        env_hint = env_match.group(1) if env_match else ""
        for sid in runtime_skills:
            requirements = self._skill_runtime_requirements(sid)
            env = str(requirements.get("env") or requirements.get("default_env") or env_hint or "").strip()
            if not env:
                continue
            self._cache_runtime_status(
                task_id,
                sid,
                env,
                False,
                evidence[:1000],
                requirements.get("healthcheck", ""),
                requirements.get("env_runner", ""),
                mode="consultant",
            )
        skill_marker = ", ".join(runtime_skills) if runtime_skills else "None"
        summary = (
            f"[SELECTED_SKILLS: {skill_marker}]\n\n"
            "[MISSING_INPUTS]\n"
            "- none\n"
            "[/MISSING_INPUTS]\n"
            "[QUESTIONS_FOR_USER]\n"
            "- none\n"
            "[/QUESTIONS_FOR_USER]\n"
            "[REQUIREMENTS_SUMMARY]\n"
            "- Intent / mode: Runtime availability verification for the requested analysis.\n"
            "- Runtime policy: The current user/benchmark instruction requires missing runtime environments/dependencies to be treated as blocked instead of installing them.\n"
            f"- Failed Consultant check: `{str(code or '').strip()[:300]}`\n"
            f"- Evidence: {evidence[:800]}\n"
            f"- Selected skill rationale: {skill_marker} matched the requested analysis context; no installation should be delegated under this task policy.\n"
            "[/REQUIREMENTS_SUMMARY]"
        )
        runtime_audits, runtime_summary = self._consultant_runtime_preflight(runtime_skills, task_id)
        if runtime_summary:
            summary = (summary.rstrip() + "\n\n" + runtime_summary).strip()
        return {
            "messages": [AIMessage(content="FINISH\n" + summary)],
            "selected_skills": runtime_skills,
            "selected_skills_raw": list(raw_selected),
            "skill_selection_source": "consultant_runtime_block",
            "requirements_summary": summary,
            "runtime_status": self._runtime_status_for_task(task_id),
            "runtime_audit": runtime_audits,
            "next_step": "planner",
        }

    def _consultant_skill_only_fallback_handoff(
        self,
        consultant_context: str,
        fallback_raw: List[str],
        task_id: str,
        consultant_memory_retrieval_context: str,
        consultant_wiki_context: str,
        input_resolution_context: str = "",
        max_skills: int = 1,
    ) -> Dict[str, Any]:
        """Hand off canonical skills to Planner when no valid workflow_state is available."""
        if not self.skill_manager:
            return {}
        raw_selected = list(dict.fromkeys([sid for sid in (fallback_raw or []) if str(sid).strip()]))
        if not raw_selected:
            raw_selected = self.skill_manager.match_skills(consultant_context, max_results=max_skills)
        discovery_hints: Dict[str, Any] = {}
        if not raw_selected:
            discovery_hints = self._controlled_runtime_discovery_hints(consultant_context, task_id)
            raw_selected = list(discovery_hints.get("candidate_skills") or [])[:max_skills]
        selected = self.skill_manager.normalize_selected_skills(raw_selected, max_results=max_skills)
        if not selected:
            return {}

        audit = input_resolution_context.strip()
        summary_parts = [
            f"[SELECTED_SKILLS: {', '.join(selected)}]",
            "",
            "[CONSULTANT_SKILL_ONLY_FALLBACK]",
            "- Consultant could not produce a valid workflow_state after repeated internal repair attempts.",
            "- No workflow_state is being constructed from this fallback; Planner must decompose the task from the selected canonical skills.",
            "- Planner must assign selected skills through each step's `Skills:` field and ask the user if required inputs or parameters remain unresolved.",
            f"- raw_skill_hints: {', '.join(raw_selected) if raw_selected else 'None'}",
            "[/CONSULTANT_SKILL_ONLY_FALLBACK]",
        ]
        if audit:
            summary_parts.extend(["", audit])
        runtime_audits, runtime_summary = self._consultant_runtime_preflight(selected, task_id)
        if discovery_hints:
            summary_parts.extend([
                "",
                "[CONTROLLED_DISCOVERY_HINTS]",
                f"- candidate_skills: {', '.join(discovery_hints.get('candidate_skills', []) or []) or 'none'}",
                f"- candidate_envs: {', '.join(discovery_hints.get('candidate_envs', []) or []) or 'none'}",
                f"- candidate_tools: {', '.join(discovery_hints.get('candidate_tools', []) or []) or 'none'}",
                "- evidence_only: true; readiness still requires PackageManager evidence plus a passing healthcheck.",
                "[/CONTROLLED_DISCOVERY_HINTS]",
            ])
        if runtime_summary:
            summary_parts.extend(["", runtime_summary])
        summary = "\n".join(summary_parts).strip()
        return {
            "messages": [AIMessage(content="FINISH\n" + summary)],
            "selected_skills": selected,
            "selected_skills_raw": raw_selected,
            "active_skills": [],
            "future_skills": [],
            "required_skills_all": list(selected),
            "candidate_skills": [],
            "workflow_id": "",
            "workflow_stages": [],
            "workflow_state": {},
            "workflow_handoff": {},
            "active_batch_id": "",
            "runtime_status": self._runtime_status_for_task(task_id),
            "runtime_audit": runtime_audits,
            "skill_selection_source": "consultant_skill_only_fallback",
            "requirements_summary": summary,
            "memory_packet_context": self._consultant_handoff_memory_packet(summary, consultant_memory_retrieval_context),
            "wiki_memory_context": consultant_wiki_context,
            "next_step": "planner",
        }

    @staticmethod
    def _workflow_cue_present(text: str, cue: str) -> bool:
        haystack = str(text or "")
        needle = str(cue or "").strip()
        if not needle:
            return False
        if needle.startswith("re:"):
            pattern = needle[3:].strip()
            return bool(pattern and re.search(pattern, haystack, re.IGNORECASE))
        return needle.lower() in haystack.lower()

    def _workflow_exclusion_reason(self, workflow: Dict[str, Any], query_text: str) -> str:
        """Apply workflow-declared exclusion cues without embedding domain rules in core."""
        if not self.workflow_manager:
            return ""
        rules = workflow.get("exclude_when") or {}
        if not isinstance(rules, dict):
            return ""

        for cue in self.workflow_manager._ensure_list(rules.get("any")):
            if self._workflow_cue_present(query_text, str(cue)):
                return f"workflow exclude_when.any matched: {cue}"

        all_cues = [str(cue) for cue in self.workflow_manager._ensure_list(rules.get("all")) if str(cue).strip()]
        if all_cues and all(self._workflow_cue_present(query_text, cue) for cue in all_cues):
            return "workflow exclude_when.all matched: " + ", ".join(all_cues)

        groups = rules.get("all_groups") or []
        if isinstance(groups, (str, bytes)):
            groups = [[str(groups)]]
        for group in groups:
            cues = [str(cue) for cue in self.workflow_manager._ensure_list(group) if str(cue).strip()]
            if cues and all(self._workflow_cue_present(query_text, cue) for cue in cues):
                return "workflow exclude_when.all_groups matched: " + ", ".join(cues)
        return ""

    def _workflow_candidate_allowed(self, workflow: Dict[str, Any], query_text: str) -> bool:
        return not self._workflow_exclusion_reason(workflow, query_text)

    def _explicit_skill_ids_from_text(self, text: str, max_results: Optional[int] = None) -> List[str]:
        if not self.skill_manager:
            return []
        candidates = re.findall(r"\b(?:[A-Za-z0-9_.-]+/){1,4}[A-Za-z0-9_.-]+\b", str(text or ""))
        normalized: List[str] = []
        for candidate in candidates:
            for skill_id in self.skill_manager.normalize_selected_skills([candidate], max_results=1):
                if skill_id not in normalized:
                    normalized.append(skill_id)
        limit = max_results or SkillManager.MAX_SELECTED_SKILLS
        return normalized[:limit]

    def _state_update_for_user_skill_correction(
        self,
        user_answer: str,
        state: AgentState,
        task_id: str,
    ) -> Dict[str, Any]:
        explicit_skills = self._explicit_skill_ids_from_text(user_answer)
        if not explicit_skills:
            return {}
        current_skills = self._selected_skill_ids(state.get("selected_skills", []))
        if explicit_skills == current_skills:
            return {}
        update = {
            "selected_skills": explicit_skills,
            "selected_skills_raw": list(explicit_skills),
            "active_skills": [],
            "future_skills": [],
            "required_skills_all": list(explicit_skills),
            "candidate_skills": [],
            "workflow_handoff": {},
            "workflow_stages": [],
            "workflow_id": "",
            "active_batch_id": "",
            "skill_selection_source": "explicit_user_skill_correction",
        }
        selected = update.get("selected_skills") or explicit_skills
        runtime_audits, runtime_summary = self._consultant_runtime_preflight(selected, task_id)
        update["runtime_status"] = self._runtime_status_for_task(task_id, selected)
        update["runtime_audit"] = runtime_audits
        update["current_plan"] = ""
        update["requirements_summary"] = (
            "[USER_SKILL_CORRECTION]\n"
            f"- User explicitly selected skill(s): {', '.join(selected)}.\n"
            "- Discard any previous incompatible workflow activation and let Planner map these skills to steps.\n"
            "[/USER_SKILL_CORRECTION]"
        )
        if runtime_summary:
            update["requirements_summary"] += "\n\n" + runtime_summary
        update["next_step"] = "planner"
        return update

    @staticmethod
    def _runtime_install_target_from_error(text: str, fallback_name: str) -> Tuple[str, str]:
        lower = str(text or "").lower()
        if re.search(r"rscript:\s*command not found|no rscript|which:\s*no rscript", lower, re.IGNORECASE):
            return "r_base_runtime", "Rscript is missing; install/register r-base before attempting CALDER."
        if re.search(r"there is no package called .{0,4}calder|calder2?\s+not available", lower, re.IGNORECASE):
            return "calder_r_runtime", "CALDER R package is missing; install it only after Rscript passes."
        if re.search(r"\b(?:makeblastdb|blastp|blastn|blastdbcmd)\b", lower, re.IGNORECASE):
            return fallback_name, "missing_tools=[makeblastdb, blastp, blastn, blastdbcmd]; suggested_package=blast"
        return fallback_name, ""

    @staticmethod
    def _explicit_runtime_repair_kind(tool_name: str, message: str = "") -> str:
        text = f"{tool_name}\n{message}".lower()
        if re.search(r"\br_base_runtime\b|rscript:\s*command not found|which:\s*no rscript|\bno rscript\b", text, re.IGNORECASE):
            return "r_base_runtime"
        if re.search(r"\bcalder_r_runtime\b|there is no package called .{0,4}calder|calder2?\s+not available", text, re.IGNORECASE):
            return "calder_r_runtime"
        return ""

    @staticmethod
    def _runtime_repair_context(kind: str, target_env: str) -> str:
        if kind == "r_base_runtime":
            return (
                "\n\n[RUNTIME REPAIR TARGET]\n"
                f"- target_env: {target_env}\n"
                "- missing dependency: Rscript / r-base\n"
                "- Repair this exact conda environment; do not switch to another environment that happens to have Rscript.\n"
                "- Install/register r-base first. Do not run CALDER installation until these checks pass:\n"
                f"  1. conda run -n {target_env} which Rscript\n"
                f"  2. conda run -n {target_env} Rscript --version\n"
            )
        if kind == "calder_r_runtime":
            return (
                "\n\n[RUNTIME REPAIR TARGET]\n"
                f"- target_env: {target_env}\n"
                "- missing dependency: CALDER R package and/or data.table\n"
                "- First verify Rscript in this exact env. If Rscript is missing, install/register r-base before CALDER.\n"
                "- Do not use absolute Rscript paths from other conda environments.\n"
                "- If the selected skill provides an install_command, prefer that command over package-name inference.\n"
                "- Installation succeeds only after this check passes:\n"
                f"  conda run -n {target_env} Rscript -e "
                "\"suppressPackageStartupMessages(library(CALDER)); "
                "suppressPackageStartupMessages(library(data.table)); cat('OK\\n')\"\n"
            )
        return ""

    @staticmethod
    def _normalize_install_command_for_contract(command: str) -> str:
        text = str(command or "").replace("\\\n", " ").strip()
        text = re.sub(r"\s+", " ", text)
        return text.rstrip(";")

    @staticmethod
    def _adapt_install_command_env(command: str, target_env: str) -> str:
        normalized = BioAnalystAgent._normalize_install_command_for_contract(command)
        if not target_env:
            return normalized
        return re.sub(
            r"((?:^|\s)(?:-n|--name)\s+)([A-Za-z0-9_.-]+)",
            lambda match: f"{match.group(1)}{target_env}",
            normalized,
            count=1,
        ).strip()

    @staticmethod
    def _strict_runtime_contract_commands(runtime_defaults: Dict[str, Any], target_env: str) -> List[str]:
        commands: List[str] = []
        raw_commands: List[str] = []
        raw_commands.extend(runtime_defaults.get("install_commands", []) or [])
        raw_commands.extend(runtime_defaults.get("install_fallbacks", []) or [])
        install_command = str(runtime_defaults.get("install_command") or "").strip()
        if install_command and install_command not in raw_commands:
            raw_commands.insert(0, install_command)

        for command in raw_commands:
            adapted = BioAnalystAgent._adapt_install_command_env(command, target_env)
            commands.append(adapted)

        env_file = str(runtime_defaults.get("env_file") or "").strip()
        if env_file:
            commands.extend([
                f"conda env create -n {target_env} -f {env_file}",
                f"conda env update -n {target_env} -f {env_file}",
            ])
        return list(dict.fromkeys(
            BioAnalystAgent._normalize_install_command_for_contract(command)
            for command in commands
            if BioAnalystAgent._normalize_install_command_for_contract(command)
        ))

    @staticmethod
    def _selected_skill_install_command_context(runtime_defaults: Dict[str, Any], target_env: str) -> str:
        allowed_commands = BioAnalystAgent._strict_runtime_contract_commands(runtime_defaults, target_env)
        install_command = str(runtime_defaults.get("install_command") or "").strip()
        env_file = str(runtime_defaults.get("env_file") or "").strip()
        install_policy = str(runtime_defaults.get("install_policy") or "").strip()
        if not install_command and not allowed_commands and not env_file and not install_policy:
            return ""
        selected = ", ".join(map(str, runtime_defaults.get("selected", []) or [])) or "None"
        healthcheck = str(runtime_defaults.get("healthcheck") or "").strip()
        if healthcheck:
            healthcheck = BioAnalystAgent._adapt_install_command_env(healthcheck, target_env)
        packages = ", ".join(map(str, runtime_defaults.get("packages", []) or []))
        capabilities = ", ".join(map(str, runtime_defaults.get("capabilities", []) or []))
        effective_policy = install_policy or "prefer-declared"
        strict_policy = effective_policy.lower() in {"declared-only", "strict", "locked"}
        lines = [
            "",
            "[SELECTED SKILL INSTALL COMMAND]",
            f"- selected_skills: {selected}",
            f"- target_env: {target_env}",
            f"- language: {runtime_defaults.get('language', '') or ''}",
            f"- packages: {packages}",
            f"- capabilities: {capabilities}",
            f"- install_policy: {effective_policy}",
            f"- install_command: {install_command}",
            "- After installation, verify the selected skill runtime before registering success.",
        ]
        if env_file:
            lines.append(f"- env_file: {env_file}")
        if allowed_commands:
            if strict_policy:
                lines.extend([
                    "[STRICT SKILL RUNTIME CONTRACT]",
                    "- install_policy: declared-only",
                    "- You may execute only these installation commands for this selected skill runtime.",
                    "- Do not infer package names, switch to another environment, downgrade R/Python, or use CRAN/GitHub/pip fallback unless the exact command is listed here.",
                    "- If all listed commands fail, stop and report the failing command and stderr; do not fabricate a new fallback.",
                ])
            else:
                lines.extend([
                    "[DECLARED SKILL INSTALL PLAN]",
                    "- install_policy: prefer-declared",
                    "- Try the declared install command(s) first.",
                    "- If a declared command fails and the observed error clearly identifies a missing package, a generic repair is allowed, but it must stay in target_env and must pass the skill healthcheck before registration.",
                ])
            for command in allowed_commands:
                prefix = "allowed_install_command" if strict_policy else "declared_install_command"
                lines.append(f"- {prefix}: {command}")
            if strict_policy:
                lines.append("[/STRICT SKILL RUNTIME CONTRACT]")
            else:
                lines.append("[/DECLARED SKILL INSTALL PLAN]")
        if healthcheck:
            lines.append(f"- healthcheck: {healthcheck}")
        return "\n" + "\n".join(lines) + "\n"

    def _runtime_install_redirect_from_execution(
        self,
        code: str,
        stdout: str,
        stderr: str,
        selected_skills: Optional[List[str]],
        task_id: str,
    ) -> Tuple[str, List[str]]:
        combined = "\n".join(part for part in [code, stdout, stderr] if part)
        if not self._looks_like_missing_runtime_dependency(combined):
            return "", list(dict.fromkeys(self._selected_skill_ids(selected_skills)))

        runtime_skills = self._infer_runtime_skills_from_code(combined, selected_skills)
        if not runtime_skills:
            return "", []

        for sid in runtime_skills:
            requirements = self._skill_runtime_requirements(sid)
            env = requirements.get("env") or requirements.get("default_env", "")
            if not env:
                continue
            defaults = self._skill_runtime_defaults([sid])
            redirect_name = self._install_tool_name(requirements.get("install_tool")) or defaults.get("tool_name", "skill_runtime")
            install_command = requirements.get("install_command", "")
            reason = stderr.strip() or stdout.strip() or f"Runtime dependency check failed for {sid}."
            redirect_name, ordered_hint = self._runtime_install_target_from_error(reason, redirect_name)
            missing_tools = self._missing_executables_from_error(reason)
            if redirect_name == "calder_r_runtime" and not self._calder_runtime_allowed([sid], env):
                redirect_name = self._install_tool_name(requirements.get("install_tool")) or defaults.get("tool_name", "skill_runtime")
                ordered_hint = "Ignoring CALDER repair for non-CALDER2 skill; ordinary A/B compartments use PC1/eigenvector runtime."
            self._cache_runtime_status(
                task_id,
                sid,
                env,
                False,
                reason[:1000],
                requirements.get("healthcheck", ""),
                requirements.get("env_runner", ""),
                mode="execution",
            )
            comment_parts = [reason[:500]]
            if missing_tools:
                comment_parts.append(f"failure_class=missing_executable; missing_tools=[{', '.join(missing_tools)}]")
            if ordered_hint:
                comment_parts.append(ordered_hint)
            if install_command:
                comment_parts.append(f"suggested install: {install_command}")
            if requirements.get("healthcheck"):
                comment_parts.append(f"healthcheck: {requirements.get('healthcheck')}")
            return f'{self._install_tool_tag(redirect_name, env)}<!-- {"; ".join(comment_parts)} -->', runtime_skills
        return "", runtime_skills

    def _selected_skill_ids(self, state_selected: Optional[List[str]] = None) -> List[str]:
        if not state_selected:
            return []
        return list(state_selected)

    def _executable_runtime_skills(self, selected_skills: Optional[List[str]] = None) -> List[str]:
        selected = self._selected_skill_ids(selected_skills)
        if not self.skill_manager or not hasattr(self.skill_manager, "is_executable_runtime_skill"):
            return selected
        executable: List[str] = []
        for sid in selected:
            if self.skill_manager.is_executable_runtime_skill(sid) and sid not in executable:
                executable.append(sid)
        return executable

    def _skill_runtime_defaults(self, selected_skills: Optional[List[str]] = None) -> Dict[str, Any]:
        selected = self._selected_skill_ids(selected_skills)
        if not selected or not self.skill_manager:
            return {
                "selected": [],
                "env": "",
                "packages": [],
                "language": "",
                "capabilities": [],
                "healthcheck": "",
                "install_command": "",
                "install_commands": [],
                "install_fallbacks": [],
                "install_tool": "",
                "install_policy": "",
                "env_file": "",
                "tool_name": "skill_runtime",
            }
        sid = selected[0]
        requirements = self.skill_manager.get_runtime_requirements(sid)
        env = requirements.get("env") or requirements.get("default_env", "")
        packages = list(requirements.get("packages", []) or [])
        capabilities = list(requirements.get("capabilities", []) or [])
        language = requirements.get("language", "")
        safe_skill = re.sub(r"[^A-Za-z0-9_]+", "_", sid).strip("_").lower() or "skill"
        tool_name = f"{safe_skill}_runtime"
        if packages:
            tool_name = f"{safe_skill}_{language or 'deps'}"
        return {
            "selected": selected,
            "env": env,
            "packages": packages,
            "language": language,
            "capabilities": capabilities,
            "healthcheck": requirements.get("healthcheck", ""),
            "install_command": requirements.get("install_command", ""),
            "install_commands": requirements.get("install_commands", []),
            "install_fallbacks": requirements.get("install_fallbacks", []),
            "install_tool": self._install_tool_name(requirements.get("install_tool", "")),
            "install_policy": requirements.get("install_policy", ""),
            "env_file": requirements.get("env_file", ""),
            "tool_name": tool_name,
        }

    def _skill_packages(self, skill_id: str) -> List[str]:
        if not self.skill_manager:
            return []
        return list(self.skill_manager.get_runtime_requirements(skill_id).get("packages", []) or [])

    def _skill_runtime_requirements(self, skill_id: str) -> Dict[str, Any]:
        if not self.skill_manager:
            return {}
        return self.skill_manager.get_runtime_requirements(skill_id)

    def _calder_runtime_allowed(self, selected_skills: Optional[List[str]] = None, env: str = "") -> bool:
        selected = self._selected_skill_ids(selected_skills)
        if any("calder2-subcompartment" in sid or "calder2-compartment" in sid for sid in selected):
            return True
        if self.skill_manager:
            for sid in selected:
                requirements = self._skill_runtime_requirements(sid)
                if self._install_tool_name(requirements.get("install_tool") or "") == "calder_r_runtime":
                    return True
        return str(env or "").strip() == "calder2" and not selected

    def _sanitize_install_tool_for_selected_runtime(
        self,
        tool_name: str,
        selected_skills: Optional[List[str]],
        runtime_defaults: Dict[str, Any],
        requested_env: str = "",
    ) -> Tuple[str, str]:
        requested_tool = self._install_tool_name(tool_name)
        selected = self._selected_skill_ids(selected_skills)
        declared_tool = self._install_tool_name(runtime_defaults.get("install_tool") or runtime_defaults.get("tool_name") or "")
        if selected and declared_tool and requested_tool and requested_tool != declared_tool:
            if requested_tool == "calder_r_runtime" and self._calder_runtime_allowed(selected_skills, requested_env):
                return requested_tool, ""
            note = (
                "[INSTALL TOOL CORRECTION]\n"
                f"- Generated install tag requested `{requested_tool}`, but the selected skill runtime declares `{declared_tool}`.\n"
                "- Do not install undeclared package names inferred from a failed import unless the selected skill runtime contract lists them.\n"
                f"- Replaced install tool with `{declared_tool}`.\n"
            )
            return declared_tool, note
        if requested_tool != "calder_r_runtime":
            return requested_tool or tool_name, ""
        if self._calder_runtime_allowed(selected_skills, requested_env):
            return requested_tool, ""
        replacement = self._install_tool_name(runtime_defaults.get("install_tool") or runtime_defaults.get("tool_name") or "skill_runtime")
        note = (
            "[INSTALL TOOL CORRECTION]\n"
            "- Generated install tag requested `calder_r_runtime`, but the selected skill is not CALDER2.\n"
            "- Ordinary/default A/B compartment calling must use PC1/eigenvector runtime, not CALDER2/Rscript.\n"
            f"- Replaced install tool with `{replacement}`.\n"
        )
        return replacement, note

    def _runtime_owner_skill_for_install(self, tool_name: str, selected_skills: Optional[List[str]]) -> str:
        """Pick the selected skill whose runtime declares the requested install tool."""
        if not self.skill_manager:
            return ""
        normalized = self._selected_skill_ids(selected_skills)
        requested_tool = self._install_tool_name(tool_name)
        for sid in normalized:
            if (
                hasattr(self.skill_manager, "is_workflow_router_skill")
                and self.skill_manager.is_workflow_router_skill(sid)
            ):
                continue
            requirements = self._skill_runtime_requirements(sid)
            if requested_tool and requested_tool == self._install_tool_name(requirements.get("install_tool") or ""):
                return sid
            defaults = self._skill_runtime_defaults([sid])
            if requested_tool and requested_tool == str(defaults.get("tool_name") or "").strip():
                return sid
        if requested_tool == "calder_r_runtime":
            for sid in normalized:
                if "calder2-subcompartment" in sid or "calder2-compartment" in sid:
                    return sid
        return ""

    def _skill_static_default_env(self, skill_id: str) -> str:
        if not self.skill_manager:
            return ""
        meta = self.skill_manager.get_skill_meta(skill_id)
        runtime = meta.get("runtime") or {}
        return str(runtime.get("default_env", "") or "")

    def _record_installed_skill_environment(self, selected_skills: Optional[List[str]], env: str, final_res: str):
        if not self.skill_manager or not env:
            return
        if not re.search(r"(success|complete|ready|✅)", final_res, re.IGNORECASE):
            return
        for sid in self._selected_skill_ids(selected_skills):
            requirements = self._skill_runtime_requirements(sid)
            packages = requirements.get("packages", [])
            capabilities = requirements.get("capabilities", [])
            install_command = requirements.get("install_command", "")
            self.skill_manager.record_environment(
                sid,
                env=env,
                packages=packages,
                install_command=install_command,
                status="installed",
                notes=final_res,
                language=requirements.get("language", ""),
                capabilities=capabilities,
                healthcheck=requirements.get("healthcheck", ""),
            )
            if self.pm:
                self.pm.register_environment(
                    env_name=env,
                    purpose=f"Runtime for skill {sid}",
                    packages=packages,
                    skill_id=sid,
                    notes=final_res,
                    language=requirements.get("language", ""),
                    capabilities=capabilities,
                    healthcheck=requirements.get("healthcheck", ""),
                    status="installed",
                )

    def _ensure_skill_runtime_ready(self, selected_skills: Optional[List[str]], task_id: str = "default", code: str = "") -> tuple:
        if not self.skill_manager or not self.pm:
            return True, ""
        for sid in self._selected_skill_ids(selected_skills):
            requirements = self._skill_runtime_requirements(sid)
            env = requirements.get("env") or requirements.get("default_env", "")
            if not env:
                continue
            if not any([requirements.get("packages"), requirements.get("capabilities"), requirements.get("language"), requirements.get("healthcheck")]):
                continue
            ok, detail, env = self._verify_skill_runtime(sid, task_id=task_id, code=code)
            if not ok:
                redirect_name = self._install_tool_name(requirements.get("install_tool")) or self._skill_runtime_defaults([sid]).get("tool_name", "skill_runtime")
                reason = detail or f"Healthcheck failed for environment {env}"
                redirect_name, ordered_hint = self._runtime_install_target_from_error(reason, redirect_name)
                if redirect_name == "calder_r_runtime" and not self._calder_runtime_allowed([sid], env):
                    redirect_name = self._install_tool_name(requirements.get("install_tool")) or self._skill_runtime_defaults([sid]).get("tool_name", "skill_runtime")
                    ordered_hint = "Ignoring CALDER repair for non-CALDER2 skill; ordinary A/B compartments use PC1/eigenvector runtime."
                if ordered_hint:
                    reason = f"{reason} {ordered_hint}"
                return False, f'{self._install_tool_tag(redirect_name, env)}<!-- {reason} -->'
        return True, ""

    def _record_successful_skill_execution(self, selected_skills: Optional[List[str]], code: str, stdout: str):
        if not self.skill_manager or not selected_skills:
            return
        selected = self._selected_skill_ids(selected_skills)
        if not selected:
            return
        outputs = []
        for match in re.findall(r"[\w./\-\u4e00-\u9fff]+(?:\.png|\.tsv|\.pdf|\.csv)", stdout + "\n" + code):
            outputs.append(match)
        for sid in selected:
            env = self.skill_manager.get_runtime_env(sid)
            script_hit = sid in code
            meta = self.skill_manager.get_skill_meta(sid)
            for script in meta.get("_scripts", []) or []:
                if Path(script).name in code:
                    script_hit = True
                    break
            packages = self._skill_packages(sid)
            import_hits = [
                pkg for pkg in packages
                if re.search(rf"\bimport\b[^\n]*\b{re.escape(pkg)}\b", code, re.IGNORECASE)
            ]
            if not script_hit and packages and len(import_hits) == len(packages):
                requirements = self._skill_runtime_requirements(sid)
                self.skill_manager.record_environment(
                    sid,
                    env=env,
                    packages=packages,
                    status="verified",
                    notes="Dependency preflight import succeeded.",
                    language=requirements.get("language", ""),
                    capabilities=requirements.get("capabilities", []),
                    healthcheck=requirements.get("healthcheck", ""),
                )
                if self.pm and env:
                    self.pm.register_environment(
                        env_name=env,
                        purpose=f"Runtime for skill {sid}",
                        packages=packages,
                        skill_id=sid,
                        notes="Dependency preflight import succeeded.",
                        language=requirements.get("language", ""),
                        capabilities=requirements.get("capabilities", []),
                        healthcheck=requirements.get("healthcheck", ""),
                        status="verified",
                    )
                continue
            if script_hit:
                self.skill_manager.record_successful_run(
                    sid,
                    env=env,
                    command=code,
                    outputs=list(dict.fromkeys(outputs)),
                    notes="Skill command completed with exit code 0.",
                )

    def _install_choice_key(self, task_id: str, tool_name: str) -> str:
        return f"{task_id}::{tool_name}"

    @staticmethod
    def _solution_from_messages(messages: Any) -> str:
        for message in reversed(list(messages or [])):
            content = str(getattr(message, "content", message))
            match = re.search(r"<solution>(.*?)</solution>", content, re.DOTALL | re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return ""

    def _record_profiler_state_update(
        self,
        task_id: str,
        node_name: str,
        state: AgentState,
        update: Any,
    ) -> None:
        if not isinstance(update, dict):
            return
        merged: Dict[str, Any] = dict(state or {})
        for key, value in update.items():
            if key != "messages":
                merged[key] = value

        try:
            durable_plan = load_plan_state(self.data_path, task_id)
            _, current_step = current_plan_step_with_index(durable_plan)
            execution = merged.get("execution_evidence") or {}
            latest_execution = {}
            if isinstance(execution, dict) and execution:
                latest_execution = {
                    key: execution.get(key)
                    for key in (
                        "execution_id", "action_id", "command", "argv", "script_file",
                        "cwd", "returncode", "signal", "language", "trace_file",
                        "plan_version", "task_revision", "step_id", "started_at",
                        "finished_at", "stdout_log", "stderr_log", "declared_outputs",
                        "observed_outputs",
                    )
                    if execution.get(key) not in (None, "", [], {})
                }
            step_states = {
                str(step.get("step_id") or ""): str(step.get("status") or "")
                for step in durable_plan.get("steps", []) or []
                if isinstance(step, dict) and str(step.get("step_id") or "")
            }
            durable_task_state = dict(durable_plan.get("task_state") or {})
            durable_budgets = dict(durable_task_state.get("budgets") or {})
            durable_budgets.update({
                "retry_count": int(merged.get("retry_count") or 0),
                "llm_retry_counts": dict(merged.get("llm_retry_counts") or {}),
                "context": merged.get("context_budget_report") or {},
            })
            recovery_projection = dict(durable_task_state.get("recovery_state") or {})
            if any(key in update for key in (
                "recovery_phase", "recovery_status", "recovery_decision",
                "pending_recovery_action", "recovery_context",
            )):
                recovery_projection = {
                    "phase": merged.get("recovery_phase"),
                    "status": merged.get("recovery_status"),
                    "decision": merged.get("recovery_decision") or {},
                    "pending_action": merged.get("pending_recovery_action") or {},
                    "failure_owner": str(merged.get("failure_owner") or ""),
                    "failure_fingerprint": (merged.get("recovery_context") or {}).get("error_fingerprint")
                    if isinstance(merged.get("recovery_context"), dict) else "",
                    "step_id": str(current_step.get("step_id") or ""),
                    "plan_version": str(durable_plan.get("plan_version") or ""),
                }
            projected_task_status = task_status(durable_plan)
            projected_next = str(update.get("next_step") or "")
            projected_phase = str(recovery_projection.get("phase") or "").lower()
            if projected_task_status in TASK_TERMINAL_STATUSES:
                pass
            elif projected_next in {"ask_user", "human_input", "waiting_user"}:
                projected_task_status = TASK_STATUS_WAITING_USER
            elif node_name == "ask_user" and projected_next in {"planner", "generate", "consultant"}:
                projected_task_status = TASK_STATUS_RUNNING
            elif projected_phase in {"capture", "unwrap", "diagnose", "probe", "verify"}:
                projected_task_status = TASK_STATUS_DIAGNOSING
            elif projected_phase in {"plan_repair", "apply_repair", "rerun", "rollback"}:
                projected_task_status = TASK_STATUS_REPAIRING
            update_task_state_projection(
                self.data_path,
                task_id,
                {
                    "task_spec": merged.get("task_spec") or durable_plan.get("task_spec") or {},
                    "status": projected_task_status,
                    "user_constraints": list(merged.get("user_constraints") or []),
                    "input_audit": merged.get("input_audit") or {},
                    "selected_skills": list(merged.get("selected_skills") or []),
                    "runtime_audit": list(merged.get("runtime_audit") or []),
                    "step_states": step_states,
                    "current_step_id": str(current_step.get("step_id") or ""),
                    "latest_execution": latest_execution,
                    "diagnosis_state": merged.get("diagnostic_summary") or {},
                    "recovery_state": recovery_projection,
                    "supervisor_state": {
                        "verdict": merged.get("supervisor_verdict"),
                        "feedback": merged.get("last_supervisor_feedback") or {},
                    },
                    "pending_question": merged.get("pending_human_question") or merged.get("pending_question") or {},
                    "budgets": durable_budgets,
                    "last_transition": {
                        "from_node": node_name,
                        "to_node": str(update.get("next_step") or ""),
                        "task_status": task_status(durable_plan),
                        "step_id": str(current_step.get("step_id") or ""),
                        "trigger": str(update.get("node_recommended_next_step") or update.get("next_step") or ""),
                        "guard": str(update.get("transition_guard") or "allowed"),
                    },
                },
                node=node_name,
                next_step=str(update.get("next_step") or ""),
            )
            transition_payload = {
                "from_node": node_name,
                "to_node": str(update.get("next_step") or ""),
                "task_status": projected_task_status,
                "step_id": str(current_step.get("step_id") or ""),
                "plan_id": str(durable_plan.get("plan_id") or ""),
                "plan_revision": durable_plan.get("plan_revision"),
                "trigger": str(update.get("node_recommended_next_step") or update.get("next_step") or ""),
                "guard": str(update.get("transition_guard") or "allowed"),
            }
            ContextLineageStore(self.data_path, task_id).record_event(
                "controller_route",
                transition_payload,
                event_id=(
                    f"route-{node_name}-{durable_task_state.get('state_version', 0)}-"
                    f"{hashlib.sha256(json.dumps(transition_payload, sort_keys=True, default=str).encode('utf-8')).hexdigest()[:12]}"
                ),
            )
        except Exception as exc:
            logger.warning("Could not persist compact task state after %s: %s", node_name, exc)

        selected = merged.get("selected_skills", []) or []
        active = merged.get("active_skills", []) or []
        future = merged.get("future_skills", []) or []
        candidate = merged.get("candidate_skills", []) or []
        raw = merged.get("selected_skills_raw", []) or []
        handoff = self._workflow_state_from_mapping(merged)
        workflow_id = merged.get("workflow_id") or handoff.get("workflow_id", "")
        active_batch_id = merged.get("active_batch_id") or handoff.get("active_batch_id", "")
        source = merged.get("skill_selection_source", "")
        if any([selected, active, future, candidate, raw, source, workflow_id, active_batch_id]):
            self.profiler.record_skill_selection(
                task_id,
                selected_skills=selected,
                selected_skills_raw=raw,
                active_skills=active,
                future_skills=future,
                candidate_skills=candidate,
                source=str(source or ""),
                workflow_id=str(workflow_id or ""),
                active_batch_id=str(active_batch_id or ""),
                node=node_name,
            )

        if node_name == "supervisor":
            self.profiler.record_supervisor_verdict(
                task_id,
                update.get("supervisor_verdict"),
                retry_count=update.get("retry_count"),
                node=node_name,
            )

        if str(update.get("next_step") or "").strip() == "finish":
            solution = self._solution_from_messages(update.get("messages", []))
            if solution:
                status = "failed" if any(marker in solution for marker in ["❌", "Failed", "failed", "Error"]) else "completed"
                self.profiler.record_final_result(
                    task_id,
                    status=status,
                    output_result=solution,
                    node=node_name,
                )

    @staticmethod
    def _workflow_state_from_mapping(mapping: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(mapping, dict):
            return {}
        workflow_state = mapping.get("workflow_state")
        if isinstance(workflow_state, dict) and workflow_state:
            return dict(workflow_state)
        workflow_handoff = mapping.get("workflow_handoff")
        if isinstance(workflow_handoff, dict) and workflow_handoff:
            return dict(workflow_handoff)
        return {}

    def _normalize_state_update_aliases(
        self,
        node_name: str,
        state: AgentState,
        update: Any,
    ) -> Any:
        if not isinstance(update, dict):
            return update
        normalized = dict(update)
        task_spec = self._task_spec_for_state(
            {**dict(state), **normalized},
            list(state.get("messages", []) or []),
        )
        input_audit = normalized.get("input_audit") or state.get("input_audit") or {}
        if isinstance(input_audit, dict) and input_audit:
            task_spec = merge_task_spec_input_audit(task_spec, input_audit)
        normalized["task_spec"] = task_spec
        normalized.setdefault("task_revision", task_spec_revision(task_spec))
        effective_facts = resolve_effective_user_facts(task_spec)
        normalized.setdefault("effective_user_facts_revision", effective_facts["effective_facts_revision"])
        if node_name == "consultant":
            normalized.setdefault("normalized_fact_mappings", {
                key: effective_facts[key]
                for key in (
                    "active_artifacts", "active_entities", "active_parameters",
                    "active_constraints", "active_decisions", "active_relations",
                    "unresolved_facts",
                )
            })
            normalized.setdefault("user_source_refs", effective_facts["source_refs"])
        constraints = list(state.get("user_constraints", []) or [])
        for constraint in task_spec_constraints(task_spec):
            if constraint not in constraints:
                constraints.append(constraint)
        normalized.setdefault("user_constraints", constraints)
        if str(normalized.get("next_step") or "").strip():
            normalized["node_recommended_next_step"] = str(normalized.get("next_step") or "").strip()
        workflow_state = self._workflow_state_from_mapping(normalized) or self._workflow_state_from_mapping(state)
        if workflow_state:
            normalized.setdefault("workflow_state", workflow_state)
            normalized.setdefault("workflow_handoff", workflow_state)
        if node_name in {"execute", "install_subagent"}:
            normalized.setdefault("pending_action", {})
        task_id = str(normalized.get("task_id") or state.get("task_id") or "")
        trace_refs = [
            *list(state.get("llm_call_refs") or []),
            *list(getattr(self, "_latest_llm_call_refs", {}).get(task_id, []) or []),
        ]
        trace_refs = list({str(item.get("call_id") or index): item for index, item in enumerate(trace_refs) if isinstance(item, dict)}.values())
        if trace_refs:
            normalized["llm_call_refs"] = trace_refs[-50:]
            normalized.setdefault(
                "context_lineage_ref",
                str(Path(self.data_path) / "task_outputs" / task_id / "context_trace"),
            )
            if node_name == "consultant":
                latest = next(
                    (item for item in reversed(trace_refs) if item.get("call_kind") == "consultant"),
                    {},
                )
                if latest.get("call_id"):
                    try:
                        ContextLineageStore(self.data_path, task_id).annotate_call(
                            str(latest["call_id"]),
                            {
                                "candidate_skill_ids": list(
                                    normalized.get("candidate_skills")
                                    or state.get("candidate_skills")
                                    or []
                                ),
                                "selected_skill_ids": list(normalized.get("selected_skills") or []),
                                "consult_handoff": self._workflow_state_from_mapping(normalized),
                                "runtime_audit": list(normalized.get("runtime_audit") or []),
                            },
                        )
                    except Exception as exc:  # noqa: BLE001 - tracing cannot change task routing.
                        logger.warning("Could not annotate Consultant lineage snapshot: %s", exc)
        return normalized

    @staticmethod
    def _extract_json_object(text: str) -> Dict[str, Any]:
        raw = str(text or "").strip()
        if not raw:
            return {}
        candidates = [raw]
        fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
        if fenced:
            candidates.insert(0, fenced.group(1).strip())
        brace = re.search(r"\{.*\}", raw, re.DOTALL)
        if brace:
            candidates.append(brace.group(0))
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except Exception:
                continue
            if isinstance(parsed, dict):
                return parsed
        return {}

    @staticmethod
    def _recent_nonempty_execute_body(messages: Any, window: int = 3) -> str:
        for message in reversed(list(messages or [])[-window:]):
            if not isinstance(message, AIMessage):
                continue
            content = str(getattr(message, "content", "") or "")
            match = re.search(r"<execute>(.*?)</execute>", content, re.DOTALL)
            if match and match.group(1).strip():
                return match.group(1).strip()
        return ""

    @staticmethod
    def _normalize_action_type(action_type: str) -> str:
        value = str(action_type or "").strip().lower().replace("-", "_")
        aliases = {
            "ask": "ask_user",
            "question": "ask_user",
            "install": "install_tool",
            "finish": "solution",
            "final": "solution",
            "answer": "solution",
            "activate": "activate_batch",
            "invoke_renderer": "renderer_invocation",
            "render": "renderer_invocation",
            "step_complete": "step_completion_candidate",
            "step_completion": "step_completion_candidate",
            "patch_plan": "plan_patch",
            "block": "blocked",
        }
        return aliases.get(value, value)

    @classmethod
    def _structured_action_from_object(cls, obj: Dict[str, Any], source: str = "action_json") -> Dict[str, Any]:
        if not isinstance(obj, dict):
            return {}
        action_type = cls._normalize_action_type(
            obj.get("action_type") or obj.get("type") or obj.get("action") or ""
        )
        if not action_type:
            return {}
        action: Dict[str, Any] = {"type": action_type, "source": source}
        if action_type == "renderer_invocation":
            arguments = obj.get("arguments") or obj.get("args") or {}
            if not isinstance(arguments, (dict, list)):
                return {}
            action.update({
                "renderer_id": str(obj.get("renderer_id") or obj.get("renderer") or "").strip(),
                "arguments": arguments,
                "outdir": str(obj.get("outdir") or "").strip(),
            })
            return action
        if action_type == "execute":
            code = str(obj.get("code") or obj.get("body") or obj.get("command") or "").strip()
            if not code:
                return {}
            action.update({"code": code, "language": str(obj.get("language") or "").strip()})
            purpose = str(obj.get("purpose") or "").strip().lower()
            if purpose in {"analysis", "probe", "validation", "record_failure"}:
                action["purpose"] = purpose
            command_role = str(obj.get("command_role") or obj.get("action_kind") or "").strip().lower()
            command_role = {
                "verification": "required_validation",
                "recovery": "recovery",
            }.get(command_role, command_role)
            if command_role in {
                "primary", "diagnostic", "required_validation", "optional_probe", "fallback", "recovery",
                "renderer_invocation",
            }:
                action["command_role"] = command_role
            return action
        if action_type == "ask_user":
            question = str(obj.get("question") or obj.get("body") or obj.get("prompt") or "").strip()
            if not question:
                return {}
            action["question"] = question
            return action
        if action_type == "search":
            query = str(obj.get("query") or obj.get("question") or obj.get("body") or "").strip()
            if not query:
                return {}
            action["query"] = query
            return action
        if action_type == "install_tool":
            tool = str(obj.get("tool") or obj.get("tool_name") or obj.get("body") or "").strip()
            if not tool:
                return {}
            action.update({"tool": tool, "env": str(obj.get("env") or "").strip()})
            return action
        if action_type == "activate_batch":
            batch_id = str(obj.get("batch_id") or obj.get("batch") or "").strip()
            if not batch_id:
                return {}
            action["batch_id"] = batch_id
            return action
        if action_type == "step_completion_candidate":
            step_id = str(obj.get("step_id") or "").strip()
            reason = str(obj.get("reason") or obj.get("body") or "").strip()
            raw_evidence_ids = obj.get("evidence_ids") or obj.get("evidence_id") or []
            if isinstance(raw_evidence_ids, str):
                raw_evidence_ids = [raw_evidence_ids]
            evidence_ids = [
                str(item).strip()
                for item in raw_evidence_ids
                if str(item).strip()
            ]
            if not step_id or not reason or not evidence_ids:
                return {}
            action.update({"step_id": step_id, "reason": reason, "evidence_ids": evidence_ids})
            return action
        if action_type == "plan_patch":
            reason = str(obj.get("reason") or obj.get("body") or "").strip()
            if not reason:
                return {}
            action.update({
                "reason": reason,
                "patch": obj.get("patch") or {},
                "failure_owner": str(obj.get("failure_owner") or "").strip().upper(),
            })
            return action
        if action_type == "blocked":
            reason = str(obj.get("reason") or obj.get("body") or "").strip()
            if not reason:
                return {}
            raw_missing = obj.get("missing_evidence") or obj.get("evidence") or []
            if isinstance(raw_missing, str):
                raw_missing = [raw_missing]
            action.update({
                "reason": reason,
                "missing_evidence": [str(item).strip() for item in raw_missing if str(item).strip()],
            })
            return action
        if action_type == "solution":
            text = str(obj.get("text") or obj.get("answer") or obj.get("body") or "").strip()
            if not text:
                return {}
            action["text"] = text
            return action
        return {}

    @classmethod
    def _extract_structured_action(cls, text: str) -> Dict[str, Any]:
        raw = str(text or "").strip()
        if not raw:
            return {}

        json_candidates: List[str] = []
        for match in re.finditer(r"<action_json>(.*?)</action_json>", raw, re.DOTALL | re.IGNORECASE):
            json_candidates.append(match.group(1).strip())
        fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
        if fenced:
            json_candidates.append(fenced.group(1).strip())
        if raw.startswith("{"):
            json_candidates.append(raw)
        for candidate in json_candidates:
            action = cls._structured_action_from_object(cls._extract_json_object(candidate))
            if action:
                return action

        execute_match = re.search(r"<execute>(.*?)</execute>", raw, re.DOTALL | re.IGNORECASE)
        if execute_match and execute_match.group(1).strip():
            code = execute_match.group(1).strip()
            return {"type": "execute", "code": code, "language": detect_script_language(code), "source": "xml"}
        ask_match = re.search(r"<ask_user>(.*?)</ask_user>", raw, re.DOTALL | re.IGNORECASE)
        if ask_match and ask_match.group(1).strip():
            return {"type": "ask_user", "question": ask_match.group(1).strip(), "source": "xml"}
        search_match = re.search(r"<search>(.*?)</search>", raw, re.DOTALL | re.IGNORECASE)
        if search_match and search_match.group(1).strip():
            return {"type": "search", "query": search_match.group(1).strip(), "source": "xml"}
        install_match = re.search(
            r"<install_tool(?:\s+env=[\"'](.*?)[\"'])?>(.*?)</install_tool>",
            raw,
            re.DOTALL | re.IGNORECASE,
        )
        if install_match and install_match.group(2).strip():
            return {
                "type": "install_tool",
                "env": str(install_match.group(1) or "").strip(),
                "tool": install_match.group(2).strip(),
                "source": "xml",
            }
        activate_match = re.search(
            r"<activate_batch\s+batch_id=[\"']?([^\"'\s>/]+)[\"']?\s*/?>",
            raw,
            re.DOTALL | re.IGNORECASE,
        )
        if activate_match and activate_match.group(1).strip():
            return {"type": "activate_batch", "batch_id": activate_match.group(1).strip(), "source": "xml"}
        solution_match = re.search(r"<solution>(.*?)</solution>", raw, re.DOTALL | re.IGNORECASE)
        if solution_match and solution_match.group(1).strip():
            return {"type": "solution", "text": solution_match.group(1).strip(), "source": "xml"}
        return {}

    @classmethod
    def _consultant_command_from_json(cls, text: str) -> str:
        """Normalize Consultant JSON action output into the legacy command protocol."""
        raw = str(text or "").strip()
        if not raw:
            return ""

        json_candidates: List[str] = []
        for match in re.finditer(r"<action_json>(.*?)</action_json>", raw, re.DOTALL | re.IGNORECASE):
            json_candidates.append(match.group(1).strip())
        fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
        if fenced:
            json_candidates.append(fenced.group(1).strip())
        if raw.startswith("{"):
            json_candidates.append(raw)

        for candidate in json_candidates:
            obj = cls._extract_json_object(candidate)
            if not obj:
                continue
            action_type = str(
                obj.get("action_type") or obj.get("type") or obj.get("action") or ""
            ).strip().lower().replace("-", "_")
            action_type = {
                "ask_user": "ask",
                "question": "ask",
                "final": "finish",
                "solution": "finish",
                "answer": "finish",
                "load_workflow": "load_workflow_template",
                "workflow_template": "load_workflow_template",
                "load_skill": "load_skill_doc",
                "skill_doc": "load_skill_doc",
                "load_ref": "load_reference",
            }.get(action_type, action_type)

            if action_type == "execute":
                code = str(obj.get("code") or obj.get("command") or obj.get("body") or "").strip()
                if code:
                    language = str(obj.get("language") or "bash").strip().lower()
                    fence = "python" if "python" in language else "bash"
                    return f"EXECUTE\n```{fence}\n{code}\n```"
            if action_type == "search":
                query = str(obj.get("query") or obj.get("body") or obj.get("question") or "").strip()
                kb = str(obj.get("kb") or obj.get("kb_name") or obj.get("category") or "SOP").strip().upper()
                if query:
                    return f"SEARCH {kb} {query}"
            if action_type == "load_workflow_template":
                workflow_id = str(obj.get("workflow_id") or obj.get("id") or obj.get("body") or "").strip()
                if workflow_id:
                    return f"LOAD_WORKFLOW_TEMPLATE {workflow_id}"
            if action_type == "load_skill_doc":
                skill_id = str(
                    obj.get("skill_id")
                    or obj.get("canonical_skill_id")
                    or obj.get("id")
                    or obj.get("body")
                    or ""
                ).strip()
                if skill_id:
                    return f"LOAD_SKILL_DOC {skill_id}"
            if action_type == "load_reference":
                skill_id = str(obj.get("skill_id") or "").strip()
                path = str(obj.get("path") or obj.get("reference_path") or obj.get("body") or "").strip()
                if skill_id and path:
                    return f"LOAD_REFERENCE {skill_id} {path}"
            if action_type == "load_script":
                skill_id = str(obj.get("skill_id") or "").strip()
                path = str(obj.get("path") or obj.get("script_path") or obj.get("body") or "").strip()
                if skill_id and path:
                    return f"LOAD_SCRIPT {skill_id} {path}"
            if action_type == "ask":
                question = str(obj.get("question") or obj.get("body") or obj.get("prompt") or "").strip()
                if question:
                    return f"ASK {question}"
            if action_type == "finish":
                summary = str(obj.get("summary") or obj.get("text") or obj.get("body") or "").strip()
                selected = obj.get("selected_skills") or obj.get("skills") or []
                if isinstance(selected, str):
                    selected = [item.strip() for item in re.split(r"[,，;\n]+", selected) if item.strip()]
                if isinstance(selected, list) and selected and "[SELECTED_SKILLS:" not in summary:
                    selected_text = ", ".join(str(item).strip() for item in selected if str(item).strip())
                    if selected_text:
                        summary = (f"[SELECTED_SKILLS: {selected_text}]\n\n" + summary).strip()
                if summary:
                    return "FINISH " + summary
        return ""

    @classmethod
    def _looks_like_plain_final_solution(cls, text: str) -> bool:
        raw = str(text or "").strip()
        if not raw:
            return False
        if re.search(
            r"</?(?:execute|ask_user|install_tool|search|activate_batch|action_json|solution)\b",
            raw,
            re.IGNORECASE,
        ):
            return False
        lower = raw.lower()
        final_phrases = (
            "all deliverables are complete",
            "analysis is finished",
            "analysis complete",
            "task complete",
            "completed successfully",
            "final result",
            "deliverables",
        )
        if any(phrase in lower for phrase in final_phrases):
            return True
        if {"report.md", "reproduce.sh"}.issubset(set(re.findall(r"[a-z0-9_.-]+\.[a-z0-9]+", lower))):
            if re.search(r"\b(?:complete|completed|summary|conclusion|result|finished)\b|完成|结果|总结|结论", raw, re.IGNORECASE):
                return True
        if raw.startswith("#") and re.search(r"结果|总结|结论|analysis result|results summary|final", raw, re.IGNORECASE):
            return True
        return False

    @staticmethod
    def _pending_action_is(action: Any, action_type: str) -> bool:
        return (
            isinstance(action, dict)
            and str(action.get("type") or "").strip().lower() == action_type
        )

    @staticmethod
    def _plan_patch_requested(state: Dict[str, Any]) -> bool:
        """Return whether a cross-node transition explicitly requests Plan repair."""
        required = str(state.get("required_next_action") or "").strip().upper()
        decision = state.get("recovery_decision") if isinstance(state.get("recovery_decision"), dict) else {}
        pending = state.get("pending_recovery_action") if isinstance(state.get("pending_recovery_action"), dict) else {}
        recommended = str(decision.get("recommended_action") or "").strip().upper()
        pending_type = str(pending.get("type") or "").strip().lower()
        owner = str(
            state.get("failure_owner")
            or decision.get("failure_owner")
            or pending.get("failure_owner")
            or ""
        ).strip().upper()
        requested = (
            required in {"PLAN_PATCH", "PATCH_PLAN"}
            or recommended == RecoveryAction.PATCH_PLAN.value
            or pending_type in {"plan_patch", "patch_plan"}
        )
        return requested and owner == RecoveryFailureOwner.PLAN.value

    def _restore_generate_plan_authority(
        self,
        task_id: str,
        before_statuses: Dict[str, str],
    ) -> Dict[str, str]:
        """Keep Generate read-only except for starting the controller-selected step."""
        if not before_statuses:
            return {}
        plan_state = load_plan_state(self.data_path, task_id)
        attempted = restore_step_status_authority(plan_state, before_statuses)
        if attempted:
            save_plan_state(self.data_path, task_id, plan_state)
            if plan_state.get("plan_file"):
                update_plan_markdown_status(plan_state["plan_file"], plan_state)
            logger.warning(
                "Generate attempted checklist status mutation; controller restored authority: task=%s changes=%s",
                task_id,
                attempted,
            )
        return attempted

    def _finalize_blocked_state(self, task_id: str, reason: str) -> str:
        """Persist a truthful framework-level blocked result without another LLM call."""
        plan_state = load_plan_state(self.data_path, task_id)
        _, step = current_plan_step_with_index(plan_state)
        decision = RecoveryPolicy().decide(
            pre_action_evidence={
                "valid": False,
                "missing_bindings": ["terminal_recovery_path"],
                "blocked_evidence": [str(reason)],
                "repairable": False,
            },
            action_id=str((plan_state.get("task_state") or {}).get("current_action_id") or ""),
            step_id=str((step or {}).get("step_id") or ""),
            attempt_id=f"blocked:{hashlib.sha256(str(reason).encode('utf-8')).hexdigest()[:16]}",
            budgets=dict((plan_state.get("task_state") or {}).get("budgets") or {}),
        )
        decision = contextualize_recovery_decision(decision, source_node="controller")
        apply_recovery_decision(plan_state, decision)
        progress = self._persist_plan_tracker(task_id, plan_state)
        root = Path(self.data_path) / "task_outputs" / str(task_id or "default")
        root.mkdir(parents=True, exist_ok=True)
        blocked_payload = {
            "task_id": str(task_id),
            "status": TASK_STATUS_BLOCKED,
            "reason": str(reason)[:8000],
            "current_step_id": str((plan_state.get("task_state") or {}).get("current_step_id") or ""),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        (root / "blocked_status.json").write_text(
            json.dumps(blocked_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        stage_status = root / "stage_status.tsv"
        if not stage_status.exists():
            stage_status.write_text(
                "stage\tstatus\toutput_path\tnotes\n"
                f"task\tblocked\t-\t{str(reason).replace(chr(9), ' ')[:2000]}\n",
                encoding="utf-8",
            )
        else:
            existing_status = stage_status.read_text(encoding="utf-8", errors="replace")
            if not re.search(r"(?:^|\t)blocked(?:\t|$)", existing_status, re.IGNORECASE | re.MULTILINE):
                with stage_status.open("a", encoding="utf-8") as handle:
                    handle.write(
                        f"controller_terminal\tblocked\t-\t{str(reason).replace(chr(9), ' ')[:2000]}\n"
                    )
        report = root / "report.md"
        if not report.exists():
            report.write_text(
                "# BioMind Task Status\n\n"
                "Status: blocked\n\n"
                f"Reason: {reason}\n",
                encoding="utf-8",
            )
        else:
            existing_report = report.read_text(encoding="utf-8", errors="replace")
            if "## Controller Terminal Status" not in existing_report:
                with report.open("a", encoding="utf-8") as handle:
                    handle.write(
                        "\n\n## Controller Terminal Status\n\n"
                        "Status: blocked\n\n"
                        f"Reason: {reason}\n\n"
                        "Resume condition: provide new evidence, change the environment, or explicitly choose a different recovery strategy.\n"
                    )
        return progress

    def _apply_transition_guard(
        self,
        node_name: str,
        state: AgentState,
        update: Dict[str, Any],
        task_id: str,
        *,
        generate_mutations: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Deterministically validate every cross-node transition."""
        guarded = dict(update or {})
        merged = {**dict(state or {}), **guarded}
        plan_state = load_plan_state(self.data_path, task_id)
        durable_recovery = dict((plan_state.get("task_state") or {}).get("recovery_state") or {})
        durable_decision = durable_recovery.get("last_recovery_decision")
        if (
            isinstance(durable_decision, dict)
            and durable_decision
            and not isinstance(guarded.get("recovery_decision"), dict)
        ):
            guarded["recovery_decision"] = durable_decision
            merged["recovery_decision"] = durable_decision
        durable_status = task_status(plan_state)
        pending_recovery = guarded.get("pending_recovery_action") or merged.get("pending_recovery_action") or {}
        recovery_type = str(pending_recovery.get("type") or "").strip().lower() if isinstance(pending_recovery, dict) else ""
        recovery_phase = str(guarded.get("recovery_phase") or merged.get("recovery_phase") or "").strip().lower()
        pending_question = guarded.get("pending_human_question") or merged.get("pending_human_question") or {}
        has_pending_question = bool(
            isinstance(pending_question, dict)
            and str(pending_question.get("question") or "").strip()
        )
        recovery_decision = merged.get("recovery_decision") if isinstance(merged.get("recovery_decision"), dict) else {}
        recovery_owner = str(
            (pending_recovery.get("failure_owner") if isinstance(pending_recovery, dict) else "")
            or recovery_decision.get("failure_owner")
            or merged.get("failure_owner")
            or ""
        ).strip().upper()
        recovery_requires_user = bool(
            (pending_recovery.get("requires_user_input") if isinstance(pending_recovery, dict) else False)
            or recovery_decision.get("requires_user_input")
        )
        attempted_block = any(
            status in {STATUS_BLOCKED, STATUS_FAILED}
            for status in (generate_mutations or {}).values()
        )
        blocked_reason = str(
            (pending_recovery.get("reason") if isinstance(pending_recovery, dict) else "")
            or guarded.get("recovery_status")
            or guarded.get("generate_exit_reason")
            or "A node reported a terminal blocked recovery state."
        )
        if recovery_type == "ask_user" and (
            recovery_owner != RecoveryFailureOwner.INPUT_DATA.value
            or not recovery_requires_user
        ):
            guarded.update({
                "next_step": "supervisor",
                "pending_action": {},
                "pending_human_question": {},
                "pending_recovery_action": {
                    **(pending_recovery if isinstance(pending_recovery, dict) else {}),
                    "type": "internal_protocol_error",
                    "reason": "Internal recovery error attempted to enter the user-decision channel.",
                    "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                    "requires_user_input": False,
                },
                "required_next_action": "INTERNAL_PROTOCOL_ERROR",
                "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                "transition_guard": "internal_error_cannot_ask_user",
            })
            return guarded
        if recovery_type == "ask_user" and has_pending_question:
            guarded.update({
                "next_step": "ask_user",
                "pending_action": {},
                "transition_guard": "awaiting_recovery_user_decision",
            })
            return guarded
        if durable_status in {TASK_STATUS_FAILED, TASK_STATUS_CANCELLED} and recovery_type != "blocked" and recovery_phase not in {"blocked", "exhausted"}:
            guarded.update({
                "next_step": "finish",
                "pending_action": {},
                "task_status": durable_status,
                "transition_guard": f"terminal_{durable_status}",
            })
            return guarded

        if (
            durable_status == TASK_STATUS_BLOCKED
            or recovery_type == "blocked"
            or recovery_phase in {"blocked", "exhausted"}
            or attempted_block
        ):
            progress = self._finalize_blocked_state(task_id, blocked_reason)
            guarded.update({
                "next_step": "finish",
                "pending_action": {},
                "pending_recovery_action": {},
                "plan_progress": progress,
                "task_status": TASK_STATUS_BLOCKED,
                "transition_guard": "terminal_blocked",
            })
            return guarded

        if durable_status == TASK_STATUS_COMPLETED:
            guarded.update({
                "next_step": "finish",
                "pending_action": {},
                "transition_guard": "terminal_completed",
            })
            return guarded

        step_candidate = guarded.get("step_completion_candidate")
        if node_name == "generate" and isinstance(step_candidate, dict) and step_candidate:
            renderer_protocol = self._renderer_action_protocol(merged, task_id)
            if (
                renderer_protocol.get("renderer_composite")
                and renderer_protocol.get("manifest_status") != "registered"
            ):
                candidate_hash = hashlib.sha256(
                    json.dumps(step_candidate, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()[:20]
                repair_feedback = self._action_format_repair_feedback(
                    merged,
                    task_id,
                    invalid_hash=candidate_hash,
                    invalid_excerpt=json.dumps(step_candidate, ensure_ascii=False, default=str),
                    contract_error={
                        "error_type": "RENDERER_MANIFEST_REQUIRED",
                        "reason": "STEP_COMPLETION_CANDIDATE is invalid before Renderer manifest registration.",
                    },
                )
                policy_decision = self._commit_preaction_policy_decision(
                    task_id,
                    str(step_candidate.get("step_id") or "task"),
                    "renderer_invocation_required_before_step_completion",
                    attempt_id=f"step-candidate:{candidate_hash}",
                )
                guarded.update({
                    "next_step": policy_decision.next_route,
                    "recovery_decision": policy_decision.to_dict(),
                    "pending_action": {},
                    "step_completion_candidate": {},
                    "pending_recovery_action": {
                        "type": "ACTION_FORMAT_REPAIR",
                        "reason": "Renderer manifest is absent; invoke the declared Renderer first.",
                        "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                        "allowed_actions": renderer_protocol.get("allowed_actions") or [],
                    },
                    "required_next_action": "ACTION_FORMAT_REPAIR",
                    "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                    "recovery_scope": "CURRENT_STEP",
                    "recovery_phase": "diagnose",
                    "transition_guard": "renderer_manifest_required_before_step_completion",
                    "messages": list(guarded.get("messages") or []) + [SystemMessage(content=repair_feedback)],
                })
                return guarded
            passed, gate, progress = self._validate_and_advance_step_candidate(
                task_id,
                step_candidate,
                source_node=node_name,
            )
            guarded.update({
                "pending_action": {},
                "step_completion_candidate": {},
                "completion_gate_result": gate.to_dict(),
                "plan_progress": progress,
                "pending_recovery_action": {},
                "required_next_action": "",
            })
            if passed:
                if self._plan_tracker_is_complete(task_id):
                    guarded.update({
                        "next_step": "supervisor_final_audit",
                        "final_audit_requested": True,
                        "completion_candidate": {
                            "text": "Controller verified the final plan step from persisted execution evidence.",
                            "source": "step_completion_candidate",
                            "step_id": str(step_candidate.get("step_id") or ""),
                        },
                        "transition_guard": "step_completion_candidate_final_audit",
                    })
                else:
                    repair_feedback = self._action_format_repair_feedback(
                        merged,
                        task_id,
                        invalid_hash=hashlib.sha256(contract_error.encode("utf-8")).hexdigest()[:20],
                        invalid_excerpt=str((guarded.get("pending_action") or {}).get("code") or ""),
                        contract_error={"error_type": "PRE_ACTION_CONTRACT_ERROR", "reason": contract_error},
                    )
                    guarded.update({
                        "next_step": "generate",
                        "transition_guard": "step_completion_candidate_accepted",
                    })
            else:
                rejection_signature = hashlib.sha256(json.dumps(
                    {
                        "step_id": step_candidate.get("step_id"),
                        "evidence_ids": step_candidate.get("evidence_ids") or [],
                        "classification": gate.classification,
                        "message": gate.message,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8", errors="replace")).hexdigest()[:20]
                candidate_budgets = dict((load_plan_state(self.data_path, task_id).get("task_state") or {}).get("budgets") or {})
                candidate_sequence = int(candidate_budgets.get("generate_action_count") or 0) + 1
                policy_decision = self._commit_preaction_policy_decision(
                    task_id,
                    str(step_candidate.get("step_id") or "task"),
                    f"step_completion_candidate_rejected: {gate.message}",
                    attempt_id=f"step-candidate-rejected:{rejection_signature}:{candidate_sequence}",
                    source_node=node_name,
                )
                guarded.update({
                    "next_step": policy_decision.next_route,
                    "recovery_decision": policy_decision.to_dict(),
                    "pending_recovery_action": {
                        "type": (
                            "ACTION_FORMAT_REPAIR"
                            if policy_decision.kind == RecoveryDecisionKind.FORMAT_REPAIR.value
                            else "internal_protocol_error"
                        ),
                        "reason": gate.message,
                        "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                    },
                    "required_next_action": (
                        "ACTION_FORMAT_REPAIR"
                        if policy_decision.kind == RecoveryDecisionKind.FORMAT_REPAIR.value
                        else "INTERNAL_PROTOCOL_ERROR"
                    ),
                    "transition_guard": "step_completion_candidate_rejected",
                    "messages": list(guarded.get("messages") or []) + [SystemMessage(content=(
                        "<step_completion_candidate_rejected>\n"
                        f"Current step: {step_candidate.get('step_id') or '<missing>'}\n"
                        f"Reason: {gate.message}\n{gate.feedback}\n"
                        "Return one EXECUTE action that supplies the missing evidence, or another "
                        "STEP_COMPLETION_CANDIDATE citing different eligible evidence.\n"
                        "</step_completion_candidate_rejected>"
                    ))],
                })
            return guarded

        next_step = str(guarded.get("next_step") or "").strip()
        if next_step == "execute":
            contract_error = self._validate_pending_action_contract(
                guarded.get("pending_action") or merged.get("pending_action") or {},
                merged,
                task_id,
            )
            if contract_error:
                _, current_step = current_plan_step_with_index(plan_state)
                step_id = str(current_step.get("step_id") or "task")
                invalid_action = guarded.get("pending_action") or merged.get("pending_action") or {}
                invalid_excerpt = str(
                    invalid_action.get("code")
                    or invalid_action.get("reason")
                    or invalid_action
                )[:500]
                repair_feedback = self._action_format_repair_feedback(
                    merged,
                    task_id,
                    invalid_hash=hashlib.sha256(invalid_excerpt.encode("utf-8")).hexdigest()[:16],
                    invalid_excerpt=invalid_excerpt,
                    contract_error={"reason": contract_error},
                )
                current_budgets = dict((plan_state.get("task_state") or {}).get("budgets") or {})
                attempt_sequence = int(current_budgets.get("generate_action_count") or 0) + 1
                policy_decision = self._commit_preaction_policy_decision(
                    task_id,
                    step_id,
                    contract_error,
                    attempt_id=(
                        f"preaction:{hashlib.sha256(invalid_excerpt.encode('utf-8')).hexdigest()[:16]}:"
                        f"{attempt_sequence}"
                    ),
                )
                pending_type = (
                    "ACTION_FORMAT_REPAIR"
                    if policy_decision.kind == RecoveryDecisionKind.FORMAT_REPAIR.value
                    else "internal_protocol_error"
                )
                guarded.update({
                    "next_step": policy_decision.next_route,
                    "recovery_decision": policy_decision.to_dict(),
                    "pending_action": {},
                    "pending_recovery_action": {
                        "type": pending_type,
                        "reason": contract_error,
                        "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                    },
                    "required_next_action": (
                        "ACTION_FORMAT_REPAIR"
                        if policy_decision.kind == RecoveryDecisionKind.FORMAT_REPAIR.value
                        else "INTERNAL_PROTOCOL_ERROR"
                    ),
                    "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                    "recovery_scope": "CURRENT_STEP",
                    "recovery_phase": "diagnose",
                    "transition_guard": (
                        "invalid_action_requires_format_repair"
                        if policy_decision.kind == RecoveryDecisionKind.FORMAT_REPAIR.value
                        else "invalid_action_budget_exhausted"
                    ),
                    "messages": list(guarded.get("messages") or []) + [SystemMessage(content=repair_feedback)],
                })
                return guarded

        if node_name == "generate" and next_step == "finish" and str(merged.get("task_mode") or "managed").lower() == "managed":
            guarded["next_step"] = "supervisor_final_audit"
            guarded["transition_guard"] = "managed_finish_requires_final_audit"
        elif next_step:
            guarded["next_step"] = next_step
        elif node_name == "generate":
            _, missing_transition_step = current_plan_step_with_index(plan_state)
            missing_transition_id = str((missing_transition_step or {}).get("step_id") or "task")
            policy_decision = self._commit_preaction_policy_decision(
                task_id,
                missing_transition_id,
                "generate_result_missing_transition",
                attempt_id=f"missing-transition:{hashlib.sha256(json.dumps(guarded, default=str, sort_keys=True).encode('utf-8')).hexdigest()[:16]}",
            )
            guarded.update({
                "next_step": policy_decision.next_route,
                "recovery_decision": policy_decision.to_dict(),
                "pending_action": {},
                "pending_recovery_action": {
                    "type": "ACTION_FORMAT_REPAIR",
                    "reason": "Generate produced no next-node candidate.",
                    "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                },
                "required_next_action": "ACTION_FORMAT_REPAIR",
                "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                "recovery_scope": "CURRENT_STEP",
                "transition_guard": "node_result_missing_transition",
                "state_transition_error": "NODE_RESULT_INVALID: Generate produced no next-node candidate.",
            })
        if (
            node_name in {"generate", "execute", "supervisor", "supervisor_final_audit"}
            and str(guarded.get("next_step") or "") == "planner"
            and self._plan_patch_requested({**merged, **guarded})
        ):
            _, patch_step = current_plan_step_with_index(plan_state)
            patch_step_id = str((patch_step or {}).get("step_id") or "task")
            patch_hash = str(plan_state.get("semantic_plan_hash") or "unknown")
            patch_signature = f"{patch_step_id}:{patch_hash}"
            budgets = dict((plan_state.get("task_state") or {}).get("budgets") or {})
            no_op_counts = dict(budgets.get("no_op_plan_patch_by_signature") or {})
            if int(no_op_counts.get(patch_signature) or 0) >= 1:
                guarded.update({
                    "next_step": "generate",
                    "pending_action": {},
                    "pending_recovery_action": {
                        "type": "same_step_generate",
                        "reason": "repeated_no_op_plan_patch",
                        "step_id": patch_step_id,
                        "signature": patch_signature,
                    },
                    "recovery_decision": {},
                    "required_next_action": "SAME_STEP_GENERATE",
                    "failure_owner": "",
                    "recovery_scope": "CURRENT_STEP",
                    "recovery_phase": "apply_repair",
                    "recovery_status": "",
                    "supervisor_verdict": "",
                    "completion_gate_result": {},
                    "transition_guard": "repeated_no_op_plan_patch_fused",
                    "messages": list(guarded.get("messages") or []) + [SystemMessage(content=(
                        "The same current-step PLAN_PATCH already produced no semantic change. "
                        "Planner routing was fused; repair the execution action using the existing plan."
                    ))],
                })
        if (
            node_name in {"generate", "execute", "supervisor", "supervisor_final_audit"}
            and str(guarded.get("next_step") or "") == "planner"
            and not self._plan_patch_requested({**merged, **guarded})
        ):
            guarded.update({
                "next_step": "generate",
                "pending_action": {},
                "transition_guard": "planner_requires_explicit_plan_patch",
                "state_transition_error": (
                    "NODE_RESULT_INVALID: runtime nodes may route to Planner only with required_next_action=PLAN_PATCH."
                ),
                "messages": list(guarded.get("messages") or []) + [SystemMessage(content=(
                    "Planner routing was rejected because no structured PLAN_PATCH was requested. "
                    "Keep the approved plan and repair or regenerate the current action instead."
                ))],
            })
        guarded_next = str(guarded.get("next_step") or "")
        guarded_self_transition = (
            (node_name == "generate" and guarded_next == "generate")
            or (node_name == "execute" and guarded_next == "generate")
        )
        transition_recovery = guarded.get("pending_recovery_action") or merged.get("pending_recovery_action") or {}
        transition_recovery_type = str(
            transition_recovery.get("type") if isinstance(transition_recovery, dict) else ""
        ).strip().lower()
        if guarded_self_transition and transition_recovery_type != "action_format_repair":
            _, transition_step = current_plan_step_with_index(load_plan_state(self.data_path, task_id))
            transition_step_id = str(transition_step.get("step_id") or "task")
            transition_reason = str(
                guarded.get("generate_exit_reason")
                or guarded.get("transition_guard")
                or guarded.get("required_next_action")
                or "continue"
            ).strip().lower()
            transition_fingerprint = str(
                transition_recovery.get("invalid_response_hash")
                if isinstance(transition_recovery, dict)
                else ""
            )
            signature = ":".join(
                part for part in (
                    f"{node_name}->{guarded_next}",
                    transition_step_id,
                    transition_reason,
                    transition_fingerprint,
                )
                if part
            )
            count, _ = self._increment_durable_budget(
                task_id,
                "same_transition_by_signature",
                signature,
                limit=2,
            )
            if count > 2:
                reason = f"STATE_MACHINE_STALLED:{node_name}->{guarded_next}"
                progress = self._finalize_blocked_state(task_id, reason)
                output.print_once(
                    f"state_machine_stalled:{task_id}:{node_name}:{guarded_next}",
                    (
                        "Task stopped by the state-machine loop guard: "
                        f"{node_name} -> {guarded_next}. "
                        f"Details were written to {self.data_path}/task_outputs/{task_id}/report.md."
                    ),
                    Fore.RED,
                )
                guarded.update({
                    "next_step": "finish",
                    "pending_action": {},
                    "plan_progress": progress,
                    "task_status": TASK_STATUS_BLOCKED,
                    "transition_guard": "same_transition_budget_exhausted",
                    "state_transition_error": reason,
                })
        return guarded

    def _profile_node(self, node_name: str, func):
        async def wrapped(state: AgentState):
            task_id = state.get("task_id") or state.get("session_id") or "default"
            before_plan = load_plan_state(self.data_path, task_id)
            before_statuses = {
                str(step.get("step_id") or ""): str(step.get("status") or "")
                for step in before_plan.get("steps", []) or []
                if isinstance(step, dict) and str(step.get("step_id") or "")
            }
            with self.profiler.node(task_id, node_name):
                result = await func(state)
            if not isinstance(result, dict):
                result = {
                    "messages": [SystemMessage(content=(
                        f"{node_name} returned an invalid NodeResult ({type(result).__name__}). "
                        "The state machine stopped instead of reusing a stale transition."
                    ))],
                    "pending_action": {},
                    "pending_recovery_action": {
                        "type": "blocked",
                        "reason": f"NODE_RESULT_INVALID:{node_name}",
                    },
                    "next_step": "finish",
                    "state_transition_error": f"NODE_RESULT_INVALID:{node_name}",
                }
            if isinstance(result, dict) and node_name in {"consultant", "generate"}:
                combined_messages = add_messages(
                    list(state.get("messages", []) or []),
                    list(result.get("messages", []) or []),
                )
                try:
                    compacted_messages, compressed = await self.memory_manager.maybe_compress(
                        combined_messages,
                        task_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "Message compaction failed after %s for task %s; keeping graph messages unchanged: %s",
                        node_name,
                        task_id,
                        exc,
                    )
                    compacted_messages, compressed = combined_messages, False
                if compressed:
                    output.print_once(
                        f"memory_state_compacted:{task_id}:{node_name}",
                        "🗜️ Memory compressed; graph working messages were replaced with the compact state.",
                        Fore.YELLOW,
                    )
                    result = dict(result)
                    result["messages"] = [
                        RemoveMessage(id=REMOVE_ALL_MESSAGES),
                        *compacted_messages,
                    ]
            result = self._normalize_state_update_aliases(node_name, state, result)
            generate_mutations: Dict[str, str] = {}
            if node_name == "generate":
                generate_mutations = self._restore_generate_plan_authority(task_id, before_statuses)
                if isinstance(result, dict):
                    result.pop("plan_progress", None)
                    recommended = str(result.pop("next_step", "") or "").strip()
                    if recommended:
                        result["node_recommended_next_step"] = recommended
                    if recommended != "supervisor_final_audit":
                        result.setdefault("final_audit_requested", False)
                        result.setdefault("completion_candidate", {})
                    for forbidden_key in (
                        "current_step_id", "current_stage_id", "completed_steps", "step_status", "stage_status",
                        "task_status", "depends_on", "artifact_status", "provenance_state",
                    ):
                        result.pop(forbidden_key, None)
            if isinstance(result, dict):
                if node_name == "generate" and not result.get("next_step"):
                    result["next_step"] = str(result.get("node_recommended_next_step") or "")
                result = self._apply_transition_guard(
                    node_name,
                    state,
                    result,
                    task_id,
                    generate_mutations=generate_mutations,
                )
            self._record_profiler_state_update(task_id, node_name, state, result)
            return result
        return wrapped

    def _record_human_wait(
        self,
        task_id: str,
        prompt_type: str,
        wait_started: float,
        response: Any,
        payload: Optional[Dict[str, Any]] = None,
        started_at: Optional[str] = None,
    ) -> None:
        self.profiler.record_confirmation(
            task_id=task_id,
            prompt_type=prompt_type,
            response=response,
            duration_seconds=time.perf_counter() - wait_started,
            payload=payload or {},
            started_at=started_at,
        )
        self.profiler.flush(task_id)

    def _prompt_user(
        self,
        task_id: str,
        stage: str,
        prompt: str,
        payload: Optional[Dict[str, Any]] = None,
        started_at: Optional[str] = None,
        multiline: bool = False,
    ) -> str:
        """Collect human input with timeout and waiting-state persistence."""
        try:
            if multiline:
                response = safe_multiline_input(
                    prompt,
                    timeout_seconds=self.input_timeout_seconds,
                    history=False,
                )
            else:
                response = safe_input(
                    prompt,
                    timeout_seconds=self.input_timeout_seconds,
                    history=(stage == "interactive_prompt"),
                )
                if stage == "interactive_prompt":
                    continuation_lines = read_pending_paste_lines()
                    if continuation_lines:
                        response = "\n".join([response, *continuation_lines])
            clear_waiting_state(self.data_path, task_id)
            return response
        except HumanInputUnavailable as exc:
            path = save_waiting_state(
                self.data_path,
                task_id,
                stage=stage,
                prompt=prompt,
                payload=payload or {},
                reason=type(exc).__name__,
                timeout_seconds=self.input_timeout_seconds,
                started_at=started_at,
            )
            if self.state_manager:
                try:
                    self.state_manager.update_task_status(task_id, "waiting_user")
                except Exception:
                    pass
            restore_terminal_state()
            print(
                f"\n{Fore.YELLOW}⏸ Waiting for user input saved to {path}. "
                f"Resume later with /resume {task_id}.{Style.RESET_ALL}"
            )
            raise

    async def _aprompt_user(
        self,
        task_id: str,
        stage: str,
        prompt: str,
        payload: Optional[Dict[str, Any]] = None,
        started_at: Optional[str] = None,
        multiline: bool = False,
    ) -> str:
        if os.name == "posix" and sys.stdin.isatty():
            return self._prompt_user(
                task_id,
                stage,
                prompt,
                payload,
                started_at,
                multiline,
            )
        return await asyncio.to_thread(
            self._prompt_user,
            task_id,
            stage,
            prompt,
            payload,
            started_at,
            multiline,
        )

    async def _suspend_for_human_input(
        self,
        task_id: str,
        stage: str,
        messages: List[BaseMessage],
        payload: Dict[str, Any],
        reason: str,
    ) -> Dict[str, Any]:
        content = f"⏸ Waiting for human input at {stage}: {reason}"
        await self._observe_session_memory(
            task_id,
            stage,
            messages + [HumanMessage(content=content)],
            {"decision": "waiting_user", "payload": payload, "reason": reason},
        )
        return {
            "messages": [HumanMessage(content=content)],
            "next_step": "human_input",
        }

    # ────────────────────────────────────────────────────────────
    #  图构建
    # ────────────────────────────────────────────────────────────

    def _build_graph(self):
        workflow = StateGraph(AgentState)

        # ════════════════════════════════════════════
        # Node -1: Triage（轻量四分类路由）
        # ════════════════════════════════════════════
        async def triage(state: AgentState):
            output.print_node_header("triage")
            messages = state.get("messages", [])
            task_id = state.get("task_id", "default")
            task_spec = self._task_spec_for_state(dict(state), messages)
            routing_context = format_effective_user_facts_context(
                task_spec,
                audience="triage",
                max_chars=3000,
            )
            user_text = "\n\n".join(
                part for part in [str(task_spec.get("goal") or "").strip(), routing_context]
                if part
            )

            resume_node = str(state.get("resolved_resume_node") or "").strip()
            if str(state.get("resume_mode") or "") == "business_state" and resume_node:
                audit = state.get("resume_audit") or {}
                output.print_once(
                    f"resume_route:{task_id}:{resume_node}",
                    f"↪ Resume router: {resume_node} | {audit.get('resume_reason', 'durable business state')}",
                    Fore.CYAN,
                )
                return {
                    "task_mode": str(state.get("task_mode") or "managed"),
                    "retrieval_mode": str(state.get("retrieval_mode") or "kb"),
                    "use_scientific_review": True,
                    "validation_level": str(state.get("validation_level") or "scientific"),
                    "next_step": resume_node,
                }

            decision = await classify_task_mode(self.llm,
                user_text,
                data_path=self.data_path,
                task_id=task_id,
                trace_metadata={
                    "task_revision": self._task_revision_for_state(dict(state)),
                    "task_spec_revision": self._task_revision_for_state(dict(state)),
                    "effective_facts_revision": str(state.get("effective_user_facts_revision") or ""),
                    "user_source_refs": list(state.get("user_source_refs") or []),
                },
            )
            mode = str(decision.get("task_mode") or "managed").strip().lower()
            retrieval_mode = str(decision.get("retrieval_mode") or "kb").strip().lower()
            if mode not in {"chat", "readonly", "simple_execute", "managed"}:
                mode = "managed"
                retrieval_mode = "kb"

            validation_by_mode = {
                "chat": "none",
                "readonly": "none",
                "simple_execute": "basic",
                "managed": "scientific",
            }
            next_by_mode = {
                "chat": "direct_chat",
                "readonly": "generate",
                "simple_execute": "generate",
                "managed": "consultant",
            }
            update = {
                "task_mode": mode,
                "retrieval_mode": retrieval_mode,
                "use_scientific_review": mode == "managed",
                "validation_level": validation_by_mode.get(mode, "scientific"),
                "next_step": next_by_mode.get(mode, "consultant"),
            }
            if decision.get("llm_call_ref"):
                update["llm_call_refs"] = [dict(decision["llm_call_ref"])]
                update["context_lineage_ref"] = str(
                    Path(self.data_path) / "task_outputs" / task_id / "context_trace"
                )
            if mode in {"readonly", "simple_execute"}:
                update["current_plan"] = (
                    f"Lightweight {mode} task. Satisfy the user request with one minimal command, "
                    "then stop. Do not plan a workflow.\n\n"
                    f"User request:\n{user_text}"
                )
            output.print_once(
                f"triage:{task_id}",
                (
                    f"↪ Fast router: {mode}"
                    f" | retrieval={retrieval_mode}"
                    f" | confidence={decision.get('confidence', 0):.2f}"
                    f" | {decision.get('reason', '')}"
                ),
                Fore.CYAN,
            )
            return update

        # ════════════════════════════════════════════
        # Node -0.5: Direct Chat（轻量知识问答）
        # ════════════════════════════════════════════
        async def direct_chat(state: AgentState):
            output.print_node_header("direct_chat")
            messages = state.get("messages", [])
            task_id = state.get("task_id", "default")
            project_id = state.get("project_id") or self.default_project_id
            query = "\n".join(
                str(m.content) for m in messages if isinstance(m, HumanMessage)
            ).strip()
            retrieval_mode = str(state.get("retrieval_mode") or "none").strip().lower()

            context_parts = []
            if retrieval_mode in {"kb", "skill_docs"}:
                wiki_context = self._wiki_memory_stage_context(
                    task_id=task_id,
                    project_id=project_id,
                    query=query,
                    digest_chars=1200,
                    context_chars=2400,
                )
                if wiki_context:
                    context_parts.append(wiki_context)
                if self.retriever and KB_MAP.get("SOP"):
                    try:
                        rag_result = self.retriever.retrieve(query, target_datasets=[KB_MAP["SOP"]])
                        if rag_result and str(rag_result).strip() and "Nothing found" not in str(rag_result):
                            context_parts.append(f"<kb_context>\n{str(rag_result)[:3000]}\n</kb_context>")
                    except Exception as exc:
                        logger.warning("Direct chat KB retrieval failed: %s", exc)

            if retrieval_mode == "skill_docs" and self.skill_manager:
                try:
                    matched = self.skill_manager.match_skills(query, max_results=2)
                    if matched:
                        skill_docs = self.skill_manager.load_skills_for_prompt(matched)
                        if skill_docs:
                            context_parts.append(
                                "<skill_docs_readonly>\n"
                                f"{skill_docs[:5000]}\n"
                                "</skill_docs_readonly>"
                            )
                except Exception as exc:
                    logger.warning("Direct chat skill-doc retrieval failed: %s", exc)

            context = "\n\n".join(context_parts) if context_parts else "No retrieved context."
            prompt = f"""You are BioMind's direct chat answerer.

Answer the user directly and concisely.
You may use the read-only context below.
Do not plan workflows, execute commands, install tools, ask for code review, or emit XML action tags such as <execute>, <install_tool>, <search>, or <activate_batch>.
If the user is actually asking you to run analysis or inspect local files, say that it should be handled outside chat mode.

<read_only_context>
{context}
</read_only_context>
"""
            try:
                response = await self._ainvoke_llm(
                    [SystemMessage(content=prompt)] + messages,
                    stage="direct_chat",
                    task_id=task_id,
                    task_mode="chat",
                )
            except Exception as exc:
                response = self._llm_failure_response("direct_chat", exc, task_id)

            content = str(response.content or "").strip()
            if re.search(r"<\s*(execute|install_tool|search|activate_batch|ask_user)\b", content, re.IGNORECASE):
                content = (
                    "这个问题不能在 chat 模式里执行动作。请把需求改成明确的只读检查、"
                    "简单执行，或者交给 managed 流程处理。"
                )
                response = AIMessage(content=content)
            output.print_ai(content)
            await self._finalize_task_memory(
                task_id,
                "completed",
                messages + [response],
                {"task_mode": "chat", "retrieval_mode": retrieval_mode, "solution": content[:3000]},
            )
            return {"messages": [response], "next_step": "finish"}

        # ════════════════════════════════════════════
        # Node 0: Consultant
        # ════════════════════════════════════════════
        async def consultant(state: AgentState):
            output.print_node_header("consultant")
            messages = state["messages"]
            rag_history = state.get("rag_history", [])
            task_spec = self._task_spec_for_state(dict(state), messages)
            later_directives = [
                str(item.get("text") or "").strip()
                for item in task_spec.get("user_directives", [])
                if isinstance(item, dict)
                and str(item.get("kind") or "") != "initial_request"
                and str(item.get("text") or "").strip()
            ]
            consultant_context = "\n".join(
                [str(task_spec.get("goal") or "").strip(), *later_directives[-6:]]
            ).strip()
            consultant_max_skills = self._selection_max_skills(consultant_context)
            last_human_index = -1
            for idx, msg in enumerate(messages):
                if isinstance(msg, HumanMessage):
                    last_human_index = idx
            recent_consultant_messages = messages[last_human_index + 1:] if last_human_index >= 0 else messages
            load_actions = [
                str(m.content).strip()
                for m in recent_consultant_messages
                if isinstance(m, AIMessage) and str(m.content).strip().startswith("LOAD_")
            ]
            execute_actions = [
                str(m.content).strip()
                for m in recent_consultant_messages
                if isinstance(m, AIMessage) and str(m.content).strip().startswith("EXECUTE")
            ]
            consultant_observations = [
                str(m.content)
                for m in recent_consultant_messages
                if isinstance(m, SystemMessage) and "<observation>" in str(m.content)
            ]
            validation_errors = [
                str(m.content)
                for m in recent_consultant_messages
                if isinstance(m, SystemMessage) and "<consultant_validation_error>" in str(m.content)
            ]
            format_repair_errors = [
                err for err in validation_errors
                if _consultant_validation_is_format_repair_error(err)
            ]
            internal_validation_errors = [
                err for err in validation_errors
                if not _consultant_validation_is_format_repair_error(err)
            ]
            load_errors = [
                str(m.content)
                for m in recent_consultant_messages
                if isinstance(m, SystemMessage) and "<load_error" in str(m.content)
            ]
            failed_consultant_execs = [
                str(m.content)
                for m in recent_consultant_messages
                if isinstance(m, SystemMessage)
                and "<observation>" in str(m.content)
                and "❌" in str(m.content)
            ]
            force_finish_indices = [
                idx
                for idx, msg in enumerate(recent_consultant_messages)
                if isinstance(msg, SystemMessage)
                and "<consultant_force_finish_from_observations>" in str(msg.content)
            ]
            last_force_finish_index = force_finish_indices[-1] if force_finish_indices else -1
            execute_after_force_finish = [
                str(msg.content).strip()
                for idx, msg in enumerate(recent_consultant_messages)
                if idx > last_force_finish_index
                and isinstance(msg, AIMessage)
                and str(msg.content).strip().startswith("EXECUTE")
            ]
            task_id = state.get("task_id", "default")
            project_id = state.get("project_id") or self.default_project_id
            work_dir = self.pm.work_dir if self.pm else os.getcwd()

            consultant_wiki_context = self._wiki_memory_stage_context(
                task_id=task_id,
                project_id=project_id,
                query=consultant_context,
            )
            consultant_memory_retrieval_context = await self._consultant_memory_retrieval_context(
                task_id=task_id,
                project_id=project_id,
                query=consultant_context,
                wiki_context=consultant_wiki_context,
            )
            input_resolution_result = resolve_formal_upstream_artifacts(
                consultant_context,
                work_dir=work_dir,
                task_id=task_id,
                project_root=project_root,
            )
            input_resolution_context = format_formal_upstream_context(input_resolution_result)
            if input_resolution_context.strip():
                output.print_once(
                    f"formal_upstream_audit:{task_id}",
                    "🔎 Consultant checked formal upstream artifacts before asking or blocking.",
                    Fore.CYAN,
                )

            if self.skill_manager and (
                len(load_actions) >= CONSULTANT_LOAD_ACTION_LOOP_LIMIT
                or len(internal_validation_errors) >= CONSULTANT_INTERNAL_VALIDATION_ERROR_LIMIT
                or len(load_errors) >= CONSULTANT_LOAD_ERROR_LOOP_LIMIT
                or len(format_repair_errors) > CONSULTANT_PARSE_REPAIR_LIMIT
            ):
                fallback_raw = []
                for action in load_actions:
                    parts = action.split(maxsplit=2)
                    if len(parts) >= 2:
                        fallback_raw.append(parts[1])
                if not fallback_raw:
                    fallback_raw = self.skill_manager.match_skills(consultant_context, max_results=consultant_max_skills)
                selected = self.skill_manager.normalize_selected_skills(fallback_raw, max_results=consultant_max_skills)
                hints = ", ".join(selected) if selected else "None"
                skill_only_handoff = self._consultant_skill_only_fallback_handoff(
                    consultant_context=consultant_context,
                    fallback_raw=fallback_raw,
                    task_id=task_id,
                    consultant_memory_retrieval_context=consultant_memory_retrieval_context,
                    consultant_wiki_context=consultant_wiki_context,
                    input_resolution_context=input_resolution_context,
                    max_skills=consultant_max_skills,
                )
                if skill_only_handoff:
                    skill_only_handoff["input_audit"] = input_resolution_result
                    output.print_once(
                        "consultant_loop_guard_skill_only",
                        (
                            "✓ Consultant loop guard found canonical skills; "
                            "skipping workflow_state and handing skill-only context to Planner."
                        ),
                        Fore.GREEN,
                    )
                    return skill_only_handoff
                message = (
                    "<consultant_loop_guard_blocked>\n"
                    "Consultant handoff validation or skill loading failed repeatedly. "
                    "BioMind could not derive any valid canonical skills for an automatic Planner handoff.\n"
                    f"Candidate skill hints from metadata matching: {hints}\n"
                    "If no workflow template clearly matches, provide canonical skill id(s) only; "
                    "otherwise provide a corrected workflow_state. You may also reply with `blocked: <reason>`.\n"
                    "</consultant_loop_guard_blocked>"
                )
                output.print_once(
                    "consultant_loop_guard_blocked",
                    "⚠️ Consultant repeated internal actions and no valid canonical skill fallback was available.",
                    Fore.YELLOW,
                )
                return {
                    "messages": [SystemMessage(content=message)],
                    "input_audit": input_resolution_result,
                    "pending_human_question": {
                        "type": "consultant_question",
                        "stage": "consultant",
                        "question": (
                            "Consultant failed to produce a valid handoff after repeated internal actions. "
                            f"Candidate skill hints: {hints}. If no workflow template clearly matches, provide canonical skill id(s) only; "
                            "otherwise provide a corrected workflow_state, or `blocked: <reason>`."
                        ),
                        "resume_step": "consultant",
                    },
                    "next_step": "ask_user",
                }

            if len(execute_actions) >= 4 and len(failed_consultant_execs) < 3:
                fallback_raw = self.skill_manager.candidate_skill_ids_for_prompt(
                    consultant_context,
                    max_results=consultant_max_skills,
                ) if self.skill_manager else []
                skill_only_handoff = self._consultant_skill_only_fallback_handoff(
                    consultant_context=consultant_context,
                    fallback_raw=fallback_raw,
                    task_id=task_id,
                    consultant_memory_retrieval_context=consultant_memory_retrieval_context,
                    consultant_wiki_context=consultant_wiki_context,
                    input_resolution_context=input_resolution_context,
                    max_skills=consultant_max_skills,
                )
                if skill_only_handoff:
                    output.print_once(
                        "consultant_execute_count_loop_guard_handoff",
                        (
                            "✓ Consultant read-only loop guard reached; handing verified evidence "
                            "and canonical skills directly to Planner."
                        ),
                        Fore.GREEN,
                    )
                    return skill_only_handoff
                message = (
                    "<consultant_loop_guard_blocked>\n"
                    "Repeated Consultant read-only checks reached the loop guard, but no canonical skill "
                    "could be resolved for Planner.\n"
                    "</consultant_loop_guard_blocked>"
                )
                return {
                    "messages": [SystemMessage(content=message)],
                    "pending_human_question": {
                        "type": "consultant_question",
                        "stage": "consultant",
                        "question": (
                            "Consultant completed the bounded read-only checks but could not resolve a canonical "
                            "skill. Please provide the skill ID or clarify the analysis goal."
                        ),
                        "resume_step": "consultant",
                    },
                    "next_step": "ask_user",
                }

            if len(failed_consultant_execs) >= 3:
                recent_error_preview = "\n".join(
                    f"- {re.sub(r'<[^>]+>', '', err).strip()[:500]}"
                    for err in failed_consultant_execs[-3:]
                    if str(err).strip()
                )
                question = (
                    "Consultant read-only verification failed repeatedly in the current turn, so BioMind stopped "
                    "the Consultant EXECUTE loop.\n"
                    f"Recent failed observations:\n{recent_error_preview or '- none'}\n\n"
                    "Please provide corrected inputs/runtime instructions, or reply with `blocked: <reason>`."
                )
                obs = (
                    "<consultant_loop_guard_blocked>\n"
                    "Repeated Consultant EXECUTE failures reached the loop guard. "
                    "Automatic read-only checks are paused.\n"
                    "</consultant_loop_guard_blocked>"
                )
                output.print_once(
                    "consultant_execute_loop_guard_blocked",
                    "⚠️ Consultant read-only checks failed repeatedly; asking for explicit repair instead of looping.",
                    Fore.YELLOW,
                )
                return {
                    "messages": [SystemMessage(content=obs)],
                    "pending_human_question": {
                        "type": "consultant_question",
                        "stage": "consultant",
                        "question": question,
                        "resume_step": "consultant",
                    },
                    "next_step": "ask_user",
                }

            kb_list_desc = ", ".join(KB_MAP.keys())
            workflow_candidate_ids: List[str] = []
            if self.workflow_manager:
                workflow_candidate_ids = self.workflow_manager.candidate_workflow_ids_for_prompt(
                    consultant_context,
                    max_results=2,
                )
                if workflow_candidate_ids:
                    workflow_catalog = self.workflow_manager.get_workflow_catalog_prompt(
                        workflow_ids=workflow_candidate_ids,
                        max_entries=2,
                        description_chars=180,
                    )
                else:
                    workflow_catalog = (
                        "<workflow_catalog>\n"
                        "No metadata workflow match was found. Do not invent a workflow ID; use canonical skills directly unless a loaded template proves a match.\n"
                        "</workflow_catalog>"
                    )
            else:
                workflow_catalog = "<workflow_catalog>\nNo workflow templates available.\n</workflow_catalog>"
            workflow_catalog = self._clip_observation_text(
                workflow_catalog,
                CONSULTANT_PROMPT_COMPONENT_BUDGETS["workflow_catalog"],
            )
            candidate_skill_ids: List[str] = []
            skill_tree = ""
            skill_catalog = ""
            if self.skill_manager:
                candidate_skill_ids = self.skill_manager.candidate_skill_ids_for_prompt(
                    consultant_context,
                    max_results=consultant_max_skills,
                )
                if candidate_skill_ids:
                    skill_catalog = self._clip_observation_text(
                        self.skill_manager.get_skill_catalog_prompt(candidate_skill_ids),
                        CONSULTANT_PROMPT_COMPONENT_BUDGETS["skill_catalog"],
                    )
                    skill_tree = ""
                else:
                    skill_tree = self.skill_manager.get_skill_tree_prompt(
                        max_chars=CONSULTANT_PROMPT_COMPONENT_BUDGETS["skill_tree"],
                    )
                    skill_catalog = (
                        "<skill_catalog>\n"
                        "No metadata candidate matched this request. Do not invent an ID; use the visible canonical tree only to choose a documented candidate, or ask for clarification.\n"
                        "</skill_catalog>"
                    )
                skills_summary = (
                    f"{skill_catalog}\n{skill_tree}" if skill_tree else skill_catalog
                )
            else:
                skills_summary = "None"
            runtime_discovery_hints: Dict[str, Any] = {}
            needs_controlled_discovery = not candidate_skill_ids or any(
                not self._skill_runtime_requirements(skill_id).get("metadata_present", True)
                for skill_id in candidate_skill_ids
            )
            if needs_controlled_discovery:
                runtime_discovery_hints = self._controlled_runtime_discovery_hints(
                    consultant_context,
                    task_id,
                )
                if runtime_discovery_hints:
                    skills_summary += (
                        "\n<controlled_discovery_hints>\n"
                        f"candidate_skills: {', '.join(runtime_discovery_hints.get('candidate_skills', []) or []) or 'none'}\n"
                        f"candidate_envs: {', '.join(runtime_discovery_hints.get('candidate_envs', []) or []) or 'none'}\n"
                        f"candidate_tools: {', '.join(runtime_discovery_hints.get('candidate_tools', []) or []) or 'none'}\n"
                        "evidence_only: true; do not claim runtime readiness without PackageManager evidence and a passing healthcheck.\n"
                        "</controlled_discovery_hints>"
                    )
            skills_summary = self._clip_observation_text(
                skills_summary,
                CONSULTANT_PROMPT_COMPONENT_BUDGETS["skills_summary"],
            )
            runtime_evidence_context = self._clip_observation_text(
                self._consultant_runtime_context(candidate_skill_ids),
                CONSULTANT_PROMPT_COMPONENT_BUDGETS["runtime_evidence"],
            )
            consultant_memory_retrieval_context = self._clip_observation_text(
                consultant_memory_retrieval_context,
                CONSULTANT_PROMPT_COMPONENT_BUDGETS["memory_context"],
            )
            input_resolution_context = self._clip_observation_text(
                input_resolution_context,
                CONSULTANT_PROMPT_COMPONENT_BUDGETS["input_resolution"],
            )
            rag_history_text = self._clip_observation_text(
                "\n".join(str(item) for item in rag_history),
                CONSULTANT_PROMPT_COMPONENT_BUDGETS["rag_history"],
            )
            rag_history_for_prompt = [rag_history_text] if rag_history_text else []
            user_preferences_context = self._clip_prompt_text(
                self._load_user_preferences(),
                CONSULTANT_PROMPT_COMPONENT_BUDGETS["user_preferences"],
            )
            consultant_visible_messages = self._step_local_messages(
                messages,
                max_messages=LLM_STEP_CONTEXT_MESSAGES,
                max_chars=LLM_STEP_CONTEXT_CHARS,
            )
            original_goal = str(task_spec.get("goal") or "").strip()
            consultant_visible_messages = [
                message
                for message in consultant_visible_messages
                if not (
                    isinstance(message, HumanMessage)
                    and str(getattr(message, "content", "")).strip() == original_goal
                )
            ]

            consultant_prompt = PromptManager.get_consultant_prompt(
                intent="General Inquiry",
                kb_list_desc=kb_list_desc,
                rag_history=rag_history_for_prompt,
                skills_summary=skills_summary,
                runtime_evidence_context=runtime_evidence_context,
                work_dir=self.pm.work_dir if self.pm else ".",
                max_skills=consultant_max_skills,
                wiki_memory_context=consultant_memory_retrieval_context,
                input_resolution_context=input_resolution_context,
                workflow_catalog=workflow_catalog,
                user_preferences_context=user_preferences_context,
                task_spec_context=format_effective_user_facts_context(
                    task_spec,
                    audience="consultant",
                    max_chars=4500,
                ),
            )
            history_chars = sum(
                len(str(getattr(message, "content", message) or ""))
                for message in consultant_visible_messages
            )
            prompt_chars = len(consultant_prompt) + history_chars
            prompt_components = {
                "total_chars": prompt_chars,
                "system_prompt": len(consultant_prompt),
                "history": history_chars,
                "skill_tree": len(skill_tree),
                "candidate_skill_catalog": len(skill_catalog),
                "candidate_skill_count": len(candidate_skill_ids),
                "workflow_catalog": len(workflow_catalog),
                "runtime_evidence": len(runtime_evidence_context),
                "memory_context": len(consultant_memory_retrieval_context),
                "input_resolution": len(input_resolution_context),
                "rag_history": len(rag_history_text),
                "user_preferences": len(user_preferences_context),
            }
            logger.info(
                "Consultant prompt components: task=%s %s",
                task_id,
                json.dumps(prompt_components, ensure_ascii=False, sort_keys=True),
            )
            if prompt_chars > CONSULTANT_PROMPT_WARNING_CHARS:
                logger.warning(
                    "Consultant prompt exceeds warning budget: task=%s chars=%s warning=%s",
                    task_id,
                    prompt_chars,
                    CONSULTANT_PROMPT_WARNING_CHARS,
                )

            try:
                response = await self._ainvoke_llm(
                    [SystemMessage(content=consultant_prompt)] + consultant_visible_messages,
                    stage="consultant",
                    task_id=task_id,
                    task_mode=str(state.get("task_mode") or "managed"),
                    trace_metadata={
                        "call_kind": "consultant",
                        "task_spec_revision": task_spec_revision(task_spec),
                        "effective_facts_revision": str(state.get("effective_user_facts_revision") or ""),
                        "candidate_skill_ids": candidate_skill_ids,
                        "selected_skill_ids": list(state.get("selected_skills") or []),
                        "retrieval_refs": list(state.get("rag_history") or []),
                        "component_token_counts": {
                            key: max(0, int(value) // 4)
                            for key, value in prompt_components.items()
                            if isinstance(value, int)
                        },
                        "formal_input_audit": input_resolution_context,
                        "user_source_refs": list(state.get("user_source_refs") or []),
                    },
                )
            except Exception as exc:
                failure_summary = llm_failure_summary(exc, "consultant", task_id)
                retry_counts = dict(state.get("llm_retry_counts") or {})
                retry_attempt = int(retry_counts.get("consultant") or 0)
                if is_transient_llm_failure(exc) and not is_llm_input_rejected(exc) and retry_attempt < 1:
                    retry_counts["consultant"] = retry_attempt + 1
                    logger.warning(
                        "Transient Consultant LLM failure; retrying once without changing task state: task=%s attempt=%s",
                        task_id,
                        retry_counts["consultant"],
                    )
                    return {
                        "messages": [
                            SystemMessage(
                                content=(
                                    "<llm_transient_retry>\n"
                                    f"{failure_summary}\n"
                                    "Retry the same Consultant decision once; do not repeat read-only actions.\n"
                                    "</llm_transient_retry>"
                                )
                            )
                        ],
                        "llm_retry_counts": retry_counts,
                        "next_step": "consultant",
                    }
                if is_transient_llm_failure(exc) and not is_llm_input_rejected(exc):
                    question = (
                        "The Consultant LLM connection failed after one bounded retry. "
                        f"{failure_summary} Verify the API/network connection, then resume this task; "
                        "the verified task state and read-only evidence were preserved."
                    )
                    output.print_once(
                        f"consultant_llm_wait:{task_id}",
                        "⚠️ Consultant API connection failed after the bounded retry; task state was preserved for resume.",
                        Fore.YELLOW,
                    )
                    return {
                        "messages": [SystemMessage(content=f"<llm_transient_failure>\n{failure_summary}\n</llm_transient_failure>")],
                        "pending_human_question": {
                            "type": "llm_retry_question",
                            "stage": "consultant",
                            "question": question,
                            "resume_step": "consultant",
                        },
                        "llm_retry_counts": retry_counts,
                        "next_step": "ask_user",
                    }
                response = self._llm_failure_response("consultant", exc, task_id)
                output.print_ai(response.content)
                plan_progress = self._mark_plan_current_blocked(task_id, failure_summary)
                await self._finalize_task_memory(
                    task_id,
                    "failed",
                    messages + [response],
                    {"reason": failure_summary, "plan_progress": plan_progress},
                )
                return {"messages": [response], "plan_progress": plan_progress, "next_step": "finish"}
            content = response.content.strip()
            await self._observe_session_memory(
                task_id,
                "consultant",
                messages + [response],
                {"consultant_output": content[:3000]},
            )
            consultant_json_command = self._consultant_command_from_json(content)
            if consultant_json_command:
                output.print_once(
                    f"consultant_json_action:{task_id}:{consultant_json_command.splitlines()[0][:80]}",
                    "✓ Consultant JSON action parsed and normalized.",
                    Fore.GREEN,
                )
                content = consultant_json_command

            # EXECUTE 动作
            if content.startswith("EXECUTE"):
                if last_force_finish_index >= 0 and not execute_after_force_finish:
                    obs = (
                        "<consultant_validation_error>\n"
                        "A read-only EXECUTE loop guard has already fired in this Consultant turn. "
                        "Do not run another EXECUTE. Use the verified observations already collected "
                        "to FINISH with [SELECTED_SKILLS: ...] or ASK the user for the one unresolved decision.\n"
                        "</consultant_validation_error>"
                    )
                    output.print_once(
                        "consultant_execute_after_force_finish_blocked",
                        "⚠️ Consultant tried another EXECUTE after the read-only loop guard; blocking it and requesting FINISH/ASK.",
                        Fore.YELLOW,
                    )
                    return {"messages": [AIMessage(content=content), SystemMessage(content=obs)], "next_step": "consultant"}
                output.print_node_header("execute")
                code_match = re.search(r"```(?:bash|python)?\n(.*?)```", content, re.DOTALL)
                clean_code = code_match.group(1).strip() if code_match else content.replace("EXECUTE", "").strip()
                output_violation = self._consultant_unbounded_output_violation(clean_code)
                if output_violation:
                    obs = (
                        "<consultant_validation_error>\n"
                        f"{output_violation}\n"
                        "Regenerate one bounded read-only command.\n"
                        "</consultant_validation_error>"
                    )
                    return {
                        "messages": [AIMessage(content=content), SystemMessage(content=obs)],
                        "next_step": "consultant",
                    }

                wait_started = time.perf_counter()
                wait_started_at = timestamp_now()
                user_response = await asyncio.to_thread(
                    self.confirmation_manager.request_confirmation,
                    code=clean_code, language="Bash",
                )
                self._record_human_wait(
                    task_id,
                    "legacy_code_review",
                    wait_started,
                    user_response,
                    {"content": clean_code},
                    started_at=wait_started_at,
                )
                if user_response == 'y':
                    shim_path = self.pm.get_shim_dir_str()
                    stdout, stderr, ret = await run_bash_script(f'export PATH="$PATH:{shim_path}"\n{clean_code}')
                    obs = (
                        f"<observation>\n{'✅' if ret==0 else '❌'}\n"
                        f"{self._clip_observation_text(stdout, 1000)}\n"
                        f"{self._clip_observation_text(stderr, 500)}\n"
                        "</observation>"
                    )
                    output.print_observation(obs)
                    if (
                        ret != 0
                        and self._looks_like_missing_runtime_dependency("\n".join([clean_code, stdout, stderr]))
                        and self._runtime_install_disallowed_by_context(consultant_context)
                    ):
                        return self._consultant_runtime_block_handoff(
                            consultant_context,
                            clean_code,
                            stdout,
                            stderr,
                            task_id,
                            max_skills=consultant_max_skills,
                        )
                else:
                    obs = f"User denied. Feedback: {user_response}" if len(user_response) > 1 else "User denied execution."
                return {"messages": [AIMessage(content=content), SystemMessage(content=obs)], "next_step": "consultant"}

            # SEARCH 动作
            if content.startswith("SEARCH"):
                parts = content.split(" ", 2)
                if len(parts) >= 3:
                    kb_cat, query = parts[1].upper(), parts[2]
                    target_kb = KB_MAP.get(kb_cat, KB_MAP.get("SOP"))
                    output.print_once(f"search:{query[:30]}", f"🔍 Searching [{kb_cat}]: {query}", Fore.CYAN)

                    new_knowledge = "Nothing found."
                    if self.retriever:
                        retrieved = self.retriever.retrieve(query, target_datasets=[target_kb])
                        if retrieved and retrieved.strip() and "Nothing found" not in retrieved:
                            print(f"{Fore.GREEN}📚 RAG: Found relevant content.{Style.RESET_ALL}")
                            new_knowledge = retrieved
                        else:
                            print(f"{Fore.YELLOW}📭 RAG: No results for '{query}'.{Style.RESET_ALL}")
                    else:
                        print(f"{Fore.RED}🔴 RAG: Retriever not initialized.{Style.RESET_ALL}")

                    rag_history.append(f"--- [Search: {kb_cat}] {query} ---\n{new_knowledge}")
                    return {
                        "messages": [AIMessage(content=f"(Searching {kb_cat}...)"),
                                     SystemMessage(content=f"Search Result:\n{new_knowledge}")],
                        "rag_history": rag_history,
                        "next_step": "consultant",
                    }

            if content.startswith("LOAD_WORKFLOW_TEMPLATE"):
                parts = content.split(maxsplit=1)
                workflow_id = parts[1].strip() if len(parts) == 2 else ""
                if content in load_actions:
                    obs = (
                        "<consultant_validation_error>\n"
                        f"The workflow requested by `{content}` is already loaded. Use the loaded template to FINISH or ASK.\n"
                        "</consultant_validation_error>"
                    )
                    return {"messages": [AIMessage(content=content), SystemMessage(content=obs)], "next_step": "consultant"}
                output.print_once(f"consultant_load:{content}", f"📖 Consultant loading workflow template: {workflow_id}", Fore.CYAN)
                loaded = self.workflow_manager.load_workflow_prompt(workflow_id) if self.workflow_manager and workflow_id else ""
                if loaded:
                    obs = f'<loaded_workflow_template workflow_id="{workflow_id}">\n{loaded}\n</loaded_workflow_template>'
                else:
                    obs = (
                        f'<load_error action="LOAD_WORKFLOW_TEMPLATE" workflow_id="{workflow_id or "unknown"}">\n'
                        "Unable to load that workflow template. Use a workflow_id from Available Workflow Templates.\n"
                        "</load_error>"
                    )
                return {
                    "messages": [AIMessage(content=content), SystemMessage(content=obs)],
                    "next_step": "consultant",
                }

            if content.startswith("LOAD_SKILL_DOC"):
                parts = content.split(maxsplit=1)
                skill_id = parts[1].strip() if len(parts) == 2 else ""
                if content in load_actions:
                    skill_only_handoff = self._consultant_skill_only_fallback_handoff(
                        consultant_context=consultant_context,
                        fallback_raw=[skill_id],
                        task_id=task_id,
                        consultant_memory_retrieval_context=consultant_memory_retrieval_context,
                        consultant_wiki_context=consultant_wiki_context,
                        input_resolution_context=input_resolution_context,
                        max_skills=consultant_max_skills,
                    )
                    if skill_only_handoff:
                        skill_only_handoff["input_audit"] = input_resolution_result
                        output.print_once(
                            f"consultant_duplicate_skill_handoff:{skill_id}",
                            (
                                f"✓ Consultant already loaded {skill_id}; "
                                "handing the selected skill and runtime audit to Planner."
                            ),
                            Fore.GREEN,
                        )
                        return skill_only_handoff
                    obs = (
                        "<consultant_validation_error>\n"
                        f"The artifact requested by `{content}` is already loaded. Do not request it again; FINISH or ASK.\n"
                        "</consultant_validation_error>"
                    )
                    return {"messages": [AIMessage(content=content), SystemMessage(content=obs)], "next_step": "consultant"}
                output.print_once(f"consultant_load:{content}", f"📖 Consultant loading skill doc: {skill_id}", Fore.CYAN)
                loaded = self.skill_manager.load_skill(skill_id) if self.skill_manager and skill_id else ""
                if loaded:
                    obs = f'<loaded_skill_doc skill_id="{skill_id}">\n{loaded}\n</loaded_skill_doc>'
                else:
                    obs = (
                        f'<load_error action="LOAD_SKILL_DOC" skill_id="{skill_id or "unknown"}">\n'
                        "Unable to load that skill doc. Use a skill ID from Available Local Skills.\n"
                        "</load_error>"
                    )
                return {
                    "messages": [AIMessage(content=content), SystemMessage(content=obs)],
                    "next_step": "consultant",
                }

            if content.startswith("LOAD_REFERENCE"):
                parts = content.split(maxsplit=2)
                skill_id = parts[1].strip() if len(parts) >= 2 else ""
                ref_path = parts[2].strip() if len(parts) >= 3 else ""
                if content in load_actions:
                    obs = (
                        "<consultant_validation_error>\n"
                        f"The artifact requested by `{content}` is already loaded. Do not request it again; FINISH or ASK.\n"
                        "</consultant_validation_error>"
                    )
                    return {"messages": [AIMessage(content=content), SystemMessage(content=obs)], "next_step": "consultant"}
                output.print_once(f"consultant_load:{content}", f"📖 Consultant loading reference: {skill_id} {ref_path}", Fore.CYAN)
                loaded = self.skill_manager.load_reference(skill_id, ref_path) if self.skill_manager and skill_id and ref_path else ""
                if loaded:
                    obs = (
                        f'<loaded_skill_reference skill_id="{skill_id}" path="{ref_path}">\n'
                        f"{loaded}\n"
                        "</loaded_skill_reference>"
                    )
                else:
                    obs = (
                        f'<load_error action="LOAD_REFERENCE" skill_id="{skill_id or "unknown"}" path="{ref_path}">\n'
                        "Unable to load that reference. Use a reference path listed by the loaded skill doc.\n"
                        "</load_error>"
                    )
                return {
                    "messages": [AIMessage(content=content), SystemMessage(content=obs)],
                    "next_step": "consultant",
                }

            if content.startswith("LOAD_SCRIPT"):
                parts = content.split(maxsplit=2)
                skill_id = parts[1].strip() if len(parts) >= 2 else ""
                script_path = parts[2].strip() if len(parts) >= 3 else ""
                if content in load_actions:
                    obs = (
                        "<consultant_validation_error>\n"
                        f"The artifact requested by `{content}` is already loaded. Do not request it again; FINISH or ASK.\n"
                        "</consultant_validation_error>"
                    )
                    return {"messages": [AIMessage(content=content), SystemMessage(content=obs)], "next_step": "consultant"}
                output.print_once(f"consultant_load:{content}", f"📖 Consultant loading script: {skill_id} {script_path}", Fore.CYAN)
                loaded = self.skill_manager.load_script(skill_id, script_path) if self.skill_manager and skill_id and script_path else ""
                if loaded:
                    obs = (
                        f'<loaded_skill_script skill_id="{skill_id}" path="{script_path}">\n'
                        f"{loaded}\n"
                        "</loaded_skill_script>"
                    )
                else:
                    obs = (
                        f'<load_error action="LOAD_SCRIPT" skill_id="{skill_id or "unknown"}" path="{script_path}">\n'
                        "Unable to load that script. Use a script path listed by the loaded skill doc.\n"
                        "</load_error>"
                    )
                return {
                    "messages": [AIMessage(content=content), SystemMessage(content=obs)],
                    "next_step": "consultant",
                }

            # ASK 动作
            if content.startswith("ASK"):
                question = content.replace("ASK", "").strip()
                return {
                    "messages": [AIMessage(content=content)],
                    "pending_human_question": {
                        "type": "consultant_question",
                        "stage": "consultant",
                        "question": question,
                    },
                    "next_step": "ask_user",
                }

            # FINISH 动作. Some models omit the literal FINISH prefix while still
            # returning a complete structured handoff; accept those robustly.
            looks_like_structured_handoff = (
                "[WORKFLOW_DECISION]" in content
                or "[WORKFLOW_STAGES]" in content
                or "[SELECTED_SKILLS:" in content
            )
            if content.startswith("FINISH") or looks_like_structured_handoff:
                summary = content[len("FINISH"):].strip() if content.startswith("FINISH") else content.strip()
                workflow_handoff = parse_workflow_handoff(summary)
                has_workflow_handoff = bool(
                    workflow_handoff.get("workflow_id")
                    or workflow_handoff.get("workflow_stages")
                )
                if has_workflow_handoff:
                    workflow_handoff = strip_workflow_activation(workflow_handoff)
                active_skills: List[str] = []
                future_skills: List[str] = []
                required_skills_all: List[str] = []
                candidate_skills: List[str] = []
                active_batch_id = ""
                if self.skill_manager:
                    if has_workflow_handoff:
                        validation = validate_workflow_handoff(
                            workflow_handoff,
                            self.skill_manager,
                            self.workflow_manager,
                            max_active_skills=SkillManager.MAX_ACTIVE_SKILLS,
                        )
                        if validation["errors"]:
                            repaired = repair_workflow_handoff(
                                workflow_handoff,
                                self.skill_manager,
                                self.workflow_manager,
                                max_active_skills=SkillManager.MAX_ACTIVE_SKILLS,
                            )
                            if repaired.get("repaired"):
                                validation = {
                                    "errors": repaired.get("errors", []),
                                    "warnings": list(repaired.get("warnings", []))
                                    + [
                                        f"Auto-repaired workflow handoff: {note}"
                                        for note in repaired.get("notes", [])
                                    ],
                                    "handoff": repaired.get("handoff", workflow_handoff),
                                }
                                output.print_once(
                                    f"workflow_autorepair:{len(validation_errors)}",
                                    "🔧 Auto-repaired Consultant workflow handoff using workflow stage metadata.",
                                    Fore.YELLOW,
                                )
                            else:
                                validation["errors"].extend(
                                    f"Auto-repair could not resolve: {note}"
                                    for note in repaired.get("notes", [])
                                    if note
                                )
                        if validation["errors"]:
                            obs = (
                                "<consultant_validation_error>\n"
                                "Workflow handoff is invalid:\n"
                                + "\n".join(f"- {issue}" for issue in validation["errors"])
                                + "\nRepair by choosing one valid path:\n"
                                "1. If a loaded workflow template clearly matches the task, repair WORKFLOW_STAGES using exact template stage names. "
                                "Do not include ACTIVE_SKILLS, FUTURE_SKILLS, active_batch_id, or active_now; Planner owns step scheduling.\n"
                                "2. If no available workflow template clearly matches the task, discard all workflow blocks and FINISH with only "
                                "[SELECTED_SKILLS: canonical_skill_id, ...]. Do not invent workflow_id, WORKFLOW_STAGES, or placeholder stages.\n"
                                "</consultant_validation_error>"
                            )
                            output.print_once(
                                f"workflow_validation:{len(validation_errors)}",
                                "⚠️ Consultant workflow handoff failed validation; asking it to repair the stage coverage.",
                                Fore.YELLOW,
                            )
                            return {"messages": [AIMessage(content=content), SystemMessage(content=obs)], "next_step": "consultant"}
                        workflow_handoff = strip_workflow_activation(validation["handoff"])
                        active_skills = []
                        future_skills = []
                        required_skills_all = workflow_handoff.get("required_skills_all", [])
                        candidate_skills = workflow_handoff.get("candidate_skills", [])
                        active_batch_id = workflow_handoff.get("active_batch_id", "")
                        stage_skills = [
                            str(stage.get("skill") or "").strip()
                            for stage in (workflow_handoff.get("workflow_stages", []) or [])
                            if isinstance(stage, dict)
                            and str(stage.get("status") or "").strip() == "selected_skill"
                            and str(stage.get("skill") or "").strip()
                        ]
                        # Planner needs the complete Consultant-selected pool;
                        # workflow activation is deliberately stripped here.
                        explicit_selected = self.skill_manager.parse_selected_skill_ids(summary)
                        raw_selected = list(dict.fromkeys(
                            [
                                *explicit_selected,
                                *required_skills_all,
                                *stage_skills,
                            ]
                        ))
                        selected = self.skill_manager.normalize_selected_skills(
                            raw_selected,
                            max_results=SkillManager.MAX_SELECTED_SKILLS,
                        )
                        if validation["warnings"]:
                            summary += (
                                "\n[WORKFLOW_VALIDATION_WARNINGS]\n"
                                + "\n".join(f"- {warning}" for warning in validation["warnings"])
                                + "\n[/WORKFLOW_VALIDATION_WARNINGS]"
                            )
                    elif "[SELECTED_SKILLS:" not in summary:
                        obs = (
                            "<consultant_validation_error>\n"
                            "FINISH must include either a coarse workflow handoff ([WORKFLOW_DECISION], [WORKFLOW_STAGES]) or a [SELECTED_SKILLS: ...] marker.\n"
                            "</consultant_validation_error>"
                        )
                        output.print_once(
                            f"consultant_validation:{len(validation_errors)}",
                            "⚠️ Consultant FINISH was missing skill/workflow handoff markers; asking it to repair.",
                            Fore.YELLOW,
                        )
                        return {"messages": [AIMessage(content=content), SystemMessage(content=obs)], "next_step": "consultant"}
                    else:
                        raw_selected = self.skill_manager.parse_selected_skill_ids(summary)
                        selected = self.skill_manager.normalize_selected_skills(raw_selected, max_results=consultant_max_skills)
                        issues = self.skill_manager.validate_selected_skills(raw_selected, max_results=consultant_max_skills) if raw_selected else []
                    if not has_workflow_handoff and issues:
                        repair_context = consultant_context + "\n" + summary
                        repair_raw = self.skill_manager.match_skills(
                            repair_context,
                            max_results=consultant_max_skills,
                        )
                        repair_selected = self.skill_manager.normalize_selected_skills(repair_raw, max_results=consultant_max_skills)
                        repair_hint = (
                            "\nUse these exact canonical skill IDs if they match the request: "
                            + (", ".join(repair_selected) if repair_selected else "None")
                        )
                        obs = (
                            "<consultant_validation_error>\n"
                            + "\n".join(f"- {issue}" for issue in issues)
                            + f"\nRaw selected items: {', '.join(raw_selected) if raw_selected else 'None'}"
                            + repair_hint
                            + "\n</consultant_validation_error>"
                        )
                        output.print_once(
                            f"consultant_validation:{len(validation_errors)}",
                            (
                                "⚠️ Consultant selected invalid skills; "
                                f"raw_selected={raw_selected}; "
                                f"normalized_selected={selected}; "
                                f"issues={issues}; "
                                f"repair_selected={repair_selected or []}"
                            ),
                            Fore.YELLOW,
                        )
                        if repair_selected:
                            runtime_audits, runtime_summary = self._consultant_runtime_preflight(
                                repair_selected,
                                task_id,
                            )
                            repaired_summary = self._repair_consultant_skill_summary(
                                summary,
                                repair_selected,
                                list(raw_selected),
                                list(selected),
                                list(issues),
                            )
                            if runtime_summary:
                                repaired_summary = (repaired_summary.rstrip() + "\n\n" + runtime_summary).strip()
                            output.print_once(
                                f"consultant_auto_repair:{len(validation_errors)}",
                                "✓ Consultant skill selection auto-repaired. Handing to Planner.",
                                Fore.GREEN,
                            )
                            return {
                                "messages": [AIMessage(content="FINISH\n" + repaired_summary)],
                                "input_audit": input_resolution_result,
                                "selected_skills": repair_selected,
                                "selected_skills_raw": list(raw_selected),
                                "skill_selection_source": "consultant_auto_repair",
                                "requirements_summary": repaired_summary,
                                "runtime_audit": runtime_audits,
                                "runtime_status": self._runtime_status_for_task(task_id),
                                "memory_packet_context": self._consultant_handoff_memory_packet(repaired_summary, consultant_memory_retrieval_context),
                                "wiki_memory_context": consultant_wiki_context,
                                "next_step": "planner",
                            }
                        return {"messages": [AIMessage(content=content), SystemMessage(content=obs)], "next_step": "consultant"}
                else:
                    raw_selected = []
                    selected = []
                runtime_audits, runtime_preflight_summary = self._consultant_runtime_preflight(selected, task_id)
                if runtime_preflight_summary:
                    summary = (summary.rstrip() + "\n\n" + runtime_preflight_summary).strip()
                output.print_once("consultant_finish", "✓ Consultant complete. Handing to Planner.", Fore.GREEN)
                return {
                    "messages": [AIMessage(content=content)],
                    "input_audit": input_resolution_result,
                    "selected_skills": selected,
                    "selected_skills_raw": list(raw_selected),
                    "active_skills": active_skills,
                    "future_skills": future_skills,
                    "required_skills_all": required_skills_all,
                    "candidate_skills": candidate_skills,
                    "workflow_id": workflow_handoff.get("workflow_id", ""),
                    "workflow_stages": workflow_handoff.get("workflow_stages", []),
                    "workflow_state": workflow_handoff if has_workflow_handoff else {},
                    "workflow_handoff": workflow_handoff if has_workflow_handoff else {},
                    "active_batch_id": active_batch_id,
                    "runtime_status": self._runtime_status_for_task(task_id),
                    "runtime_audit": runtime_audits,
                    "skill_selection_source": "consultant_finish",
                    "requirements_summary": summary,
                    "memory_packet_context": self._consultant_handoff_memory_packet(summary, consultant_memory_retrieval_context),
                    "wiki_memory_context": consultant_wiki_context,
                    "next_step": "planner",
                }
            obs = (
                "<consultant_validation_error>\n"
                "Consultant response did not use any supported action format. Reply with exactly one "
                "<action_json> block or one legacy command: EXECUTE, SEARCH, LOAD_WORKFLOW_TEMPLATE, "
                "LOAD_SKILL_DOC, LOAD_REFERENCE, LOAD_SCRIPT, ASK, or FINISH.\n"
                "JSON examples: "
                '<action_json>{"action_type":"load_skill_doc","skill_id":"genomics/wgs-short-read-mapping"}</action_json> '
                'or <action_json>{"action_type":"finish","selected_skills":["skill/id"],"summary":"verified context"}</action_json>.\n'
                "If verification is complete, use FINISH with [SELECTED_SKILLS: ...] or a valid workflow handoff. "
                "Use workflow handoff only when a loaded workflow template clearly matches; otherwise use only [SELECTED_SKILLS: ...]. "
                "For workflow handoff, ACTIVE_SKILLS may be empty when Planner should choose the current batch.\n"
                f"Unrecognized response preview:\n{content[:1200]}\n"
                "</consultant_validation_error>"
            )
            output.print_once(
                f"consultant_unrecognized:{len(validation_errors)}",
                "⚠️ Consultant returned an unrecognized response; asking it to repair the command.",
                Fore.YELLOW,
            )
            return {"messages": [AIMessage(content=content), SystemMessage(content=obs)], "next_step": "consultant"}

        async def ask_user(state: AgentState):
            task_id = state.get("task_id", "default")
            pending = state.get("pending_human_question") or {}
            question = str(pending.get("question") or "").strip()
            if not question:
                return {
                    "messages": [SystemMessage(content="No pending human question was found.")],
                    "pending_human_question": {},
                    "next_step": "consultant",
                }
            output.print_once(
                f"consultant_question:{task_id}:{question}",
                f"{Fore.GREEN}🗣️  {question}{Style.RESET_ALL}",
            )
            user_answer = interrupt({
                "type": pending.get("type", "consultant_question"),
                "stage": pending.get("stage", "consultant"),
                "question": question,
            })
            stage = str(pending.get("stage") or "consultant")
            resume_step = str(pending.get("resume_step") or ("planner" if stage == "generate" else "consultant"))
            task_spec = apply_user_feedback(
                self._task_spec_for_state(dict(state), state.get("messages", [])),
                str(user_answer or ""),
                kind=f"{stage}_answer",
                question=question,
                change_type="requirement_change",
                source_task_id=task_id,
                message_origin="user",
                project_root=project_root,
            )
            if stage == "generate" and str(state.get("task_mode") or "managed") == "managed":
                resume_step = "planner"
            user_correction = self._state_update_for_user_skill_correction(str(user_answer or ""), state, task_id)
            plan_state = load_plan_state(self.data_path, task_id)
            running_step = self._running_plan_step(plan_state)
            if self._plan_step_action_is(running_step, "ask_user"):
                plan_progress = self._mark_plan_current_done(
                    task_id,
                    f"user_answered: {str(user_answer or '')[:500]}",
                )
            else:
                plan_progress = self._record_plan_current_evidence(
                    task_id,
                    "user_answered",
                    str(user_answer or "")[:500],
                )
            base_update = {
                "messages": [HumanMessage(
                    content=f"<human_answer stage=\"{stage}\">\nQuestion: {question}\nAnswer: {user_answer}\n</human_answer>",
                    additional_kwargs={"message_origin": "user"},
                )],
                "pending_human_question": {},
                "llm_retry_counts": {},
                "plan_progress": plan_progress,
                "next_step": resume_step,
                "task_spec": task_spec,
                "task_revision": task_spec_revision(task_spec),
            }
            if stage == "recovery":
                base_update.update({
                    "pending_recovery_action": {},
                    "recovery_decision": {},
                    "recovery_phase": "",
                    "recovery_status": "USER_DECISION_RECEIVED",
                    "forbidden_recovery_actions": [],
                })
            if user_correction:
                base_update.update(user_correction)
                base_update["messages"].append(SystemMessage(
                    content=(
                        "User explicitly corrected the active skill/workflow. "
                        "Re-plan using the corrected active skill state; do not reuse the prior incompatible workflow."
                    )
                ))
            return base_update

        # ════════════════════════════════════════════
        # Node 1: Planner（详细规划 + 脚本预生成）
        # ════════════════════════════════════════════
        async def planner(state: AgentState):
            output.print_node_header("planner")
            messages   = state["messages"]
            task_spec = self._task_spec_for_state(dict(state), messages)
            task_revision = task_spec_revision(task_spec)
            task_id = state.get("task_id", "default")
            task_spec_context = format_effective_user_facts_context(
                task_spec,
                audience="planner",
                max_chars=6000,
            )
            task_requirement_text = "\n".join(
                [
                    str(task_spec.get("goal") or ""),
                    *[
                        str(item.get("text") or "")
                        for item in task_spec.get("user_directives", [])
                        if isinstance(item, dict) and str(item.get("kind") or "") != "initial_request"
                    ],
                ]
            ).strip()
            user_msg = self._clip_prompt_text(
                str(task_spec.get("goal") or ""),
                800,
            )
            summary = self._clip_prompt_text(state.get("requirements_summary", ""), 2500)
            rag_history = state.get("rag_history", [])
            rag_knowledge = self._clip_prompt_text(
                str(rag_history[-1]) if rag_history else "",
                1000,
            )

            runtime_audits = list(state.get("runtime_audit", []) or [])
            runtime_audit_context = self._format_runtime_audit(runtime_audits) if runtime_audits else ""
            if not runtime_audit_context:
                legacy_runtime_audit = re.search(
                    r"\[RUNTIME_AUDIT\].*?\[/RUNTIME_AUDIT\]",
                    summary,
                    re.DOTALL,
                )
                runtime_audit_context = legacy_runtime_audit.group(0) if legacy_runtime_audit else ""
            runtime_audit_context = self._clip_prompt_text(runtime_audit_context, 3000)
            selected_skills = state.get("selected_skills", []) or []
            # Planner schedules the Consultant-selected pool. No skill is
            # active until an approved plan step starts.
            active_skills_state: List[str] = []
            planner_context_skills: List[str] = []
            selected_skills_raw = state.get("selected_skills_raw", []) or list(selected_skills)
            skill_selection_source = state.get("skill_selection_source", "consultant_finish")
            workflow_handoff = self._workflow_state_from_mapping(state)
            workflow_handoff = strip_workflow_activation(workflow_handoff)
            active_batch_id = ""
            future_skills: List[str] = []
            required_skills_all = state.get("required_skills_all", []) or workflow_handoff.get("required_skills_all", []) or []
            candidate_skills = state.get("candidate_skills", []) or workflow_handoff.get("candidate_skills", []) or []
            planner_skill_pool = list(selected_skills)
            if not planner_skill_pool and workflow_handoff:
                legacy_stage_skills = [
                    str(stage.get("skill") or "").strip()
                    for stage in workflow_handoff.get("workflow_stages", []) or []
                    if isinstance(stage, dict)
                    and str(stage.get("status") or "").strip() == "selected_skill"
                    and str(stage.get("skill") or "").strip()
                ]
                planner_skill_pool = self.skill_manager.normalize_selected_skills(
                    legacy_stage_skills,
                    max_results=SkillManager.MAX_SELECTED_SKILLS,
                ) if self.skill_manager else []
                if planner_skill_pool:
                    selected_skills = planner_skill_pool
                    skill_selection_source = "legacy_workflow_stage_migration"
            if self.skill_manager and not planner_skill_pool:
                return {
                    "messages": [
                        SystemMessage(
                            content=(
                                "Planner cannot proceed because Consultant did not provide a canonical skill pool. "
                                "Return to Consultant for skill discovery; Planner must not search the full catalog or invent skills."
                            )
                        )
                    ],
                    "next_step": "consultant",
                }
            # Planner may inspect only Consultant-selected skills. It never
            # receives the global catalog and activates skills per plan step.
            planner_context_skills = list(planner_skill_pool)
            if self.skill_manager and planner_skill_pool:
                skill_tree = (
                    "<skill_tree>\n"
                    "Use only the Consultant-selected canonical IDs below; map them explicitly to individual plan steps.\n"
                    "</skill_tree>"
                )
                skill_catalog = self.skill_manager.get_skill_catalog_prompt(planner_skill_pool)
            elif self.skill_manager:
                skill_tree = (
                    "<skill_tree>\n"
                    "No Consultant-selected skill pool was provided. Return to Consultant; do not search the full catalog or invent a skill ID.\n"
                    "</skill_tree>"
                )
                skill_catalog = "<skill_catalog>\nNo Consultant-selected skills available.\n</skill_catalog>"
            else:
                skill_tree = "<skill_tree>\nNo external skills available.\n</skill_tree>"
                skill_catalog = "<skill_catalog>\nNo external skills available.\n</skill_catalog>"
            skills_summary = self._clip_prompt_text(
                f"{skill_tree}\n{skill_catalog}" if self.skill_manager else "None",
                3000,
            )
            skill_reference_index = self.skill_manager.get_skill_reference_index_prompt(planner_context_skills) if self.skill_manager else ""
            skill_execution_context = self._skill_execution_context(planner_context_skills)
            # Consultant selects the skill pool, while Generate loads the full
            # documentation for the running step.  Planner needs only the
            # compact catalog and execution cards; repeating full SKILL.md
            # content here bloats the request without adding scheduling facts.
            active_skill_docs = ""
            skill_reference_index = self._clip_prompt_text(skill_reference_index, 3000)
            skill_execution_context = self._clip_prompt_text(skill_execution_context, 5000)
            selected_skills_text = ", ".join(selected_skills) if selected_skills else "None"
            active_skills_text = ", ".join(active_skills_state) if active_skills_state else "None - Planner must choose the first executable step's ACTIVE_SKILLS"
            workflow_handoff_text = self._clip_prompt_text(
                self._format_workflow_handoff_for_prompt(workflow_handoff),
                1500,
            )
            project_id = state.get("project_id") or self.default_project_id
            planner_wiki_query = "\n".join(
                part for part in [str(user_msg)[:1200], str(summary)[:1200]]
                if part
            )
            planner_wiki_context = state.get("memory_packet_context") or state.get("wiki_memory_context") or ""
            if not planner_wiki_context:
                planner_memory_parts = [
                    self._wiki_memory_stage_context(
                        task_id=task_id,
                        project_id=project_id,
                        query=planner_wiki_query,
                    ),
                    self._pending_archive_buffer_context(
                        task_id=task_id,
                        project_id=project_id,
                        query=planner_wiki_query,
                    ),
                ]
                planner_wiki_context = "\n\n".join(part for part in planner_memory_parts if part)
            planner_wiki_context = self._clip_prompt_text(planner_wiki_context, 1000)

            detailed_plan_prompt = PromptManager.get_detailed_planner_prompt(
                user_msg=str(user_msg),
                task_spec_context=task_spec_context,
                consultant_summary=summary,
                planner_wiki_context=planner_wiki_context,
                runtime_audit_context=runtime_audit_context,
                rag_knowledge=rag_knowledge,
                skills_summary=skills_summary,
                selected_skills_text=selected_skills_text,
                active_skill_docs=active_skill_docs,
                skill_reference_index=skill_reference_index,
                skill_execution_context=skill_execution_context,
                workflow_handoff=workflow_handoff_text,
                active_batch_id=str(active_batch_id or ""),
                active_skills_text=active_skills_text,
                future_skills_text=", ".join(future_skills) if future_skills else "None",
                required_skills_all_text=", ".join(required_skills_all) if required_skills_all else "None",
                candidate_skills_text=", ".join(candidate_skills) if candidate_skills else "None",
                user_preferences_context=self._clip_prompt_text(self._load_user_preferences(), 500),
            )
            planner_prompt_components = {
                "total_chars": len(detailed_plan_prompt),
                "task_spec": len(task_spec_context),
                "consultant_summary": len(summary),
                "runtime_audit": len(runtime_audit_context),
                "memory_context": len(planner_wiki_context),
                "rag_knowledge": len(rag_knowledge),
                "selected_skill_catalog": len(skills_summary),
                "active_skill_docs": len(active_skill_docs),
                "skill_reference_index": len(skill_reference_index),
                "skill_execution_context": len(skill_execution_context),
                "workflow_state": len(workflow_handoff_text),
            }
            logger.info(
                "Planner prompt components: task=%s %s",
                task_id,
                json.dumps(planner_prompt_components, ensure_ascii=False, sort_keys=True),
            )
            if len(detailed_plan_prompt) > LLM_STEP_EPHEMERAL_CHARS:
                logger.warning(
                    "Planner prompt exceeds step-local budget: task=%s chars=%s budget=%s",
                    task_id,
                    len(detailed_plan_prompt),
                    LLM_STEP_EPHEMERAL_CHARS,
                )

            # Persist the Planner input contract before the remote call so a
            # timeout/restart cannot erase Consultant-selected skills or task facts.
            durable_preplan = load_plan_state(self.data_path, task_id)
            durable_preplan["task_spec"] = task_spec
            durable_preplan["selected_skill_ids"] = list(selected_skills)
            if not durable_preplan.get("steps"):
                durable_preplan["generated_from_task_revision"] = task_revision
            save_plan_state(self.data_path, task_id, durable_preplan)

            plan_content = ""
            violations: List[Any] = []
            plan_patch_audits: List[Dict[str, Any]] = []
            resume_audit = state.get("resume_audit") or {}
            stale_step_ids = list(durable_preplan.get("revalidation_required_step_ids") or [])
            missing_fields = list(resume_audit.get("missing_fields") or [])
            local_violations: List[Dict[str, str]] = []
            for missing_field in missing_fields:
                match = re.fullmatch(r"steps\[(\d+)\]\.([A-Za-z_][A-Za-z0-9_]*)", str(missing_field))
                index = int(match.group(1)) if match else -1
                steps = durable_preplan.get("steps") or []
                step_id = str(steps[index].get("step_id") or "") if 0 <= index < len(steps) else ""
                local_violations.append({
                    "step_id": step_id,
                    "field": str(match.group(2) if match else missing_field),
                    "reason": f"Canonical plan validation reported missing field: {missing_field}",
                })
            for step_id in stale_step_ids:
                local_violations.append({
                    "step_id": str(step_id),
                    "field": "",
                    "reason": "This step requires local revalidation after a knowledge or input-state change.",
                })
            plan_patch_requested = self._plan_patch_requested(dict(state))
            if plan_patch_requested and durable_preplan.get("steps"):
                recovery_decision = (
                    state.get("recovery_decision")
                    if isinstance(state.get("recovery_decision"), dict)
                    else {}
                )
                _, repair_step = current_plan_step_with_index(durable_preplan)
                repair_step = repair_step or {}
                requested_step_id = str(
                    state.get("failed_step_id")
                    or recovery_decision.get("failed_step_id")
                    or repair_step.get("step_id")
                    or ""
                )
                known_step_ids = {
                    str(item.get("step_id") or "")
                    for item in durable_preplan.get("steps", []) or []
                    if isinstance(item, dict)
                }
                if requested_step_id not in known_step_ids:
                    requested_step_id = str(repair_step.get("step_id") or "")
                if requested_step_id and not any(
                    str(item.get("step_id") or "") == requested_step_id
                    for item in local_violations
                ):
                    local_violations.append({
                        "step_id": requested_step_id,
                        "field": "",
                        "reason": str(
                            state.get("retry_reason")
                            or recovery_decision.get("reason")
                            or "Controller requested a local PLAN_PATCH for the current step contract."
                        )[:2000],
                    })
            use_local_plan = bool(durable_preplan.get("steps") and local_violations)
            repair_operation = PLAN_OPERATION_LOCAL_REPLAN if stale_step_ids else PLAN_OPERATION_REPAIR

            if use_local_plan:
                candidate_for_repair = durable_preplan
                plan_content = render_structured_plan(candidate_for_repair)
                violations = local_violations
                logger.info(
                    "Planner using %s for task=%s affected=%s instead of CREATE_PLAN",
                    repair_operation,
                    task_id,
                    [item.get("step_id") for item in local_violations],
                )
            else:
                try:
                    response = await self._ainvoke_llm(
                        [HumanMessage(content=detailed_plan_prompt)],
                        stage="planner",
                        task_id=task_id,
                        task_mode=str(state.get("task_mode") or "managed"),
                        trace_metadata={
                            "call_kind": "planner_initial",
                            "task_spec_revision": task_spec_revision(task_spec),
                            "effective_facts_revision": str(state.get("effective_user_facts_revision") or ""),
                            "selected_skill_ids": selected_skills,
                            "runtime_audit_ref": str(
                                Path(self.data_path) / "task_outputs" / task_id / "runtime_audit.json"
                            ),
                        },
                    )
                except Exception as exc:
                    if is_transient_llm_failure(exc) and not is_llm_input_rejected(exc):
                        update = self._pause_retryable_llm_failure(state, task_id, "planner", exc)
                        output.print_ai(str(update["messages"][0].content))
                        return update
                    response = self._llm_failure_response("planner", exc, task_id)
                    output.print_ai(response.content)
                    plan_progress = self._mark_plan_current_blocked(task_id, llm_failure_summary(exc, "planner", task_id))
                    await self._finalize_task_memory(
                        task_id,
                        "failed",
                        messages + [response],
                        {"reason": llm_failure_summary(exc, "planner", task_id), "plan_progress": plan_progress},
                    )
                    return {"messages": [response], "plan_progress": plan_progress, "next_step": "finish"}

                plan_content = self._sanitize_completed_planner_plan(response.content)
                candidate_for_repair = build_plan_state(task_id, plan_content)
                renderer_skill_ids = [
                    skill_id for skill_id in selected_skills
                    if self.skill_manager and self.skill_manager.has_renderer(skill_id)
                ]
                candidate_for_repair, renderer_normalization = collapse_renderer_composite_steps(
                    candidate_for_repair,
                    renderer_skill_ids,
                )
                if renderer_normalization:
                    plan_content = render_structured_plan(candidate_for_repair)
                violations = plan_contract_violations(candidate_for_repair)
                violations.extend(self._skill_plan_violations(
                    plan_content,
                    selected_skills,
                    user_msg=task_requirement_text,
                    workflow_handoff=workflow_handoff,
                ))
                violations.extend(self._plan_step_skill_violations(candidate_for_repair, selected_skills))

            # A failed validation repairs the candidate plan in place.  It does
            # not replay the full Planner prompt or ask the model to regenerate
            # unrelated steps.  The local store validates base version/hash and
            # allowed step scope before applying each patch.
            # One PLAN-owned recovery context may generate at most one local
            # patch. A rejected patch is preserved for explicit escalation;
            # replaying the same Planner prompt cannot create new evidence.
            for _repair_attempt in range(1):
                if not violations:
                    break
                repair_request = build_plan_repair_request(
                    candidate_for_repair,
                    violations,
                    immutable_constraints=[
                        *[str(item) for item in state.get("user_constraints", []) or []],
                        *[str(item) for item in task_spec.get("constraints", []) or []],
                    ],
                    operation=repair_operation,
                )
                effective_revision = resolve_effective_user_facts(task_spec)["effective_facts_revision"]
                repair_request["effective_facts_revision"] = effective_revision
                renderer_routes = [
                    str(item.get("renderer_id") or "")
                    for item in candidate_for_repair.get("steps", []) or []
                    if isinstance(item, dict) and str(item.get("renderer_id") or "").strip()
                ]
                repair_prompt = PromptManager.get_plan_repair_prompt(
                    repair_request,
                    effective_facts_context=task_spec_context,
                    selected_skill_context=(
                        f"selected_skill_ids={json.dumps(selected_skills, ensure_ascii=False)}\n"
                        + skill_execution_context
                    ),
                    renderer_route=json.dumps(renderer_routes, ensure_ascii=False),
                )
                try:
                    recovery_store = RecoveryStateStore(self.data_path, task_id)
                    repair_context_id = str(
                        state.get("recovery_context_digest")
                        or hashlib.sha256(
                            json.dumps(
                                {
                                    "task_id": task_id,
                                    "step_ids": repair_request.get("allowed_step_ids") or [],
                                    "base_plan_hash": repair_request.get("base_plan_hash") or "",
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            ).encode("utf-8")
                        ).hexdigest()[:24]
                    )
                    repair_stage = "planner_patch:" + ",".join(
                        str(item) for item in repair_request.get("allowed_step_ids") or ["task"]
                    )
                    cached_repair = recovery_store.load_llm_result(
                        repair_context_id,
                        repair_stage,
                    )
                    if cached_repair.get("raw_response"):
                        repair_response = AIMessage(content=str(cached_repair["raw_response"]))
                    else:
                        repair_response = await self._ainvoke_llm(
                            [HumanMessage(content=repair_prompt)],
                            stage="planner_repair",
                            task_id=task_id,
                            task_mode=str(state.get("task_mode") or "managed"),
                            trace_metadata={
                                "call_kind": "planner_repair",
                                "parent_call_id": str(
                                    (getattr(self, "_latest_llm_call_refs", {}).get(task_id, [{}])[-1]).get("call_id")
                                    if getattr(self, "_latest_llm_call_refs", {}).get(task_id)
                                    else ""
                                ),
                                "task_spec_revision": task_spec_revision(task_spec),
                                "effective_facts_revision": str(state.get("effective_user_facts_revision") or ""),
                                "selected_skill_ids": selected_skills,
                                "repair_scope": repair_request,
                            },
                        )
                        recovery_store.save_llm_result(
                            repair_context_id,
                            repair_stage,
                            raw_response=str(repair_response.content or ""),
                            parsed_result={},
                        )
                    patch = parse_plan_patch(str(repair_response.content or ""))
                    candidate_for_repair, patch_audit = apply_plan_patch(
                        candidate_for_repair,
                        patch,
                        allowed_step_ids=repair_request.get("allowed_step_ids", []),
                    )
                    patch_audit["effective_facts_revision"] = effective_revision
                    plan_patch_audits.append({**patch_audit, "request": repair_request})
                    if patch_audit.get("no_op"):
                        # Repeating Planner with the same semantic plan cannot
                        # repair an execution action. Preserve the failure
                        # evidence and return ownership to same-step Generate.
                        violations = []
                        break
                except Exception as exc:
                    violations = [*violations, f"plan_patch_rejected: {type(exc).__name__}: {exc}"]
                    break
                plan_content = render_structured_plan(candidate_for_repair)
                violations = plan_contract_violations(candidate_for_repair)
                violations.extend(self._skill_plan_violations(
                    plan_content,
                    selected_skills,
                    user_msg=task_requirement_text,
                    workflow_handoff=workflow_handoff,
                ))
                violations.extend(
                    self._plan_step_skill_violations(candidate_for_repair, selected_skills)
                )

            no_op_plan_patch = bool(plan_patch_audits) and all(
                audit.get("no_op") for audit in plan_patch_audits
            )
            if no_op_plan_patch:
                plan_dir = os.path.join(self.data_path, "task_outputs", task_id)
                os.makedirs(plan_dir, exist_ok=True)
                patch_log = Path(plan_dir) / "plan_patches.jsonl"
                with patch_log.open("a", encoding="utf-8") as handle:
                    for audit in plan_patch_audits:
                        handle.write(json.dumps(audit, ensure_ascii=False, default=str) + "\n")
                _, current_step = current_plan_step_with_index(durable_preplan)
                current_step_id = str((current_step or {}).get("step_id") or "task")
                plan_hash = str(
                    durable_preplan.get("semantic_plan_hash")
                    or plan_patch_audits[-1].get("base_plan_hash")
                    or "unknown"
                )
                signature = f"{current_step_id}:{plan_hash}"
                no_op_count, _ = self._increment_durable_budget(
                    task_id,
                    "no_op_plan_patch_by_signature",
                    signature,
                    limit=1,
                )
                plan_content = str(state.get("current_plan") or "").strip() or render_structured_plan(durable_preplan)
                plan_progress = render_plan_progress(durable_preplan)
                return {
                    "current_plan": plan_content,
                    "plan_progress": plan_progress,
                    "selected_skills": selected_skills,
                    "runtime_audit": runtime_audits,
                    "messages": [SystemMessage(content=(
                        "Planner produced no semantic change for the current approved plan. "
                        "The plan-patch request has been cleared. Keep the plan and repair the "
                        f"execution action for {current_step_id}; do not request the same plan patch again."
                    ))],
                    "pending_action": {},
                    "pending_recovery_action": {
                        "type": "same_step_generate",
                        "reason": "no_op_plan_patch",
                        "step_id": current_step_id,
                        "signature": signature,
                        "count": no_op_count,
                    },
                    "recovery_decision": {},
                    "required_next_action": "SAME_STEP_GENERATE",
                    "recovery_scope": "CURRENT_STEP",
                    "recovery_phase": "apply_repair",
                    "recovery_status": "",
                    "supervisor_verdict": "",
                    "completion_gate_result": {},
                    "next_step": "generate",
                }

            if violations:
                plan_content += (
                    "\n\n## Planner Validation Warnings\n"
                    + "\n".join(f"- {v}" for v in violations)
                    + "\n"
                )

            workflow_handoff = strip_workflow_activation(workflow_handoff)
            active_skills_state = []
            future_skills = []
            active_batch_id = ""

            # 保存计划文件
            plan_dir = os.path.join(self.data_path, "task_outputs", task_id)
            os.makedirs(plan_dir, exist_ok=True)
            plan_file = os.path.join(plan_dir, "plan.md")
            previous_plan_state = load_plan_state(self.data_path, task_id)
            candidate_plan_state = (
                dict(candidate_for_repair)
                if plan_patch_audits
                else build_plan_state(task_id, plan_content, plan_file=plan_file)
            )
            candidate_plan_state["plan_file"] = plan_file
            bind_plan_to_task_revision(candidate_plan_state, task_revision)
            self._plan_step_skill_violations(candidate_plan_state, selected_skills)
            candidate_plan_state["user_constraints"] = list(state.get("user_constraints", []) or [])
            candidate_plan_state["selected_skill_ids"] = list(selected_skills)
            candidate_plan_state["task_spec"] = task_spec
            candidate_plan_state = carry_forward_plan_lifecycle(previous_plan_state, candidate_plan_state)
            if (
                plan_patch_audits
                and all(not audit.get("review_required") for audit in plan_patch_audits)
                and str(previous_plan_state.get("plan_status") or "") == "approved"
            ):
                approve_plan_state(candidate_plan_state, task_revision)
            review_diffs = [
                {
                    "operation": audit.get("operation"),
                    "changed_step_ids": audit.get("changed_step_ids", []),
                    "field_changes": audit.get("field_changes", {}),
                    "reason": (audit.get("request") or {}).get("violations", []),
                }
                for audit in plan_patch_audits
                if audit.get("review_required")
            ]
            candidate_plan_state["pending_review_diff"] = review_diffs
            if plan_patch_audits:
                patch_log = Path(plan_dir) / "plan_patches.jsonl"
                with patch_log.open("a", encoding="utf-8") as handle:
                    for audit in plan_patch_audits:
                        handle.write(json.dumps(audit, ensure_ascii=False, default=str) + "\n")
            revision_result: Dict[str, Any] = {}
            replan_requested = bool(
                previous_plan_state.get("steps")
                and (
                    previous_plan_state.get("stale_step_ids")
                    or previous_plan_state.get("revalidation_required_step_ids")
                    or (state.get("knowledge_conflict_summary") or {}).get("blocking_conflicts")
                )
            )
            if feature_enabled("ENABLE_PLAN_VERSIONING", True) and replan_requested:
                plan_manager = PlanRevisionManager(
                    previous_plan_state,
                    self._knowledge_conflict_root(task_id),
                    task_id=task_id,
                )
                changed_ids = changed_plan_step_ids(previous_plan_state, candidate_plan_state)
                revision = plan_manager.revise(
                    new_plan_state=candidate_plan_state,
                    trigger_conflict_ids=(state.get("knowledge_conflict_summary") or {}).get("blocking_conflicts", []),
                    revision_scope="stage",
                    changed_step_ids=changed_ids,
                    preserved_constraints=list(state.get("user_constraints", []) or []),
                    reason="Planner regenerated the affected plan after a knowledge conflict or stale-step audit.",
                )
                revision_result = revision.to_dict()
                if revision.status == "applied":
                    plan_state = plan_manager.plan_state
                else:
                    # A budget/constraint/oscillation rejection must not let a
                    # newly generated but unapproved plan leak into execution.
                    plan_state = previous_plan_state
                    preserved_plan = str(state.get("current_plan") or "").strip()
                    if preserved_plan:
                        plan_content = preserved_plan
                    else:
                        try:
                            plan_content = Path(plan_file).read_text(encoding="utf-8")
                        except OSError:
                            plan_content = ""
                    plan_content += (
                        "\n\n## Plan Revision Blocked\n"
                        f"- {revision.blocked_reason or 'The proposed revision was not applied.'}\n"
                    )
            else:
                plan_state = candidate_plan_state
                if feature_enabled("ENABLE_PLAN_VERSIONING", True):
                    plan_manager = PlanRevisionManager(
                        plan_state,
                        self._knowledge_conflict_root(task_id),
                        task_id=task_id,
                    )
                    plan_state = plan_manager.plan_state
                    plan_manager.persist_active()
            plan_state["user_constraints"] = list(state.get("user_constraints", []) or [])
            bind_plan_to_task_revision(plan_state, task_revision)
            save_plan_state(self.data_path, task_id, plan_state)
            with open(plan_file, "w", encoding="utf-8") as f:
                f.write(
                    "# Task Plan\n"
                    f"Generated: {datetime.now().isoformat()}\n"
                    f"Selected Skills: {', '.join(selected_skills) if selected_skills else 'None'}\n"
                    f"Workflow ID: {workflow_handoff.get('workflow_id', 'None') if workflow_handoff else 'None'}\n"
                    f"Active Batch: {active_batch_id or 'None'}\n"
                    f"Active Skills: {', '.join(active_skills_state) if active_skills_state else 'None'}\n"
                    f"Future Skills: {', '.join(future_skills) if future_skills else 'None'}\n"
                    f"Required Skills All: {', '.join(required_skills_all) if required_skills_all else 'None'}\n"
                    f"Selected Skills Raw: {', '.join(selected_skills_raw) if selected_skills_raw else 'None'}\n"
                    f"Skill Selection Source: {skill_selection_source}\n\n"
                    f"{plan_content}"
                )
            update_plan_markdown_status(plan_file, plan_state)
            plan_progress = render_plan_progress(plan_state)

            conflict_update = self._knowledge_conflict_update(
                task_id,
                {
                    **dict(state),
                    "current_plan": plan_content,
                    "user_constraints": state.get("user_constraints", []),
                },
                phase="plan_generated",
                plan_state=plan_state,
            )
            if revision_result:
                conflict_update["plan_revision"] = revision_result

            # 写入思维链日志
            log_plan_to_cot(self.supervisor_node, task_id, plan_content)
            await self._observe_session_memory(
                task_id,
                "planner",
                messages + [AIMessage(content=plan_content)],
                {
                    "plan_file": plan_file,
                    "selected_skills": selected_skills,
                    "runtime_audit": runtime_audits,
                    "active_skills": active_skills_state,
                    "future_skills": future_skills,
                    "required_skills_all": required_skills_all,
                    "workflow_id": workflow_handoff.get("workflow_id", "") if workflow_handoff else "",
                "active_batch_id": active_batch_id,
                "skill_selection_source": skill_selection_source,
                "plan_progress": plan_progress,
                "task_spec": task_spec,
                "task_revision": task_revision,
                "knowledge_conflicts": conflict_update.get("knowledge_conflicts", []),
                "knowledge_conflict_summary": conflict_update.get("knowledge_conflict_summary", {}),
                "plan_version": conflict_update.get("plan_version", plan_state.get("plan_version", "plan_v1")),
                "plan_stale_steps": conflict_update.get("plan_stale_steps", []),
                "plan_revalidation_steps": conflict_update.get("plan_revalidation_steps", []),
                "plan": plan_content[:4000],
            },
        )

            return {
                "current_plan": plan_content,
                "plan_progress": plan_progress,
                "selected_skills": selected_skills,
                "runtime_audit": runtime_audits,
                "selected_skills_raw": selected_skills_raw,
                "active_skills": active_skills_state,
                "future_skills": future_skills,
                "required_skills_all": required_skills_all,
                "candidate_skills": candidate_skills,
                **conflict_update,
                "workflow_state": workflow_handoff,
                "workflow_handoff": workflow_handoff,
                "workflow_stages": workflow_handoff.get("workflow_stages", []) if workflow_handoff else [],
                "workflow_id": workflow_handoff.get("workflow_id", "") if workflow_handoff else "",
                "active_batch_id": active_batch_id,
                "skill_selection_source": skill_selection_source,
                "messages": [AIMessage(content=f"📋 Plan ready (saved to {plan_file})")],
                "pending_action": {},
                "pending_recovery_action": {},
                "recovery_decision": {},
                "required_next_action": "",
                "failure_owner": "",
                "recovery_scope": "",
                "recovery_phase": "",
                "recovery_status": "",
                "supervisor_verdict": "",
                "completion_gate_result": {},
                "next_step": "plan_review",
            }

        # ════════════════════════════════════════════
        # Node 1.5: Plan Review（带记忆状态展示）
        # ════════════════════════════════════════════
        async def plan_review(state: AgentState):
            output.print_node_header("plan_review")
            plan    = state.get("current_plan", "")
            task_id = state.get("task_id", "default")
            messages = state.get("messages", [])
            task_revision = self._task_revision_for_state(dict(state))
            persisted_plan = load_plan_state(self.data_path, task_id)
            if plan_approval_is_current(persisted_plan, task_revision):
                plan_progress = render_plan_progress(persisted_plan)
                logger.info(
                    "Plan Review skipped for unchanged approved plan: task=%s plan_revision=%s semantic_hash=%s",
                    task_id,
                    persisted_plan.get("plan_revision"),
                    str(persisted_plan.get("semantic_plan_hash") or "")[:16],
                )
                return {
                    "messages": [SystemMessage(content="<control_event type=\"plan_approval_reused\" />")],
                    "plan_progress": plan_progress,
                    "next_step": "generate",
                }

            review_diff = persisted_plan.get("pending_review_diff") or []
            review_content = (
                "## Plan Change Review\n\n"
                + json.dumps(review_diff, ensure_ascii=False, indent=2, default=str)
                if review_diff else plan
            )

            # 显示内存状态
            mem_status = self.memory_manager.status(messages)
            print(f"\n📊 Memory: {mem_status}")

            # 展示计划（限制行数避免刷屏）
            wait_started = time.perf_counter()
            wait_started_at = timestamp_now()
            print(f"\n{Fore.CYAN}{'='*60}")
            print(f"  📋  PLAN REVIEW  |  Task: {task_id}")
            print(f"{'='*60}{Style.RESET_ALL}")
            output.page_text(f"Plan Review | Task: {task_id}", review_content, Fore.CYAN)
            print(f"{'─'*60}")

            try:
                user_input = self._prompt_user(
                    task_id,
                    "plan_review",
                    f"\n{Fore.YELLOW}Approve? [y/n/q] or type feedback > {Style.RESET_ALL}",
                    payload={"content": review_content[:2000]},
                    started_at=wait_started_at,
                ).strip()
            except HumanInputUnavailable as exc:
                self._record_human_wait(
                    task_id,
                    "plan_review",
                    wait_started,
                    f"<waiting_user:{type(exc).__name__}>",
                    {"content": review_content[:2000]},
                    started_at=wait_started_at,
                )
                return await self._suspend_for_human_input(
                    task_id,
                    "plan_review",
                    messages,
                    {"content": review_content[:2000]},
                    type(exc).__name__,
                )
            self._record_human_wait(
                task_id,
                "plan_review",
                wait_started,
                user_input,
                {"content": plan[:2000]},
                started_at=wait_started_at,
            )

            il = user_input.lower()

            if il in ["y", "yes", "approve", ""]:
                print(f"{Fore.GREEN}✓ Plan approved. Executing...{Style.RESET_ALL}")
                plan_state = self._ensure_plan_tracker(task_id, plan)
                approve_plan_state(plan_state, task_revision)
                plan_progress = self._persist_plan_tracker(task_id, plan_state) if plan_state.get("steps") else render_plan_progress(plan_state)
                approval_event = SystemMessage(
                    content=(
                        "<control_event type=\"plan_approved\" "
                        f"plan_revision=\"{int(plan_state.get('plan_revision') or 1)}\" "
                        f"semantic_hash=\"{str(plan_state.get('approved_plan_hash') or '')}\" />"
                    )
                )
                await self._observe_session_memory(
                    task_id,
                    "plan_review",
                    messages + [approval_event],
                    {"decision": "approved", "plan_excerpt": plan[:2000], "plan_progress": plan_progress},
                )
                return {
                    "messages": [approval_event],
                    "plan_progress": plan_progress,
                    "next_step": "generate",
                }
            elif il in ["q", "quit", "exit"]:
                print(f"{Fore.MAGENTA}🛑 Task cancelled.{Style.RESET_ALL}")
                await self._finalize_task_memory(
                    task_id,
                    "cancelled",
                    messages + [HumanMessage(content="Cancelled.")],
                    {"decision": "cancelled_at_plan_review"},
                )
                return {"messages": [HumanMessage(content="Cancelled.")], "next_step": "finish"}
            else:
                feedback = user_input if il not in ["n", "no"] else ""
                if not feedback:
                    feedback_wait_started = time.perf_counter()
                    feedback_started_at = timestamp_now()
                    try:
                        feedback = self._prompt_user(
                            task_id,
                            "plan_review_feedback",
                            f"{Fore.YELLOW}Feedback (Enter=auto-retry): {Style.RESET_ALL}",
                            payload={"content": plan[:2000]},
                            started_at=feedback_started_at,
                        ).strip()
                    except HumanInputUnavailable as exc:
                        self._record_human_wait(
                            task_id,
                            "plan_review_feedback",
                            feedback_wait_started,
                            f"<waiting_user:{type(exc).__name__}>",
                            {"content": plan[:2000]},
                            started_at=feedback_started_at,
                        )
                        return await self._suspend_for_human_input(
                            task_id,
                            "plan_review_feedback",
                            messages,
                            {"content": plan[:2000]},
                            type(exc).__name__,
                        )
                    if not feedback:
                        feedback = "Rejected. Try a different approach."
                print(f"{Fore.CYAN}📡 Feedback captured: {feedback}{Style.RESET_ALL}")
                task_spec = apply_user_feedback(
                    self._task_spec_for_state(dict(state), messages),
                    feedback,
                    kind="plan_review_feedback",
                    question="Revise the proposed plan",
                    change_type=classify_user_feedback_change("plan_review_feedback", feedback),
                    source_task_id=task_id,
                    message_origin="user",
                    project_root=project_root,
                )
                reset_plan_state(self.data_path, task_id)
                await self._observe_session_memory(
                    task_id,
                    "plan_review",
                    messages + [HumanMessage(content=f"Plan rejected. Feedback: {feedback}")],
                    {"decision": "rejected", "feedback": feedback, "plan_excerpt": plan[:2000]},
                )
                return {
                    "messages": [HumanMessage(
                        content=f"Plan rejected. Feedback: {feedback}",
                        additional_kwargs={"message_origin": "user"},
                    )],
                    "current_plan": None,
                    "selected_skills": [],
                    "selected_skills_raw": [],
                    "active_skills": [],
                    "future_skills": [],
                    "candidate_skills": [],
                    "workflow_state": {},
                    "workflow_handoff": {},
                    "workflow_stages": [],
                    "workflow_id": "",
                    "active_batch_id": "",
                    "skill_selection_source": "",
                    "plan_progress": "",
                    "task_spec": task_spec,
                    "task_revision": task_spec_revision(task_spec),
                    "next_step": "consultant",
                }

        # ════════════════════════════════════════════
        # Node 2: Generate
        # ════════════════════════════════════════════
        generate_call_audit: Dict[str, bool] = {}

        async def _generate_impl(state: AgentState):
            output.print_node_header("generate")
            messages         = state["messages"]
            current_plan     = state.get("current_plan", "No plan.")
            task_id          = state.get("task_id", "unknown")

            generate_validation = self._validate_generate_context(state, task_id)
            if not generate_validation.get("valid"):
                return self._generate_plan_redirect(state, task_id, generate_validation)

            resume_plan_material = self._resume_plan_material(task_id, str(current_plan or ""))
            sanitized_current_plan = self._sanitize_completed_planner_plan(resume_plan_material)
            if sanitized_current_plan != resume_plan_material:
                current_plan = sanitized_current_plan
                self._replace_plan_tracker_with_plan(
                    task_id,
                    current_plan,
                    reason="generate replaced completed-task plan with FINISH-only plan",
                )

            plan_progress = self._start_or_load_plan_progress(
                task_id,
                current_plan,
                evidence="entered generate after compression check",
            )
            step_skill_update = (
                self._active_skill_update_for_current_plan_step(state, task_id)
                if str(state.get("task_mode") or "managed").lower() == "managed"
                else {}
            )
            if step_skill_update:
                state = dict(state)
                state.update(step_skill_update)
                activation_error = str(step_skill_update.get("skill_activation_error") or "").strip()
                if activation_error:
                    redirect = self._generate_plan_redirect(
                        state,
                        task_id,
                        {
                            "valid": False,
                            "reason_code": "invalid_step_skill_mapping",
                            "missing_fields": [
                                str(step_skill_update.get("skill_activation_error_field") or "current_step.skill_ids")
                            ],
                            "recoverable": True,
                            "validation_function": "_active_skill_update_for_current_plan_step",
                        },
                        message=activation_error,
                    )
                    redirect.update({
                        "active_skills": [],
                        "future_skills": [],
                        "skill_activation_error": activation_error,
                    })
                    return redirect
                plan_progress = self._record_plan_current_evidence(
                    task_id,
                    "active_skills_derived_from_plan_step",
                    ", ".join(step_skill_update.get("active_skills", [])),
                )
            renderer_update = self._registered_renderer_action_update(state, task_id)
            if renderer_update:
                renderer_update.setdefault("plan_progress", render_plan_progress(load_plan_state(self.data_path, task_id)))
                renderer_update.setdefault("active_skills", state.get("active_skills", []))
                return renderer_update
            projected_renderer_update = self._projected_renderer_action_update(state, task_id)
            if projected_renderer_update:
                projected_renderer_update.setdefault(
                    "plan_progress",
                    render_plan_progress(load_plan_state(self.data_path, task_id)),
                )
                projected_renderer_update.setdefault("active_skills", state.get("active_skills", []))
                return projected_renderer_update
            if self._current_plan_step_requests_finish(task_id):
                solution_text = self._finish_solution_from_plan(str(current_plan or ""))
                response = AIMessage(content=f"<solution>{solution_text}</solution>")
                output.print_ai(response.content)
                can_finish, plan_progress, final_status, gate_feedback = self._prepare_final_solution_progress(
                    task_id,
                    solution_text,
                    str(current_plan or ""),
                )
                if not can_finish:
                    gate_message = (
                        "Final solution validation failed.\n\n"
                        f"{gate_feedback}\n\n"
                        "Do not execute compatibility copies or symlinks to appease validation. "
                        "Repair the validation context or report the real blocker."
                    )
                    return {
                        "messages": [response, SystemMessage(content=gate_message)],
                        "plan_progress": plan_progress,
                        "next_step": "generate",
                    }
                await self._finalize_task_memory(
                    task_id,
                    final_status,
                    messages + [response],
                    {"solution": solution_text[:3000], "plan_progress": plan_progress},
                )
                return {"messages": [response], "plan_progress": plan_progress, "next_step": "finish"}
            # 熔断（连续 5 次 AI 消息含错误）
            recent_errors = sum(
                1 for m in messages[-8:]
                if isinstance(m, AIMessage) and
                any(k in str(m.content) for k in ["❌", "Failed", "Error"])
            )
            if recent_errors >= 5:
                plan_progress = self._record_plan_current_evidence(task_id, "system_aborted", "Too many recent errors.")
                return {
                    "messages": [AIMessage(content="<solution>❌ System Aborted: Too many errors.</solution>")],
                    "plan_progress": plan_progress,
                    "next_step": "finish",
                }

            # 技能加载
            loaded_skills = ""
            if self.skill_manager and current_plan:
                selected = state.get("active_skills", []) or []
                if selected:
                    loaded = self.skill_manager.load_skills_for_prompt(selected)
                    if loaded:
                        loaded_parts = [
                            loaded.replace("<active_skill_docs>", "").replace("</active_skill_docs>", "").strip()
                        ]
                        ref_index = self.skill_manager.get_skill_reference_index_prompt(selected)
                        if ref_index:
                            loaded_parts.append(
                                ref_index.replace("<skill_reference_index>", "### Skill Reference Index")
                                .replace("</skill_reference_index>", "")
                                .strip()
                            )
                        execution_cards = self._skill_execution_context(selected)
                        if execution_cards:
                            loaded_parts.append(execution_cards)
                        loaded_skills = (
                            "<active_skills_context>\n"
                            + "\n\n".join(loaded_parts)
                            + "\n</active_skills_context>"
                        )
                        self.profiler.record_skill_usage(
                            task_id,
                            selected,
                            usage_type="loaded_for_generate",
                            node="generate",
                            detail="Active skill docs loaded into Generate context.",
                        )

            lightweight_policy = self._lightweight_task_policy(str(state.get("task_mode") or ""))
            if lightweight_policy:
                loaded_skills = (loaded_skills + "\n\n" + lightweight_policy).strip()

            # Build step-local memory. Broad retrieval stays in Consultant; Generate only retrieves on triggers.
            last_msg_content = str(messages[-1].content) if messages else ""
            step_plan_context = self._current_step_prompt_context(
                task_id,
                current_plan=str(current_plan or ""),
                plan_progress=plan_progress,
                state=state,
            )
            md_query = "\n".join(
                part for part in [last_msg_content[:600], step_plan_context[:1200]]
                if part
            )
            session_id = state.get("session_id") or task_id
            project_id = state.get("project_id") or self.default_project_id
            memory_packet_context = state.get("memory_packet_context", "") or ""
            on_demand_memory = await self._generate_on_demand_memory_contexts(
                state=state,
                messages=messages,
                task_id=task_id,
                project_id=project_id,
                query=md_query,
                memory_packet_context=memory_packet_context,
                supervisor_verdict="",
            )
            task_summary_context = on_demand_memory["task_summary_context"]
            archive_context = on_demand_memory["archive_context"]
            wiki_context = on_demand_memory["wiki_context"]
            wiki_digest = on_demand_memory["wiki_digest"]
            task_summary_context = dict(task_summary_context or {})

            # RecoveryPolicy, not the compatibility Supervisor verdict, owns
            # repair context and routing.
            supervisor_instr = ""
            recovery_instr = ""
            recovery_context_obj: Optional[RecoveryContext] = None
            recovery_action = state.get("pending_recovery_action") or {}
            recovery_decision = state.get("recovery_decision") or {}
            if recovery_action or recovery_decision:
                recovery_store = RecoveryStateStore(self.data_path, task_id)
                recovery_history = recovery_store.load_attempts()
                recovery_history.extend(recovery_store.load_execution_records())
                recovery_state = {
                    **dict(state),
                    "recovery_decision": recovery_decision,
                    "recovery_progress": (
                        state.get("recovery_progress")
                        or recovery_action.get("progress")
                        or {}
                    ),
                }
                renderer_protocol = self._renderer_action_protocol(recovery_state, task_id)
                recovery_context_obj = build_recovery_context(
                    recovery_state,
                    dict(state.get("execution_evidence") or {}),
                    plan_state=load_plan_state(self.data_path, task_id),
                    task_root=Path(self.data_path) / "task_outputs" / task_id,
                    skill_policy={
                        "skill_ids": list(state.get("active_skills") or []),
                        "relevant_context": self._clip_observation_text(loaded_skills, 3000),
                    },
                    recovery_history=recovery_history,
                )
                canonical_step = build_current_step_context(
                    recovery_state,
                    load_plan_state(self.data_path, task_id),
                    allowed_actions=renderer_protocol.get("allowed_actions") or None,
                ).to_dict()
                recovery_context_obj.current_step = canonical_step
                recovery_context_obj.allowed_actions = list(
                    canonical_step.get("allowed_actions") or []
                )
                recovery_context_obj.recovery_decision = dict(recovery_decision)
                recovery_store.save_diagnostic_context(recovery_context_obj)
                state = dict(state)
                state["recovery_context"] = recovery_context_obj.to_dict()
                state["recovery_context_digest"] = recovery_context_obj.context_id
                state["current_step_context"] = canonical_step
                recovery_payload = recovery_context_obj.prompt_view()
                # CurrentStepContext has its own canonical block in this same
                # prompt. Keeping it here would create a second, potentially
                # divergent current-step source.
                recovery_payload.pop("current_step", None)
                recovery_instr = (
                    "<recovery_context>\n"
                    + json.dumps(
                        recovery_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                    + "\n</recovery_context>"
                )
            conflict_summary = state.get("knowledge_conflict_summary") or {}
            if conflict_summary:
                conflict_payload = self._clip_observation_text(
                    json.dumps(
                        {
                            "summary": conflict_summary,
                            "conflicts": (state.get("knowledge_conflicts") or [])[-6:],
                            "plan_version": state.get("plan_version") or "plan_v1",
                            "stale_steps": state.get("plan_stale_steps") or [],
                            "revalidation_steps": state.get("plan_revalidation_steps") or [],
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                    4000,
                )
                supervisor_instr += (
                    "\n<knowledge_conflict_context>\n"
                    + conflict_payload
                    + "\nDo not silently edit a hard constraint or execute a stale step. "
                    "If a probe is required, emit one concrete targeted probe; if a plan revision is required, "
                    "request Planner rather than inventing a new workflow inside Generate.\n</knowledge_conflict_context>\n"
                )
            task_mode = str(state.get("task_mode") or "").strip().lower()
            stable_record = self._get_or_create_stable_prompt(session_id, project_id)
            stable_sys = state.get("stable_system_prompt") or stable_record["prompt"]
            compact_projection = False
            try:
                ephemeral_context, context_budget_report = self._ephemeral_prompt_context_result(
                    state=state,
                    messages=messages,
                    current_plan=step_plan_context,
                    loaded_skills=loaded_skills,
                    task_summary_context=task_summary_context,
                    memory_packet_context=memory_packet_context,
                    wiki_context=wiki_context,
                    wiki_digest=wiki_digest,
                    archive_context=archive_context,
                    supervisor_context=supervisor_instr,
                    recovery_context=recovery_instr,
                    plan_progress="PlanTracker owns status; use CurrentStepContext.",
                )
            except RequiredContextOverflow as exc:
                report = dict(getattr(exc, "report", {}) or {})
                self.profiler.record_context_budget(task_id, report, node="generate")
                plan_progress = self._record_plan_current_evidence(
                    task_id,
                    "prompt_assembly_failure_compact_retry",
                    str(exc),
                )
                try:
                    ephemeral_context, context_budget_report = self._compact_generate_context_result(
                        state=state,
                        messages=messages,
                        current_plan=step_plan_context,
                        loaded_skills=loaded_skills,
                        supervisor_context=supervisor_instr,
                        recovery_context=recovery_instr,
                    )
                    compact_projection = True
                except RequiredContextOverflow as compact_exc:
                    compact_report = dict(getattr(compact_exc, "report", {}) or {})
                    compact_report["projection_mode"] = "compact_required_state_failed"
                    self.profiler.record_context_budget(task_id, compact_report, node="generate")
                    reason = f"PROMPT_ASSEMBLY_FAILURE: {compact_exc}"
                    plan_progress = self._record_plan_current_evidence(
                        task_id,
                        "internal_prompt_assembly_failure",
                        reason,
                    )
                    failed_state = load_plan_state(self.data_path, task_id)
                    set_task_status(failed_state, TASK_STATUS_FAILED, reason)
                    save_plan_state(self.data_path, task_id, failed_state)
                    return {
                        "messages": [AIMessage(content=f"<observation>{reason}</observation>")],
                        "context_budget_report": compact_report,
                        "plan_progress": plan_progress,
                        "pending_action": {},
                        "pending_human_question": {},
                        "task_status": TASK_STATUS_FAILED,
                        "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                        "recovery_status": "INTERNAL_PROTOCOL_ERROR",
                        "pending_recovery_action": {
                            "type": "internal_protocol_error",
                            "reason": reason,
                            "failure_scope": "CURRENT_STEP",
                            "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                        },
                        "next_step": "finish",
                        "generate_result": "BLOCKED",
                        "generate_exit_reason": "prompt_assembly_internal_error",
                    }
            self.profiler.record_context_budget(task_id, context_budget_report, node="generate")
            visible_messages = (
                []
                if compact_projection
                else self._generate_visible_messages(
                    messages,
                    structured_recovery=bool(recovery_context_obj),
                )
            )
            original_goal = str(
                self._task_spec_for_state(dict(state), messages).get("goal") or ""
            ).strip()
            visible_messages = [
                message
                for message in visible_messages
                if not (
                    isinstance(message, HumanMessage)
                    and str(getattr(message, "content", "")).strip() == original_goal
                )
            ]
            call_msgs = [SystemMessage(content=stable_sys + "\n\n" + ephemeral_context)] + visible_messages
            generate_response_cached = False
            try:
                recovery_store = RecoveryStateStore(self.data_path, task_id)
                recovery_stage = str(
                    recovery_decision.get("recommended_action")
                    or recovery_action.get("type")
                    or ""
                ).strip().upper()
                cached_generate = (
                    recovery_store.load_llm_result(
                        recovery_context_obj.context_id,
                        f"generate:{recovery_stage or 'RECOVERY'}",
                    )
                    if recovery_context_obj
                    else {}
                )
                if cached_generate.get("raw_response"):
                    generate_response_cached = True
                    response = AIMessage(content=str(cached_generate["raw_response"]))
                    logger.info(
                        "Generate reused attempt-scoped result: task=%s context_id=%s stage=%s",
                        task_id,
                        recovery_context_obj.context_id,
                        recovery_stage or "RECOVERY",
                    )
                else:
                    generate_call_audit[str(task_id)] = True
                    response = await self._ainvoke_llm(
                        call_msgs,
                        stage="generate",
                        task_id=task_id,
                        task_mode=task_mode,
                        trace_metadata={
                            "call_kind": "generate",
                            "task_spec_revision": self._task_revision_for_state(dict(state)),
                            "effective_facts_revision": str(state.get("effective_user_facts_revision") or ""),
                            "step_id": str(current_step_id or ""),
                            "selected_skill_ids": active_skills,
                            "component_token_counts": dict(
                                (context_budget_report or {}).get("component_token_counts") or {}
                            ),
                            "truncation_manifest": list(
                                (context_budget_report or {}).get("truncation_manifest") or []
                            ),
                            "action_contract_ref": str(
                                Path(self.data_path) / "task_outputs" / task_id / "plan_state.json"
                            ),
                        },
                    )
                    if recovery_context_obj:
                        recovery_store.save_llm_result(
                            recovery_context_obj.context_id,
                            f"generate:{recovery_stage or 'RECOVERY'}",
                            raw_response=str(response.content or ""),
                            parsed_result={},
                        )
            except Exception as exc:
                response = self._llm_failure_response("generate", exc, task_id)
                content = response.content
                output.print_ai(content)
                plan_progress = self._mark_plan_current_blocked(task_id, llm_failure_summary(exc, "generate", task_id))
                await self._finalize_task_memory(
                    task_id,
                    "failed",
                    messages + [response],
                    {"reason": llm_failure_summary(exc, "generate", task_id), "plan_progress": plan_progress},
                )
                return {"messages": [response], "plan_progress": plan_progress, "next_step": "finish"}
            content   = response.content

            # 打印（去重）
            output.print_ai(content)

            # 解析
            search_match  = re.search(r"<search>(.*?)</search>", content, re.DOTALL)
            install_match = re.search(r"<install_tool.*?>.*?</install_tool>", content, re.DOTALL)
            execute_match = re.search(r"<execute>(.*?)</execute>", content, re.DOTALL)
            ask_user_match = re.search(r"<ask_user>(.*?)</ask_user>", content, re.DOTALL)
            answer_match  = re.search(r"<solution>(.*?)</solution>", content, re.DOTALL)
            activate_match = re.search(r"<activate_batch\s+batch_id=[\"']?([^\"'\s>/]+)[\"']?\s*/?>", content, re.DOTALL)
            structured_action = self._extract_structured_action(content)
            renderer_contract_error: Dict[str, Any] = {}
            if str(structured_action.get("type") or "") == "renderer_invocation":
                materialized_renderer = self._materialize_renderer_invocation(
                    structured_action,
                    state,
                    task_id,
                )
                if str(materialized_renderer.get("type") or "") == "renderer_contract_error":
                    renderer_contract_error = dict(materialized_renderer.get("contract_error") or {})
                    structured_action = {}
                else:
                    structured_action = materialized_renderer
            if structured_action and str(structured_action.get("type") or "") not in {
                "step_completion_candidate", "plan_patch", "blocked"
            }:
                structured_action = self._bind_pending_action(structured_action, state, task_id)
            action_type = str(structured_action.get("type") or "").strip()
            has_execute_body = bool(
                (execute_match and execute_match.group(1).strip())
                or (action_type == "execute" and str(structured_action.get("code") or "").strip())
            )
            plain_final_solution = (
                not any([search_match, install_match, execute_match, ask_user_match, activate_match])
                and not structured_action
                and self._looks_like_plain_final_solution(content)
            )

            generated_action = (
                "finish" if answer_match or action_type == "solution" or plain_final_solution else
                "ask_user" if ask_user_match or action_type == "ask_user" else
                "activate_batch" if activate_match or action_type == "activate_batch" else
                "install" if install_match or action_type == "install_tool" else
                "execute" if has_execute_body else
                "search" if search_match or action_type == "search" else
                "step_completion_candidate" if action_type == "step_completion_candidate" else
                "plan_patch" if action_type == "plan_patch" else
                "blocked" if action_type == "blocked" else
                "continue"
            )
            if generated_action != "continue" and not generate_response_cached:
                current_state = load_plan_state(self.data_path, task_id)
                _, counted_step = current_plan_step_with_index(current_state)
                self._record_generate_action(task_id, str(counted_step.get("step_id") or "lightweight"))
            await self._observe_session_memory(
                task_id,
                "generate",
                messages + [response],
                {
                    "action": generated_action,
                    "content": content[:3000],
                    "current_plan_excerpt": str(current_plan)[:1200],
                    "plan_progress": plan_progress,
                },
            )
            if task_mode in {"readonly", "simple_execute"} and generated_action in {"install", "activate_batch", "search"}:
                plan_progress = self._record_plan_current_evidence(
                    task_id,
                    "lightweight_disallowed_action",
                    f"{task_mode} task emitted disallowed action: {generated_action}",
                )
                repair = (
                    f"{task_mode} task mode forbids `{generated_action}`. Do not install tools, activate workflow batches, "
                    "or search memory. For this lightweight request, output exactly one <execute> block with the explicit "
                    "read-only/check command requested by the user, one <ask_user> if a detail is missing, or one "
                    "<solution> explaining the observed state."
                )
                return {
                    "messages": [response, SystemMessage(content=repair)],
                    "plan_progress": plan_progress,
                    "next_step": "generate",
                    "pending_action": {},
                }
            if answer_match or action_type == "solution" or plain_final_solution:
                solution_text = (
                    answer_match.group(1).strip()
                    if answer_match
                    else str(structured_action.get("text") or "").strip()
                    if action_type == "solution"
                    else str(content or "").strip()
                )
                if task_mode == "managed":
                    plan_state = load_plan_state(self.data_path, task_id)
                    _, candidate_step = current_plan_step_with_index(plan_state)
                    candidate_step = candidate_step or {}
                    return {
                        "messages": [response],
                        "pending_action": {},
                        "completion_candidate": {
                            "text": solution_text[:12000],
                            "source": (
                                "solution_tag" if answer_match
                                else "structured_solution" if action_type == "solution"
                                else "plain_language_completion"
                            ),
                            "step_id": str(candidate_step.get("step_id") or ""),
                            "plan_version": str(plan_state.get("plan_version") or ""),
                            "created_at": datetime.now().isoformat(timespec="seconds"),
                        },
                        "final_audit_requested": True,
                        "generate_result": "GENERATED_ACTION",
                        "generate_exit_reason": "completion_candidate",
                        "next_step": "supervisor_final_audit",
                    }
                can_finish, plan_progress, final_status, gate_feedback = self._prepare_final_solution_progress(
                    task_id,
                    solution_text,
                    str(current_plan or ""),
                )
                if not can_finish:
                    gate_message = (
                        "Final solution validation failed.\n\n"
                        f"{gate_feedback}\n\n"
                        "Do not finish yet. Continue the current plan step, run concrete verification, "
                        "and only produce <solution> after the promised artifacts exist or a real blocker is recorded."
                    )
                    return {
                        "messages": [response, SystemMessage(content=gate_message)],
                        "plan_progress": plan_progress,
                        "next_step": "generate",
                        "pending_action": {},
                    }
                await self._finalize_task_memory(
                    task_id,
                    final_status,
                    messages + [response],
                    {"solution": solution_text[:3000], "plan_progress": plan_progress},
                )
                return {"messages": [response], "plan_progress": plan_progress, "next_step": "finish", "pending_action": {}}

            if action_type == "step_completion_candidate":
                return {
                    "messages": [response],
                    "pending_action": {},
                    "step_completion_candidate": dict(structured_action),
                    "generate_result": "GENERATED_ACTION",
                    "generate_exit_reason": "step_completion_candidate",
                    "next_step": "generate",
                }

            if action_type == "plan_patch":
                failure_owner = str(structured_action.get("failure_owner") or "").strip().upper()
                if failure_owner != RecoveryFailureOwner.PLAN.value:
                    return {
                        "messages": [response, SystemMessage(content=(
                            "PLAN_PATCH was rejected because it did not declare failure_owner=PLAN. "
                            "Execution, generated-action, script, runtime, dependency, artifact, and verification "
                            "failures must be repaired in the current step. Return one current-step action."
                        ))],
                        "pending_action": {},
                        "pending_recovery_action": {
                            "type": "same_step_generate",
                            "reason": "plan_patch_without_confirmed_plan_owner",
                            "failure_owner": RecoveryFailureOwner.GENERATED_ACTION.value,
                        },
                        "required_next_action": "SAME_STEP_GENERATE",
                        "failure_owner": RecoveryFailureOwner.GENERATED_ACTION.value,
                        "recovery_scope": "CURRENT_STEP",
                        "generate_result": "NEEDS_RECOVERY",
                        "generate_exit_reason": "plan_patch_owner_rejected",
                        "next_step": "generate",
                    }
                return {
                    "messages": [response],
                    "pending_action": {},
                    "pending_recovery_action": {
                        "type": "plan_patch",
                        "reason": str(structured_action.get("reason") or ""),
                        "patch": structured_action.get("patch") or {},
                        "failure_owner": RecoveryFailureOwner.PLAN.value,
                    },
                    "required_next_action": "PLAN_PATCH",
                    "failure_owner": RecoveryFailureOwner.PLAN.value,
                    "recovery_scope": "CURRENT_STEP",
                    "generate_result": "NEEDS_RECOVERY",
                    "generate_exit_reason": "plan_patch",
                    "next_step": "planner",
                }

            if action_type == "blocked":
                renderer_protocol = self._renderer_action_protocol(state, task_id)
                if (
                    renderer_protocol.get("renderer_composite")
                    and renderer_protocol.get("manifest_status") != "registered"
                    and not self._renderer_blocked_has_deterministic_evidence(
                        structured_action, renderer_protocol
                    )
                ):
                    blocked_hash = hashlib.sha256(
                        json.dumps(structured_action, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    ).hexdigest()[:20]
                    repair_feedback = self._action_format_repair_feedback(
                        state,
                        task_id,
                        invalid_hash=blocked_hash,
                        invalid_excerpt=str(content or ""),
                        contract_error={
                            "error_type": "BLOCKED_WITHOUT_DETERMINISTIC_MISSING_EVIDENCE",
                            "missing_roles": renderer_protocol.get("missing_roles") or [],
                            "ambiguous_roles": renderer_protocol.get("ambiguous_roles") or [],
                        },
                    )
                    return {
                        "messages": [response, SystemMessage(content=repair_feedback)],
                        "pending_action": {},
                        "pending_recovery_action": {
                            "type": "ACTION_FORMAT_REPAIR",
                            "reason": "BLOCKED lacked deterministic missing-input evidence.",
                            "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                        },
                        "required_next_action": "ACTION_FORMAT_REPAIR",
                        "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                        "generate_result": "NEEDS_RECOVERY",
                        "generate_exit_reason": "blocked_evidence_rejected",
                        "next_step": "generate",
                    }
                return {
                    "messages": [response],
                    "pending_action": {},
                    "pending_recovery_action": {
                        "type": "blocked",
                        "reason": str(structured_action.get("reason") or ""),
                    },
                    "generate_result": "BLOCKED",
                    "generate_exit_reason": "blocked",
                    "next_step": "finish",
                }

            if execute_match and not has_execute_body:
                plan_progress = self._record_plan_current_evidence(
                    task_id,
                    "execute_empty_body",
                    "Generate emitted an empty <execute> block.",
                )
                repair = (
                    "Generate emitted an empty <execute> block. The next response must either provide a non-empty "
                    "<execute>...</execute> command for the current checklist step, ask the user for a missing decision, "
                    "or write truthful blocked/partial-success artifacts. Do not explain only."
                )
                return {
                    "messages": [response, SystemMessage(content=repair)],
                    "plan_progress": plan_progress,
                    "next_step": "generate",
                    "pending_action": {},
                }

            if (activate_match or action_type == "activate_batch") and not answer_match:
                plan_state = load_plan_state(self.data_path, task_id)
                if str(plan_state.get("plan_version") or "") == "plan_v2":
                    plan_progress = self._record_plan_current_evidence(
                        task_id,
                        "generate_attempted_activation",
                        "Generate emitted legacy ACTIVATE_BATCH for a step-scheduled plan.",
                    )
                    return {
                        "messages": [
                            response,
                            SystemMessage(
                                content=(
                                    "ACTIVATE_BATCH is not a valid action for plan_v2. The controller already derived "
                                    "the current stage and ACTIVE_SKILLS from the running step. Execute that step, ask "
                                    "for required input, install its declared runtime, or finish as specified."
                                )
                            ),
                        ],
                        "plan_progress": plan_progress,
                        "next_step": "generate",
                        "pending_action": {},
                    }
                batch_id = (
                    activate_match.group(1).strip()
                    if activate_match
                    else str(structured_action.get("batch_id") or "").strip()
                )
                update = self._activate_workflow_batch(state, batch_id)
                update_messages = update.get("messages", []) if isinstance(update, dict) else []
                update_text = "\n".join(str(getattr(msg, "content", msg)) for msg in update_messages)
                plan_progress = self._record_activate_batch_progress(
                    task_id,
                    batch_id,
                    update_text,
                )
                return {
                    **update,
                    "messages": [response] + update.get("messages", []),
                    "plan_progress": plan_progress,
                    "next_step": "generate",
                    "pending_action": {},
                }

            if (ask_user_match or action_type == "ask_user") and not answer_match:
                question = (
                    ask_user_match.group(1).strip()
                    if ask_user_match
                    else str(structured_action.get("question") or "").strip()
                )
                plan_progress = self._record_plan_current_evidence(task_id, "generate_asked_user", question[:1000])
                return {
                    "messages": [response],
                    "pending_human_question": {
                        "type": "generate_question",
                        "stage": "generate",
                        "question": question,
                        "context": str(current_plan)[:1200],
                        "resume_step": "generate",
                    },
                    "plan_progress": plan_progress,
                    "next_step": "ask_user",
                    "pending_action": structured_action if structured_action else {},
                }

            # Active RAG
            if search_match or action_type == "search":
                query = search_match.group(1).strip() if search_match else str(structured_action.get("query") or "").strip()
                output.print_once(f"rag:{query[:30]}", f"🔍 Searching KB: {query}", Fore.CYAN)
                rag_result = "No info found."
                if self.retriever:
                    try:
                        is_error = "error" in query.lower()
                        target_kb = KB_MAP.get("ERRORS" if is_error else "SOP")
                        rag_result = self.retriever.retrieve(query, target_datasets=[target_kb]) or "No info found."
                    except Exception as e:
                        rag_result = f"Search error: {e}"
                if str(rag_result).startswith("Search error:"):
                    plan_progress = self._record_plan_current_evidence(task_id, "search_failed", rag_result[:1000])
                elif self._plan_step_action_is(self._running_plan_step(load_plan_state(self.data_path, task_id)), "search"):
                    plan_progress = self._mark_plan_current_done(task_id, f"search completed: {query[:500]}")
                else:
                    plan_progress = self._record_plan_current_evidence(task_id, "search_completed", query[:500])
                tool_msg = HumanMessage(content=f"<rag_result>\n{query}\n{rag_result}\n</rag_result>")
                return {"messages": [response, tool_msg], "plan_progress": plan_progress, "next_step": "generate", "pending_action": {}}

            next_step = "generate"
            if answer_match or action_type == "solution":
                next_step = "finish"
            elif ask_user_match or action_type == "ask_user":
                next_step = "ask_user"
            elif (install_match or action_type == "install_tool") and task_mode not in {"readonly", "simple_execute"}:
                next_step = "install_subagent"
            elif has_execute_body:
                next_step = "execute"

            if generated_action == "continue":
                format_count, duplicate_count, invalid_hash = self._record_action_format_failure(
                    task_id,
                    str((current_plan_step_with_index(load_plan_state(self.data_path, task_id))[1] or {}).get("step_id") or "lightweight"),
                    str(content or ""),
                    renderer_contract_error,
                )
                plan_progress = self._record_plan_current_evidence(
                    task_id,
                    "generate_unrecognized_action",
                    json.dumps(
                        {
                            "response_hash": invalid_hash,
                            "format_attempt": format_count,
                            "duplicate_count": duplicate_count,
                            "response_excerpt": str(content or "")[:700],
                            "schema_error": renderer_contract_error,
                        },
                        ensure_ascii=False,
                    ),
                )
                current_step_state = load_plan_state(self.data_path, task_id)
                _, format_step = current_plan_step_with_index(current_step_state)
                format_step_id = str(format_step.get("step_id") or "lightweight")
                schema_error_text = (
                    json.dumps(renderer_contract_error, ensure_ascii=False, sort_keys=True)
                    if renderer_contract_error
                    else ""
                )
                format_repair_already_used = (
                    str((state.get("pending_recovery_action") or {}).get("type") or "").strip().lower()
                    in {"action_format_repair", "format_repair"}
                )
                if format_repair_already_used:
                    internal_error = (
                        f"INTERNAL_PROTOCOL_ERROR: Generate failed the structured action protocol after its one repair "
                        f"for {format_step_id}. The raw responses were persisted and no execution retry "
                        "was consumed."
                    )
                    plan_progress = self._finalize_blocked_state(task_id, internal_error)
                    return {
                        "messages": [response, SystemMessage(content=(
                            "<action_format_repair>\n"
                            f"Escalated after repeated invalid actions for {format_step_id}; response_hash={invalid_hash}.\n"
                            "No third identical-context LLM call is permitted.\n"
                            f"{internal_error}\n"
                            "</action_format_repair>"
                        ))],
                        "plan_progress": plan_progress,
                        "pending_action": {},
                        "pending_recovery_action": {
                            "type": "internal_protocol_error",
                            "reason": "action_format_repair_exhausted",
                            "step_id": format_step_id,
                            "invalid_response_hash": invalid_hash,
                            "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                        },
                        "diagnostic_summary": {
                            "root_cause": internal_error,
                            "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                            "failure_type": "action_schema_error",
                            "evidence": [
                                f"response_hash:{invalid_hash}",
                                *([schema_error_text] if schema_error_text else []),
                            ],
                            "confidence": 1.0,
                            "missing_evidence": [],
                            "repair_strategy": "Repair the internal action protocol before resuming the step.",
                            "next_action": "INTERNAL_PROTOCOL_ERROR",
                            "requires_user_input": False,
                        },
                        "pending_human_question": {},
                        "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                        "required_next_action": "INTERNAL_PROTOCOL_ERROR",
                        "generate_result": "BLOCKED",
                        "generate_exit_reason": "internal_protocol_error",
                        "task_status": TASK_STATUS_BLOCKED,
                        "next_step": "finish",
                    }
                repair_feedback = self._action_format_repair_feedback(
                    state,
                    task_id,
                    invalid_hash=invalid_hash,
                    invalid_excerpt=str(content or ""),
                    contract_error=renderer_contract_error,
                )
                renderer_protocol = self._renderer_action_protocol(state, task_id)
                return {
                    "messages": [
                        response,
                        SystemMessage(content=repair_feedback),
                    ],
                    "plan_progress": plan_progress,
                    "pending_action": {},
                    "pending_recovery_action": {
                        "type": "ACTION_FORMAT_REPAIR",
                        "step_id": format_step_id,
                        "invalid_response_hash": invalid_hash,
                        "invalid_response_excerpt": str(content or "")[:700],
                        "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                        "allowed_actions": renderer_protocol.get("allowed_actions") or [],
                        "schema_error": renderer_contract_error,
                    },
                    "action_format_repair": {
                        "step_id": format_step_id,
                        "invalid_response_hash": invalid_hash,
                        "attempt": format_count,
                        "feedback": repair_feedback,
                    },
                    "required_next_action": "ACTION_FORMAT_REPAIR",
                    "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                    "generate_result": "NEEDS_RECOVERY",
                    "generate_exit_reason": "unrecognized_action",
                    "next_step": "generate",
                }

            return {
                "messages": [response],
                "plan_progress": plan_progress,
                **step_skill_update,
                "next_step": next_step,
                "pending_action": structured_action if structured_action else {},
            }

        async def generate(state: AgentState):
            """Lifecycle-logged boundary around every Generate return path."""
            task_id = str(state.get("task_id") or "unknown")
            generate_call_audit[task_id] = False

            def logged_early_exit(result: Dict[str, Any], entry_record: Dict[str, Any]) -> Dict[str, Any]:
                exit_record = {
                    "event": "generate_exit",
                    "exit_reason": str(result.get("generate_exit_reason") or result.get("next_step") or "guarded_exit"),
                    "next_node": str(result.get("next_step") or ""),
                    "llm_invoked": False,
                    "state_changes": sorted(str(key) for key in result.keys()),
                    "result": str(result.get("generate_result") or "NEEDS_RECOVERY"),
                }
                logger.info("Generate lifecycle: task=%s %s", task_id, json.dumps(exit_record, ensure_ascii=False, sort_keys=True))
                result["generate_lifecycle"] = {"enter": entry_record, "exit": exit_record}
                return result

            plan_state = load_plan_state(self.data_path, task_id)
            _, current_step = current_plan_step_with_index(plan_state)
            current_step = current_step or {}
            current_step_id = str(current_step.get("step_id") or state.get("current_step_id") or "")
            recovery_action = str(
                (state.get("pending_recovery_action") or {}).get("type")
                or state.get("required_next_action")
                or ""
            )
            recovery_action_type = recovery_action.lower()
            durable_status = task_status(plan_state)
            if durable_status in TASK_TERMINAL_STATUSES or recovery_action_type == "blocked":
                reason = str((state.get("pending_recovery_action") or {}).get("reason") or f"task status is {durable_status}")
                early_entry = {
                    "event": "generate_enter",
                    "current_step_id": current_step_id,
                    "plan_version": str(plan_state.get("plan_version") or ""),
                    "recovery_action": recovery_action,
                    "remaining_budget": 0,
                }
                logger.info(
                    "Generate lifecycle: task=%s %s",
                    task_id,
                    json.dumps(early_entry, ensure_ascii=False, sort_keys=True),
                )
                return logged_early_exit({
                    "messages": [SystemMessage(content=f"Generate was not invoked because the task is terminal: {reason}")],
                    "pending_action": {},
                    "pending_recovery_action": {},
                    "next_step": "finish",
                    "generate_result": "BLOCKED" if durable_status != TASK_STATUS_COMPLETED else "FINISHED",
                    "generate_exit_reason": "terminal_state_before_generate",
                }, early_entry)
            if str(state.get("task_mode") or "managed").lower() == "managed" and not current_step_id:
                early_entry = {
                    "event": "generate_enter",
                    "current_step_id": "",
                    "plan_version": str(plan_state.get("plan_version") or ""),
                    "recovery_action": recovery_action,
                    "remaining_budget": 0,
                }
                logger.info("Generate lifecycle: task=%s %s", task_id, json.dumps(early_entry, ensure_ascii=False, sort_keys=True))
                return logged_early_exit({
                    "messages": [SystemMessage(content="Generate was not invoked because no executable current plan step exists.")],
                    "pending_action": {},
                    "required_next_action": "PLAN_PATCH",
                    "failure_owner": RecoveryFailureOwner.PLAN.value,
                    "recovery_scope": "PLAN",
                    "recovery_phase": "plan_repair",
                    "next_step": "planner",
                    "generate_result": "NEEDS_RECOVERY",
                    "generate_exit_reason": "current_step_missing",
                }, early_entry)
            task_budgets = dict((plan_state.get("task_state") or {}).get("budgets") or {})
            if recovery_action_type == "action_format_repair":
                repair_counts = dict(task_budgets.get("action_format_repair_by_step") or {})
                generate_remaining = max(0, 1 - int(repair_counts.get(current_step_id or "lightweight") or 0))
            else:
                action_counts = dict(task_budgets.get("generate_action_count_by_step") or {})
                generate_remaining = max(0, 8 - int(action_counts.get(current_step_id or "lightweight") or 0))
            entry = {
                "event": "generate_enter",
                "current_step_id": current_step_id,
                "plan_version": str(plan_state.get("plan_version") or state.get("plan_version") or ""),
                "validation_status": str((state.get("validation_evidence") or {}).get("status") or ""),
                "retry_count": int(state.get("retry_count") or 0),
                "recovery_action": recovery_action,
                "context_mode": str((state.get("context_budget_report") or {}).get("projection_mode") or "current_step"),
                "remaining_budget": generate_remaining,
            }
            logger.info("Generate lifecycle: task=%s %s", task_id, json.dumps(entry, ensure_ascii=False, sort_keys=True))

            try:
                result = await _generate_impl(state)
            except Exception as exc:
                logger.exception("Generate failed before producing an explicit state transition: task=%s", task_id)
                result = {
                    "messages": [SystemMessage(content=f"Generate failed explicitly: {type(exc).__name__}: {exc}")],
                    "next_step": "supervisor",
                    "generate_result": "NEEDS_RECOVERY",
                    "generate_exit_reason": "generate_exception",
                }

            if not isinstance(result, dict):
                result = {
                    "messages": [SystemMessage(content="Generate returned no structured result; recovery is required.")],
                    "next_step": "supervisor",
                    "generate_result": "NEEDS_RECOVERY",
                    "generate_exit_reason": "invalid_generate_return",
                }

            if (
                recovery_action_type == "action_format_repair"
                and str(result.get("generate_exit_reason") or "") != "unrecognized_action"
                and not result.get("pending_recovery_action")
            ):
                result["pending_recovery_action"] = {}
                result["required_next_action"] = ""
                result["action_format_repair"] = {}

            next_node = str(result.get("next_step") or "")
            outcome = str(result.get("generate_result") or "").upper()
            if outcome not in {"GENERATED_ACTION", "NEEDS_RECOVERY", "NEEDS_CONTEXT_COMPACTION", "BLOCKED", "FINISHED"}:
                if str(result.get("recovery_status") or "") == "PROMPT_ASSEMBLY_FAILURE":
                    outcome = "BLOCKED"
                elif next_node == "finish":
                    outcome = "FINISHED"
                elif next_node in {"supervisor", "planner"} or result.get("pending_recovery_action"):
                    outcome = "NEEDS_RECOVERY"
                else:
                    outcome = "GENERATED_ACTION"
            exit_reason = str(result.get("generate_exit_reason") or result.get("recovery_status") or next_node or outcome)
            exit_record = {
                "event": "generate_exit",
                "exit_reason": exit_reason,
                "next_node": next_node,
                "llm_invoked": bool(generate_call_audit.pop(task_id, False)),
                "state_changes": sorted(str(key) for key in result.keys()),
                "result": outcome,
            }
            logger.info("Generate lifecycle: task=%s %s", task_id, json.dumps(exit_record, ensure_ascii=False, sort_keys=True))
            result["generate_result"] = outcome
            result["generate_lifecycle"] = {"enter": entry, "exit": exit_record}
            if outcome == "BLOCKED" and str(result.get("recovery_status") or "") == "INTERNAL_PROTOCOL_ERROR":
                output.print_ai(
                    "Generate stopped with an explicit internal protocol failure because required current-step "
                    "context could not fit the prompt budget. The scientific plan step was not marked blocked."
                )
            return result

        # ════════════════════════════════════════════
        # Node 3: Execute
        # ════════════════════════════════════════════
        async def execute_logic(state: AgentState) -> AgentState:
            output.print_node_header("execute")
            task_id      = state.get("task_id", "default")

            stale_reason = self._stale_plan_task_revision_reason(state, task_id)
            if stale_reason:
                plan_progress = self._record_plan_current_evidence(
                    task_id,
                    "stale_task_revision_before_execute",
                    stale_reason,
                )
                return {
                    "messages": [HumanMessage(content=f"<observation>⚠️ {stale_reason}</observation>")],
                    "plan_progress": plan_progress,
                    "pending_action": {},
                    "required_next_action": "PLAN_PATCH",
                    "failure_owner": RecoveryFailureOwner.PLAN.value,
                    "recovery_scope": "CURRENT_STEP",
                    "recovery_phase": "plan_repair",
                    "next_step": "planner",
                }

            preflight_conflicts = self._knowledge_conflict_update(
                task_id,
                dict(state),
                phase="before_execute",
            )
            guarded_state = {**dict(state), **preflight_conflicts}
            guard_action, guard_reason = self._knowledge_conflict_execution_guard(task_id, guarded_state)
            if guard_action == "ask_user":
                question = (
                    "A required user constraint conflicts with the current recovery/plan proposal. "
                    "Please decide whether to provide a compatible alternative or stop this stage.\n\n"
                    + guard_reason
                )
                return {
                    **preflight_conflicts,
                    "messages": [HumanMessage(content=f"<observation>⚠️ {guard_reason}</observation>")],
                    "pending_human_question": {
                        "type": "knowledge_conflict",
                        "stage": "execute",
                        "question": question,
                        "resume_step": "planner",
                    },
                    "next_step": "ask_user",
                    "pending_action": {},
                }
            pending_action = state.get("pending_action") or {}
            managed_mode = str(state.get("task_mode") or "managed").lower() == "managed"
            if managed_mode:
                contract_error = self._validate_pending_action_contract(
                    pending_action,
                    state,
                    task_id,
                )
                if contract_error:
                    plan_state = load_plan_state(self.data_path, task_id)
                    _, current_step = current_plan_step_with_index(plan_state)
                    step_id = str(current_step.get("step_id") or "task")
                    count, _ = self._increment_durable_budget(
                        task_id,
                        "empty_execute_by_step",
                        step_id,
                        limit=2,
                    )
                    if count >= 2:
                        progress = self._finalize_blocked_state(task_id, contract_error)
                        return {
                            "messages": [HumanMessage(content=f"<observation>❌ {contract_error}</observation>")],
                            "plan_progress": progress,
                            "pending_action": {},
                            "task_status": TASK_STATUS_BLOCKED,
                            "next_step": "finish",
                        }
                    plan_progress = self._record_plan_current_evidence(
                        task_id,
                        "execute_action_contract_failed",
                        contract_error,
                    )
                    return {
                        "messages": [SystemMessage(content=(
                            "Execute was not entered because its action contract is invalid: "
                            + contract_error
                            + " Generate must repair the action metadata for the unchanged current step."
                        ))],
                        "plan_progress": plan_progress,
                        "pending_action": {},
                        "pending_recovery_action": {
                            "type": "ACTION_FORMAT_REPAIR",
                            "reason": contract_error,
                            "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                        },
                        "required_next_action": "ACTION_FORMAT_REPAIR",
                        "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                        "recovery_scope": "CURRENT_STEP",
                        "recovery_phase": "diagnose",
                        "next_step": "generate",
                    }
            original_code = ""
            if self._pending_action_is(pending_action, "execute"):
                pending_action_reason = self._pending_action_stale_reason(pending_action, state, task_id)
                if pending_action_reason:
                    plan_progress = self._record_plan_current_evidence(
                        task_id,
                        "stale_or_unbound_pending_action",
                        pending_action_reason,
                    )
                    return {
                        "messages": [HumanMessage(content=f"<observation>⚠️ {pending_action_reason}</observation>")],
                        "plan_progress": plan_progress,
                        "pending_action": {},
                        "next_step": "generate",
                    }
                original_code = str(pending_action.get("code") or "").strip()
            if not original_code and str(state.get("task_mode") or "managed").lower() in {"readonly", "simple_execute"}:
                original_code = self._recent_nonempty_execute_body(state.get("messages", []), window=6)
            if not original_code:
                detail = "No current, revision-bound pending execute action was found."
                plan_progress = self._record_plan_current_evidence(task_id, "execute_missing_code", detail)
                return {"messages": [HumanMessage(content=f"<observation>❌ {detail}</observation>")],
                        "plan_progress": plan_progress,
                        "pending_action": {},
                        "next_step": "generate"}

            recovery_action_type = str(
                (state.get("pending_recovery_action") or {}).get("type")
                or state.get("required_next_action")
                or ""
            ).lower()
            declared_purpose = str(pending_action.get("purpose") or "analysis").strip().lower()
            command_role = str(pending_action.get("command_role") or "").strip().lower()
            if not command_role:
                if recovery_action_type in {"diagnostic_probe", "probe", "run_probe"} or declared_purpose == "probe":
                    command_role = "diagnostic"
                elif declared_purpose == "validation":
                    command_role = "required_validation"
                elif declared_purpose == "fallback" or recovery_action_type == "fallback":
                    command_role = "fallback"
                else:
                    command_role = "primary"

            action_plan_state = load_plan_state(self.data_path, task_id)
            _, action_step = current_plan_step_with_index(action_plan_state)
            action_step_purpose = str((action_step or {}).get("purpose") or "analysis").strip().lower()
            manifest_managed_action = str(pending_action.get("action_source") or "") == "manifest_runner"
            bypass_step_gate = (
                not manifest_managed_action
                and (
                    command_role in {"diagnostic", "optional_probe", "recovery"}
                    or (command_role == "required_validation" and action_step_purpose != "validation")
                )
            )

            if managed_mode and command_role not in {"diagnostic", "optional_probe", "recovery"}:
                dependency_evidence = CompletionGate.validate_dependencies(
                    action_plan_state,
                    str((action_step or {}).get("step_id") or pending_action.get("step_id") or ""),
                    str(pending_action.get("stage_id") or action_plan_state.get("current_stage_id") or ""),
                )
                if not dependency_evidence.satisfied:
                    decision = RecoveryPolicy().decide(
                        dependency_evidence=dependency_evidence.to_dict(),
                        budgets=dict((action_plan_state.get("task_state") or {}).get("budgets") or {}),
                        action_id=str(pending_action.get("action_id") or "dependency-gate"),
                        step_id=str((action_step or {}).get("step_id") or pending_action.get("step_id") or ""),
                        attempt_id=str(pending_action.get("attempt_id") or "dependency-gate"),
                    )
                    decision = contextualize_recovery_decision(decision, source_node="controller")
                    application = apply_recovery_decision(action_plan_state, decision)
                    save_plan_state(self.data_path, task_id, action_plan_state)
                    pending_type = {
                        RecoveryDecisionKind.PLAN_PATCH_REQUEST.value: "PLAN_PATCH",
                        RecoveryDecisionKind.ROLLBACK_UPSTREAM_STEP.value: "ROLLBACK_UPSTREAM_STEP",
                        RecoveryDecisionKind.REWIND_MANIFEST_STAGE.value: "REWIND_MANIFEST_STAGE",
                    }.get(decision.kind, decision.kind)
                    return {
                        "messages": [SystemMessage(content=(
                            "DependencyGate rejected the formal action before execution. "
                            f"RecoveryPolicy committed {decision.kind}: {decision.reason}"
                        ))],
                        "dependency_evidence": dependency_evidence.to_dict(),
                        "recovery_decision": decision.to_dict(),
                        "recovery_application": application,
                        "recovery_status": decision.kind,
                        "pending_recovery_action": {
                            "type": pending_type,
                            "reason": decision.reason,
                            "step_id": application.get("current_step_id") or dependency_evidence.step_id,
                        },
                        "required_next_action": pending_type,
                        "current_step_id": str(application.get("current_step_id") or ""),
                        "current_stage_id": str(application.get("current_stage_id") or ""),
                        "plan_progress": render_plan_progress(action_plan_state),
                        "pending_action": {},
                        "next_step": decision.next_route,
                    }

            if command_role in {"diagnostic", "optional_probe"}:
                probe_allowed, probe_error = validate_diagnostic_probe_code(original_code)
                if not probe_allowed:
                    plan_progress = self._record_plan_current_evidence(
                        task_id,
                        "diagnostic_probe_boundary_failed",
                        probe_error,
                    )
                    return {
                        "messages": [SystemMessage(content=(
                            "Diagnostic probe was rejected before execution: "
                            + probe_error
                            + ". Generate one bounded read-only probe for the current task root."
                        ))],
                        "plan_progress": plan_progress,
                        "pending_action": {},
                        "next_step": "generate",
                    }

            if managed_mode:
                if command_role == "renderer_invocation":
                    execute_count, _ = self._increment_durable_budget(
                        task_id,
                        "renderer_invocations_by_step",
                        str(pending_action.get("step_id") or "task"),
                        limit=2,
                    )
                elif command_role in {"diagnostic", "optional_probe", "required_validation"}:
                    execute_count = len(RecoveryStateStore(self.data_path, task_id).load_execution_records()) + 1
                elif command_role == "recovery":
                    execute_count = len(RecoveryStateStore(self.data_path, task_id).load_execution_records()) + 1
                else:
                    execute_count, _ = self._increment_durable_budget(
                        task_id,
                        "execute_attempts_by_step",
                        str(pending_action.get("step_id") or "task"),
                        limit=3,
                    )
            else:
                execute_count = 1

            if guard_action == "probe":
                # A targeted read-only probe is allowed to resolve the
                # conflict; the old analysis command is not.
                if int(state.get("retry_count") or 0) >= 4:
                    question = (
                        "The same knowledge conflict has remained unresolved after four probe opportunities. "
                        "Please provide a decision or evidence before the analysis can continue.\n\n"
                        + guard_reason
                    )
                    return {
                        **preflight_conflicts,
                        "messages": [HumanMessage(content=f"<observation>⚠️ {guard_reason}</observation>")],
                        "pending_human_question": {
                            "type": "knowledge_conflict_probe_budget",
                            "stage": "execute",
                            "question": question,
                            "resume_step": "planner",
                        },
                        "next_step": "ask_user",
                        "pending_action": {},
                    }
                probe_code = str(original_code or "").lower()
                probe_markers = (
                    "--version", "-version", "--help", "command -v", "type -a", "which ",
                    "stat ", "file ", "sha256sum", "test -", "ls ", "head ", "tail ",
                    "grep ", "find ", "printf ", "echo ",
                )
                if not any(marker in probe_code for marker in probe_markers) or re.search(r"(?:^|\s)-o\s+\S+", probe_code):
                    plan_progress = self._record_plan_current_evidence(task_id, "knowledge_conflict_probe_required", guard_reason)
                    return {
                        **preflight_conflicts,
                        "messages": [HumanMessage(content=(
                            "<observation>⚠️ Execution held because the current plan is stale or has an unresolved "
                            f"knowledge conflict. {guard_reason} Generate a targeted read-only probe or revised plan; "
                            "do not repeat the old analysis command.</observation>"
                        ))],
                        "plan_progress": plan_progress,
                        "pending_action": {},
                        "next_step": "generate",
                    }

            language = detect_script_language(original_code)
            runtime_bindings = {
                str(key): str(value)
                for key, value in (pending_action.get("runtime_bindings") or {}).items()
                if re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", str(key or "")) and str(value or "")
            }
            runtime_bindings.update({
                "BIOMIND_TASK_ID": task_id,
                "BIOMIND_ACTION_ID": str(pending_action.get("action_id") or ""),
                "BIOMIND_STEP_ID": str(pending_action.get("step_id") or ""),
            })
            if language == "Bash" and runtime_bindings:
                binding_lines = [
                    f"if [[ -z \"${{{key}:-}}\" ]]; then export {key}={shlex.quote(value)}; fi"
                    for key, value in sorted(runtime_bindings.items())
                ]
                original_code = "\n".join(binding_lines) + "\n" + original_code
            elif language == "Python" and runtime_bindings:
                binding_lines = [
                    f"os.environ.setdefault({key!r}, {value!r})"
                    for key, value in sorted(runtime_bindings.items())
                ]
                original_code = "import os\n" + "\n".join(binding_lines) + "\n" + original_code
            code = normalize_script_code(original_code)
            forbidden_actions = [str(item).strip() for item in (state.get("forbidden_recovery_actions") or []) if str(item).strip()]
            if forbidden_actions:
                normalized_code = ActionSignature.normalize_command(code)
                readonly_forbidden = {"cat", "ls", "find", "head", "tail", "grep", "help"}
                repeated = any(
                    normalized_code == ActionSignature.normalize_command(item)
                    or (" " in item and ActionSignature.normalize_command(item) in normalized_code)
                    or (item.lower() in readonly_forbidden and re.search(rf"\b{re.escape(item)}\b", normalized_code, re.IGNORECASE))
                    for item in forbidden_actions
                )
                if repeated:
                    plan_progress = self._record_plan_current_evidence(
                        task_id,
                        "forbidden_recovery_action",
                        "Generate proposed an action forbidden by the persisted recovery progress: " + ", ".join(forbidden_actions[:5]),
                    )
                    return {
                        "messages": [HumanMessage(content=(
                            "<observation>❌ Recovery action blocked: the proposed command repeats an action "
                            "that produced no independent diagnostic progress. Choose a different probe/repair, "
                            "rollback, or write a truthful blocked artifact.</observation>"
                        ))],
                        "plan_progress": plan_progress,
                        "pending_action": {},
                        "next_step": "generate",
                    }
            task_boundary_context = "\n".join(
                str(getattr(message, "content", message))
                for message in state.get("messages", [])[:8]
            )
            runtime_policy_context = self._runtime_policy_context_from_state(state)
            install_disallowed = self._runtime_install_disallowed_by_context(runtime_policy_context)
            boundary_gate = CompletionGate.validate_code_boundary(code, task_boundary_context)
            if not boundary_gate.passed:
                plan_progress = self._record_plan_current_evidence(
                    task_id,
                    "pre_execution_boundary_gate_failed",
                    boundary_gate.message,
                )
                obs = f"<observation>❌ Pre-execution boundary gate blocked execution.\n{boundary_gate.feedback}\n</observation>"
                return {
                    "messages": [HumanMessage(content=obs)],
                    "plan_progress": plan_progress,
                    "runtime_status": self._runtime_status_for_task(task_id),
                    "next_step": "generate",
                }
            selected_skills_state = state.get("active_skills", []) or []
            future_skills_state = state.get("future_skills", []) or []
            runtime_skills_state = self._infer_runtime_skills_from_code(code, selected_skills_state)
            selected_skill_pool_state = state.get("selected_skills", []) or runtime_skills_state
            install_redirect = self._redirect_install_command(code, runtime_skills_state)
            if install_redirect:
                if install_disallowed:
                    final_res = self._runtime_block_message(install_redirect, runtime_skills_state, code[:1000])
                    plan_progress = self._mark_plan_current_blocked(task_id, final_res)
                    return {
                        "messages": [AIMessage(content=f"<observation>{final_res}</observation>")],
                        "plan_progress": plan_progress,
                        "selected_skills": selected_skill_pool_state,
                        "active_skills": runtime_skills_state,
                        "runtime_status": self._runtime_status_for_task(task_id, runtime_skills_state),
                        "next_step": self._next_step_after_runtime_block(task_id),
                    }
                plan_progress = self._record_plan_current_evidence(task_id, "redirect_to_install", install_redirect[:1000])
                return {
                    "messages": [AIMessage(content=install_redirect)],
                    "plan_progress": plan_progress,
                    "selected_skills": selected_skill_pool_state,
                    "active_skills": runtime_skills_state,
                    "runtime_status": self._runtime_status_for_task(task_id, runtime_skills_state),
                    "next_step": "install_subagent",
                }

            future_violation = self._workflow_future_skill_violation(code, selected_skills_state, future_skills_state)
            if future_violation:
                plan_progress = self._record_plan_current_evidence(task_id, "future_skill_guard", future_violation[:1000])
                obs = f"<observation>❌ Workflow skill guard blocked execution.\n{future_violation}\n</observation>"
                return {
                    "messages": [HumanMessage(content=obs)],
                    "plan_progress": plan_progress,
                    "runtime_status": self._runtime_status_for_task(task_id, runtime_skills_state),
                    "next_step": "generate",
                }

            runner_violation = self._selected_skill_env_runner_violation(code, runtime_skills_state)
            if runner_violation:
                plan_progress = self._record_plan_current_evidence(task_id, "skill_env_runner_guard", runner_violation[:1000])
                obs = f"<observation>❌ Skill runtime guard blocked execution.\n{runner_violation}\n</observation>"
                return {
                    "messages": [HumanMessage(content=obs)],
                    "plan_progress": plan_progress,
                    "runtime_status": self._runtime_status_for_task(task_id, runtime_skills_state),
                    "next_step": "generate",
                }

            runtime_ready, runtime_redirect = self._ensure_skill_runtime_ready(runtime_skills_state, task_id=task_id, code=code)
            if not runtime_ready and runtime_redirect:
                if install_disallowed:
                    final_res = self._runtime_block_message(runtime_redirect, runtime_skills_state, runtime_redirect)
                    plan_progress = self._mark_plan_current_blocked(task_id, final_res)
                    return {
                        "messages": [AIMessage(content=f"<observation>{final_res}</observation>")],
                        "plan_progress": plan_progress,
                        "selected_skills": selected_skill_pool_state,
                        "active_skills": runtime_skills_state,
                        "runtime_status": self._runtime_status_for_task(task_id, runtime_skills_state),
                        "next_step": self._next_step_after_runtime_block(task_id),
                    }
                plan_progress = self._record_plan_current_evidence(task_id, "runtime_not_ready", runtime_redirect[:1000])
                return {
                    "messages": [AIMessage(content=runtime_redirect)],
                    "plan_progress": plan_progress,
                    "selected_skills": selected_skill_pool_state,
                    "active_skills": runtime_skills_state,
                    "runtime_status": self._runtime_status_for_task(task_id, runtime_skills_state),
                    "next_step": "install_subagent",
                }

            # 中间件审查
            action, final_code = self.middleware.review_code(code, language)
            if action == "reject":
                rejection_reason = str(final_code or "User rejected generated code.")
                plan_progress = self._record_plan_current_evidence(
                    task_id,
                    "execute_rejected_by_user",
                    rejection_reason[:1000],
                )
                obs = (
                    "<observation>❌ Rejected by user.\n"
                    f"Reason/feedback: {rejection_reason}\n"
                    "</observation>"
                )
                return {"messages": [HumanMessage(content=obs)],
                        "plan_progress": plan_progress,
                        "runtime_status": self._runtime_status_for_task(task_id, runtime_skills_state),
                        "next_step": "generate"}
            if action == "quit":
                await self._finalize_task_memory(
                    task_id,
                    "cancelled",
                    state.get("messages", []),
                    {"reason": "User terminated during code review."},
                )
                return {"messages": [HumanMessage(content="Task terminated.")], "next_step": "finish"}

            elif action == "edit":
                # 判断：是直接粘贴的代码，还是指导意见？
                # 简单策略：如果包含中文或明显的描述性词汇，视为反馈
                if re.search(r'[\u4e00-\u9fff]', final_code) or \
                    any(keyword in final_code.lower() for keyword in ['correct', '正确', 'should', '应该', 'use', '使用']):
                    # 这是指导意见，不是代码
                    feedback_msg = f"<observation>⚠️ User provided guidance:\n{final_code}\n\nPlease regenerate code based on this feedback.</observation>"
                    plan_progress = self._record_plan_current_evidence(task_id, "execute_user_guidance", final_code[:1000])
                    return {"messages": [HumanMessage(content=feedback_msg)], "plan_progress": plan_progress, "next_step": "generate"}
                else:   
                    # 这是修改后的代码，可以执行
                    code_to_run = normalize_script_code(final_code)
                    language = detect_script_language(code_to_run)
            else:
                code_to_run = normalize_script_code(final_code)  # approve 的情况  

            boundary_gate = CompletionGate.validate_code_boundary(code_to_run, task_boundary_context)
            if not boundary_gate.passed:
                plan_progress = self._record_plan_current_evidence(
                    task_id,
                    "pre_execution_boundary_gate_failed",
                    boundary_gate.message,
                )
                obs = f"<observation>❌ Pre-execution boundary gate blocked execution.\n{boundary_gate.feedback}\n</observation>"
                return {
                    "messages": [HumanMessage(content=obs)],
                    "plan_progress": plan_progress,
                    "runtime_status": self._runtime_status_for_task(task_id, runtime_skills_state),
                    "next_step": "generate",
                }

            runtime_skills_state = self._infer_runtime_skills_from_code(code_to_run, selected_skills_state)
            selected_skill_pool_state = state.get("selected_skills", []) or runtime_skills_state
            install_redirect = self._redirect_install_command(code_to_run, runtime_skills_state)
            if install_redirect:
                if install_disallowed:
                    final_res = self._runtime_block_message(install_redirect, runtime_skills_state, code_to_run[:1000])
                    plan_progress = self._mark_plan_current_blocked(task_id, final_res)
                    return {
                        "messages": [AIMessage(content=f"<observation>{final_res}</observation>")],
                        "plan_progress": plan_progress,
                        "selected_skills": selected_skill_pool_state,
                        "active_skills": runtime_skills_state,
                        "runtime_status": self._runtime_status_for_task(task_id, runtime_skills_state),
                        "next_step": self._next_step_after_runtime_block(task_id),
                    }
                plan_progress = self._record_plan_current_evidence(task_id, "redirect_to_install", install_redirect[:1000])
                return {
                    "messages": [AIMessage(content=install_redirect)],
                    "plan_progress": plan_progress,
                    "selected_skills": selected_skill_pool_state,
                    "active_skills": runtime_skills_state,
                    "runtime_status": self._runtime_status_for_task(task_id, runtime_skills_state),
                    "next_step": "install_subagent",
                }

            future_violation = self._workflow_future_skill_violation(code_to_run, selected_skills_state, future_skills_state)
            if future_violation:
                plan_progress = self._record_plan_current_evidence(task_id, "future_skill_guard", future_violation[:1000])
                obs = f"<observation>❌ Workflow skill guard blocked execution.\n{future_violation}\n</observation>"
                return {
                    "messages": [HumanMessage(content=obs)],
                    "plan_progress": plan_progress,
                    "runtime_status": self._runtime_status_for_task(task_id, runtime_skills_state),
                    "next_step": "generate",
                }

            runner_violation = self._selected_skill_env_runner_violation(code_to_run, runtime_skills_state)
            if runner_violation:
                plan_progress = self._record_plan_current_evidence(task_id, "skill_env_runner_guard", runner_violation[:1000])
                obs = f"<observation>❌ Skill runtime guard blocked execution.\n{runner_violation}\n</observation>"
                return {
                    "messages": [HumanMessage(content=obs)],
                    "plan_progress": plan_progress,
                    "runtime_status": self._runtime_status_for_task(task_id, runtime_skills_state),
                    "next_step": "generate",
                }

            runtime_ready, runtime_redirect = self._ensure_skill_runtime_ready(runtime_skills_state, task_id=task_id, code=code_to_run)
            if not runtime_ready and runtime_redirect:
                if install_disallowed:
                    final_res = self._runtime_block_message(runtime_redirect, runtime_skills_state, runtime_redirect)
                    plan_progress = self._mark_plan_current_blocked(task_id, final_res)
                    return {
                        "messages": [AIMessage(content=f"<observation>{final_res}</observation>")],
                        "plan_progress": plan_progress,
                        "selected_skills": selected_skill_pool_state,
                        "active_skills": runtime_skills_state,
                        "runtime_status": self._runtime_status_for_task(task_id, runtime_skills_state),
                        "next_step": self._next_step_after_runtime_block(task_id),
                    }
                plan_progress = self._record_plan_current_evidence(task_id, "runtime_not_ready", runtime_redirect[:1000])
                return {
                    "messages": [AIMessage(content=runtime_redirect)],
                    "plan_progress": plan_progress,
                    "selected_skills": selected_skill_pool_state,
                    "active_skills": runtime_skills_state,
                    "runtime_status": self._runtime_status_for_task(task_id, runtime_skills_state),
                    "next_step": "install_subagent",
                }

            # 保存脚本
            step_index = len(state["messages"])
            script_dir = os.path.join(self.data_path, "task_outputs", task_id)
            os.makedirs(script_dir, exist_ok=True)
            ext = ".py" if language == "Python" else ".sh"
            script_file = os.path.join(script_dir, f"step_{step_index}{ext}")
            with open(script_file, "w", encoding="utf-8") as f:
                if language == "Bash" and not code_to_run.startswith("#!"):
                    f.write(BASH_SCRIPT_PROLOGUE)
                f.write(code_to_run)
            output.print_once(f"script:{script_file}", f"💾 Script: {script_file}", Fore.GREEN)

            # 执行
            shim_path  = self.pm.get_shim_dir_str()
            execution_argv = ["bash", script_file] if language == "Bash" else ["python", script_file]
            execution_command = f"{execution_argv[0]} {shlex.quote(script_file)}"
            log_metadata_before: Dict[str, Dict[str, int]] = {}
            if ENABLE_ROOT_CAUSE_ANALYZER:
                try:
                    log_metadata_before = RootCauseAnalyzer().capture_log_metadata(
                        command=execution_command,
                        cwd=os.getcwd(),
                        generated_script_path=script_file,
                    )
                except Exception as exc:
                    logger.debug("Could not capture pre-run log metadata: %s", exc)
            recovery_dir = os.path.join(script_dir, "recovery")
            trace_file = ""
            trace_prefix = ""
            if language == "Bash" and ENABLE_COMMAND_TRACE:
                os.makedirs(recovery_dir, exist_ok=True)
                trace_file = os.path.join(recovery_dir, "command_trace.log")
                trace_prefix = (
                    f"exec 9>{shlex.quote(trace_file)}\n"
                )
                if ENABLE_PIPESTATUS_CAPTURE:
                    trace_prefix += (
                        "set -o pipefail\n"
                        "set -E\n"
                        "trap 'ps=(\"${PIPESTATUS[@]}\"); rc=\"${ps[-1]}\"; "
                        "printf \"+BIO_ERR|${BASH_SOURCE}:${LINENO}|${rc}| ${BASH_COMMAND}\\n\" >&9; "
                        "printf \"PIPESTATUS=(%s)\\n\" \"${ps[*]}\" >&9' ERR\n"
                    )
            started_at = datetime.now().isoformat(timespec="seconds")
            attempt_plan_state = load_plan_state(self.data_path, task_id)
            record_provenance_event(attempt_plan_state, "action_bound", {
                "task_id": task_id,
                "action": dict(pending_action),
                "raw_action": dict(pending_action),
            })
            attempt_record = record_provenance_event(attempt_plan_state, "attempt_started", {
                "action_id": str(pending_action.get("action_id") or ""),
                "step_id": str(pending_action.get("step_id") or ""),
                "stage_id": str(pending_action.get("stage_id") or ""),
                "executed_script_path": script_file,
                "started_at": started_at,
            })
            save_plan_state(self.data_path, task_id, attempt_plan_state)
            start_time = time.time()
            try:
                if language == "Bash":
                    code_with_path = f'export PATH="$PATH:{shim_path}"\n{trace_prefix}{code_to_run}'
                    stdout, stderr, returncode = await run_bash_script(code_with_path)
                else:
                    code_with_path = (f"import os\nos.environ['PATH']="
                                      f"os.environ.get('PATH','')+os.pathsep+r'{shim_path}'\n{code_to_run}")
                    stdout, stderr, returncode = await run_python_repl(code_with_path)
            except Exception as e:
                stdout, stderr, returncode = "", str(e), 1

            duration = time.time() - start_time
            finished_at = datetime.now().isoformat(timespec="seconds")
            self.profiler.record_skill_usage(
                task_id,
                runtime_skills_state,
                usage_type="executed",
                node="execute",
                detail=script_file,
            )
            validation_result, validation_block, validation_level = self._validate_execution_result(
                state,
                code_to_run,
                stdout,
                stderr,
                returncode,
            )
            validation_failed = bool(validation_result and not validation_result.get("passed", True))
            benign_nonzero_preview = bool(
                validation_result
                and validation_result.get("benign_nonzero_readonly_preview")
            )
            effective_success = returncode == 0 or benign_nonzero_preview
            runtime_install_redirect, runtime_skills_after_execute = self._runtime_install_redirect_from_execution(
                code_to_run,
                stdout,
                stderr,
                runtime_skills_state,
                task_id,
            )
            runtime_missing = bool(runtime_install_redirect)
            runtime_install_blocked = bool(runtime_missing and install_disallowed)
            task_mode = str(state.get("task_mode") or "").strip().lower()
            lightweight_mode = task_mode in {"readonly", "simple_execute"}
            if runtime_missing and lightweight_mode:
                runtime_install_blocked = True
            retry_plan = load_plan_state(self.data_path, task_id)
            retry_count = int(
                ((retry_plan.get("task_state") or {}).get("budgets") or {}).get("execution_retry_count")
                or 0
            )

            # 持久化
            if self.state_manager:
                self.state_manager.save_execution(task_id, step_index, code_to_run, language,
                                                  stdout, stderr, returncode, duration)
                if returncode == 0 and not validation_failed and not runtime_missing:
                    self._auto_save_results(task_id, step_index, code_to_run, stdout)
                    self._record_successful_skill_execution(
                        runtime_skills_state,
                        code_to_run,
                        stdout,
                    )

            runtime_status_block = self._format_runtime_status_for_observation(task_id, runtime_skills_state)
            stdout_obs = self._clip_observation_text(stdout, 2000)
            stderr_obs = self._clip_observation_text(stderr, 1000)
            if runtime_missing:
                obs = (f"<observation>\n[{language}] ❌ Runtime missing ({duration:.1f}s)\n"
                       f"STDOUT:\n{stdout_obs}\n"
                       f"STDERR:\n{stderr_obs}"
                       f"{validation_block}"
                       f"{runtime_status_block}\n</observation>")
            elif effective_success and not validation_failed:
                obs = (f"<observation>\n[{language}] ✅ Success ({duration:.1f}s)\n"
                       f"STDOUT:\n{stdout_obs}\n"
                       f"STDERR:\n{stderr_obs}"
                       f"{validation_block}"
                       f"{runtime_status_block}\n</observation>")
            elif effective_success and validation_failed:
                obs = (f"<observation>\n[{language}] ❌ Failed validation ({duration:.1f}s)\n"
                       f"STDOUT:\n{stdout_obs}\n"
                       f"STDERR:\n{stderr_obs}"
                       f"{validation_block}"
                       f"{runtime_status_block}\n</observation>")
            else:
                obs = (f"<observation>\n[{language}] ❌ Failed ({duration:.1f}s)\n"
                       f"STDOUT:\n{stdout_obs}\n"
                       f"STDERR:\n{stderr_obs}"
                       f"{validation_block}"
                       f"{runtime_status_block}\n</observation>")

            output.print_observation(obs)
            if runtime_install_blocked:
                final_res = self._runtime_block_message(
                    runtime_install_redirect,
                    runtime_skills_after_execute,
                    stderr or stdout or runtime_install_redirect,
                )
                if lightweight_mode:
                    plan_progress = self._record_plan_current_evidence(
                        task_id,
                        "lightweight_runtime_missing_no_install",
                        final_res[:1000],
                    )
                else:
                    plan_progress = self._mark_plan_current_blocked(task_id, final_res)
            elif runtime_missing:
                plan_progress = self._record_plan_current_evidence(
                    task_id,
                    "runtime_missing_after_execute",
                    f"{script_file}\n{runtime_install_redirect[:1000]}",
                )
            elif bypass_step_gate:
                plan_progress = self._record_plan_current_evidence(
                    task_id,
                    "diagnostic_observation" if command_role != "recovery" else "recovery_observation",
                    (
                        f"{script_file}\nreturncode={returncode}\n"
                        f"STDOUT:\n{self._clip_observation_text(stdout, 1000)}\n"
                        f"STDERR:\n{self._clip_observation_text(stderr, 1000)}"
                    ),
                )
            elif returncode == 0 and not validation_failed:
                plan_progress = self._record_plan_current_evidence(
                    task_id,
                    "execute_success",
                    (
                        f"{script_file}\n"
                        f"STDOUT:\n{self._clip_observation_text(stdout, 1000)}\n"
                        f"STDERR:\n{self._clip_observation_text(stderr, 1000)}"
                    ),
                )
            else:
                plan_progress = self._record_plan_current_evidence(
                    task_id,
                    "execute_failed",
                    f"{script_file}\nSTDOUT:\n{self._clip_observation_text(stdout, 1000)}\nSTDERR:\n{self._clip_observation_text(stderr, 1000)}",
                )
            await self._observe_session_memory(
                task_id,
                "execute",
                state.get("messages", []) + [HumanMessage(content=obs)],
                {
                    "script_file": script_file,
                    "language": language,
                    "returncode": returncode,
                    "duration_seconds": round(duration, 3),
                    "stdout": stdout[:3000],
                    "stderr": stderr[:1500],
                    "validation_level": validation_level,
                    "validation": validation_result,
                    "plan_progress": plan_progress,
                },
            )
            trace_text = ""
            if trace_file:
                try:
                    trace_text = Path(trace_file).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    trace_text = ""
            pipeline_statuses = None
            status_match = re.search(r"PIPESTATUS=\(([^)]*)\)", trace_text)
            if status_match:
                try:
                    pipeline_statuses = [int(item) for item in re.findall(r"-?\d+", status_match.group(1))]
                except ValueError:
                    pipeline_statuses = None
            root_cause_analysis: Dict[str, Any] = {}
            command_chain: Dict[str, Any] = {}
            if ENABLE_ROOT_CAUSE_ANALYZER:
                try:
                    analysis = await asyncio.wait_for(
                        asyncio.to_thread(
                            RootCauseAnalyzer().analyze,
                            command=execution_command,
                            argv=execution_argv,
                            cwd=os.getcwd(),
                            environment_name=str((state.get("runtime_status") or {}).get("env") or "") or None,
                            exit_code=returncode,
                            signal=-returncode if returncode < 0 else None,
                            stdout=stdout,
                            stderr=stderr,
                            generated_script_path=script_file,
                            trace_text=trace_text,
                            pipeline_statuses=pipeline_statuses,
                            stage_status=state.get("plan_progress"),
                            log_metadata_before=log_metadata_before,
                        ),
                        timeout=ROOT_CAUSE_TIMEOUT_SECONDS,
                    )
                    root_cause_analysis = analysis.llm_view()
                    full_chain = analysis.command_chain.to_dict()
                    command_chain = {
                        "root_node_id": full_chain.get("root_node_id"),
                        "confirmed_failed_node_id": full_chain.get("confirmed_failed_node_id"),
                        "candidate_failed_node_ids": full_chain.get("candidate_failed_node_ids", [])[:12],
                        "chain_confidence": full_chain.get("chain_confidence"),
                        "extraction_warnings": full_chain.get("extraction_warnings", [])[:12],
                    }
                    RootCauseAnalyzer.persist(analysis, recovery_dir)
                except asyncio.TimeoutError:
                    root_cause_analysis = {
                        "status": "diagnostic_timeout",
                        "root_cause_summary": (
                            "Root-cause post-processing exceeded its bounded diagnostic timeout; "
                            "raw execution evidence remains authoritative."
                        ),
                        "root_cause_confidence": 0.0,
                        "evidence": [f"timeout_seconds={ROOT_CAUSE_TIMEOUT_SECONDS}"],
                    }
                    logger.warning(
                        "RootCauseAnalyzer timed out after %.1fs; continuing to Supervisor with raw evidence.",
                        ROOT_CAUSE_TIMEOUT_SECONDS,
                    )
                except Exception as exc:
                    logger.warning("RootCauseAnalyzer failed; keeping raw execution evidence: %s", exc)
            execution_plan_state = load_plan_state(self.data_path, task_id)
            execution_step = self._running_plan_step(execution_plan_state) or {}
            execution_contracts = step_artifact_contracts(execution_step)
            declared_outputs = [
                str(item.get("path") or "")
                for item in execution_contracts
                if str(item.get("path") or "").strip()
            ]
            if not declared_outputs:
                declared_outputs = [
                    str(item) for item in execution_step.get("outputs", []) or [] if str(item).strip()
                ]
            observed_outputs: List[str] = []
            produced_artifacts: List[str] = []
            contract_by_path = {
                str(item.get("path") or ""): item
                for item in execution_contracts
                if str(item.get("path") or "")
            }
            for item in declared_outputs:
                try:
                    candidate_path = Path(item)
                    if not candidate_path.is_absolute():
                        base_dir = str((contract_by_path.get(item) or {}).get("base_dir") or "")
                        candidate_path = (Path(base_dir) if base_dir else Path(os.getcwd())) / candidate_path
                    if candidate_path.exists() and (not candidate_path.is_file() or candidate_path.stat().st_size > 0):
                        resolved_output = str(candidate_path.resolve())
                        observed_outputs.append(resolved_output)
                        if candidate_path.stat().st_mtime + 1.0 >= start_time:
                            produced_artifacts.append(resolved_output)
                except OSError:
                    continue
            execution_failure_fingerprint = str(
                (root_cause_analysis or {}).get("inner_fingerprint")
                or (root_cause_analysis or {}).get("failure_fingerprint")
                or ""
            )
            if not execution_failure_fingerprint and (returncode != 0 or validation_failed):
                fingerprint_text = "\n".join([stderr[-4000:], stdout[-4000:], str(returncode)])
                execution_failure_fingerprint = hashlib.sha256(
                    fingerprint_text.encode("utf-8", errors="replace")
                ).hexdigest()[:20]
            execution_evidence = {
                "execution_id": f"exec-{str(attempt_record.get('attempt_id') or pending_action.get('action_id') or Path(script_file or 'execute').stem)}",
                "plan_id": str(execution_plan_state.get("plan_id") or ""),
                "action_id": str(pending_action.get("action_id") or ""),
                "execution_status": (
                    "succeeded" if effective_success and not validation_failed and not runtime_missing else "failed"
                ),
                "purpose": declared_purpose,
                "attempt_id": str(attempt_record.get("attempt_id") or ""),
                "command_role": command_role,
                "action_kind": str(pending_action.get("action_kind") or pending_action.get("kind") or ""),
                "action_source": str(pending_action.get("action_source") or "generate"),
                "renderer_id": str(pending_action.get("renderer_id") or ""),
                "manifest_path": str(pending_action.get("manifest_path") or ""),
                "manifest_id": str(pending_action.get("manifest_id") or attempt_record.get("manifest_id") or ""),
                "stage_id": str(pending_action.get("stage_id") or attempt_record.get("stage_id") or ""),
                "manifest_fingerprint": str(pending_action.get("manifest_fingerprint") or ""),
                "manifest_action_id": str(pending_action.get("manifest_action_id") or ""),
                "manifest_script_hash": str(pending_action.get("manifest_script_hash") or ""),
                "success_checks": list(pending_action.get("success_checks") or []),
                "command": execution_command,
                "code": code_to_run,
                "generated_code": code_to_run,
                "argv": execution_argv,
                "script_file": script_file,
                "generated_script_path": script_file,
                "cwd": os.getcwd(),
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_seconds": round(duration, 3),
                "stdout": stdout,
                "stderr": stderr,
                "returncode": returncode,
                "signal": -returncode if returncode < 0 else None,
                "language": language,
                "validation": validation_result,
                "trace_file": trace_file or None,
                "trace_text": trace_text,
                "pipeline_statuses": pipeline_statuses,
                "log_metadata_before": log_metadata_before,
                "executor_metadata": get_last_execution_metadata() if language == "Bash" else {},
                "root_cause_analysis": root_cause_analysis,
                "command_chain": command_chain,
                "plan_version": str(execution_plan_state.get("plan_version") or "plan_v2"),
                "task_revision": self._task_revision_for_state(dict(state)),
                "step_id": str(execution_step.get("step_id") or ""),
                "attempt": len(state.get("recovery_attempts", []) or []) + 1,
                "declared_outputs": declared_outputs,
                "observed_outputs": observed_outputs,
                "produced_artifacts": produced_artifacts,
                "fallback_used": command_role == "fallback" or bool(pending_action.get("fallback_used")),
                "retry_count_recorded": bool(
                    command_role == "primary"
                    and (not effective_success or validation_failed or runtime_missing)
                ),
                "failure_fingerprint": execution_failure_fingerprint,
                "stdout_summary": self._clip_observation_text(stdout, 2000),
                "stderr_summary": self._clip_observation_text(stderr, 2000),
                "expected_runtime_contract": {
                    "audits": [
                        {
                            "skill_id": str(item.get("skill_id") or item.get("skill") or ""),
                            "env": str(item.get("checked_env") or item.get("env") or item.get("declared_env") or ""),
                            "status": str(item.get("status") or ""),
                            "env_runner": str(item.get("env_runner") or ""),
                            "healthcheck": str(item.get("healthcheck") or item.get("declared_healthcheck") or ""),
                            "resolved_runtime_paths": dict(item.get("resolved_runtime_paths") or {}),
                        }
                        for item in state.get("runtime_audit", []) or [] if isinstance(item, dict)
                    ],
                    "bindings": self._runtime_bindings_from_state(dict(state)),
                },
                "actual_environment": {
                    key: os.environ.get(key, "")
                    for key in (
                        "CONDA_DEFAULT_ENV", "CONDA_EXE", "CONDA_PREFIX", "ENV_PREFIX",
                        "JAVA_HOME", "RESOLVED_ENV_PREFIX", "RESOLVED_JAVA_HOME",
                        "RESOLVED_SNPEFF_JAR", "SNPEFF_JAR", "VIRTUAL_ENV",
                    )
                    if os.environ.get(key)
                },
                "action_contract": {
                    key: value for key, value in pending_action.items()
                    if key not in {"code", "command", "messages"}
                },
            }
            executor_metadata = dict(execution_evidence.get("executor_metadata") or {})
            runtime_tool_names = {"bash", "java", "python"}
            for audit in state.get("runtime_audit", []) or []:
                if isinstance(audit, dict):
                    runtime_tool_names.update(str(item) for item in audit.get("tools", []) or [] if str(item))
            execution_evidence["actual_environment"]["resolved_executables"] = {
                name: str(shutil.which(name) or "") for name in sorted(runtime_tool_names)
            }
            if executor_metadata.get("stdout_log"):
                execution_evidence["stdout_log"] = str(executor_metadata["stdout_log"])
            if executor_metadata.get("stderr_log"):
                execution_evidence["stderr_log"] = str(executor_metadata["stderr_log"])
            try:
                prior_records = RecoveryStateStore(self.data_path, task_id).load_execution_records()
                execution_evidence["recent_attempts"] = [
                    {
                        "execution_id": str(item.get("execution_id") or ""),
                        "step_id": str(item.get("step_id") or ""),
                        "action_id": str(item.get("action_id") or ""),
                        "action_kind": str(item.get("action_kind") or ""),
                        "command_role": str(item.get("command_role") or ""),
                        "exit_code": item.get("exit_code"),
                        "failure_fingerprint": str(item.get("failure_fingerprint") or ""),
                    }
                    for item in prior_records[-3:]
                ]
                execution_evidence = enrich_execution_evidence(
                    {**dict(state), "pending_action": pending_action},
                    execution_evidence,
                )
                record = ExecutionRecord.from_mapping(execution_evidence)
                RecoveryStateStore(self.data_path, task_id).save_execution_record(record)
                provenance_plan = load_plan_state(self.data_path, task_id)
                provenance_result = record_provenance_event(provenance_plan, "execution_finished", {
                    "attempt_id": record.attempt_id,
                    "execution_record": {**execution_evidence, **record.summary()},
                })
                record.produced_artifact_ids = list(provenance_result.get("produced_artifact_ids") or [])
                RecoveryStateStore(self.data_path, task_id).save_execution_record(record)
                save_plan_state(self.data_path, task_id, provenance_plan)
                logger.info(
                    "Execution provenance: task=%s plan=%s step=%s stage=%s action=%s attempt=%s role=%s artifacts=%s source=%s",
                    task_id,
                    record.plan_id,
                    record.step_id,
                    record.stage_id,
                    record.action_id,
                    record.attempt_id,
                    record.command_role,
                    ",".join(record.produced_artifact_ids) or "none",
                    str(pending_action.get("action_source") or "unknown"),
                )
                execution_evidence.update(record.summary())
                # Keep executor-facing aliases while the graph state migrates.
                execution_evidence["returncode"] = record.exit_code
                execution_evidence["script_file"] = record.script_path
                execution_evidence["cwd"] = record.working_directory
            except Exception as exc:
                logger.warning("Could not persist execution record for %s: %s", task_id, exc)
            execution_evidence["failure_log_projection"] = failure_log_projection(
                "\n".join([stderr, stdout]),
                log_path=str(execution_evidence.get("stderr_log") or execution_evidence.get("stdout_log") or ""),
            )
            execution_evidence["generated_code"] = self._clip_observation_text(code_to_run, 4000)
            # Raw streams are durable files. Keep only a bounded current-step
            # projection in graph state so one failed build cannot inflate all
            # downstream prompts and checkpoints.
            execution_evidence.pop("stdout", None)
            execution_evidence.pop("stderr", None)
            execution_evidence.pop("trace_text", None)
            execution_evidence.pop("code", None)
            conflict_update = self._knowledge_conflict_update(
                task_id,
                {
                    **dict(state),
                    "execution_evidence": execution_evidence,
                    "runtime_status": self._runtime_status_for_task(task_id, runtime_skills_state),
                },
                phase="after_execute",
            )
            if bypass_step_gate:
                return {
                    **conflict_update,
                    "messages": [HumanMessage(content=obs)],
                    "plan_progress": plan_progress,
                    "runtime_status": self._runtime_status_for_task(task_id, runtime_skills_state),
                    "execution_evidence": execution_evidence,
                    "root_cause_analysis": execution_evidence.get("root_cause_analysis", {}),
                    "command_chain": execution_evidence.get("command_chain", {}),
                    "pending_action": {},
                    "pending_recovery_action": {
                        "type": "same_step_generate",
                        "reason": (
                            "recovery_observation_available"
                            if command_role == "recovery"
                            else "diagnostic_observation_available"
                        ),
                        "step_id": str(execution_step.get("step_id") or ""),
                        "failure_owner": (
                            RecoveryFailureOwner.SCRIPT.value
                            if command_role == "recovery"
                            else RecoveryFailureOwner.DIAGNOSTIC.value
                        ),
                    },
                    "required_next_action": "SAME_STEP_GENERATE",
                    "failure_owner": (
                        RecoveryFailureOwner.SCRIPT.value
                        if command_role == "recovery"
                        else RecoveryFailureOwner.DIAGNOSTIC.value
                    ),
                    "recovery_scope": "CURRENT_STEP",
                    "recovery_phase": "diagnose",
                    "supervisor_verdict": "",
                    "completion_gate_result": {},
                    "retry_count": retry_count,
                    "next_step": "generate",
                }
            if runtime_install_blocked:
                if lightweight_mode:
                    await self._finalize_task_memory(
                        task_id,
                        "failed",
                        state.get("messages", []) + [HumanMessage(content=obs), AIMessage(content=f"<observation>{final_res}</observation>")],
                        {
                            "task_mode": task_mode,
                            "reason": final_res[:2000],
                            "plan_progress": plan_progress,
                        },
                    )
                return {
                    **conflict_update,
                    "messages": [HumanMessage(content=obs), AIMessage(content=f"<observation>{final_res}</observation>")],
                    "plan_progress": plan_progress,
                    "selected_skills": state.get("selected_skills", []) or runtime_skills_after_execute,
                    "active_skills": runtime_skills_after_execute,
                    "runtime_status": self._runtime_status_for_task(task_id, runtime_skills_after_execute),
                    "execution_evidence": execution_evidence,
                    "root_cause_analysis": execution_evidence.get("root_cause_analysis", {}),
                    "command_chain": execution_evidence.get("command_chain", {}),
                    "retry_count": retry_count,
                    "failure_owner": RecoveryFailureOwner.DEPENDENCY.value,
                    "next_step": "finish" if lightweight_mode else self._next_step_after_runtime_block(task_id),
                }
            if runtime_missing:
                return {
                    **conflict_update,
                    "messages": [HumanMessage(content=obs), AIMessage(content=runtime_install_redirect)],
                    "plan_progress": plan_progress,
                    "selected_skills": state.get("selected_skills", []) or runtime_skills_after_execute,
                    "active_skills": runtime_skills_after_execute,
                    "runtime_status": self._runtime_status_for_task(task_id, runtime_skills_after_execute),
                    "execution_evidence": execution_evidence,
                    "root_cause_analysis": execution_evidence.get("root_cause_analysis", {}),
                    "command_chain": execution_evidence.get("command_chain", {}),
                    "retry_count": retry_count,
                    "failure_owner": RecoveryFailureOwner.DEPENDENCY.value,
                    "next_step": "install_subagent",
                }
            if task_mode in {"readonly", "simple_execute"}:
                await self._finalize_task_memory(
                    task_id,
                    "completed" if returncode == 0 and not validation_failed else "failed",
                    state.get("messages", []) + [HumanMessage(content=obs)],
                    {
                        "task_mode": task_mode,
                        "script_file": script_file,
                        "returncode": returncode,
                        "validation_level": validation_level,
                        "validation": validation_result,
                    },
                )
            return {
                **conflict_update,
                "messages": [HumanMessage(content=obs)],
                "plan_progress": plan_progress,
                "runtime_status": self._runtime_status_for_task(task_id, runtime_skills_state),
                "execution_evidence": execution_evidence,
                "root_cause_analysis": execution_evidence.get("root_cause_analysis", {}),
                "command_chain": execution_evidence.get("command_chain", {}),
                "pending_recovery_action": {},
                "required_next_action": "",
                "recovery_scope": "",
                "recovery_phase": "",
                "supervisor_verdict": "",
                "retry_count": retry_count,
                "next_step": "finish" if task_mode in {"readonly", "simple_execute"} else "supervisor",
            }

        execute_node = error_recovery_middleware(
            self.llm,
            self.retriever,
            skill_manager=self.skill_manager,
            data_path=self.data_path,
        )(execute_logic)

        # ════════════════════════════════════════════
        # Node 4: Supervisor（新节点）
        # ════════════════════════════════════════════
        async def supervisor(state: AgentState):
            """Controller adapter for fact-only Supervisor validation."""
            output.print_node_header("supervisor")
            task_id = state.get("task_id", "default")
            conflict_update = self._knowledge_conflict_update(
                task_id,
                dict(state),
                phase="before_supervisor",
            )
            supervisor_state = {**dict(state), **conflict_update}
            raw_update = await self.supervisor_node.run(supervisor_state)
            update = {**conflict_update, **(raw_update if isinstance(raw_update, dict) else {})}
            execution = dict(state.get("execution_evidence") or {})
            validation = ValidationEvidence.from_mapping(update.get("validation_evidence") or {})
            plan_state = load_plan_state(self.data_path, task_id)
            _, current_step = current_plan_step_with_index(plan_state)
            current_step = current_step or {}
            current_step_id = str(current_step.get("step_id") or validation.step_id or "")
            command_role = str(validation.command_role or execution.get("command_role") or "analysis_execution").lower()
            execution_purpose = str(execution.get("purpose") or "analysis").strip().lower()

            def replace_validation(**changes: Any) -> None:
                nonlocal validation
                value = validation.to_dict()
                for key, item in changes.items():
                    if key in {"errors", "warnings", "info", "recoverability_hints"}:
                        value[key] = list(dict.fromkeys([*list(value.get(key) or []), *list(item or [])]))
                    else:
                        value[key] = item
                validation = ValidationEvidence.from_mapping(value)
                update["validation_evidence"] = validation.to_dict()
                update["supervisor_verdict"] = self.supervisor_node._legacy_compat_status(validation.status)

            execution_contract_errors: List[str] = []
            if not str(execution.get("execution_id") or execution.get("attempt_id") or ""):
                execution_contract_errors.append("No persisted ExecutionRecord identifies the current attempt.")
            if current_step_id and str(execution.get("step_id") or "") not in {"", current_step_id}:
                execution_contract_errors.append(
                    f"ExecutionRecord belongs to step {execution.get('step_id')}, not {current_step_id}."
                )
            if str(execution.get("execution_status") or "").lower() == "failed" and validation.status == ValidationStatus.VALID.value:
                execution_contract_errors.append("ExecutionRecord status is failed despite a valid return-code projection.")
            if execution_contract_errors:
                replace_validation(
                    status=ValidationStatus.INVALID.value,
                    errors=execution_contract_errors,
                    recoverability_hints=["execution_contract"],
                )

            conflict_summary = conflict_update.get("knowledge_conflict_summary") or {}
            blocking_conflicts = list(conflict_summary.get("blocking_conflicts") or []) if isinstance(conflict_summary, dict) else []
            conflict_decisions = list(conflict_summary.get("decisions") or []) if isinstance(conflict_summary, dict) else []
            plan_conflict = any(
                isinstance(item, dict)
                and bool(item.get("requires_plan_revision"))
                and str(item.get("decision") or "") in {
                    "local_replan", "stage_replan", "downstream_replan", "global_replan",
                }
                for item in conflict_decisions
            )
            if blocking_conflicts:
                replace_validation(
                    status=ValidationStatus.INVALID.value,
                    errors=["Unresolved knowledge conflicts: " + ", ".join(map(str, blocking_conflicts[:8]))],
                    recoverability_hints=["knowledge_conflict"],
                )
            validation_payload = validation.to_dict()
            if plan_conflict:
                validation_payload["failure_owner"] = RecoveryFailureOwner.PLAN.value
                validation_payload["contract_violations"] = [
                    "Knowledge conflict resolution explicitly requires a plan revision."
                ]

            def commit_policy(
                *,
                evidence: Optional[Dict[str, Any]] = None,
                completion: Optional[Dict[str, Any]] = None,
            ) -> RecoveryDecision:
                current = load_plan_state(self.data_path, task_id)
                decision = RecoveryPolicy().decide(
                    execution_evidence=execution,
                    validation_evidence=evidence if evidence is not None else validation_payload,
                    progress_snapshot=dict(state.get("recovery_progress") or {}),
                    completion_evidence=completion,
                    budgets=dict((current.get("task_state") or {}).get("budgets") or {}),
                    action_id=str(execution.get("action_id") or execution.get("execution_id") or "supervisor"),
                    step_id=current_step_id,
                    attempt_id=str(execution.get("attempt_id") or execution.get("execution_id") or validation.validation_id),
                )
                decision = contextualize_recovery_decision(decision, source_node="supervisor")
                application = apply_recovery_decision(current, decision)
                save_plan_state(self.data_path, task_id, current)
                update["recovery_decision"] = decision.to_dict()
                update["recovery_application"] = application
                update["current_step_id"] = str(application.get("current_step_id") or "")
                update["current_stage_id"] = str(application.get("current_stage_id") or "")
                update["recovery_status"] = decision.kind
                update["plan_progress"] = render_plan_progress(current)
                update["pending_action"] = {}
                return decision

            if validation.status not in {
                ValidationStatus.VALID.value,
                ValidationStatus.VALID_WITH_WARNINGS.value,
            }:
                decision = commit_policy()
                update.setdefault("messages", []).append(SystemMessage(content=(
                    f"RecoveryPolicy interpreted ValidationEvidence {validation.validation_id} as "
                    f"{decision.kind}; Supervisor supplied facts only."
                )))
            elif execution_purpose == "record_failure":
                reason = "The helper action recorded a blocked business stage; it did not complete the analysis."
                self._finalize_blocked_state(task_id, reason)
                blocked_state = load_plan_state(self.data_path, task_id)
                committed = dict(((blocked_state.get("task_state") or {}).get("recovery_state") or {}).get("last_recovery_decision") or {})
                update["recovery_decision"] = committed
                update["plan_progress"] = render_plan_progress(blocked_state)
            elif command_role in {"probe", "diagnostic", "optional_probe", "capability_probe"}:
                decision = commit_policy()
                update["completion_gate_result"] = {
                    "passed": True,
                    "classification": "probe_evidence_recorded",
                    "formal_artifact_gate_skipped": True,
                }
                update.setdefault("messages", []).append(SystemMessage(content=(
                    "The bounded probe was recorded as capability evidence. No formal artifact or step completion gate ran."
                )))
            else:
                renderer_controlled = bool(
                    command_role == "renderer_invocation"
                    or str(execution.get("action_source") or "") == "manifest_runner"
                )
                renderer_incomplete = False
                if renderer_controlled:
                    try:
                        manifest_path = str(execution.get("manifest_path") or "")
                        raw_manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
                        renderer_plan = load_plan_state(self.data_path, task_id)
                        _, renderer_step = current_plan_step_with_index(renderer_plan)
                        renderer_step = renderer_step or {}
                        if command_role == "renderer_invocation":
                            manifest = register_renderer_manifest(
                                renderer_plan,
                                str(renderer_step.get("step_id") or current_step_id),
                                raw_manifest,
                                manifest_path=manifest_path,
                                task_id=str(task_id),
                                producer_action_id=str(execution.get("action_id") or ""),
                            )
                            renderer_incomplete = True
                        else:
                            manifest = normalize_renderer_manifest(raw_manifest, manifest_path)
                            action_id = str(execution.get("manifest_action_id") or "")
                            action = next(
                                item for item in manifest.get("actions", [])
                                if str(item.get("id") or "") == action_id
                            )
                            action_validation = validate_renderer_action_artifacts(manifest, action)
                            if not action_validation["passed"]:
                                raise ValueError(json.dumps(action_validation, ensure_ascii=False, sort_keys=True))
                            mark_renderer_action_completed(
                                renderer_plan,
                                str(renderer_step.get("step_id") or current_step_id),
                                action_id,
                                {
                                    "execution_id": str(execution.get("execution_id") or ""),
                                    "action_id": str(execution.get("action_id") or ""),
                                    "manifest_fingerprint": str(execution.get("manifest_fingerprint") or ""),
                                    "manifest_script_hash": str(execution.get("manifest_script_hash") or ""),
                                    "artifact_validation": action_validation,
                                },
                            )
                            renderer_execution = dict(renderer_step.get("renderer_execution") or {})
                            renderer_incomplete = not renderer_manifest_complete(manifest, renderer_execution)
                        save_plan_state(self.data_path, task_id, renderer_plan)
                    except (OSError, json.JSONDecodeError, StopIteration, ValueError) as exc:
                        replace_validation(
                            status=ValidationStatus.INVALID.value,
                            errors=[f"Renderer manifest/action contract failed: {exc}"],
                            recoverability_hints=["artifact_contract"],
                        )
                        validation_payload = validation.to_dict()
                        decision = commit_policy(evidence=validation_payload)
                        update.setdefault("messages", []).append(SystemMessage(content=(
                            f"Renderer contract validation failed; RecoveryPolicy selected {decision.kind}."
                        )))
                        renderer_incomplete = True

                if "recovery_decision" not in update and renderer_incomplete:
                    decision = commit_policy()
                    update["completion_gate_result"] = {
                        "passed": True,
                        "classification": (
                            "renderer_manifest_registered"
                            if command_role == "renderer_invocation"
                            else "manifest_action_completed"
                        ),
                    }
                elif "recovery_decision" not in update and not self._execution_purpose_completes_step(
                    current_step,
                    execution_purpose,
                ):
                    decision = commit_policy()
                    update.setdefault("messages", []).append(SystemMessage(content=(
                        "The execution produced validation evidence but did not satisfy the business purpose of the current step."
                    )))
                elif "recovery_decision" not in update:
                    gate_state = load_plan_state(self.data_path, task_id)
                    step_gate = CompletionGate.validate_current_step(self.data_path, task_id, gate_state)
                    if (
                        not step_gate.passed
                        and step_gate.classification == "contract_reconciliation_required"
                        and isinstance(step_gate.reconciliation, dict)
                    ):
                        reconciliation = step_gate.reconciliation
                        events = apply_artifact_reconciliation(
                            gate_state,
                            str(reconciliation.get("step_id") or current_step_id),
                            reconciliation.get("manifest") or {},
                            manifest_path=str(reconciliation.get("manifest_path") or ""),
                        )
                        if events:
                            save_plan_state(self.data_path, task_id, gate_state)
                            gate_state = load_plan_state(self.data_path, task_id)
                            step_gate = CompletionGate.validate_current_step(self.data_path, task_id, gate_state)
                    update["completion_gate_result"] = step_gate.to_dict()
                    if step_gate.passed:
                        decision = commit_policy(
                            completion={
                                "completion_satisfied": True,
                                "task_complete": False,
                                "evidence": [validation.validation_id, "current_step_completion_gate_passed"],
                            }
                        )
                        if self._plan_tracker_is_complete(task_id):
                            next_batch = self._activate_next_workflow_batch_after_plan_completion(state, task_id)
                            if next_batch:
                                update.update(next_batch)
                    else:
                        failed_validation = validation.to_dict()
                        failed_validation["status"] = ValidationStatus.INVALID.value
                        failed_validation["errors"] = list(dict.fromkeys([
                            *validation.errors,
                            str(step_gate.message or step_gate.feedback or "Current step contract failed."),
                        ]))
                        decision = commit_policy(evidence=failed_validation)

            update["validation_evidence"] = validation.to_dict()
            update["last_supervisor_feedback"] = {
                "validation_id": validation.validation_id,
                "status": validation.status,
                "legacy_compat_only": True,
                "step_id": current_step_id,
                "execution_id": str(execution.get("execution_id") or execution.get("action_id") or ""),
            }
            await self._observe_session_memory(
                task_id,
                "supervisor",
                state.get("messages", []) + list(update.get("messages") or []),
                {
                    "validation_status": validation.status,
                    "validation_id": validation.validation_id,
                    "recovery_decision": str((update.get("recovery_decision") or {}).get("kind") or ""),
                },
            )
            return update

        async def _legacy_supervisor_controller(state: AgentState):
            """Deprecated compatibility node; not registered in the active graph."""
            output.print_node_header("supervisor")
            task_id = state.get("task_id", "default")
            conflict_update = self._knowledge_conflict_update(
                task_id,
                dict(state),
                phase="before_supervisor",
            )
            supervisor_state = {**dict(state), **conflict_update}
            if state.get("final_audit_requested"):
                update = await self.supervisor_node.final_audit(supervisor_state)
            else:
                update = await self.supervisor_node.run(supervisor_state)
            update = {**conflict_update, **(update if isinstance(update, dict) else {})}
            execution = state.get("execution_evidence") or {}
            current_plan_state = load_plan_state(self.data_path, task_id)
            _, current_plan_step = current_plan_step_with_index(current_plan_state)
            current_step_id = str(current_plan_step.get("step_id") or "")
            execution_step_id = str(execution.get("step_id") or "") if isinstance(execution, dict) else ""
            execution_purpose = str(execution.get("purpose") or "analysis").strip().lower() if isinstance(execution, dict) else ""
            renderer_controlled = bool(
                isinstance(execution, dict)
                and (
                    str(execution.get("command_role") or "") == "renderer_invocation"
                    or str(execution.get("action_source") or "") == "manifest_runner"
                )
            )
            renderer_probe = bool(
                isinstance(execution, dict)
                and bool(current_plan_step.get("renderer_composite"))
                and str(execution.get("command_role") or "").lower() in {"diagnostic", "optional_probe"}
                and execution_purpose in {"probe", "validation"}
            )
            provisional_verdict = str(update.get("supervisor_verdict") or "").upper()
            if not state.get("final_audit_requested") and provisional_verdict in {"PASS", "WARN"}:
                execution_contract_error = ""
                if not isinstance(execution, dict) or not str(execution.get("execution_id") or ""):
                    execution_contract_error = "Supervisor has no persisted ExecutionRecord for the current step."
                elif current_step_id and execution_step_id != current_step_id:
                    execution_contract_error = (
                        f"ExecutionRecord belongs to step {execution_step_id or '<missing>'}, not current step {current_step_id}."
                    )
                elif str(execution.get("execution_status") or "").lower() != "succeeded":
                    execution_contract_error = "The current ExecutionRecord is not successful."
                if execution_contract_error:
                    update["supervisor_verdict"] = "RETRY"
                    update["next_step"] = "generate"
                    failed_execution = (
                        isinstance(execution, dict)
                        and bool(str(execution.get("execution_id") or ""))
                        and str(execution.get("execution_status") or "").lower() != "succeeded"
                    )
                    update["required_next_action"] = "SCRIPT_PATCH" if failed_execution else "PROBE"
                    update["failure_owner"] = (
                        RecoveryFailureOwner.SCRIPT.value
                        if failed_execution
                        else RecoveryFailureOwner.VERIFICATION.value
                    )
                    update["recovery_scope"] = "CURRENT_STEP"
                    update["recovery_phase"] = "apply_repair" if failed_execution else "probe"
                    update["pending_recovery_action"] = {
                        "type": "script_patch" if failed_execution else "diagnostic_probe",
                        "reason": execution_contract_error,
                        "failure_owner": update["failure_owner"],
                    }
                    update.setdefault("messages", []).append(SystemMessage(content=execution_contract_error))
                elif execution_purpose == "record_failure":
                    reason = "The helper command successfully recorded that the business step is blocked; it did not complete the analysis."
                    progress = self._finalize_blocked_state(task_id, reason)
                    update.update({
                        "supervisor_verdict": "ESCALATE",
                        "next_step": "finish",
                        "plan_progress": progress,
                        "pending_action": {},
                        "pending_recovery_action": {},
                        "task_status": TASK_STATUS_BLOCKED,
                    })
                    update.setdefault("messages", []).append(SystemMessage(content=reason))
                elif renderer_probe:
                    update.update({
                        "next_step": "generate",
                        "pending_action": {},
                        "pending_recovery_action": {},
                        "required_next_action": "",
                        "failure_owner": "",
                        "recovery_scope": "",
                        "recovery_phase": "",
                        "action_status": "completed",
                        "step_status": "in_progress",
                        "completion_gate_result": {
                            "passed": True,
                            "classification": "renderer_probe_recorded",
                            "formal_artifact_gate_skipped": True,
                        },
                    })
                    update.setdefault("messages", []).append(SystemMessage(content=(
                        "The bounded Renderer capability probe completed and was recorded as diagnostic evidence. "
                        "It did not invoke the Renderer, consume Renderer invocation budget, run the formal Renderer "
                        "artifact gate, or complete the composite step."
                    )))
                elif not renderer_controlled and not self._execution_purpose_completes_step(
                    current_plan_step,
                    execution_purpose,
                ):
                    update["supervisor_verdict"] = "RETRY"
                    update["next_step"] = "generate"
                    update.setdefault("messages", []).append(SystemMessage(content=(
                        "The command produced diagnostic/validation evidence only. The current business analysis step "
                        "declares a different purpose and remains incomplete; use the evidence for a concrete repair "
                        "or analysis action."
                    )))
            if renderer_probe and str(update.get("supervisor_verdict") or "").upper() in {"PASS", "WARN"}:
                update["last_supervisor_feedback"] = self._supervisor_feedback_snapshot(
                    update,
                    state,
                    task_id,
                )
                await self._observe_session_memory(
                    task_id,
                    "supervisor",
                    state.get("messages", []) + list(update.get("messages") or []),
                    {
                        "next_step": "generate",
                        "supervisor_verdict": update.get("supervisor_verdict"),
                        "renderer_probe": True,
                    },
                )
                return update
            effective_renderer_verdict = str(update.get("supervisor_verdict") or "").upper()
            if renderer_controlled and effective_renderer_verdict in {"PASS", "WARN"}:
                renderer_result: Dict[str, Any] = {}
                renderer_plan = load_plan_state(self.data_path, task_id)
                _, renderer_step = current_plan_step_with_index(renderer_plan)
                renderer_step = renderer_step or {}
                role = str(execution.get("command_role") or "")
                manifest_path = str(execution.get("manifest_path") or "")
                try:
                    raw_manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
                    if role == "renderer_invocation":
                        manifest = register_renderer_manifest(
                            renderer_plan,
                            str(renderer_step.get("step_id") or ""),
                            raw_manifest,
                            manifest_path=manifest_path,
                            task_id=str(task_id),
                            producer_action_id=str(execution.get("action_id") or ""),
                        )
                    else:
                        manifest = normalize_renderer_manifest(raw_manifest, manifest_path)
                    requirement_result = evaluate_requirements(manifest.get("requirements", []) or [])
                    if requirement_result["blocked"]:
                        raise ValueError(
                            "Missing required renderer requirements: "
                            + ", ".join(requirement_result["missing_required"] + requirement_result["missing_conditional"])
                        )
                    if requirement_result["needs_reconciliation"]:
                        raise ValueError(
                            "Renderer requirements need contract reconciliation: "
                            + ", ".join(requirement_result["unknown"])
                        )
                    update["missing_required_inputs"] = []
                    update["skipped_optional_checks"] = list(requirement_result["skipped_optional"])
                    update["proposed_requirements"] = []
                    update["contract_status"] = "valid"
                    update["action_status"] = "completed"
                    update["step_status"] = "in_progress"
                    if role == "renderer_invocation":
                        save_plan_state(self.data_path, task_id, renderer_plan)
                        renderer_result = {
                            "messages": list(update.get("messages") or []) + [SystemMessage(content=(
                                "Renderer manifest registered. Subsequent professional actions are controller-selected "
                                "from the manifest; this invocation does not complete the composite plan step."
                            ))],
                            "supervisor_verdict": effective_renderer_verdict,
                            "next_step": "generate",
                            "pending_action": {},
                            "pending_recovery_action": {},
                            "required_next_action": "",
                            "failure_owner": "",
                            "recovery_scope": "",
                            "recovery_phase": "",
                            "action_status": "completed",
                            "step_status": "in_progress",
                            "contract_status": "valid",
                            "missing_required_inputs": [],
                            "skipped_optional_checks": list(requirement_result["skipped_optional"]),
                            "proposed_requirements": [],
                            "completion_gate_result": {
                                "passed": True,
                                "classification": "renderer_manifest_registered",
                            },
                        }
                    else:
                        action_id = str(execution.get("manifest_action_id") or "")
                        action = next(
                            item for item in manifest.get("actions", [])
                            if str(item.get("id") or "") == action_id
                        )
                        action_validation = validate_renderer_action_artifacts(manifest, action)
                        if not action_validation["passed"]:
                            raise ValueError(
                                "Renderer action artifact validation failed: "
                                + json.dumps(action_validation, ensure_ascii=False, sort_keys=True)
                            )
                        mark_renderer_action_completed(
                            renderer_plan,
                            str(renderer_step.get("step_id") or ""),
                            action_id,
                            {
                                "execution_id": str(execution.get("execution_id") or ""),
                                "action_id": str(execution.get("action_id") or ""),
                                "manifest_fingerprint": str(execution.get("manifest_fingerprint") or ""),
                                "manifest_script_hash": str(execution.get("manifest_script_hash") or ""),
                                "artifact_validation": action_validation,
                            },
                        )
                        save_plan_state(self.data_path, task_id, renderer_plan)
                        renderer_execution = dict(renderer_step.get("renderer_execution") or {})
                        if not renderer_manifest_complete(manifest, renderer_execution):
                            renderer_result = {
                                "messages": list(update.get("messages") or []) + [SystemMessage(content=(
                                    f"Renderer action {action_id} passed its declared contract. "
                                    "The composite step remains in progress; scheduling the next manifest action."
                                ))],
                                "supervisor_verdict": effective_renderer_verdict,
                                "next_step": "generate",
                                "pending_action": {},
                                "pending_recovery_action": {},
                                "required_next_action": "",
                                "failure_owner": "",
                                "recovery_scope": "",
                                "recovery_phase": "",
                                "action_status": "completed",
                                "step_status": "in_progress",
                                "contract_status": "valid",
                                "missing_required_inputs": [],
                                "skipped_optional_checks": list(requirement_result["skipped_optional"]),
                                "proposed_requirements": [],
                                "completion_gate_result": {
                                    "passed": True,
                                    "classification": "manifest_action_completed",
                                    "action_id": action_id,
                                },
                            }
                except (OSError, json.JSONDecodeError, StopIteration, ValueError) as exc:
                    update.update({
                        "supervisor_verdict": "RETRY",
                        "next_step": "generate",
                        "required_next_action": "RECONCILE_ARTIFACTS",
                        "failure_owner": RecoveryFailureOwner.ARTIFACT_CONTRACT.value,
                        "recovery_scope": "CURRENT_STEP",
                        "recovery_phase": "reconcile",
                        "action_status": "failed",
                        "step_status": "in_progress",
                        "contract_status": "conflict",
                        "pending_recovery_action": {
                            "type": "reconcile_artifacts",
                            "reason": str(exc),
                            "failure_owner": RecoveryFailureOwner.ARTIFACT_CONTRACT.value,
                        },
                    })
                    update.setdefault("messages", []).append(SystemMessage(content=str(exc)))
                if renderer_result:
                    renderer_result["last_supervisor_feedback"] = self._supervisor_feedback_snapshot(
                        renderer_result,
                        state,
                        task_id,
                    )
                    await self._observe_session_memory(
                        task_id,
                        "supervisor",
                        state.get("messages", []) + list(renderer_result.get("messages") or []),
                        {
                            "next_step": "generate",
                            "supervisor_verdict": effective_renderer_verdict,
                            "renderer_action": str(execution.get("manifest_action_id") or role),
                        },
                    )
                    return renderer_result
            conflict_summary = conflict_update.get("knowledge_conflict_summary", {})
            blocking_conflicts = conflict_summary.get("blocking_conflicts", []) if isinstance(conflict_summary, dict) else []
            if blocking_conflicts and str(update.get("supervisor_verdict") or "") in {"PASS", "WARN"}:
                update["supervisor_verdict"] = "RETRY"
                update["next_step"] = "generate"
                update.setdefault("messages", []).append(
                    SystemMessage(
                        content=(
                            "Knowledge conflict audit blocked Supervisor completion. Unresolved blocking conflicts: "
                            + ", ".join(str(item) for item in blocking_conflicts[:8])
                            + ". Resolve or revalidate them before PASS."
                        )
                    )
                )
            decisions = conflict_summary.get("decisions", []) if isinstance(conflict_summary, dict) else []
            if any(
                isinstance(item, dict)
                and item.get("requires_plan_revision")
                and item.get("decision") in {"local_replan", "stage_replan", "downstream_replan", "global_replan"}
                for item in decisions
            ):
                update.setdefault("messages", []).append(
                    SystemMessage(content="Knowledge conflict requires an auditable plan revision before the next execution step.")
                )
                update["next_step"] = "planner"
                update["required_next_action"] = "PLAN_PATCH"
                update["failure_owner"] = RecoveryFailureOwner.PLAN.value
                update["recovery_scope"] = "CURRENT_STEP"
                update["recovery_phase"] = "plan_repair"
            update_messages = update.get("messages", []) if isinstance(update, dict) else []
            await self._observe_session_memory(
                task_id,
                "supervisor",
                state.get("messages", []) + update_messages,
                {
                    "next_step": update.get("next_step") if isinstance(update, dict) else "",
                    "retry_count": update.get("retry_count") if isinstance(update, dict) else "",
                    "supervisor_verdict": update.get("supervisor_verdict") if isinstance(update, dict) else "",
                },
            )
            if isinstance(update, dict):
                verdict = str(update.get("supervisor_verdict") or "")
                warn_requires_followup = verdict == "WARN" and self._supervisor_warn_requires_followup(update_messages)
                if warn_requires_followup:
                    warn_step_gate_state = load_plan_state(self.data_path, task_id)
                    warn_step_gate = CompletionGate.validate_current_step(
                        self.data_path,
                        task_id,
                        warn_step_gate_state,
                    )
                    if self._supervisor_warn_is_acceptably_artifact_bounded(
                        update_messages,
                        warn_step_gate_state,
                        warn_step_gate,
                    ):
                        update.setdefault("messages", []).append(
                            SystemMessage(
                                content=(
                                    "Supervisor WARN requested an additional artifact inspection, but the deterministic "
                                    "step completion gate already verified the final/status artifacts for this output step. "
                                    "Treating the WARN as advisory and allowing normal completion checks to proceed."
                                )
                            )
                        )
                        warn_requires_followup = False
                if warn_requires_followup:
                    followup_text = "\n".join(
                        str(getattr(msg, "content", msg) or "") for msg in update_messages
                    )[:1200]
                    plan_progress = self._record_plan_current_evidence(
                        task_id,
                        "warn_followup_required",
                        followup_text,
                    )
                    update.setdefault("messages", []).append(
                        SystemMessage(
                            content=(
                                "Supervisor WARN contains a required follow-up action, so this step is not complete yet. "
                                "Continue with Generate and perform the concrete action from the supervisor feedback before "
                                "advancing or finishing."
                            )
                        )
                    )
                    update["supervisor_verdict"] = "RETRY"
                    update["next_step"] = "generate"
                    update["plan_progress"] = plan_progress
                elif verdict in {"PASS", "WARN"}:
                    step_gate_state = load_plan_state(self.data_path, task_id)
                    step_gate = CompletionGate.validate_current_step(
                        self.data_path,
                        task_id,
                        step_gate_state,
                    )
                    update["completion_gate_result"] = step_gate.to_dict()
                    if (
                        not step_gate.passed
                        and step_gate.classification == "contract_reconciliation_required"
                        and isinstance(step_gate.reconciliation, dict)
                    ):
                        reconciliation = step_gate.reconciliation
                        events = apply_artifact_reconciliation(
                            step_gate_state,
                            str(reconciliation.get("step_id") or current_step_id),
                            reconciliation.get("manifest") or {},
                            manifest_path=str(reconciliation.get("manifest_path") or ""),
                        )
                        if events:
                            save_plan_state(self.data_path, task_id, step_gate_state)
                            step_gate_state = load_plan_state(self.data_path, task_id)
                            step_gate = CompletionGate.validate_current_step(
                                self.data_path,
                                task_id,
                                step_gate_state,
                            )
                            update["completion_gate_result"] = step_gate.to_dict()
                            update.setdefault("messages", []).append(SystemMessage(content=(
                                "Renderer artifact manifest reconciled the current plan contract without rerunning "
                                "the successful command. Completion Gate was re-evaluated against canonical paths."
                            )))
                    structured_recovery_action = ""
                    if not step_gate.passed:
                        update = await self._adjudicate_post_review_recovery(state, update, task_id)
                        structured_recovery_action = str(
                            (update.get("recovery_decision") or {}).get("recommended_action") or ""
                        )
                    if structured_recovery_action:
                        plan_progress = self._record_plan_current_evidence(
                            task_id,
                            "structured_recovery_decision",
                            json.dumps(update.get("recovery_decision") or {}, ensure_ascii=False)[:3000],
                        )
                        update["plan_progress"] = plan_progress
                        if structured_recovery_action == RecoveryAction.RECONCILE_AND_CONTINUE.value:
                            # Reconciliation was already attempted above. A still-failing
                            # gate means the canonical producer/output still needs a
                            # current-step repair; it is not evidence that the Plan is wrong.
                            update["next_step"] = "generate"
                            update["required_next_action"] = "SCRIPT_PATCH"
                            update["failure_owner"] = RecoveryFailureOwner.GENERATED_ACTION.value
                            update["recovery_scope"] = "CURRENT_STEP"
                    elif not step_gate.passed and step_gate.classification == "diagnostic_probe_required":
                        plan_progress = self._record_plan_current_evidence(
                            task_id,
                            "artifact_contract_diagnostic_required",
                            step_gate.feedback,
                        )
                        update.setdefault("messages", []).append(SystemMessage(content=(
                            "Completion Gate found bounded artifact candidates but no authoritative manifest mapping. "
                            "This is a control-plane contract ambiguity, not an execution retry. Planner must perform "
                            "a local contract repair or preserve a bounded read-only diagnostic step; do not rerun the "
                            "successful producer command.\n\n" + step_gate.feedback
                        )))
                        update["supervisor_verdict"] = "WARN"
                        update["next_step"] = "generate"
                        update["required_next_action"] = "DIAGNOSTIC_PROBE"
                        update["plan_progress"] = plan_progress
                    elif not step_gate.passed:
                        plan_progress = self._record_plan_current_evidence(
                            task_id,
                            "step_completion_gate_failed",
                            step_gate.message,
                        )
                        gate_message = (
                            "Step completion validation failed.\n\n"
                            f"{step_gate.feedback}\n\n"
                            "Do not advance to the next plan step yet. Continue or repair the current step, "
                            "then run a concrete verification command."
                        )
                        update.setdefault("messages", []).append(SystemMessage(content=gate_message))
                        update["supervisor_verdict"] = "RETRY"
                        update["next_step"] = "generate"
                        update["plan_progress"] = plan_progress
                    else:
                        plan_progress = self._mark_plan_current_done(
                            task_id,
                            f"supervisor_verdict: {verdict}",
                            source_node="supervisor",
                        )
                        converged_steps, converged_progress = self._advance_satisfied_validation_steps(
                            task_id,
                            execution if isinstance(execution, dict) else {},
                            source_node="supervisor",
                        )
                        if converged_steps:
                            plan_progress = converged_progress
                            update.setdefault("messages", []).append(SystemMessage(content=(
                                f"Controller linked the successful ExecutionEvidence to {converged_steps} consecutive "
                                "validation step(s), verified each step's success criteria through CompletionGate, "
                                "and advanced without repeating the same check."
                            )))
                        update["plan_progress"] = plan_progress
                        committed_plan = load_plan_state(self.data_path, task_id)
                        committed_recovery = dict(
                            ((committed_plan.get("task_state") or {}).get("recovery_state") or {})
                        )
                        committed_decision = dict(committed_recovery.get("last_recovery_decision") or {})
                        update["pending_recovery_action"] = {}
                        update["recovery_phase"] = "committed"
                        update["recovery_status"] = str(committed_decision.get("kind") or "COMPLETE_STEP")
                        update["retry_count"] = int(
                            (((committed_plan.get("task_state") or {}).get("budgets") or {}).get("execution_retry_count"))
                            or 0
                        )
                        update["recovery_decision"] = committed_decision
                        update["next_step"] = str(committed_decision.get("next_route") or "generate")
                        update["verification_checks"] = []
                        update["forbidden_recovery_actions"] = []
                        for field in SUPERVISOR_RETRY_FIELDS:
                            update[field] = ""
                        if self._plan_tracker_is_complete(task_id):
                            final_gate_state = load_plan_state(self.data_path, task_id)
                            final_gate = CompletionGate.validate_final(
                                self.data_path,
                                task_id,
                                final_gate_state,
                                state.get("current_plan", ""),
                                require_execution_evidence=True,
                                require_supervisor_evidence=True,
                            )
                            update["completion_gate_result"] = final_gate.to_dict()
                            if final_gate.passed:
                                next_batch_update = self._activate_next_workflow_batch_after_plan_completion(
                                    state,
                                    task_id,
                                )
                                if next_batch_update:
                                    update.update(next_batch_update)
                                    update["supervisor_verdict"] = "PASS"
                                    update["plan_progress"] = ""
                                else:
                                    completed_state = load_plan_state(self.data_path, task_id)
                                    set_task_status(completed_state, TASK_STATUS_COMPLETED, "final completion gate passed")
                                    save_plan_state(self.data_path, task_id, completed_state)
                                    update["next_step"] = "finish"
                            else:
                                reopened_state = load_plan_state(self.data_path, task_id)
                                reopen_last_done_step(
                                    reopened_state,
                                    f"final_completion_gate_failed: {final_gate.message}",
                                )
                                plan_progress = self._persist_plan_tracker(task_id, reopened_state)
                                gate_message = (
                                    "Final completion validation failed.\n\n"
                                    f"{final_gate.feedback}\n\n"
                                    "The plan was marked complete, but the task is not ready to finish. "
                                    "Repair the reopened final/output step and verify the artifacts."
                                )
                                update.setdefault("messages", []).append(SystemMessage(content=gate_message))
                                update["supervisor_verdict"] = "RETRY"
                                update["next_step"] = "generate"
                                update["plan_progress"] = plan_progress
                                update = await self._adjudicate_post_review_recovery(state, update, task_id)
                elif verdict == "RETRY":
                    update = await self._adjudicate_post_review_recovery(state, update, task_id)
                    plan_progress = self._record_plan_current_evidence(
                        task_id,
                        "structured_recovery_decision",
                        json.dumps(update.get("recovery_decision") or {}, ensure_ascii=False)[:3000],
                    )
                    update["plan_progress"] = plan_progress
                elif verdict == "ESCALATE":
                    if (
                        str(state.get("task_mode") or "managed").lower() == "managed"
                        and not bool(state.get("non_interactive"))
                    ):
                        plan_progress = self._record_plan_current_evidence(
                            task_id,
                            "supervisor_escalation_waiting_user",
                            "Automatic retries were exhausted without a verified completion.",
                        )
                        failed_checks = (update.get("supervisor_feedback") or {}).get("failed_checks", [])
                        reason = "; ".join(
                            str(item.get("message") or "") for item in failed_checks[:3] if isinstance(item, dict)
                        ) or "Supervisor exhausted automatic retries for the current step."
                        question = (
                            f"Automatic recovery could not verify the current step: {reason} "
                            "Please provide a different recovery direction, or confirm that this stage should be recorded as blocked."
                        )
                        update.update({
                            "plan_progress": plan_progress,
                            "next_step": "ask_user",
                            "pending_human_question": {
                                "type": "supervisor_escalation",
                                "stage": "supervisor",
                                "question": question,
                                "resume_step": "generate",
                            },
                            "task_status": TASK_STATUS_WAITING_USER,
                        })
                    else:
                        plan_progress = self._mark_plan_current_blocked(task_id, "supervisor_verdict: ESCALATE")
                        update["plan_progress"] = plan_progress
                        update["next_step"] = self._next_step_after_runtime_block(task_id)
            if isinstance(update, dict) and str(update.get("supervisor_verdict") or "").upper() == "RETRY":
                if missing_supervisor_retry_fields(update):
                    retry_text = "\n".join(
                        str(getattr(message, "content", message) or "")
                        for message in list(update.get("messages", []) or [])[-4:]
                    )
                    retry_state = {**dict(state), **update}
                    contract = self.supervisor_node._retry_contract(
                        retry_state,
                        [],
                        fallback_reason=self._clip_observation_text(retry_text, 3000) or "Supervisor requested retry.",
                    )
                    if update.get("next_step") == "planner":
                        contract.update({
                            "required_next_action": "PLAN_PATCH",
                            "failure_owner": RecoveryFailureOwner.PLAN.value,
                            "recovery_scope": "PLAN",
                            "recovery_phase": "plan_repair",
                        })
                    update.update(contract)
            update = self._commit_legacy_recovery_projection(
                state,
                update,
                task_id,
                source_node="supervisor",
            )
            effective_verdict = str(update.get("supervisor_verdict") or "").upper() if isinstance(update, dict) else ""
            if provisional_verdict and effective_verdict and effective_verdict != provisional_verdict:
                logger.warning(
                    "Controller adjusted Supervisor verdict: task=%s step=%s %s->%s",
                    task_id,
                    current_step_id or "-",
                    provisional_verdict,
                    effective_verdict,
                )
                output.print_once(
                    f"supervisor_effective_verdict:{task_id}:{current_step_id}:{effective_verdict}",
                    (
                        f"Controller effective verdict: {effective_verdict} "
                        f"(Supervisor initially reported {provisional_verdict}; controller contract checks adjusted it)."
                    ),
                    Fore.YELLOW,
                )
            if isinstance(update, dict) and update.get("next_step") == "finish":
                verdict = str(update.get("supervisor_verdict") or "")
                final_status = "completed" if verdict in {"PASS", "WARN"} else "failed"
                if not update.get("plan_progress"):
                    if final_status == "failed":
                        update["plan_progress"] = self._mark_plan_current_blocked(
                            task_id,
                            f"supervisor_finished: {str(update.get('supervisor_verdict') or 'finish')}",
                        )
                    else:
                        update["plan_progress"] = render_plan_progress(load_plan_state(self.data_path, task_id))
                await self._finalize_task_memory(
                    task_id,
                    final_status,
                    state.get("messages", []) + update_messages,
                    {"reason": f"Supervisor ended the task with verdict {verdict or 'finish'}."},
                )
            update["last_supervisor_feedback"] = self._supervisor_feedback_snapshot(
                update,
                state,
                task_id,
            )
            final_validation = dict(update.get("validation_evidence") or validation.to_dict())
            attempt_id = str(execution.get("attempt_id") or "")
            if attempt_id:
                try:
                    provenance_plan = load_plan_state(self.data_path, task_id)
                    record_provenance_event(provenance_plan, "validation_recorded", {
                        "attempt_id": attempt_id,
                        "validation": final_validation,
                    })
                    save_plan_state(self.data_path, task_id, provenance_plan)
                    update["provenance_summary"] = provenance_debug_dump(provenance_plan)["summary"]
                    update["provenance_state_path"] = str(plan_state_path(self.data_path, task_id))
                except ValueError as exc:
                    logger.warning("Could not associate validation provenance for %s: %s", task_id, exc)
            return update

        async def supervisor_final_audit(state: AgentState):
            """Controller-owned final gate following fact-only validation."""
            output.print_node_header("supervisor_final_audit")
            task_id = state.get("task_id", "default")
            synced_from_status, reconciled_progress = self._reconcile_completion_candidate_plan(
                task_id,
                dict(state),
            )
            audit_state = dict(state)
            if synced_from_status:
                audit_state["plan_progress"] = reconciled_progress
            conflict_update = self._knowledge_conflict_update(task_id, audit_state, phase="final_audit")
            raw_update = await self.supervisor_node.final_audit({**audit_state, **conflict_update})
            update = {**conflict_update, **(raw_update if isinstance(raw_update, dict) else {})}
            validation = ValidationEvidence.from_mapping(update.get("validation_evidence") or {})
            blocking_conflicts = list(
                ((conflict_update.get("knowledge_conflict_summary") or {}).get("blocking_conflicts") or [])
            )
            validation_payload = validation.to_dict()
            if blocking_conflicts:
                validation_payload["status"] = ValidationStatus.INVALID.value
                validation_payload["errors"] = list(dict.fromkeys([
                    *validation.errors,
                    "Unresolved final-audit knowledge conflicts: " + ", ".join(map(str, blocking_conflicts[:8])),
                ]))
                validation = ValidationEvidence.from_mapping(validation_payload)
                update["validation_evidence"] = validation.to_dict()
                update["supervisor_verdict"] = self.supervisor_node._legacy_compat_status(validation.status)

            plan_state = load_plan_state(self.data_path, task_id)
            execution = dict(state.get("execution_evidence") or {})
            final_gate = CompletionGate.validate_final(
                self.data_path,
                task_id,
                plan_state,
                state.get("current_plan", ""),
                require_execution_evidence=True,
                require_supervisor_evidence=False,
            )
            update["completion_gate_result"] = final_gate.to_dict()
            completion: Dict[str, Any] = {}
            if validation.status in {
                ValidationStatus.VALID.value,
                ValidationStatus.VALID_WITH_WARNINGS.value,
            } and final_gate.passed:
                completion = {
                    "completion_satisfied": True,
                    "task_complete": True,
                    "evidence": [validation.validation_id, "final_completion_gate_passed"],
                }
            elif not final_gate.passed:
                validation_payload = validation.to_dict()
                validation_payload["status"] = ValidationStatus.INVALID.value
                validation_payload["errors"] = list(dict.fromkeys([
                    *validation.errors,
                    str(final_gate.message or final_gate.feedback or "Final completion contract failed."),
                ]))
                validation = ValidationEvidence.from_mapping(validation_payload)
                update["validation_evidence"] = validation.to_dict()
                update["supervisor_verdict"] = self.supervisor_node._legacy_compat_status(validation.status)

            decision = RecoveryPolicy().decide(
                execution_evidence=execution,
                validation_evidence=validation.to_dict(),
                completion_evidence=completion,
                progress_snapshot=dict(state.get("recovery_progress") or {}),
                budgets=dict((plan_state.get("task_state") or {}).get("budgets") or {}),
                action_id=str(execution.get("action_id") or execution.get("execution_id") or "final-audit"),
                step_id=str(state.get("current_step_id") or ""),
                attempt_id=f"final-audit:{task_id}:{validation.validation_id}",
            )
            decision = contextualize_recovery_decision(decision, source_node="supervisor")
            application = apply_recovery_decision(plan_state, decision)
            save_plan_state(self.data_path, task_id, plan_state)
            update["recovery_decision"] = decision.to_dict()
            update["recovery_application"] = application
            update["current_step_id"] = str(application.get("current_step_id") or "")
            update["current_stage_id"] = str(application.get("current_stage_id") or "")
            update["recovery_status"] = decision.kind
            update["plan_progress"] = render_plan_progress(plan_state)
            update["final_audit_requested"] = False
            if decision.kind == RecoveryDecisionKind.COMPLETE_TASK.value:
                update["completion_candidate"] = {}
                await self._finalize_task_memory(
                    task_id,
                    "completed",
                    state.get("messages", []) + list(update.get("messages") or []),
                    {"reason": "ValidationEvidence and final CompletionGate passed."},
                )
            return update

        async def _legacy_supervisor_final_audit(state: AgentState):
            """Deprecated compatibility node; active final audit uses supervisor_final_audit."""
            """Run the objective final audit requested by the main router."""
            output.print_node_header("supervisor_final_audit")
            task_id = state.get("task_id", "default")
            synced_from_status, reconciled_progress = self._reconcile_completion_candidate_plan(
                task_id,
                dict(state),
            )
            audit_state = dict(state)
            if synced_from_status:
                audit_state["plan_progress"] = reconciled_progress
            conflict_update = self._knowledge_conflict_update(
                task_id,
                audit_state,
                phase="final_audit",
            )
            update = await self.supervisor_node.final_audit({**audit_state, **conflict_update})
            update = {**conflict_update, **(update if isinstance(update, dict) else {})}
            if synced_from_status:
                update.setdefault("messages", []).append(SystemMessage(content=(
                    "Controller synchronized the approved checklist from the terminal stage_status.tsv before final audit. "
                    "This is evidence reconciliation, not an LLM checklist mutation."
                )))
                update["plan_progress"] = audit_state.get("plan_progress", "")
            blocking_conflicts = (conflict_update.get("knowledge_conflict_summary") or {}).get("blocking_conflicts", [])
            if blocking_conflicts and str(update.get("supervisor_verdict") or "") in {"PASS", "WARN"}:
                update["supervisor_verdict"] = "RETRY"
                update["next_step"] = "generate"
                update.setdefault("messages", []).append(
                    SystemMessage(
                        content=(
                            "Final audit blocked by unresolved knowledge conflicts: "
                            + ", ".join(str(item) for item in blocking_conflicts[:8])
                        )
                    )
                )
            final_decisions = (conflict_update.get("knowledge_conflict_summary") or {}).get("decisions", [])
            if any(
                isinstance(item, dict)
                and item.get("requires_plan_revision")
                and item.get("decision") in {"local_replan", "stage_replan", "downstream_replan", "global_replan"}
                for item in final_decisions
            ):
                update.setdefault("messages", []).append(
                    SystemMessage(content="Final audit requires an auditable plan revision before execution can resume.")
                )
                update["next_step"] = "planner"
                update["required_next_action"] = "PLAN_PATCH"
                update["failure_owner"] = RecoveryFailureOwner.PLAN.value
                update["recovery_scope"] = "PLAN"
                update["recovery_phase"] = "plan_repair"
            update_messages = update.get("messages", []) if isinstance(update, dict) else []
            await self._observe_session_memory(
                task_id,
                "supervisor_final_audit",
                state.get("messages", []) + update_messages,
                {
                    "next_step": update.get("next_step") if isinstance(update, dict) else "",
                    "supervisor_verdict": update.get("supervisor_verdict") if isinstance(update, dict) else "",
                    "final_audit": True,
                },
            )
            if isinstance(update, dict) and update.get("next_step") == "finish":
                final_state = load_plan_state(self.data_path, task_id)
                final_gate = CompletionGate.validate_final(
                    self.data_path,
                    task_id,
                    final_state,
                    state.get("current_plan", ""),
                    require_execution_evidence=True,
                    require_supervisor_evidence=False,
                )
                update["completion_gate_result"] = final_gate.to_dict()
                if not final_gate.passed:
                    update.setdefault("messages", []).append(
                        SystemMessage(
                            content=(
                                "Final audit reached a finish recommendation, but the deterministic completion gate "
                                "rejected it.\n\n" + final_gate.feedback
                            )
                        )
                    )
                    update["next_step"] = "generate"
                    update["supervisor_verdict"] = "RETRY"
                    update = await self._adjudicate_post_review_recovery(state, update, task_id)
                else:
                    completed_state = load_plan_state(self.data_path, task_id)
                    completion_decision = RecoveryPolicy().decide(
                        completion_evidence={
                            "completion_satisfied": True,
                            "task_complete": True,
                            "evidence": ["supervisor_final_audit", "completion_gate_passed"],
                        },
                        action_id=str((state.get("execution_evidence") or {}).get("action_id") or "final-audit"),
                        step_id=str(state.get("current_step_id") or ""),
                        attempt_id=f"final-audit:{task_id}",
                        budgets=dict((completed_state.get("task_state") or {}).get("budgets") or {}),
                    )
                    completion_decision = contextualize_recovery_decision(
                        completion_decision,
                        source_node="supervisor",
                    )
                    application = apply_recovery_decision(completed_state, completion_decision)
                    save_plan_state(self.data_path, task_id, completed_state)
                    update["recovery_decision"] = completion_decision.to_dict()
                    update["recovery_application"] = application
                    update["current_step_id"] = str(application.get("current_step_id") or "")
                    update["current_stage_id"] = str(application.get("current_stage_id") or "")
                    update["next_step"] = completion_decision.next_route
                    update["final_audit_requested"] = False
                    update["completion_candidate"] = {}
            if isinstance(update, dict):
                update = self._commit_legacy_recovery_projection(
                    state,
                    update,
                    task_id,
                    source_node="supervisor",
                )
                update["last_supervisor_feedback"] = self._supervisor_feedback_snapshot(
                    update,
                    state,
                    task_id,
                )
            return update

        # ════════════════════════════════════════════
        # Node 5: Install Sub-Agent
        # ════════════════════════════════════════════
        async def install_subagent(state: AgentState):
            output.print_node_header("install_subagent")
            last_msg = state["messages"][-1].content
            task_id  = state.get("task_id", "default")
            pending_action = state.get("pending_action") or {}
            context_summary = self._clip_prompt_text(
                state.get("requirements_summary", "No context."),
                LLM_STEP_PLAN_CHARS,
            )
            current_plan_for_install = self._current_step_prompt_context(
                task_id,
                current_plan=str(state.get("current_plan", "") or ""),
                plan_progress=str(state.get("plan_progress", "") or ""),
                state=state,
            )
            selected_for_install = state.get("active_skills", []) or []
            narrowed_for_step = self._narrow_skills_to_current_plan_step(selected_for_install, task_id)
            if narrowed_for_step != self._selected_skill_ids(selected_for_install):
                previous_skills = ", ".join(self._selected_skill_ids(selected_for_install)) or "None"
                selected_for_install = narrowed_for_step
                if narrowed_for_step:
                    context_summary += (
                        "\n\n[INSTALL TARGET STEP GUARD]\n"
                        f"- Previous active skills: {previous_skills}\n"
                        f"- Current plan step declared skill(s): {', '.join(narrowed_for_step)}\n"
                        "- Installation is restricted to the current plan step.\n"
                    )
            if not selected_for_install:
                plan_progress = self._record_plan_current_evidence(
                    task_id,
                    "install_without_step_skill",
                    "Installer refused to fall back from current-step ACTIVE_SKILLS to task-wide SELECTED_SKILLS.",
                )
                return {
                    "messages": [
                        SystemMessage(
                            content=(
                                "Installer has no skill assigned by the current plan step. "
                                "Return to Planner and add the intended canonical skill to this step's `Skills:` field; "
                                "do not install from the task-wide selected pool."
                            )
                        )
                    ],
                    "plan_progress": plan_progress,
                    "next_step": "generate",
                }
            if current_plan_for_install:
                context_summary += f"\n\nCurrent approved step:\n{current_plan_for_install}"
            runtime_defaults = self._skill_runtime_defaults(selected_for_install)
            if self.skill_manager and selected_for_install:
                skill_ctx = self.skill_manager.load_skills_for_prompt(selected_for_install)
                cards = self._skill_execution_context(selected_for_install)
                if skill_ctx or cards:
                    context_summary += (
                        "\n\nSelected skill runtime context:\n"
                        f"{self._clip_prompt_text(cards, 5000)}\n"
                        f"{self._clip_prompt_text(skill_ctx, 4000)}"
                    )

            match = re.search(r"<install_tool(?:\s+env=[\"'](.*?)[\"'])?>(.*?)</install_tool>", last_msg, re.DOTALL)
            if self._pending_action_is(pending_action, "install_tool"):
                specified_env = str(pending_action.get("env") or "").strip() or None
                raw_tool_name = str(pending_action.get("tool") or "").strip() or runtime_defaults.get("tool_name", "skill_runtime")
            else:
                specified_env = match.group(1) if match else None
                raw_tool_name = match.group(2).strip() if match else runtime_defaults.get("tool_name", "skill_runtime")
            tool_name, inner_env = self._install_tool_parts(raw_tool_name, specified_env or runtime_defaults.get("env", ""))
            if inner_env and not specified_env:
                specified_env = inner_env
            if not tool_name or tool_name == "unknown":
                tool_name = runtime_defaults.get("tool_name", "skill_runtime")
            install_context_text = self._clip_prompt_text(
                "\n".join(part for part in [last_msg, context_summary] if part),
                LLM_STEP_CONTEXT_CHARS,
            )
            inferred_for_install = self._executable_runtime_skills(
                self._infer_runtime_skills_from_code(install_context_text, selected_for_install)
            )
            if inferred_for_install and inferred_for_install != self._selected_skill_ids(selected_for_install):
                previous_skills = ", ".join(self._selected_skill_ids(selected_for_install)) or "None"
                selected_for_install = inferred_for_install
                runtime_defaults = self._skill_runtime_defaults(selected_for_install)
                context_summary += (
                    "\n\n[INSTALL TARGET RESOLUTION]\n"
                    f"- Previous active/selected skills: {previous_skills}\n"
                    f"- Resolved executable runtime skill(s) from failed tool/context: {', '.join(inferred_for_install)}\n"
                    "- Workflow/router skills are not installable runtimes; repair the concrete executable skill runtime.\n"
                )
                if self.skill_manager:
                    resolved_cards = self._skill_execution_context(selected_for_install)
                    resolved_docs = self.skill_manager.load_skills_for_prompt(selected_for_install)
                    context_summary += (
                        "\n\nResolved executable skill runtime context:\n"
                        f"{self._clip_prompt_text(resolved_cards, 5000)}\n"
                        f"{self._clip_prompt_text(resolved_docs, 4000)}"
                    )
            owner_skill = self._runtime_owner_skill_for_install(tool_name, selected_for_install)
            if owner_skill:
                runtime_defaults = self._skill_runtime_defaults([owner_skill])
            selected_skills = runtime_defaults.get("selected", []) or []
            tool_name, correction_note = self._sanitize_install_tool_for_selected_runtime(
                tool_name,
                selected_skills,
                runtime_defaults,
                specified_env or runtime_defaults.get("env", ""),
            )
            if correction_note:
                context_summary += "\n\n" + correction_note
                last_msg = re.sub(r"\bcalder_r_runtime\b", tool_name, last_msg)
            explicit_runtime_repair = self._explicit_runtime_repair_kind(tool_name, last_msg)
            choice_key = self._install_choice_key(task_id, tool_name)
            static_skill_env = self._skill_static_default_env(selected_skills[0]) if selected_skills else ""

            if static_skill_env:
                default_env = static_skill_env
                if specified_env and specified_env not in {static_skill_env, "default"}:
                    context_summary += (
                        f"\n\n[INSTALL ENV OVERRIDE NOTICE]\n"
                        f"- Selected skill default_env: {static_skill_env}\n"
                        f"- Generated install tag requested env: {specified_env}\n"
                        f"- Prefer the selected skill default_env unless the user explicitly overrides it.\n"
                    )
            else:
                default_env = specified_env if specified_env and specified_env != "default" else (
                    runtime_defaults.get("env") or f"bio_{tool_name}"
                )

            registered_reuse = {}
            if not explicit_runtime_repair:
                registered_reuse = self._reuse_registered_runtime_for_install(
                    selected_skills or selected_for_install,
                    specified_env,
                    task_id,
                )
            if registered_reuse.get("ok"):
                final_res = registered_reuse["message"]
                self.profiler.record_skill_usage(
                    task_id,
                    selected_skills or selected_for_install,
                    usage_type="installed_runtime",
                    node="install_subagent",
                    detail=registered_reuse.get("env", ""),
                )
                plan_progress = self._mark_plan_current_done(task_id, final_res)
                self._record_installed_skill_environment(selected_skills or selected_for_install, registered_reuse["env"], final_res)
                await self._observe_session_memory(
                    task_id,
                    "install_subagent",
                    state.get("messages", []) + [AIMessage(content=f"<observation>{final_res}</observation>")],
                    {
                        "tool_name": tool_name,
                        "target_env": registered_reuse["env"],
                        "result": final_res,
                        "plan_progress": plan_progress,
                        "runtime_status": self._runtime_status_for_task(task_id),
                    },
                )
                return {
                    "messages": [AIMessage(content=f"<observation>{final_res}</observation>")],
                    "plan_progress": plan_progress,
                    "runtime_status": self._runtime_status_for_task(task_id),
                    "next_step": self._next_step_after_install_result(task_id),
                }
            if registered_reuse and registered_reuse.get("env"):
                default_env = registered_reuse["env"]
                context_summary += (
                    f"\n\n[REGISTERED RUNTIME CHECK]\n"
                    f"- Registered environment: {registered_reuse['env']}\n"
                    f"- Healthcheck failed: {registered_reuse.get('detail', 'unknown')}\n"
                    f"- Repair or reinstall this same environment. Do not create a duplicate environment unless the user explicitly overrides the env name.\n"
                )
            reuse_match = None
            if self.pm and not explicit_runtime_repair and any([
                runtime_defaults.get("packages"),
                runtime_defaults.get("capabilities"),
                runtime_defaults.get("language"),
            ]):
                reuse_match = self.pm.find_matching_environment({
                    "language": runtime_defaults.get("language", ""),
                    "packages": runtime_defaults.get("packages", []),
                    "capabilities": runtime_defaults.get("capabilities", []),
                })
                if reuse_match and reuse_match.get("match_type") == "exact":
                    ok, detail = self.pm.verify_environment(
                        env_name=reuse_match["env"],
                        language=runtime_defaults.get("language", ""),
                        healthcheck=runtime_defaults.get("healthcheck", ""),
                    )
                    if ok:
                        final_res = (
                            f"✅ Reusing existing environment '{reuse_match['env']}' for "
                            f"{', '.join(runtime_defaults.get('selected', []) or [tool_name])}."
                        )
                        self.profiler.record_skill_usage(
                            task_id,
                            selected_skills or selected_for_install,
                            usage_type="installed_runtime",
                            node="install_subagent",
                            detail=reuse_match.get("env", ""),
                        )
                        plan_progress = self._mark_plan_current_done(task_id, final_res)
                        self._record_installed_skill_environment(selected_skills or selected_for_install, reuse_match["env"], final_res)
                        await self._observe_session_memory(
                            task_id,
                            "install_subagent",
                            state.get("messages", []) + [AIMessage(content=f"<observation>{final_res}</observation>")],
                            {
                                "tool_name": tool_name,
                                "target_env": reuse_match["env"],
                                "result": final_res,
                                "plan_progress": plan_progress,
                                "runtime_status": self._runtime_status_for_task(task_id),
                            },
                        )
                        return {
                            "messages": [AIMessage(content=f"<observation>{final_res}</observation>")],
                            "plan_progress": plan_progress,
                            "runtime_status": self._runtime_status_for_task(task_id),
                            "next_step": "generate",
                        }
                    context_summary += (
                        f"\n\n[ENVIRONMENT CHECK]\n"
                        f"- Existing matching environment: {reuse_match['env']}\n"
                        f"- Healthcheck failed: {detail}\n"
                        f"- Please repair or reinstall this environment instead of creating a duplicate.\n"
                    )
                    default_env = reuse_match["env"]
                elif reuse_match and reuse_match.get("match_type") == "partial":
                    missing_packages = ", ".join(reuse_match.get("missing_packages", [])) or "none"
                    missing_caps = ", ".join(reuse_match.get("missing_capabilities", [])) or "none"
                    context_summary += (
                        f"\n\n[ENVIRONMENT REUSE CANDIDATE]\n"
                        f"- Reuse and extend environment: {reuse_match['env']}\n"
                        f"- Missing packages: {missing_packages}\n"
                        f"- Missing capabilities: {missing_caps}\n"
                        f"- Prefer extending this environment instead of creating a new one.\n"
                    )
                    default_env = reuse_match["env"]
            cached_env = self._install_env_choices.get(choice_key)
            if cached_env:
                target_env = cached_env
            elif specified_env and (not runtime_defaults.get("env") or specified_env == runtime_defaults.get("env")):
                target_env = specified_env
            else:
                print(f"\n{Fore.CYAN}❓ Confirm environment for {tool_name}?{Style.RESET_ALL}")
                user_choice = interrupt({
                    "type": "env_selection",
                    "tool_name": tool_name,
                    "default_env": default_env,
                    "reason": "Confirm or override the environment before installation.",
                })
                target_env = user_choice.strip() if isinstance(user_choice, str) and user_choice.strip() else default_env
            self._install_env_choices[choice_key] = target_env
            attempt_key = f"{choice_key}::{target_env}"
            sub_config = {"configurable": {"thread_id": f"{task_id}_installer_{tool_name}"}}
            installer_snapshot = self.installer_app.get_state(sub_config)
            installer_has_pending_interrupt = bool(
                installer_snapshot.tasks and installer_snapshot.tasks[0].interrupts
            )
            if not installer_has_pending_interrupt:
                self._install_attempt_counts[attempt_key] = self._install_attempt_counts.get(attempt_key, 0) + 1
            scope_key = ""
            if explicit_runtime_repair:
                skill_scope = ",".join(selected_skills or selected_for_install or [tool_name])
                scope_key = f"{task_id}::runtime_repair::{explicit_runtime_repair}::{target_env}::{skill_scope}"
                if not installer_has_pending_interrupt:
                    self._install_scope_attempt_counts[scope_key] = self._install_scope_attempt_counts.get(scope_key, 0) + 1
                if self._install_scope_attempt_counts.get(scope_key, 0) > 2:
                    final_res = (
                        f"blocked: repeated runtime repair loop detected for `{explicit_runtime_repair}` in "
                        f"`{target_env}` after {self._install_scope_attempt_counts[scope_key]} attempts. "
                        "The root cause is still unresolved; do not switch to another conda environment or rerun "
                        "availability checks as a substitute for installation. Repair the selected runtime or ask "
                        "the user to install/register it explicitly."
                    )
                    plan_progress = self._mark_plan_current_blocked(task_id, final_res)
                    self._install_env_choices.pop(choice_key, None)
                    await self._observe_session_memory(
                        task_id,
                        "install_subagent",
                        state.get("messages", []) + [AIMessage(content=f"<observation>{final_res}</observation>")],
                        {
                            "tool_name": tool_name,
                            "target_env": target_env,
                            "result": final_res,
                            "plan_progress": plan_progress,
                            "runtime_status": self._runtime_status_for_task(task_id),
                        },
                    )
                    return {
                        "messages": [AIMessage(content=f"<observation>{final_res}</observation>")],
                        "plan_progress": plan_progress,
                        "runtime_status": self._runtime_status_for_task(task_id),
                        "next_step": self._next_step_after_install_result(task_id),
                    }
            if self._install_attempt_counts.get(attempt_key, 0) > 2:
                final_res = (
                    f"blocked: repeated installation loop detected for `{tool_name}` in environment "
                    f"`{target_env}` after {self._install_attempt_counts[attempt_key]} attempts. "
                    "Run the skill runtime healthcheck directly and continue execution if it passes; "
                    "do not re-enter INSTALL_SUBAGENT for the same target."
                )
                plan_progress = self._mark_plan_current_blocked(task_id, final_res)
                self._install_env_choices.pop(choice_key, None)
                await self._observe_session_memory(
                    task_id,
                    "install_subagent",
                    state.get("messages", []) + [AIMessage(content=f"<observation>{final_res}</observation>")],
                    {
                        "tool_name": tool_name,
                        "target_env": target_env,
                        "result": final_res,
                        "plan_progress": plan_progress,
                        "runtime_status": self._runtime_status_for_task(task_id),
                    },
                )
                return {
                    "messages": [AIMessage(content=f"<observation>{final_res}</observation>")],
                    "plan_progress": plan_progress,
                    "runtime_status": self._runtime_status_for_task(task_id),
                    "next_step": self._next_step_after_install_result(task_id),
                }

            if explicit_runtime_repair:
                context_summary += self._runtime_repair_context(explicit_runtime_repair, target_env)

            install_command_context = self._selected_skill_install_command_context(runtime_defaults, target_env)
            if install_command_context:
                context_summary += install_command_context

            packages = [] if explicit_runtime_repair else runtime_defaults.get("packages", [])
            if packages:
                context_summary += (
                    f"\n\n[INSTALL TARGET OVERRIDE]\n"
                    f"- target_env: {target_env}\n"
                    f"- language: {runtime_defaults.get('language', 'python') or 'python'}\n"
                    f"- requested packages: {', '.join(packages)}\n"
                    f"- requested capabilities: {', '.join(runtime_defaults.get('capabilities', []) or []) or 'none'}\n"
                    f"- The target_env above overrides any default_env written in the skill docs for this installation.\n"
                    f"- Create/use `{target_env}`, install the packages there, and register the environment before any tool registration.\n"
                )

            print(f"\n{Fore.CYAN}🔧 INSTALLER: {tool_name} → {target_env}{Style.RESET_ALL}")

            initial_msg = INSTALLER_HANDOVER_TEMPLATE.format(
                user_context=context_summary, rag_context="",
                tool_name=tool_name, target_env=target_env,
                env_instruction=f"Target environment: {target_env}",
            )

            final_res    = "Installation Failed"
            snapshot     = installer_snapshot
            current_payload = None if installer_has_pending_interrupt \
                else {
                    "tool_name": tool_name,
                    "task_id": task_id,
                    "selected_skills": selected_skills or selected_for_install,
                    "messages": [HumanMessage(content=initial_msg)],
                }

            while True:
                if current_payload:
                    async for event in self.installer_app.astream(current_payload, config=sub_config):
                        for node_name, update in event.items():
                            if "messages" in update:
                                last_m = update["messages"][-1]
                                if isinstance(last_m, AIMessage):
                                    output.print_once(f"inst:{last_m.content[:40]}",
                                                      f"[Installer] {last_m.content[:200]}", Fore.MAGENTA)
                            if "final_result" in update:
                                final_res = update["final_result"]

                snapshot = self.installer_app.get_state(sub_config)
                if not snapshot.next and (not snapshot.tasks or not snapshot.tasks[0].interrupts):
                    break
                if snapshot.tasks and snapshot.tasks[0].interrupts:
                    sub_iv = snapshot.tasks[0].interrupts[0].value
                    user_resp = interrupt(sub_iv)
                    current_payload = Command(resume=user_resp)
                    continue
                break

            self.profiler.record_skill_usage(
                task_id,
                selected_skills or selected_for_install,
                usage_type="installed_runtime",
                node="install_subagent",
                detail=target_env,
            )
            final_res_lower = str(final_res).lower()
            install_success = not any(
                marker in final_res_lower
                for marker in ("failed", "❌", "blocked", "loop detected", "exceeded")
            )
            if selected_skills or selected_for_install:
                recheck_failures = []
                recheck_successes = []
                for sid in self._selected_skill_ids(selected_skills or selected_for_install):
                    ok, detail, verified_env = self._verify_skill_runtime(
                        sid,
                        task_id=task_id,
                        use_cache=False,
                    )
                    if not ok:
                        recheck_failures.append(
                            f"{sid} in `{verified_env or target_env}` failed healthcheck: {detail or 'Healthcheck failed.'}"
                        )
                    else:
                        recheck_successes.append(
                            f"{sid} in `{verified_env or target_env}` passed healthcheck: {detail or 'Healthcheck passed.'}"
                        )
                if recheck_successes and not recheck_failures:
                    install_success = True
                    if any(marker in final_res_lower for marker in ("failed", "❌", "blocked", "loop detected", "exceeded")):
                        final_res = (
                            "✅ Installation/runtime is usable after direct skill healthcheck.\n"
                            + "\n".join(f"- {item}" for item in recheck_successes)
                            + "\n\nOriginal installer final message was overridden because the verified runtime healthcheck passed:\n"
                            + str(final_res)[:1500]
                        )
                elif recheck_failures:
                    install_success = False
                    final_res = (
                        f"{final_res}\n\n"
                        "Post-install skill runtime healthcheck failed:\n"
                        + "\n".join(f"- {item}" for item in recheck_failures)
                    )
            if install_success:
                self._install_env_choices.pop(choice_key, None)
                self._install_attempt_counts.pop(attempt_key, None)
                if scope_key:
                    self._install_scope_attempt_counts.pop(scope_key, None)
            if install_success:
                self._record_installed_skill_environment(selected_skills or selected_for_install, target_env, final_res)
                plan_progress = self._mark_plan_current_done(task_id, str(final_res)[:1000])
            else:
                install_command = runtime_defaults.get("install_command", "")
                blocked_note = (
                    f"{final_res}\n\n"
                    "blocked: required runtime installation did not complete. "
                    "Do not retry the same healthcheck or fabricate outputs."
                )
                if install_command:
                    blocked_note += f"\nSuggested install command: {install_command}"
                final_res = blocked_note
                plan_progress = self._mark_plan_current_blocked(task_id, f"install_failed: {str(final_res)[:1000]}")
            await self._observe_session_memory(
                task_id,
                "install_subagent",
                state.get("messages", []) + [AIMessage(content=f"<observation>{final_res}</observation>")],
                {
                    "tool_name": tool_name,
                    "target_env": target_env,
                    "result": final_res[:3000],
                    "plan_progress": plan_progress,
                    "runtime_status": self._runtime_status_for_task(task_id),
                },
            )
            output.print_observation(f"<observation>{final_res}</observation>")

            return {"messages": [AIMessage(content=f"<observation>{final_res}</observation>")],
                    "plan_progress": plan_progress,
                    "runtime_status": self._runtime_status_for_task(task_id),
                    "next_step": self._next_step_after_install_result(task_id)}

        # ════════════════════════════════════════════
        # 节点注册 & 连线
        # ════════════════════════════════════════════
        workflow.add_node("triage",          self._profile_node("triage", triage))
        workflow.add_node("direct_chat",     self._profile_node("direct_chat", direct_chat))
        workflow.add_node("consultant",      self._profile_node("consultant", consultant))
        workflow.add_node("ask_user",        self._profile_node("ask_user", ask_user))
        workflow.add_node("planner",         self._profile_node("planner", planner))
        workflow.add_node("plan_review",     self._profile_node("plan_review", plan_review))
        workflow.add_node("generate",        self._profile_node("generate", generate))
        workflow.add_node("execute",         self._profile_node("execute", execute_node))
        workflow.add_node("supervisor",      self._profile_node("supervisor", supervisor))          # ← 新节点
        workflow.add_node("supervisor_final_audit", self._profile_node("supervisor_final_audit", supervisor_final_audit))
        workflow.add_node("install_subagent", self._profile_node("install_subagent", install_subagent))

        workflow.add_edge(START, "triage")

        def committed_recovery_route(state, source_node):
            """Consume only the structured RecoveryPolicy decision for recovery routing."""
            try:
                decision = recovery_decision_from_state(dict(state or {}))
            except ValueError as exc:
                logger.warning("Ignoring invalid recovery decision during routing: %s", exc)
                return ""
            if decision is None:
                return ""
            route = recovery_route_from_state(dict(state or {}), source_node)
            if not route and decision.source_node:
                return ""
            if route == "generate" and (
                decision.kind == RecoveryDecisionKind.PLAN_PATCH_REQUEST.value
                and decision.failure_owner != RecoveryFailureOwner.PLAN.value
            ):
                logger.warning(
                    "RecoveryPolicy rejected non-PLAN route to Planner: decision_id=%s owner=%s",
                    decision.decision_id,
                    decision.failure_owner,
                )
            if decision.migration_source:
                logger.warning(
                    "Routing migrated recovery state: source=%s conflicts=%s decision_id=%s",
                    decision.migration_source,
                    decision.migration_conflicts,
                    decision.decision_id,
                )
            return route

        def triage_router(state):
            step = state.get("next_step")
            if step == "direct_chat": return "direct_chat"
            if step == "generate":    return "generate"
            if step == "execute":     return "execute"
            if step == "planner":     return "planner"
            if step == "plan_review": return "plan_review"
            if step == "ask_user":    return "ask_user"
            if step == "waiting_user": return END
            if step == "finish":      return END
            if step == "supervisor_final_audit": return "supervisor_final_audit"
            return "consultant"

        workflow.add_conditional_edges("triage", triage_router,
                                       ["direct_chat", "generate", "execute", "planner", "plan_review", "ask_user", "consultant", "supervisor_final_audit", END])
        workflow.add_edge("direct_chat", END)

        def consultant_router(state):
            step = state.get("next_step")
            if step == "planner":     return "planner"
            if step == "ask_user":    return "ask_user"
            if step == "human_input": return END
            if step == "finish":      return END
            return "consultant"

        workflow.add_conditional_edges("consultant", consultant_router,
                                       ["planner", "ask_user", "consultant", END])

        def ask_user_router(state):
            step = state.get("next_step")
            if step == "generate": return "generate"
            if step == "planner": return "planner"
            return "consultant"

        workflow.add_conditional_edges("ask_user", ask_user_router,
                                       ["generate", "planner", "consultant"])

        def planner_router(state):
            step = state.get("next_step")
            if step == "consultant": return "consultant"
            if step == "finish": return END
            return "plan_review"

        workflow.add_conditional_edges("planner", planner_router,
                                       ["consultant", "plan_review", END])

        def install_subagent_router(state):
            step = state.get("next_step", "generate")
            if step == "finish": return END
            if step == "human_input": return END
            return "generate"

        workflow.add_conditional_edges("install_subagent", install_subagent_router,
                                       ["generate", END])

        def plan_review_router(state):
            step = state.get("next_step")
            if step == "planner": return "planner"
            if step == "finish":  return END
            if step == "human_input": return END
            return "generate"

        workflow.add_conditional_edges("plan_review", plan_review_router,
                                       ["generate", "planner", END])

        def generate_router(state):
            recovery_route = committed_recovery_route(state, "generate")
            if recovery_route:
                if recovery_route == "finish": return END
                if recovery_route == "planner": return "planner"
                if recovery_route in {"generate", "execute", "supervisor", "ask_user"}:
                    return recovery_route
            step = state.get("next_step")
            if step == "finish":         return END
            if step == "planner":        return "planner" if self._plan_patch_requested(state) else "generate"
            if step == "generate":       return "generate"
            if step == "ask_user":       return "ask_user"
            if step == "execute":        return "execute"
            if step == "supervisor":     return "supervisor"
            if step == "install_subagent": return "install_subagent"
            if step == "supervisor_final_audit": return "supervisor_final_audit"
            return "generate"

        workflow.add_conditional_edges("generate", generate_router,
                                       ["planner", "ask_user", "execute", "install_subagent", "supervisor", "supervisor_final_audit", "generate", END])

        # execute → supervisor → generate（managed 核心链路）；轻量任务直接结束。
        def execute_router(state):
            recovery_route = committed_recovery_route(state, "execute")
            if recovery_route:
                if recovery_route == "finish": return END
                if recovery_route == "planner": return "planner"
                if recovery_route in {"generate", "supervisor", "ask_user"}:
                    return recovery_route
            if state.get("next_step") == "ask_user":
                return "ask_user"
            if state.get("next_step") == "finish":
                return END
            if state.get("next_step") == "install_subagent":
                return "install_subagent"
            if state.get("next_step") == "generate":
                return "generate"
            if state.get("next_step") == "planner":
                return "planner" if self._plan_patch_requested(state) else "generate"
            if state.get("next_step") == "supervisor_final_audit":
                return "supervisor_final_audit"
            if str(state.get("task_mode") or "").strip().lower() in {"readonly", "simple_execute"}:
                return END
            return "supervisor"

        # Keep the stable legacy execute chain explicit; knowledge-conflict
        # user decisions add one guarded edge before it.
        legacy_execute_routes = ["generate", "planner", "install_subagent", "supervisor", "supervisor_final_audit", END]
        workflow.add_conditional_edges("execute", execute_router,
                                       ["ask_user"] + legacy_execute_routes)

        def supervisor_router(state):
            recovery_route = committed_recovery_route(state, "supervisor")
            if recovery_route:
                if recovery_route == "finish": return END
                if recovery_route == "planner": return "planner"
                if recovery_route in {"generate", "supervisor", "ask_user"}:
                    return recovery_route
            # Supervisor is an evidence producer. Without a committed
            # RecoveryPolicy decision it has no authority to route elsewhere.
            return "generate"

        workflow.add_conditional_edges("supervisor", supervisor_router,
                                       ["ask_user", "planner", "generate", "supervisor_final_audit", END])

        def final_audit_router(state):
            recovery_route = committed_recovery_route(state, "supervisor")
            if recovery_route:
                if recovery_route == "finish": return END
                if recovery_route == "planner": return "planner"
                if recovery_route == "generate": return "generate"
            # Final audit follows the same ownership rule: only the committed
            # policy decision may complete, repair, or patch the task.
            return "generate"

        workflow.add_conditional_edges("supervisor_final_audit", final_audit_router,
                                       ["planner", "generate", END])

        return workflow.compile(checkpointer=self.checkpointer)

    # ────────────────────────────────────────────────────────────
    #  主运行入口
    # ────────────────────────────────────────────────────────────

    def _messages_for_memory_status(self, task_id: str, config: Dict[str, Any], runner_input: Any) -> List[BaseMessage]:
        """Return the best available message list for the memory pressure display."""
        try:
            snapshot = self.app.get_state(config)
            values = snapshot.values if snapshot and snapshot.values else {}
            messages = values.get("messages", []) if isinstance(values, dict) else []
            if messages:
                return list(messages)
        except Exception:
            pass
        if isinstance(runner_input, dict):
            return list(runner_input.get("messages", []) or [])
        return []

    @staticmethod
    def _snapshot_values(snapshot: Any) -> Dict[str, Any]:
        values = getattr(snapshot, "values", None)
        return values if isinstance(values, dict) else {}

    @staticmethod
    def _snapshot_next_step(snapshot: Any) -> str:
        return str(BioAnalystAgent._snapshot_values(snapshot).get("next_step") or "").strip()

    @staticmethod
    def _messages_terminal_text(messages: Any) -> str:
        parts: List[str] = []
        for message in list(messages or [])[-3:]:
            content = str(getattr(message, "content", message) or "").strip()
            if content:
                parts.append(content)
        return "\n".join(parts)

    @staticmethod
    def _terminal_status_from_text(text: str) -> str:
        normalized = str(text or "").strip()
        plain = re.sub(r"</?[^>]+>", "", normalized).strip()
        lowered = plain.lower()
        if not lowered:
            return ""
        if lowered in {"cancelled.", "cancelled", "task terminated."}:
            return "cancelled"
        if (
            lowered.startswith("blocked:")
            or "\nblocked:" in lowered
            or "required runtime installation did not complete" in lowered
            or "loop detected" in lowered
        ):
            return "blocked"
        if (
            "❌" in plain
            or "task stopped before the next generated action" in lowered
            or "llm request_failed" in lowered
            or "llm input_rejected" in lowered
            or "the llm call failed" in lowered
            or lowered.startswith(("failed", "error:"))
            or "\nfailed" in lowered
            or "\nerror:" in lowered
        ):
            return "failed"
        return ""

    def _terminal_status_from_plan(self, task_id: str, values: Optional[Dict[str, Any]] = None) -> str:
        try:
            state = load_plan_state(self.data_path, task_id)
        except Exception:
            state = {}
        durable_status = task_status(state)
        if durable_status in TASK_TERMINAL_STATUSES:
            return durable_status
        steps = [step for step in state.get("steps", []) or [] if isinstance(step, dict)]
        if steps:
            statuses = {str(step.get("status") or "").strip().lower() for step in steps}
            if statuses & {"blocked", "failed", "skipped_upstream_blocked"}:
                return "blocked"
            if statuses <= {"done", "passed", "skipped", "cancelled"}:
                return "completed"
            return "blocked"

        plan_progress = str((values or {}).get("plan_progress") or "")
        if "- [!]" in plan_progress or "Next step: BLOCKED:" in plan_progress:
            return "blocked"
        return ""

    def _terminal_status_from_snapshot(self, task_id: str, snapshot: Any) -> str:
        values = self._snapshot_values(snapshot)
        if str(values.get("recovery_status") or "") == "paused_retryable":
            return "paused_retryable"
        if self._snapshot_next_step(snapshot) == "human_input" or self._business_waiting_state(load_waiting_state(self.data_path, task_id)):
            return "waiting_user"

        plan_status = self._terminal_status_from_plan(task_id, values)
        if plan_status:
            return plan_status

        text_status = self._terminal_status_from_text(
            self._messages_terminal_text(values.get("messages", []))
        )
        if text_status:
            return text_status
        return "completed"

    async def go(
        self,
        input_data: Any,
        task_id: Optional[str] = None,
        resume: bool = False,
    ):
        if not task_id:
            task_id = self._auto_task_id_for_prompt(input_data) or f"task_{int(time.time())}"
        config = {"configurable": {"thread_id": task_id}}
        self.profiler.start_task(task_id)
        self._record_initial_profiler_metadata(task_id, input_data)
        self._sync_runtime_registries_at_startup()

        output.reset_for_task()

        if isinstance(input_data, Command):
            runner_input = input_data
            print(f"\n🔄 Resuming: {task_id}")
        else:
            prompt = input_data
            project_id = self.default_project_id
            if resume:
                try:
                    checkpoint = self.app.get_state(config)
                    checkpoint_values = self._snapshot_values(checkpoint)
                except Exception:
                    checkpoint_values = {}
                runner_input = self._build_resume_runner_state(task_id, prompt, checkpoint_values)
                print(f"\n🔄 Resuming: {task_id}")
            else:
                stable_record = self._get_or_create_stable_prompt(task_id, project_id)
                self.task_summary_renderer.initialize_task(task_id, str(prompt), status="running")
                if self.state_manager:
                    self.state_manager.create_task(task_id, prompt)
                runner_input = {
                    "messages": [HumanMessage(
                        content=prompt,
                        additional_kwargs={"message_origin": "user"},
                    )],
                    "task_id": task_id,
                    "session_id": task_id,
                    "project_id": project_id,
                    "stable_system_prompt": stable_record["prompt"],
                    "core_memory_loaded_at": stable_record["core_memory_loaded_at"],
                    "runtime_status": self._runtime_status_for_task(task_id),
                    "task_spec": create_task_spec(
                        str(prompt),
                        task_id=task_id,
                        base_dir=project_root,
                        project_root=project_root,
                    ),
                    "task_revision": 1,
                }
                print(f"\n🚀 Task: {task_id}")

        # 内存状态展示
        self.memory_manager.print_status(
            self._messages_for_memory_status(task_id, config, runner_input)
        )

        with self.profiler.task_context(task_id):
            try:
                async for event in self.app.astream(runner_input, config=config):
                    pass   # 所有打印由 OutputRouter 在节点内处理，此处不重复
            finally:
                self.profiler.flush(task_id)

    async def _run_agent_interrupt_loop(self, initial_input: Any, task_id: str, *, resume_initial: bool = False) -> str:
        """Run a fresh or resumed task until terminal state or saved user wait."""
        next_input = initial_input
        while True:
            await self.go(
                next_input,
                task_id=task_id,
                resume=resume_initial or isinstance(next_input, Command),
            )
            resume_initial = False
            config = {"configurable": {"thread_id": task_id}}
            snapshot = self.app.get_state(config)

            if not (snapshot.tasks and snapshot.tasks[0].interrupts):
                terminal_status = self._terminal_status_from_snapshot(task_id, snapshot)
                if terminal_status == "waiting_user":
                    if self.state_manager:
                        self.state_manager.update_task_status(task_id, "waiting_user")
                    self.profiler.finish_task(task_id, "waiting_user")
                    return terminal_status
                if self.state_manager:
                    try:
                        self.state_manager.update_task_status(task_id, terminal_status)
                    except Exception:
                        pass
                self.profiler.finish_task(task_id, terminal_status)
                if terminal_status == "completed":
                    await self._maybe_offer_wiki_ingest(task_id, self.default_project_id)
                return terminal_status

            payload = snapshot.tasks[0].interrupts[0].value
            user_response = ""

            if isinstance(payload, dict) and payload.get("type") == "code_review":
                risk_info = payload.get("risk", {})
                risk = risk_info.get("level") or risk_info.get("risk_level") or "MEDIUM"
                print(f"\n{Fore.YELLOW}⚠️  CODE REVIEW  |  Risk: {risk}{Style.RESET_ALL}")
                wait_started = time.perf_counter()
                wait_started_at = timestamp_now()
                output.page_text("Code Review", payload.get("content", ""), Fore.CYAN)
                try:
                    user_response = await self._aprompt_user(
                        task_id,
                        "code_review",
                        f"{Fore.YELLOW}[y/Enter=run, n=reject, q=quit, e:<code>=edit, text=feedback] > {Style.RESET_ALL}",
                        payload=payload,
                        started_at=wait_started_at,
                    )
                except HumanInputUnavailable as exc:
                    self._record_human_wait(
                        task_id,
                        "code_review",
                        wait_started,
                        f"<waiting_user:{type(exc).__name__}>",
                        payload,
                        started_at=wait_started_at,
                    )
                    return "waiting_user"
                self._record_human_wait(
                    task_id,
                    "code_review",
                    wait_started,
                    user_response,
                    payload,
                    started_at=wait_started_at,
                )

            elif isinstance(payload, dict) and payload.get("type") == "env_selection":
                tool = payload.get("tool_name")
                dflt = payload.get("default_env")
                reason = payload.get("reason", "")
                print(f"\n{Fore.CYAN}🔧 Install '{tool}' → env [{dflt}]?{Style.RESET_ALL}")
                if reason:
                    print(f"{Fore.CYAN}{reason}{Style.RESET_ALL}")
                wait_started = time.perf_counter()
                wait_started_at = timestamp_now()
                try:
                    user_response = await self._aprompt_user(
                        task_id,
                        "env_selection",
                        f"Target env (Enter={dflt}) > ",
                        payload=payload,
                        started_at=wait_started_at,
                    )
                except HumanInputUnavailable as exc:
                    self._record_human_wait(
                        task_id,
                        "env_selection",
                        wait_started,
                        f"<waiting_user:{type(exc).__name__}>",
                        payload,
                        started_at=wait_started_at,
                    )
                    return "waiting_user"
                raw_response = user_response
                if not user_response.strip():
                    user_response = dflt
                self._record_human_wait(
                    task_id,
                    "env_selection",
                    wait_started,
                    raw_response,
                    payload,
                    started_at=wait_started_at,
                )

            elif isinstance(payload, dict) and payload.get("type") in {"consultant_question", "generate_question"}:
                wait_started = time.perf_counter()
                wait_started_at = timestamp_now()
                prompt_type = payload.get("type", "human_question")
                try:
                    user_response = await self._aprompt_user(
                        task_id,
                        prompt_type,
                        f"{Fore.YELLOW}Response (q=quit) > {Style.RESET_ALL}",
                        payload=payload,
                        started_at=wait_started_at,
                    )
                except HumanInputUnavailable as exc:
                    self._record_human_wait(
                        task_id,
                        prompt_type,
                        wait_started,
                        f"<waiting_user:{type(exc).__name__}>",
                        payload,
                        started_at=wait_started_at,
                    )
                    return "waiting_user"
                self._record_human_wait(
                    task_id,
                    prompt_type,
                    wait_started,
                    user_response,
                    payload,
                    started_at=wait_started_at,
                )

            else:
                wait_started = time.perf_counter()
                wait_started_at = timestamp_now()
                generic_payload = payload if isinstance(payload, dict) else {"content": payload}
                try:
                    user_response = await self._aprompt_user(
                        task_id,
                        "interrupt",
                        f"{Fore.YELLOW}Response (q=quit) > {Style.RESET_ALL}",
                        payload=generic_payload,
                        started_at=wait_started_at,
                    )
                except HumanInputUnavailable as exc:
                    self._record_human_wait(
                        task_id,
                        "interrupt",
                        wait_started,
                        f"<waiting_user:{type(exc).__name__}>",
                        generic_payload,
                        started_at=wait_started_at,
                    )
                    return "waiting_user"
                self._record_human_wait(
                    task_id,
                    "interrupt",
                    wait_started,
                    user_response,
                    generic_payload,
                    started_at=wait_started_at,
                )

            if user_response.strip().lower() in ["q", "quit", "exit"]:
                self.profiler.finish_task(task_id, "cancelled")
                return "cancelled"
            next_input = Command(resume=user_response)

    # ────────────────────────────────────────────────────────────
    #  交互模式
    # ────────────────────────────────────────────────────────────

    async def interactive_mode(self):
        print(f"""
{Fore.CYAN}╔══════════════════════════════════════════════════╗
║  🧬  BioAnalyst V4  |  Interactive Mode          ║
╚══════════════════════════════════════════════════╝{Style.RESET_ALL}
Commands:
  /tasks              List recent tasks
  /new [name]         New session
  /resume <id>        Resume task
  /memory             Show memory status
  /memory search <q>  Search SessionArchive events
  /memory lint        Check Core memory and optional curated-claim conflicts
  /memory promote     Promote safe wiki index refs into Core project memory
  /wiki               Show wiki disabled/enabled status
  /wiki search <q>    Search wiki memory when BIOMIND_WIKI_ENABLED=true
  /wiki ingest        Curate events only when BIOMIND_WIKI_ENABLED=true
  /wiki ingest task   Curate current task only when BIOMIND_WIKI_ENABLED=true
  /wiki compile       Rebuild wiki digest only when BIOMIND_WIKI_ENABLED=true
  /wiki lint          Check wiki links only when BIOMIND_WIKI_ENABLED=true
  /wiki page <id>     Show a wiki page only when BIOMIND_WIKI_ENABLED=true
  /wiki review        Show pending wiki review items only when enabled
  /task summarize [id] Render a task summary report from archive evidence
  /paste              Enter multiline task input; finish with /end
  /cot <id>           Open CoT log for task
  /stop               Cancel current task
  /debug              Inspect graph state
  /exit               Quit
  ![cmd]              Run shell command directly
""")
        current_session_id = f"session_{int(time.time())}"
        print(f"{Fore.GREEN}● Session: {current_session_id}{Style.RESET_ALL}")

        DIRECT_CMDS = ['ls', 'll', 'la', 'pwd', 'whoami', 'date', 'head', 'tail',
                       'cat', 'mkdir', 'touch', 'which', 'df', 'free']

        while True:
            try:
                cwd_name  = os.path.basename(os.getcwd()) or "/"
                try:
                    user_input = await self._aprompt_user(
                        current_session_id,
                        "interactive_prompt",
                        f"\n{Fore.CYAN}[{current_session_id[:20]}]{Fore.WHITE} {cwd_name}> {Style.RESET_ALL}",
                        payload={"cwd": os.getcwd()},
                    )
                except HumanInputUnavailable:
                    continue
                user_input = user_input.strip()
                if not user_input:
                    continue

                if user_input.lower() in {"继续", "继续执行", "continue", "resume"}:
                    control = self._continue_control_state(current_session_id)
                    if control.get("action") != "new_task":
                        if control.get("action") == "waiting_user":
                            waiting = control.get("waiting") or {}
                            output.page_text(
                                f"Waiting State | {current_session_id}",
                                (
                                    "任务仍在等待具体输入；“继续”不能替代该回答。\n\n"
                                    f"Stage: {waiting.get('stage', '')}\n"
                                    f"Prompt: {waiting.get('prompt', '')}\n"
                                    f"Payload: {json.dumps(waiting.get('payload') or {}, ensure_ascii=False)}"
                                ),
                                Fore.YELLOW,
                            )
                            continue
                        if control.get("action") == "completed":
                            print(f"{Fore.GREEN}✓ Task {current_session_id} is already complete; nothing was rerun.{Style.RESET_ALL}")
                            continue
                        logger.info(
                            "Plain continue resolved as control event: %s",
                            json.dumps(control.get("resume_audit") or {}, ensure_ascii=False, sort_keys=True),
                        )
                        await self._run_agent_interrupt_loop("", current_session_id, resume_initial=True)
                        continue

                # ── 系统命令 ─────────────────────────────────
                if user_input.strip().lower() in {"/exit", "/quit", "q", "quit", "exit"}:
                    break

                if user_input == "/paste":
                    try:
                        user_input = await self._aprompt_user(
                            current_session_id,
                            "interactive_prompt",
                            f"{Fore.CYAN}Paste task content. Finish with /end on its own line.{Style.RESET_ALL}\n... ",
                            payload={"cwd": os.getcwd(), "mode": "multiline"},
                            multiline=True,
                        )
                    except HumanInputUnavailable:
                        continue
                    user_input = user_input.strip()
                    if not user_input:
                        continue

                if user_input.startswith("/memory search "):
                    query = user_input.split(" ", 2)[2].strip()
                    result = await self.task_summary_renderer.asearch(query, task_id=current_session_id)
                    output.page_text(f"Session Archive Search | {query}", result, Fore.CYAN)
                    continue

                if user_input == "/memory":
                    config = {"configurable": {"thread_id": current_session_id}}
                    snap   = self.app.get_state(config)
                    msgs   = snap.values.get("messages", []) if snap.values else []
                    self.memory_manager.print_status(msgs)
                    print(self.core_memory_store.status_text(self.default_project_id))
                    print(self.task_summary_renderer.status_text(current_session_id))
                    output.page_text(
                        "Task Summary",
                        self.task_summary_renderer.read_task_summary(current_session_id),
                        Fore.CYAN,
                    )
                    continue

                if user_input == "/memory lint":
                    wiki_lint = (
                        self.wiki_memory_manager.lint(project_id=self.default_project_id)
                        if self.wiki_enabled
                        else self._wiki_disabled_message()
                    )
                    wiki_claims = (
                        self.wiki_memory_manager.read_claims_index(self.default_project_id)
                        if self.wiki_enabled
                        else []
                    )
                    core_lint = self.core_memory_store.lint_project_memory(
                        self.default_project_id,
                        wiki_claims=wiki_claims,
                    )
                    output.page_text(
                        "Memory Lint",
                        f"{core_lint}\n\n{wiki_lint}",
                        Fore.CYAN,
                    )
                    continue

                if user_input == "/memory promote":
                    if not self.wiki_enabled:
                        output.page_text("Memory Promote", self._wiki_disabled_message(), Fore.CYAN)
                        continue
                    result = self.wiki_memory_manager.sync_project_index_fields(project_id=self.default_project_id)
                    output.page_text("Memory Promote", result, Fore.CYAN)
                    continue

                if user_input == "/wiki" or user_input.startswith("/wiki "):
                    if not self.wiki_enabled:
                        output.page_text("Wiki Disabled", self._wiki_disabled_message(), Fore.CYAN)
                        continue

                if user_input.startswith("/wiki search "):
                    query = user_input.split(" ", 2)[2].strip()
                    hits = self.wiki_memory_manager.search(
                        query,
                        project_id=self.default_project_id,
                    )
                    result = self.wiki_memory_manager.format_hits(hits, title=f"Wiki Search | {query}")
                    output.page_text(f"Wiki Search | {query}", result, Fore.CYAN)
                    continue

                if user_input in {"/wiki ingest", "/wiki ingest task"}:
                    ingest_task_id = current_session_id if user_input.endswith(" task") else None
                    result = await self.wiki_memory_manager.ingest_from_archive(
                        self.session_archive,
                        project_id=self.default_project_id,
                        task_id=ingest_task_id,
                    )
                    output.page_text("Wiki Ingest", result, Fore.CYAN)
                    continue

                if user_input == "/wiki compile":
                    result = self.wiki_memory_manager.compile_digest(project_id=self.default_project_id)
                    output.page_text("Wiki Compile", result, Fore.CYAN)
                    continue

                if user_input == "/wiki lint":
                    result = self.wiki_memory_manager.lint(project_id=self.default_project_id)
                    output.page_text("Wiki Lint", result, Fore.CYAN)
                    continue

                if user_input.startswith("/wiki page "):
                    identifier = user_input.split(" ", 2)[2].strip()
                    result = self.wiki_memory_manager.read_page(identifier)
                    output.page_text(f"Wiki Page | {identifier}", result, Fore.CYAN)
                    continue

                if user_input == "/wiki review":
                    result = self.wiki_memory_manager.read_review_queue()
                    output.page_text("Wiki Review Queue", result, Fore.CYAN)
                    continue

                if user_input == "/wiki":
                    print(self.wiki_memory_manager.status_text(self.default_project_id))
                    output.page_text(
                        "Wiki Overview",
                        self.wiki_memory_manager.overview_file.read_text(encoding="utf-8"),
                        Fore.CYAN,
                    )
                    continue

                if user_input == "/task summarize" or user_input.startswith("/task summarize "):
                    parts = user_input.split(" ", 2)
                    target_task_id = parts[2].strip() if len(parts) > 2 and parts[2].strip() else current_session_id
                    path = self.task_summary_renderer.write_task_summary(target_task_id)
                    output.page_text(
                        f"Task Summary | {target_task_id}",
                        path.read_text(encoding="utf-8"),
                        Fore.CYAN,
                    )
                    continue

                if user_input.startswith("/cot "):
                    tid  = user_input.split(" ", 1)[1].strip()
                    path = Path(self.data_path) / "task_outputs" / tid / f"cot_{tid}.md"
                    if path.exists():
                        import subprocess
                        subprocess.run(["less", str(path)])
                    else:
                        print(f"{Fore.RED}CoT log not found: {path}{Style.RESET_ALL}")
                    continue

                if user_input.startswith("/new"):
                    if self.state_manager:
                        self.state_manager.update_task_status(current_session_id, "suspended")
                    self.profiler.finish_task(current_session_id, "suspended")
                    await self._finalize_task_memory(
                        current_session_id,
                        "interrupted",
                        payload={"reason": "User started a new session."},
                    )
                    parts = user_input.split(" ", 1)
                    name  = re.sub(r"[^a-zA-Z0-9_-]", "", parts[1]) if len(parts) > 1 else ""
                    current_session_id = f"{name}_{int(time.time())}" if name else f"session_{int(time.time())}"
                    output.reset_for_task()
                    print(f"{Fore.GREEN}● New session: {current_session_id}{Style.RESET_ALL}")
                    continue

                if user_input == "/stop":
                    if self.state_manager:
                        self.state_manager.update_task_status(current_session_id, "cancelled")
                    self.profiler.finish_task(current_session_id, "cancelled")
                    await self._finalize_task_memory(
                        current_session_id,
                        "cancelled",
                        payload={"reason": "User stopped the current task."},
                    )
                    current_session_id = f"session_{int(time.time())}"
                    output.reset_for_task()
                    print(f"{Fore.GREEN}● New session: {current_session_id}{Style.RESET_ALL}")
                    continue

                if user_input == "/tasks":
                    if self.state_manager:
                        tasks = self.state_manager.list_tasks(limit=15)
                        print(f"\n{'Task ID':<32} | {'Status':<10} | {'Updated'}")
                        print("─" * 68)
                        for t in tasks:
                            print(f"{t['task_id']:<32} | {t['status']:<10} | {t['updated_at']}")
                    continue

                if user_input.startswith("/resume "):
                    tid = user_input.split(" ", 1)[1].strip()
                    current_session_id = tid
                    waiting = load_waiting_state(self.data_path, tid)
                    if waiting:
                        output.page_text(
                            f"Waiting State | {tid}",
                            json.dumps(waiting, ensure_ascii=False, indent=2),
                            Fore.CYAN,
                        )
                    config = {"configurable": {"thread_id": tid}}
                    snapshot = self.app.get_state(config)
                    if snapshot.tasks and snapshot.tasks[0].interrupts:
                        payload = snapshot.tasks[0].interrupts[0].value
                        output.page_text(
                            f"Pending Interrupt | {tid}",
                            json.dumps(payload, ensure_ascii=False, indent=2)
                            if isinstance(payload, dict)
                            else str(payload),
                            Fore.CYAN,
                        )
                        wait_started = time.perf_counter()
                        wait_started_at = timestamp_now()
                        try:
                            user_response = await self._aprompt_user(
                                tid,
                                "resume_interrupt",
                                f"{Fore.YELLOW}Resume response (q=quit) > {Style.RESET_ALL}",
                                payload=payload if isinstance(payload, dict) else {"content": payload},
                                started_at=wait_started_at,
                            )
                        except HumanInputUnavailable:
                            continue
                        self._record_human_wait(
                            tid,
                            "resume_interrupt",
                            wait_started,
                            user_response,
                            payload if isinstance(payload, dict) else {"content": payload},
                            started_at=wait_started_at,
                        )
                        if user_response.strip().lower() in ["q", "quit", "exit"]:
                            self.profiler.finish_task(tid, "cancelled")
                            continue
                        await self._run_agent_interrupt_loop(Command(resume=user_response), tid)
                    else:
                        await self._run_agent_interrupt_loop("", tid, resume_initial=True)
                    continue

                if user_input == "/debug":
                    config   = {"configurable": {"thread_id": current_session_id}}
                    snapshot = self.app.get_state(config)
                    print(f"Next nodes: {snapshot.next}")
                    if snapshot.tasks and snapshot.tasks[0].interrupts:
                        print(f"Interrupt payload: {snapshot.tasks[0].interrupts[0].value}")
                    continue

                # ── Shell 直通 ────────────────────────────────
                if user_input.startswith("cd "):
                    try:
                        os.chdir(user_input[3:].strip())
                        if self.pm: self.pm.work_dir = Path(os.getcwd())
                        print(f"📂 {os.getcwd()}")
                    except Exception as e:
                        print(f"❌ {e}")
                    continue

                cmd_parts = user_input.split()
                base_cmd  = cmd_parts[0] if cmd_parts else ""
                is_direct = (user_input.startswith("!") or
                             (base_cmd in DIRECT_CMDS and len(user_input) < 60))

                if is_direct:
                    real_cmd = user_input[1:] if user_input.startswith("!") else user_input
                    if base_cmd == "ll":
                        real_cmd = "ls -lh " + " ".join(cmd_parts[1:])
                    import subprocess
                    subprocess.run(real_cmd, shell=True, executable="/bin/bash")
                    continue

                # ── Agent 处理（含中断循环）────────────────────
                next_input = user_input
                if self._should_auto_rename_session(current_session_id):
                    suggested_session_id = self._auto_task_id_for_prompt(next_input, fallback_prefix="session")
                    if suggested_session_id:
                        current_session_id = suggested_session_id
                        print(f"{Fore.GREEN}● Task title: {current_session_id}{Style.RESET_ALL}")
                await self._run_agent_interrupt_loop(next_input, current_session_id)

            except KeyboardInterrupt:
                restore_terminal_state()
                print(f"\n{Fore.YELLOW}⚡ Interrupted. Exiting interactive mode.{Style.RESET_ALL}")
                if self.state_manager:
                    self.state_manager.update_task_status(current_session_id, "interrupted")
                self.profiler.finish_task(current_session_id, "interrupted")
                break
            except Exception:
                import traceback
                traceback.print_exc()


# ════════════════════════════════════════════════════════════════
#  启动入口
# ════════════════════════════════════════════════════════════════

def restore_terminal():
    try:
        restore_terminal_state()
    except Exception:
        pass
    print("\n👋 Terminal restored.")


def _handle_suspend(signum, frame):
    """Suspend promptly; restore the TTY after the process is foregrounded again."""
    signal.signal(signal.SIGTSTP, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTSTP)
    signal.signal(signal.SIGTSTP, _handle_suspend)
    restore_terminal_state()


def _handle_interrupt(signum, frame):
    """Make Ctrl-C cancel executor-owned child process groups before exit."""
    terminate_active_processes()
    raise KeyboardInterrupt


async def main():
    command   = sys.argv[1] if len(sys.argv) > 1 else "interactive"
    data_path = str(PathManager.WORK_DIR)

    print(f"""
{Fore.CYAN}╔═══════════════════════════════════════════════════════╗
║    🧬  BioAnalyst Agent V4  –  Production System    ║
╚═══════════════════════════════════════════════════════╝{Style.RESET_ALL}
""")

    agent = BioAnalystAgent(
        data_path=data_path,
        llm_model=LLM_DEFAULT_MODEL,
        model_provider="openai",
        max_memory_tokens=200_000,
        use_llm_supervisor=True,
    )

    if command == "interactive":
        if hasattr(agent, "interactive_mode"):
            await agent.interactive_mode()
    elif command == "demo":
        await agent.go("Explore the data in ./sample_data")
    else:
        print(f"Unknown command: {command}")


if __name__ == "__main__":
    atexit.register(restore_terminal)
    try:
        signal.signal(signal.SIGINT, _handle_interrupt)
    except Exception:
        pass
    if hasattr(signal, "SIGTSTP"):
        try:
            signal.signal(signal.SIGTSTP, _handle_suspend)
        except Exception:
            pass
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bye")
    except Exception:
        import traceback
        traceback.print_exc()
