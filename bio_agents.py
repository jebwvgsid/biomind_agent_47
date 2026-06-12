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
import json
import logging
import os
import re
import shutil
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
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, BaseMessage
from langchain.chat_models import init_chat_model
from langchain_core.callbacks import BaseCallbackHandler

# ── 本地模块 ──────────────────────────────────────────────────
from prompts.templates import INSTALLER_HANDOVER_TEMPLATE
from execution.confirmation import ConfirmationUI, RiskLevel, ConfirmationManager, ConfirmationConfig, RiskAnalyzer
from config.settings import PathManager
from config.config import (
    RAGFLOW_ADDRESS, RAGFLOW_API_KEY, KB_MAP, RERANK_MODEL_ID,
    LLM_API_KEY, LLM_DEFAULT_MODEL, LLM_BASE_URL, BIO_SOFTWARE_ROOT, BIO_SOURCE_INSTALL_DIR,
    DEFAULT_PROJECT_ID, BIOMIND_MEMORY_DIR, BIOMIND_WIKI_DIR, BIOMIND_WIKI_INGEST_MODE, WIKI_EMBEDDING_ENABLED, WIKI_EMBEDDING_BASE_URL,
    WIKI_EMBEDDING_API_KEY, WIKI_EMBEDDING_MODEL, WIKI_EMBEDDING_DIMENSIONS,
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
from agent_core.workflow_manager import WorkflowManager, parse_workflow_handoff, validate_workflow_handoff
from agent_core.middleware import HITLMiddleware
from agent_core.llm_safety import deterministic_failure_solution, llm_failure_summary
from agent_core.plan_tracker import (
    build_plan_state,
    default_plan_file,
    ensure_plan_state,
    load_plan_state,
    mark_first_pending_running,
    mark_plan_step_blocked,
    mark_plan_step_done,
    record_current_step_evidence,
    render_plan_progress,
    reset_plan_state,
    save_plan_state,
    update_plan_markdown_status,
)
from execution.executor import detect_script_language, normalize_script_code, run_bash_script, run_python_repl
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
from memory.persistence import PersistentStateManager
from memory.core_store import CoreMemoryStore
from memory.session_archive import SessionArchive
from memory.task_semantics import infer_task_semantics, merge_task_semantics
from memory.task_summary import TaskSummaryRenderer
from memory.wiki_memory import WikiMemoryManager
from memory.artifact_index import ArtifactIndex
from prompts.manager import PromptManager

# ── 新增模块 ──────────────────────────────────────────────────
from memory_manager import MemoryManager
from supervisor import SupervisorNode, log_plan_to_cot

logger = logging.getLogger("BioMind.Agent")


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
            status = f" {top + 1}-{min(top + content_h, len(lines))}/{len(lines)}  n/j/down next  p/k/up prev  q/Enter return "

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
                cmd = safe_input(
                    "Scroll command > ",
                    timeout_seconds=resolve_input_timeout(300),
                    history=False,
                ).strip().lower()
                if cmd in {"", "q", "quit", "exit"}:
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
        except (EOFError, KeyboardInterrupt, HumanInputUnavailable):
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
        clean = re.sub(r"</?solution>", "", content, flags=re.IGNORECASE).strip()
        clean = re.sub(r"<[^>]+>.*?</[^>]+>", "", clean, flags=re.DOTALL).strip()
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

        # ── PackageManager ────────────────────────────────────
        try:
            self.pm = PackageManager(work_dir=data_path)
        except ImportError:
            logger.warning("PackageManager not found.")
            self.pm = None

        # ── LLM ──────────────────────────────────────────────
        self.llm = init_chat_model(
            model=llm_model,
            model_provider=model_provider,
            base_url=LLM_BASE_URL,
            streaming=True,
            api_key=LLM_API_KEY,
            callbacks=[VibeThinkingHandler(), ProfilingCallbackHandler(self.profiler)],
            temperature=0.7,
            extra_body={"enable_thinking": False},
        )

        # ── 验证 & 确认 ───────────────────────────────────────
        self.validator = ResultValidator()
        self.confirmation_manager = ConfirmationManager(
            ConfirmationConfig(auto_approve_safe=True, show_code_preview=True)
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
        self.configured_wiki_dir: Optional[Path] = None
        if BIOMIND_WIKI_DIR:
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
        self.task_summary_renderer = TaskSummaryRenderer(
            memory_dir=self.runtime_memory_dir,
            session_archive=self.session_archive,
            wiki_memory_manager=self.wiki_memory_manager,
        )
        self._wiki_digest_cache: Dict[str, Tuple[float, str]] = {}
        self.wiki_ingest_mode = BIOMIND_WIKI_INGEST_MODE
        self._wiki_ingest_offer_done: set[str] = set()
        self._wiki_auto_ingest_done: set[str] = set()

        # ── SupervisorNode（新）───────────────────────────────
        self.supervisor_node = SupervisorNode(
            llm=self.llm,
            output_dir=os.path.join(data_path, "task_outputs"),
            max_retries=3,
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
        if self.wiki_ingest_mode != "auto" or status != "completed":
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
        if self.wiki_ingest_mode != "ask":
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
        prompt = PromptManager.build_stable_system_prompt(
            package_manager=self.pm,
            work_dir=work_dir,
            core_memory=core_memory,
            agents_md_context=agents_md,
        )
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        meta = {
            "session_id": session_id,
            "project_id": project_id,
            "core_memory_loaded_at": snapshot.loaded_at,
            "work_dir": work_dir,
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
        if not query.strip():
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

        if evidence_query:
            wiki_context = self._wiki_prompt_context(task_id, project_id, trigger_text)
            if wiki_context.strip():
                contexts["wiki_context"] = wiki_context
            contexts["wiki_digest"] = self._wiki_digest_prompt_context(project_id, max_chars=2000)
        elif not memory_packet_context.strip() and state.get("wiki_memory_context"):
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

    def _wiki_prompt_context(self, task_id: str, project_id: str, query: str) -> str:
        if not query.strip():
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
        plan_progress: str = "",
    ) -> str:
        rag_history_list = state.get("rag_history", []) or []
        inherited_rag = "\n".join(rag_history_list)
        work_dir = str(self.pm.work_dir if self.pm else self.data_path)
        dynamic_tool_context = PromptManager._build_tool_context(self.pm, work_dir)
        return PromptManager.build_ephemeral_context(
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
            current_plan=current_plan,
            plan_progress=plan_progress,
        )

    @staticmethod
    def _generate_visible_messages(messages: List[BaseMessage]) -> List[BaseMessage]:
        """Drop consultant-only loading artifacts before the code-generation call."""
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
        return filtered

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
                    f"active_now={stage.get('active_now', False)}; "
                    f"reason={stage.get('reason', '')}"
                )
        for key in ("active_skills", "future_skills", "required_skills_all", "candidate_skills"):
            values = handoff.get(key) or []
            lines.append(f"{key}: {', '.join(values) if values else 'None'}")
        return "\n".join(lines)

    def _activate_workflow_batch(self, state: AgentState, batch_id: str) -> Dict[str, Any]:
        handoff = dict(state.get("workflow_handoff") or {})
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
            max_active_skills=SkillManager.MAX_SELECTED_SKILLS,
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
            "workflow_handoff": handoff,
            "workflow_stages": handoff.get("workflow_stages", []),
            "active_batch_id": handoff.get("active_batch_id", target_batch),
            "active_skills": handoff.get("active_skills", []),
            "future_skills": handoff.get("future_skills", []),
            "required_skills_all": handoff.get("required_skills_all", []),
            "selected_skills": handoff.get("active_skills", []),
            "next_step": "generate",
        }

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

    def _start_or_load_plan_progress(self, task_id: str, current_plan: Optional[str], evidence: str = "") -> str:
        state = self._ensure_plan_tracker(task_id, current_plan)
        if state.get("steps"):
            mark_first_pending_running(state, evidence=evidence or "generate_started")
            return self._persist_plan_tracker(task_id, state)
        return render_plan_progress(state)

    def _mark_plan_current_done(self, task_id: str, evidence: str) -> str:
        state = load_plan_state(self.data_path, task_id)
        if state.get("steps"):
            mark_plan_step_done(state, evidence=evidence)
            return self._persist_plan_tracker(task_id, state)
        return render_plan_progress(state)

    def _mark_plan_current_blocked(self, task_id: str, evidence: str) -> str:
        state = load_plan_state(self.data_path, task_id)
        if state.get("steps"):
            mark_plan_step_blocked(state, evidence=evidence)
            return self._persist_plan_tracker(task_id, state)
        return render_plan_progress(state)

    def _record_plan_current_evidence(self, task_id: str, event: str, detail: str = "") -> str:
        state = load_plan_state(self.data_path, task_id)
        if state.get("steps"):
            record_current_step_evidence(state, event, detail)
            return self._persist_plan_tracker(task_id, state)
        return render_plan_progress(state)

    def _plan_tracker_is_complete(self, task_id: str) -> bool:
        state = load_plan_state(self.data_path, task_id)
        steps = [step for step in state.get("steps", []) or [] if isinstance(step, dict)]
        return bool(steps) and all(str(step.get("status")) == "done" for step in steps)

    def _llm_failure_response(self, stage: str, exc: BaseException, task_id: str) -> AIMessage:
        summary = llm_failure_summary(exc, stage, task_id)
        logger.error(summary)
        return AIMessage(content=deterministic_failure_solution(exc, stage, task_id))

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

    def _runtime_status_for_task(self, task_id: str) -> Dict[str, Dict[str, Any]]:
        return dict(self.runtime_status.get(task_id or "default", {}))

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
                healthcheck=requirements.get("healthcheck", ""),
                status="registered",
            )
        cache_key = self._runtime_cache_key(skill_id, env)
        cached = self.runtime_status.get(task_id or "default", {}).get(cache_key)
        if use_cache and cached and cached.get("ok") and not mode_healthchecks:
            return True, str(cached.get("detail") or "Cached runtime healthcheck passed."), env
        if use_cache and cached and cached.get("ok"):
            ok, detail = True, str(cached.get("detail") or "Cached runtime healthcheck passed.")
        else:
            ok, detail = self.pm.verify_environment(
                env_name=env,
                language=requirements.get("language", ""),
                healthcheck=requirements.get("healthcheck", ""),
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
                healthcheck=requirements.get("healthcheck", ""),
            )
            self._cache_runtime_status(
                task_id,
                skill_id,
                env,
                ok,
                detail or ("OK" if ok else "Healthcheck failed."),
                requirements.get("healthcheck", ""),
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

    def _consultant_runtime_preflight_summary(self, selected_skills: Optional[List[str]], task_id: str) -> str:
        """Verify selected active skill runtimes before handing work to Planner."""
        if not self.skill_manager or not self.pm:
            return ""
        selected = self._selected_skill_ids(selected_skills)
        if not selected:
            return ""

        rows: List[str] = []
        for sid in selected:
            requirements = self._skill_runtime_requirements(sid)
            env = str(requirements.get("env") or requirements.get("default_env") or "").strip()
            if not env:
                continue
            if not any([
                requirements.get("packages"),
                requirements.get("capabilities"),
                requirements.get("language"),
                requirements.get("healthcheck"),
            ]):
                continue
            code_hint = self._consultant_runtime_code_hint(sid)
            ok, detail, checked_env = self._verify_skill_runtime(
                sid,
                task_id=task_id,
                use_cache=True,
                code=code_hint,
            )
            status = "passed" if ok else "failed"
            row = (
                f"- {sid}: {status}; env={checked_env or env}; "
                f"healthcheck={requirements.get('healthcheck', '') or 'metadata package check'}; "
                f"detail={str(detail or '').strip()[:500] or 'OK'}"
            )
            if not ok and hasattr(self.pm, "find_matching_environment"):
                try:
                    match = self.pm.find_matching_environment(requirements)
                except Exception:
                    match = None
                if match and match.get("env") and match.get("env") != (checked_env or env):
                    row += (
                        f"; registered_env_candidate={match.get('env')} "
                        f"({match.get('match_type', 'unknown')}; "
                        f"missing_packages={','.join(match.get('missing_packages', []) or []) or 'none'}; "
                        f"missing_capabilities={','.join(match.get('missing_capabilities', []) or []) or 'none'})"
                    )
            rows.append(row)

        if not rows:
            return ""
        return (
            "[RUNTIME_PREFLIGHT]\n"
            "- Consultant checked the selected active skill runtime(s) before Planner handoff.\n"
            "- A tool registered in a different environment is not automatically available inside the active skill runtime; Planner/Generate must use the skill env_runner, a registered command shim, or a documented fallback.\n"
            + "\n".join(rows)
            + "\n[/RUNTIME_PREFLIGHT]"
        )

    def _format_runtime_status_for_observation(self, task_id: str) -> str:
        records = self.runtime_status.get(task_id or "default", {})
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
            if not runner or runner == "bash":
                continue
            env = str(requirements.get("env") or requirements.get("default_env") or "").strip()
            meta = self.skill_manager.get_skill_meta(sid)
            skill_dir = Path(meta.get("_skill_dir", self.skill_manager.skills_root / sid)).resolve()
            runner_present = runner in code_text or (env and f"conda run -n {env}" in code_text)
            for script in meta.get("_scripts", []) or []:
                script_name = Path(script).name
                rel_script = str(script).replace("\\", "/")
                abs_script = str((skill_dir / script).resolve()).replace("\\", "/")
                mentions_script = (
                    script_name in code_text
                    or rel_script in normalized_code
                    or abs_script in normalized_code
                )
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

    def _skill_plan_violations(self, plan: str, selected_skills: List[str]) -> List[str]:
        if not selected_skills:
            return []
        if not self.skill_manager:
            return []
        return self.skill_manager.validate_plan(plan, selected_skills)

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
        """Keep selection narrow unless the request clearly describes a multi-stage workflow."""
        text_l = (text or "").lower()
        multi_markers = [
            "先", "然后", "之后", "接着", "再做", "并做", "并且", "同时",
            "first", "then", "followed by", "after that", "workflow", "pipeline",
        ]
        if not any(marker in text_l for marker in multi_markers):
            return 1
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
                    f'<install_tool env="{env}">{tool_name}</install_tool>'
                    "<!-- R package installation must be performed by install_subagent and rechecked after install. -->"
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
        return f'<install_tool env="{env}">{tool_name}</install_tool>'

    def _infer_runtime_skills_from_code(
        self,
        code: str,
        selected_skills: Optional[List[str]] = None,
    ) -> List[str]:
        skills = list(dict.fromkeys(self._selected_skill_ids(selected_skills)))
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

        for candidate in candidates:
            normalized = self.skill_manager.normalize_selected_skills([candidate], max_results=1)
            for sid in normalized:
                if sid not in skills:
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
        )
        return any(re.search(pattern, lower, re.IGNORECASE) for pattern in patterns)

    @staticmethod
    def _runtime_install_target_from_error(text: str, fallback_name: str) -> Tuple[str, str]:
        lower = str(text or "").lower()
        if re.search(r"rscript:\s*command not found|no rscript|which:\s*no rscript", lower, re.IGNORECASE):
            return "r_base_runtime", "Rscript is missing; install/register r-base before attempting CALDER."
        if re.search(r"there is no package called .{0,4}calder|calder2?\s+not available", lower, re.IGNORECASE):
            return "calder_r_runtime", "CALDER R package is missing; install it only after Rscript passes."
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
            redirect_name = requirements.get("install_tool") or defaults.get("tool_name", "skill_runtime")
            install_command = requirements.get("install_command", "")
            reason = stderr.strip() or stdout.strip() or f"Runtime dependency check failed for {sid}."
            redirect_name, ordered_hint = self._runtime_install_target_from_error(reason, redirect_name)
            if redirect_name == "calder_r_runtime" and not self._calder_runtime_allowed([sid], env):
                redirect_name = requirements.get("install_tool") or defaults.get("tool_name", "skill_runtime")
                ordered_hint = "Ignoring CALDER repair for non-CALDER2 skill; ordinary A/B compartments use PC1/eigenvector runtime."
            comment_parts = [reason[:500]]
            if ordered_hint:
                comment_parts.append(ordered_hint)
            if install_command:
                comment_parts.append(f"suggested install: {install_command}")
            return f'<install_tool env="{env}">{redirect_name}</install_tool><!-- {"; ".join(comment_parts)} -->', runtime_skills
        return "", runtime_skills

    def _selected_skill_ids(self, state_selected: Optional[List[str]] = None) -> List[str]:
        if not state_selected:
            return []
        return list(state_selected)

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
            "install_tool": requirements.get("install_tool", ""),
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
                if requirements.get("install_tool") == "calder_r_runtime":
                    return True
        return str(env or "").strip() == "calder2" and not selected

    def _sanitize_install_tool_for_selected_runtime(
        self,
        tool_name: str,
        selected_skills: Optional[List[str]],
        runtime_defaults: Dict[str, Any],
        requested_env: str = "",
    ) -> Tuple[str, str]:
        if str(tool_name or "").strip() != "calder_r_runtime":
            return tool_name, ""
        if self._calder_runtime_allowed(selected_skills, requested_env):
            return tool_name, ""
        replacement = runtime_defaults.get("install_tool") or runtime_defaults.get("tool_name") or "skill_runtime"
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
        requested_tool = str(tool_name or "").strip()
        for sid in normalized:
            requirements = self._skill_runtime_requirements(sid)
            if requested_tool and requested_tool == str(requirements.get("install_tool") or "").strip():
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
                redirect_name = requirements.get("install_tool") or self._skill_runtime_defaults([sid]).get("tool_name", "skill_runtime")
                reason = detail or f"Healthcheck failed for environment {env}"
                redirect_name, ordered_hint = self._runtime_install_target_from_error(reason, redirect_name)
                if redirect_name == "calder_r_runtime" and not self._calder_runtime_allowed([sid], env):
                    redirect_name = requirements.get("install_tool") or self._skill_runtime_defaults([sid]).get("tool_name", "skill_runtime")
                    ordered_hint = "Ignoring CALDER repair for non-CALDER2 skill; ordinary A/B compartments use PC1/eigenvector runtime."
                if ordered_hint:
                    reason = f"{reason} {ordered_hint}"
                return False, f'<install_tool env="{env}">{redirect_name}</install_tool><!-- {reason} -->'
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

        selected = merged.get("selected_skills", []) or []
        active = merged.get("active_skills", []) or []
        future = merged.get("future_skills", []) or []
        candidate = merged.get("candidate_skills", []) or []
        raw = merged.get("selected_skills_raw", []) or []
        handoff = merged.get("workflow_handoff", {}) or {}
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

    def _profile_node(self, node_name: str, func):
        async def wrapped(state: AgentState):
            task_id = state.get("task_id") or state.get("session_id") or "default"
            with self.profiler.node(task_id, node_name):
                result = await func(state)
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
            user_text = "\n".join(
                str(m.content) for m in messages if isinstance(m, HumanMessage)
            ).strip()
            task_id = state.get("task_id", "default")

            decision = await classify_task_mode(self.llm, user_text)
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
                response = await self.llm.ainvoke([SystemMessage(content=prompt)] + messages)
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
            consultant_context = "\n".join(
                str(m.content) for m in messages if isinstance(m, HumanMessage)
            )
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
            validation_errors = [
                str(m.content)
                for m in recent_consultant_messages
                if isinstance(m, SystemMessage) and "<consultant_validation_error>" in str(m.content)
            ]
            task_id = state.get("task_id", "default")
            project_id = state.get("project_id") or self.default_project_id

            # 内存压缩检查
            messages, compressed = await self.memory_manager.maybe_compress(messages, task_id)
            if compressed:
                output.print_once("mem_compress", "🗜️ Memory compressed and archived.", Fore.YELLOW)
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

            if self.skill_manager and (len(load_actions) >= 6 or len(validation_errors) >= 3):
                fallback_raw = []
                for action in load_actions:
                    parts = action.split(maxsplit=2)
                    if len(parts) >= 2:
                        fallback_raw.append(parts[1])
                if not fallback_raw:
                    fallback_raw = self.skill_manager.match_skills(consultant_context, max_results=consultant_max_skills)
                selected = self.skill_manager.normalize_selected_skills(fallback_raw, max_results=consultant_max_skills)
                recent_error_preview = "\n".join(
                    f"- {re.sub(r'<[^>]+>', '', err).strip()[:500]}"
                    for err in validation_errors[-3:]
                    if str(err).strip()
                )
                candidate_text = ", ".join(selected) if selected else "none"
                question = (
                    "Consultant handoff validation failed repeatedly in the current turn, so BioMind will not "
                    "auto-select skills or hand off to Planner.\n"
                    f"Candidate skill hints from metadata matching: {candidate_text}\n"
                    f"Recent validation errors:\n{recent_error_preview or '- none'}\n\n"
                    "Please provide the canonical skill id(s) to use, provide a corrected workflow handoff, "
                    "or reply with `blocked: <reason>`."
                )
                obs = (
                    "<consultant_loop_guard_blocked>\n"
                    "Repeated Consultant internal actions reached the loop guard. "
                    "Automatic skill fallback and Planner handoff are disabled.\n"
                    f"candidate_skill_hints: {candidate_text}\n"
                    "</consultant_loop_guard_blocked>"
                )
                output.print_once(
                    "consultant_loop_guard_blocked",
                    "⚠️ Consultant repeated internal actions; asking for explicit repair instead of auto-routing.",
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
            workflow_catalog = self.workflow_manager.get_workflow_catalog_prompt() if self.workflow_manager else "<workflow_catalog>\nNo workflow templates available.\n</workflow_catalog>"
            if self.skill_manager:
                skill_tree = self.skill_manager.get_skill_tree_prompt()
                skill_catalog = self.skill_manager.get_skill_catalog_prompt()
                skills_summary = (
                    f"Skills Absolute Directory: {self.skill_manager.skills_root}\n"
                    f"{skill_tree}\n{skill_catalog}"
                )
            else:
                skills_summary = "None"
            installed_tools_str = self.pm.list_installed_tools() if self.pm else "None"

            consultant_prompt = PromptManager.get_consultant_prompt(
                intent="General Inquiry",
                kb_list_desc=kb_list_desc,
                rag_history=rag_history,
                skills_summary=skills_summary,
                installed_tools_str=installed_tools_str,
                work_dir=self.pm.work_dir if self.pm else ".",
                max_skills=consultant_max_skills,
                wiki_memory_context=consultant_memory_retrieval_context,
                workflow_catalog=workflow_catalog,
            )

            try:
                response = await self.llm.ainvoke([SystemMessage(content=consultant_prompt)] + messages)
            except Exception as exc:
                response = self._llm_failure_response("consultant", exc, task_id)
                output.print_ai(response.content)
                plan_progress = self._mark_plan_current_blocked(task_id, llm_failure_summary(exc, "consultant", task_id))
                await self._finalize_task_memory(
                    task_id,
                    "failed",
                    messages + [response],
                    {"reason": llm_failure_summary(exc, "consultant", task_id), "plan_progress": plan_progress},
                )
                return {"messages": [response], "plan_progress": plan_progress, "next_step": "finish"}
            content = response.content.strip()
            await self._observe_session_memory(
                task_id,
                "consultant",
                messages + [response],
                {"consultant_output": content[:3000]},
            )

            # EXECUTE 动作
            if content.startswith("EXECUTE"):
                output.print_node_header("execute")
                code_match = re.search(r"```(?:bash|python)?\n(.*?)```", content, re.DOTALL)
                clean_code = code_match.group(1).strip() if code_match else content.replace("EXECUTE", "").strip()

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
                    stdout, stderr, ret = await run_bash_script(f'export PATH="{shim_path}:$PATH"\n{clean_code}')
                    obs = (
                        f"<observation>\n{'✅' if ret==0 else '❌'}\n"
                        f"{self._clip_observation_text(stdout, 1000)}\n"
                        f"{self._clip_observation_text(stderr, 500)}\n"
                        "</observation>"
                    )
                    output.print_observation(obs)
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
                    or workflow_handoff.get("active_skills")
                )
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
                            max_active_skills=SkillManager.MAX_SELECTED_SKILLS,
                        )
                        if validation["errors"]:
                            obs = (
                                "<consultant_validation_error>\n"
                                "Workflow handoff is invalid:\n"
                                + "\n".join(f"- {issue}" for issue in validation["errors"])
                                + "\nUse LOAD_WORKFLOW_TEMPLATE, repair WORKFLOW_STAGES, and keep ACTIVE_SKILLS tied to active stages.\n"
                                "</consultant_validation_error>"
                            )
                            output.print_once(
                                f"workflow_validation:{len(validation_errors)}",
                                "⚠️ Consultant workflow handoff failed validation; asking it to repair the stage coverage.",
                                Fore.YELLOW,
                            )
                            return {"messages": [AIMessage(content=content), SystemMessage(content=obs)], "next_step": "consultant"}
                        workflow_handoff = validation["handoff"]
                        active_skills = workflow_handoff.get("active_skills", [])
                        future_skills = workflow_handoff.get("future_skills", [])
                        required_skills_all = workflow_handoff.get("required_skills_all", [])
                        candidate_skills = workflow_handoff.get("candidate_skills", [])
                        active_batch_id = workflow_handoff.get("active_batch_id", "")
                        raw_selected = list(active_skills)
                        selected = list(active_skills)
                        if validation["warnings"]:
                            summary += (
                                "\n[WORKFLOW_VALIDATION_WARNINGS]\n"
                                + "\n".join(f"- {warning}" for warning in validation["warnings"])
                                + "\n[/WORKFLOW_VALIDATION_WARNINGS]"
                            )
                    elif "[SELECTED_SKILLS:" not in summary:
                        obs = (
                            "<consultant_validation_error>\n"
                            "FINISH must include either a workflow handoff ([WORKFLOW_DECISION], [WORKFLOW_STAGES], [ACTIVE_SKILLS]) or a [SELECTED_SKILLS: ...] marker.\n"
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
                            repaired_summary = self._repair_consultant_skill_summary(
                                summary,
                                repair_selected,
                                list(raw_selected),
                                list(selected),
                                list(issues),
                            )
                            output.print_once(
                                f"consultant_auto_repair:{len(validation_errors)}",
                                "✓ Consultant skill selection auto-repaired. Handing to Planner.",
                                Fore.GREEN,
                            )
                            return {
                                "messages": [AIMessage(content="FINISH\n" + repaired_summary)],
                                "selected_skills": repair_selected,
                                "selected_skills_raw": list(raw_selected),
                                "skill_selection_source": "consultant_auto_repair",
                                "requirements_summary": repaired_summary,
                                "memory_packet_context": self._consultant_handoff_memory_packet(repaired_summary, consultant_memory_retrieval_context),
                                "wiki_memory_context": consultant_wiki_context,
                                "next_step": "planner",
                            }
                        return {"messages": [AIMessage(content=content), SystemMessage(content=obs)], "next_step": "consultant"}
                else:
                    raw_selected = []
                    selected = []
                runtime_preflight_summary = self._consultant_runtime_preflight_summary(selected, task_id)
                if runtime_preflight_summary:
                    summary = (summary.rstrip() + "\n\n" + runtime_preflight_summary).strip()
                output.print_once("consultant_finish", "✓ Consultant complete. Handing to Planner.", Fore.GREEN)
                return {
                    "messages": [AIMessage(content=content)],
                    "selected_skills": selected,
                    "selected_skills_raw": list(raw_selected),
                    "active_skills": active_skills,
                    "future_skills": future_skills,
                    "required_skills_all": required_skills_all,
                    "candidate_skills": candidate_skills,
                    "workflow_id": workflow_handoff.get("workflow_id", ""),
                    "workflow_stages": workflow_handoff.get("workflow_stages", []),
                    "workflow_handoff": workflow_handoff if has_workflow_handoff else {},
                    "active_batch_id": active_batch_id,
                    "runtime_status": self._runtime_status_for_task(task_id),
                    "skill_selection_source": "consultant_finish",
                    "requirements_summary": summary,
                    "memory_packet_context": self._consultant_handoff_memory_packet(summary, consultant_memory_retrieval_context),
                    "wiki_memory_context": consultant_wiki_context,
                    "next_step": "planner",
                }
            obs = (
                "<consultant_validation_error>\n"
                "Consultant response did not use any supported command. Reply with exactly one command: "
                "EXECUTE, SEARCH, LOAD_WORKFLOW_TEMPLATE, LOAD_SKILL_DOC, LOAD_REFERENCE, LOAD_SCRIPT, ASK, or FINISH.\n"
                "If verification is complete, use FINISH with [SELECTED_SKILLS: ...] or a valid workflow handoff.\n"
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
            resume_step = str(pending.get("resume_step") or ("generate" if stage == "generate" else "consultant"))
            return {
                "messages": [HumanMessage(content=f"<human_answer stage=\"{stage}\">\nQuestion: {question}\nAnswer: {user_answer}\n</human_answer>")],
                "pending_human_question": {},
                "next_step": resume_step,
            }

        # ════════════════════════════════════════════
        # Node 1: Planner（详细规划 + 脚本预生成）
        # ════════════════════════════════════════════
        async def planner(state: AgentState):
            output.print_node_header("planner")
            messages   = state["messages"]
            user_msg   = messages[0].content
            summary    = state.get("requirements_summary", "")
            rag_history = state.get("rag_history", [])
            rag_knowledge = "\n".join(rag_history) if rag_history else ""

            installed  = self.pm.list_installed_tools() if self.pm else "None"
            skill_tree = self.skill_manager.get_skill_tree_prompt() if self.skill_manager else "<skill_tree>\nNo external skills available.\n</skill_tree>"
            active_skills_state = state.get("active_skills", []) or []
            selected_skills = active_skills_state or state.get("selected_skills", []) or []
            selected_skills_raw = state.get("selected_skills_raw", []) or list(selected_skills)
            skill_selection_source = state.get("skill_selection_source", "consultant_finish")
            workflow_handoff = state.get("workflow_handoff", {}) or {}
            active_batch_id = state.get("active_batch_id", "") or workflow_handoff.get("active_batch_id", "")
            future_skills = state.get("future_skills", []) or workflow_handoff.get("future_skills", []) or []
            required_skills_all = state.get("required_skills_all", []) or workflow_handoff.get("required_skills_all", []) or []
            candidate_skills = state.get("candidate_skills", []) or workflow_handoff.get("candidate_skills", []) or []
            skill_catalog = (
                self.skill_manager.get_skill_catalog_prompt(selected_skills)
                if self.skill_manager and selected_skills
                else self.skill_manager.get_skill_catalog_prompt() if self.skill_manager
                else "<skill_catalog>\nNo external skills available.\n</skill_catalog>"
            )
            skills_summary = f"{skill_tree}\n{skill_catalog}" if self.skill_manager else "None"
            active_skill_docs = self.skill_manager.load_skills_for_prompt(selected_skills) if self.skill_manager else ""
            skill_reference_index = self.skill_manager.get_skill_reference_index_prompt(selected_skills) if self.skill_manager else ""
            skill_execution_context = self._skill_execution_context(selected_skills)
            selected_skills_text = ", ".join(selected_skills) if selected_skills else "None"
            workflow_handoff_text = self._format_workflow_handoff_for_prompt(workflow_handoff)
            task_id = state.get("task_id", "default")
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

            detailed_plan_prompt = PromptManager.get_detailed_planner_prompt(
                user_msg=str(user_msg),
                consultant_summary=summary,
                planner_wiki_context=planner_wiki_context,
                installed_tools_str=installed,
                rag_knowledge=rag_knowledge,
                skills_summary=skills_summary,
                selected_skills_text=selected_skills_text,
                active_skill_docs=active_skill_docs,
                skill_reference_index=skill_reference_index,
                skill_execution_context=skill_execution_context,
                workflow_handoff=workflow_handoff_text,
                active_batch_id=str(active_batch_id or ""),
                active_skills_text=", ".join(selected_skills) if selected_skills else "None",
                future_skills_text=", ".join(future_skills) if future_skills else "None",
                required_skills_all_text=", ".join(required_skills_all) if required_skills_all else "None",
                candidate_skills_text=", ".join(candidate_skills) if candidate_skills else "None",
            )

            plan_content = ""
            violations = []
            for attempt in range(2):
                prompt_to_send = detailed_plan_prompt
                if violations:
                    prompt_to_send += (
                        "\n\n[PLAN VALIDATION FAILED]\n"
                        "Your previous plan violated the selected skill contract:\n"
                        + "\n".join(f"- {v}" for v in violations)
                        + "\nRegenerate the plan. Do not add placeholders or invented commands.\n"
                    )
                try:
                    response = await self.llm.ainvoke([HumanMessage(content=prompt_to_send)])
                except Exception as exc:
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
                plan_content = response.content
                violations = self._skill_plan_violations(plan_content, selected_skills)
                if not violations:
                    break

            if violations:
                plan_content += (
                    "\n\n## Planner Validation Warnings\n"
                    + "\n".join(f"- {v}" for v in violations)
                    + "\n"
                )

            if self.skill_manager:
                plan_active_skills = self.skill_manager.extract_active_skills_from_plan(plan_content)
                plan_future_skills = self.skill_manager.extract_future_skills_from_plan(plan_content)
                plan_legacy_skills = self.skill_manager.parse_selected_skill_ids(plan_content)
                proposed_skills = plan_active_skills or (
                    self.skill_manager.normalize_selected_skills(plan_legacy_skills)
                    if plan_legacy_skills and not workflow_handoff
                    else []
                )
                if proposed_skills:
                    if workflow_handoff:
                        candidate_handoff = dict(workflow_handoff)
                        candidate_handoff["active_skills"] = proposed_skills
                        if plan_future_skills:
                            candidate_handoff["future_skills"] = plan_future_skills
                        validation = validate_workflow_handoff(
                            candidate_handoff,
                            self.skill_manager,
                            self.workflow_manager,
                            max_active_skills=SkillManager.MAX_SELECTED_SKILLS,
                        )
                        if validation["errors"]:
                            plan_content += (
                                "\n\n## Workflow Skill Validation Warnings\n"
                                "- Planner ACTIVE_SKILLS were ignored because they failed validation:\n"
                                + "\n".join(f"  - {issue}" for issue in validation["errors"])
                                + "\n"
                            )
                        else:
                            workflow_handoff = validation["handoff"]
                            selected_skills = workflow_handoff.get("active_skills", [])
                            future_skills = workflow_handoff.get("future_skills", future_skills)
                            required_skills_all = workflow_handoff.get("required_skills_all", required_skills_all)
                            active_batch_id = workflow_handoff.get("active_batch_id", active_batch_id)
                            selected_skills_raw = list(selected_skills)
                            skill_selection_source = "planner_active_skills"
                    else:
                        selected_skills = proposed_skills
                        selected_skills_raw = list(proposed_skills)
                        skill_selection_source = "planner_active_skills" if plan_active_skills else "planner_selected_skills"

            # 保存计划文件
            plan_dir = os.path.join(self.data_path, "task_outputs", task_id)
            os.makedirs(plan_dir, exist_ok=True)
            plan_file = os.path.join(plan_dir, "plan.md")
            plan_state = build_plan_state(task_id, plan_content, plan_file=plan_file)
            save_plan_state(self.data_path, task_id, plan_state)
            with open(plan_file, "w", encoding="utf-8") as f:
                f.write(
                    "# Task Plan\n"
                    f"Generated: {datetime.now().isoformat()}\n"
                    f"Selected Skills: {', '.join(selected_skills) if selected_skills else 'None'}\n"
                    f"Workflow ID: {workflow_handoff.get('workflow_id', 'None') if workflow_handoff else 'None'}\n"
                    f"Active Batch: {active_batch_id or 'None'}\n"
                    f"Active Skills: {', '.join(selected_skills) if selected_skills else 'None'}\n"
                    f"Future Skills: {', '.join(future_skills) if future_skills else 'None'}\n"
                    f"Required Skills All: {', '.join(required_skills_all) if required_skills_all else 'None'}\n"
                    f"Selected Skills Raw: {', '.join(selected_skills_raw) if selected_skills_raw else 'None'}\n"
                    f"Skill Selection Source: {skill_selection_source}\n\n"
                    f"{plan_content}"
                )
            update_plan_markdown_status(plan_file, plan_state)
            plan_progress = render_plan_progress(plan_state)

            # 写入思维链日志
            log_plan_to_cot(self.supervisor_node, task_id, plan_content)
            await self._observe_session_memory(
                task_id,
                "planner",
                messages + [AIMessage(content=plan_content)],
                {
                    "plan_file": plan_file,
                    "selected_skills": selected_skills,
                    "active_skills": selected_skills,
                    "future_skills": future_skills,
                    "required_skills_all": required_skills_all,
                    "workflow_id": workflow_handoff.get("workflow_id", "") if workflow_handoff else "",
                "active_batch_id": active_batch_id,
                "skill_selection_source": skill_selection_source,
                "plan_progress": plan_progress,
                "plan": plan_content[:4000],
            },
        )

            return {
                "current_plan": plan_content,
                "plan_progress": plan_progress,
                "selected_skills": selected_skills,
                "selected_skills_raw": selected_skills_raw,
                "active_skills": selected_skills,
                "future_skills": future_skills,
                "required_skills_all": required_skills_all,
                "candidate_skills": candidate_skills,
                "workflow_handoff": workflow_handoff,
                "workflow_stages": workflow_handoff.get("workflow_stages", []) if workflow_handoff else [],
                "workflow_id": workflow_handoff.get("workflow_id", "") if workflow_handoff else "",
                "active_batch_id": active_batch_id,
                "skill_selection_source": skill_selection_source,
                "messages": [AIMessage(content=f"📋 Plan ready (saved to {plan_file})")],
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

            # 显示内存状态
            mem_status = self.memory_manager.status(messages)
            print(f"\n📊 Memory: {mem_status}")

            # 展示计划（限制行数避免刷屏）
            wait_started = time.perf_counter()
            wait_started_at = timestamp_now()
            print(f"\n{Fore.CYAN}{'='*60}")
            print(f"  📋  PLAN REVIEW  |  Task: {task_id}")
            print(f"{'='*60}{Style.RESET_ALL}")
            output.page_text(f"Plan Review | Task: {task_id}", plan, Fore.CYAN)
            print(f"{'─'*60}")

            try:
                user_input = self._prompt_user(
                    task_id,
                    "plan_review",
                    f"\n{Fore.YELLOW}Approve? [y/n/q] or type feedback > {Style.RESET_ALL}",
                    payload={"content": plan[:2000]},
                    started_at=wait_started_at,
                ).strip()
            except HumanInputUnavailable as exc:
                self._record_human_wait(
                    task_id,
                    "plan_review",
                    wait_started,
                    f"<waiting_user:{type(exc).__name__}>",
                    {"content": plan[:2000]},
                    started_at=wait_started_at,
                )
                return await self._suspend_for_human_input(
                    task_id,
                    "plan_review",
                    messages,
                    {"content": plan[:2000]},
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

            if il in ["y", "yes", ""]:
                print(f"{Fore.GREEN}✓ Plan approved. Executing...{Style.RESET_ALL}")
                plan_state = self._ensure_plan_tracker(task_id, plan)
                plan_progress = self._persist_plan_tracker(task_id, plan_state) if plan_state.get("steps") else render_plan_progress(plan_state)
                await self._observe_session_memory(
                    task_id,
                    "plan_review",
                    messages + [HumanMessage(content="Plan approved. Execute Step 1.")],
                    {"decision": "approved", "plan_excerpt": plan[:2000], "plan_progress": plan_progress},
                )
                return {
                    "messages": [HumanMessage(content="Plan approved. Execute Step 1.")],
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
                reset_plan_state(self.data_path, task_id)
                await self._observe_session_memory(
                    task_id,
                    "plan_review",
                    messages + [HumanMessage(content=f"Plan rejected. Feedback: {feedback}")],
                    {"decision": "rejected", "feedback": feedback, "plan_excerpt": plan[:2000]},
                )
                return {
                    "messages": [HumanMessage(content=f"Plan rejected. Feedback: {feedback}")],
                    "current_plan": None,
                    "selected_skills": [],
                    "selected_skills_raw": [],
                    "active_skills": [],
                    "future_skills": [],
                    "candidate_skills": [],
                    "workflow_handoff": {},
                    "workflow_stages": [],
                    "workflow_id": "",
                    "active_batch_id": "",
                    "skill_selection_source": "",
                    "plan_progress": "",
                    "next_step": "planner",
                }

        # ════════════════════════════════════════════
        # Node 2: Generate
        # ════════════════════════════════════════════
        async def generate(state: AgentState):
            output.print_node_header("generate")
            messages         = state["messages"]
            current_plan     = state.get("current_plan", "No plan.")
            task_id          = state.get("task_id", "unknown")
            supervisor_verdict = state.get("supervisor_verdict", "")

            # 内存压缩检查
            messages, compressed = await self.memory_manager.maybe_compress(messages, task_id)
            if compressed:
                print(f"{Fore.YELLOW}🗜️ Memory compressed mid-task.{Style.RESET_ALL}")

            plan_progress = self._start_or_load_plan_progress(
                task_id,
                current_plan,
                evidence="entered generate after compression check",
            )
            if supervisor_verdict and supervisor_verdict in ["RETRY", "WARN"]:
                plan_progress = self._record_plan_current_evidence(
                    task_id,
                    "supervisor_verdict",
                    str(supervisor_verdict),
                )

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
                selected = state.get("active_skills", []) or state.get("selected_skills", [])
                if selected:
                    loaded = self.skill_manager.load_skills_for_prompt(selected)
                    if loaded:
                        loaded_skills = loaded.replace("<active_skill_docs>", "<active_skills>").replace("</active_skill_docs>", "</active_skills>")
                        ref_index = self.skill_manager.get_skill_reference_index_prompt(selected)
                        if ref_index:
                            loaded_skills += "\n" + ref_index
                        loaded_skills += "\n" + self._skill_execution_context(selected)
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
            md_query = "\n".join(
                part for part in [last_msg_content[:600], str(current_plan)[:800]]
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
                supervisor_verdict=str(supervisor_verdict or ""),
            )
            task_summary_context = on_demand_memory["task_summary_context"]
            archive_context = on_demand_memory["archive_context"]
            wiki_context = on_demand_memory["wiki_context"]
            wiki_digest = on_demand_memory["wiki_digest"]

            # 注入 Supervisor 反馈（如果有）
            supervisor_instr = ""
            if supervisor_verdict and supervisor_verdict in ["RETRY", "WARN"]:
                supervisor_instr = f"""
<supervisor_context>
Last verdict: {supervisor_verdict}
The supervisor has flagged an issue. Please:
1. Read the <supervisor_feedback> in your history carefully.
2. Address the SPECIFIC problem identified.
3. Do NOT repeat the same command.
</supervisor_context>
"""

            stable_record = self._get_or_create_stable_prompt(session_id, project_id)
            stable_sys = state.get("stable_system_prompt") or stable_record["prompt"]
            ephemeral_context = self._ephemeral_prompt_context(
                state=state,
                messages=messages,
                current_plan=current_plan,
                loaded_skills=loaded_skills,
                task_summary_context=task_summary_context,
                memory_packet_context=memory_packet_context,
                wiki_context=wiki_context,
                wiki_digest=wiki_digest,
                archive_context=archive_context,
                supervisor_context=supervisor_instr,
                plan_progress=plan_progress,
            )
            visible_messages = self._generate_visible_messages(messages)
            call_msgs = [SystemMessage(content=stable_sys + "\n\n" + ephemeral_context)] + visible_messages
            try:
                response  = await self.llm.ainvoke(call_msgs)
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

            generated_action = (
                "finish" if answer_match else
                "ask_user" if ask_user_match else
                "activate_batch" if activate_match else
                "install" if install_match else
                "execute" if execute_match else
                "search" if search_match else
                "continue"
            )
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
            if answer_match:
                plan_progress = self._mark_plan_current_done(
                    task_id,
                    f"final solution generated: {answer_match.group(1).strip()[:500]}",
                )
                await self._finalize_task_memory(
                    task_id,
                    "completed",
                    messages + [response],
                    {"solution": answer_match.group(1).strip()[:3000]},
                )

            if activate_match and not answer_match:
                update = self._activate_workflow_batch(state, activate_match.group(1).strip())
                update_messages = update.get("messages", []) if isinstance(update, dict) else []
                update_text = "\n".join(str(getattr(msg, "content", msg)) for msg in update_messages)
                if "✅" in update_text:
                    plan_progress = self._mark_plan_current_done(task_id, f"activate batch success: {activate_match.group(1).strip()}")
                else:
                    plan_progress = self._record_plan_current_evidence(task_id, "activate_batch_failed", update_text[:1000])
                return {
                    **update,
                    "messages": [response] + update.get("messages", []),
                    "plan_progress": plan_progress,
                    "next_step": "generate",
                }

            if ask_user_match and not answer_match:
                question = ask_user_match.group(1).strip()
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
                }

            # Active RAG
            if search_match:
                query = search_match.group(1).strip()
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
                else:
                    plan_progress = self._mark_plan_current_done(task_id, f"search completed: {query[:500]}")
                tool_msg = HumanMessage(content=f"<rag_result>\n{query}\n{rag_result}\n</rag_result>")
                return {"messages": [response, tool_msg], "plan_progress": plan_progress, "next_step": "generate"}

            next_step = "generate"
            if answer_match:    next_step = "finish"
            elif ask_user_match: next_step = "ask_user"
            elif install_match: next_step = "install_subagent"
            elif execute_match:
                code_to_execute = execute_match.group(1).strip()
                next_step = "execute" if code_to_execute else "generate"

            return {"messages": [response], "plan_progress": plan_progress, "next_step": next_step}

        # ════════════════════════════════════════════
        # Node 3: Execute
        # ════════════════════════════════════════════
        async def execute_logic(state: AgentState) -> AgentState:
            output.print_node_header("execute")
            last_message = state["messages"][-1].content
            task_id      = state.get("task_id", "default")

            match = re.search(r"<execute>(.*?)</execute>", last_message, re.DOTALL)
            if not match:
                plan_progress = self._record_plan_current_evidence(task_id, "execute_missing_code", "No <execute> body found.")
                return {"messages": [HumanMessage(content="<observation>❌ No code found.</observation>")],
                        "plan_progress": plan_progress,
                        "next_step": "generate"}

            original_code = match.group(1).strip()
            language = detect_script_language(original_code)
            code = normalize_script_code(original_code)
            selected_skills_state = state.get("active_skills", []) or state.get("selected_skills", [])
            future_skills_state = state.get("future_skills", []) or []
            runtime_skills_state = self._infer_runtime_skills_from_code(code, selected_skills_state)
            install_redirect = self._redirect_install_command(code, runtime_skills_state)
            if install_redirect:
                plan_progress = self._record_plan_current_evidence(task_id, "redirect_to_install", install_redirect[:1000])
                return {
                    "messages": [AIMessage(content=install_redirect)],
                    "plan_progress": plan_progress,
                    "selected_skills": runtime_skills_state,
                    "active_skills": runtime_skills_state,
                    "runtime_status": self._runtime_status_for_task(task_id),
                    "next_step": "install_subagent",
                }

            future_violation = self._workflow_future_skill_violation(code, selected_skills_state, future_skills_state)
            if future_violation:
                plan_progress = self._record_plan_current_evidence(task_id, "future_skill_guard", future_violation[:1000])
                obs = f"<observation>❌ Workflow skill guard blocked execution.\n{future_violation}\n</observation>"
                return {
                    "messages": [HumanMessage(content=obs)],
                    "plan_progress": plan_progress,
                    "runtime_status": self._runtime_status_for_task(task_id),
                    "next_step": "generate",
                }

            runner_violation = self._selected_skill_env_runner_violation(code, runtime_skills_state)
            if runner_violation:
                plan_progress = self._record_plan_current_evidence(task_id, "skill_env_runner_guard", runner_violation[:1000])
                obs = f"<observation>❌ Skill runtime guard blocked execution.\n{runner_violation}\n</observation>"
                return {
                    "messages": [HumanMessage(content=obs)],
                    "plan_progress": plan_progress,
                    "runtime_status": self._runtime_status_for_task(task_id),
                    "next_step": "generate",
                }

            runtime_ready, runtime_redirect = self._ensure_skill_runtime_ready(runtime_skills_state, task_id=task_id, code=code)
            if not runtime_ready and runtime_redirect:
                plan_progress = self._record_plan_current_evidence(task_id, "runtime_not_ready", runtime_redirect[:1000])
                return {
                    "messages": [AIMessage(content=runtime_redirect)],
                    "plan_progress": plan_progress,
                    "selected_skills": runtime_skills_state,
                    "active_skills": runtime_skills_state,
                    "runtime_status": self._runtime_status_for_task(task_id),
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
                        "runtime_status": self._runtime_status_for_task(task_id),
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

            runtime_skills_state = self._infer_runtime_skills_from_code(code_to_run, selected_skills_state)
            install_redirect = self._redirect_install_command(code_to_run, runtime_skills_state)
            if install_redirect:
                plan_progress = self._record_plan_current_evidence(task_id, "redirect_to_install", install_redirect[:1000])
                return {
                    "messages": [AIMessage(content=install_redirect)],
                    "plan_progress": plan_progress,
                    "selected_skills": runtime_skills_state,
                    "active_skills": runtime_skills_state,
                    "runtime_status": self._runtime_status_for_task(task_id),
                    "next_step": "install_subagent",
                }

            future_violation = self._workflow_future_skill_violation(code_to_run, selected_skills_state, future_skills_state)
            if future_violation:
                plan_progress = self._record_plan_current_evidence(task_id, "future_skill_guard", future_violation[:1000])
                obs = f"<observation>❌ Workflow skill guard blocked execution.\n{future_violation}\n</observation>"
                return {
                    "messages": [HumanMessage(content=obs)],
                    "plan_progress": plan_progress,
                    "runtime_status": self._runtime_status_for_task(task_id),
                    "next_step": "generate",
                }

            runner_violation = self._selected_skill_env_runner_violation(code_to_run, runtime_skills_state)
            if runner_violation:
                plan_progress = self._record_plan_current_evidence(task_id, "skill_env_runner_guard", runner_violation[:1000])
                obs = f"<observation>❌ Skill runtime guard blocked execution.\n{runner_violation}\n</observation>"
                return {
                    "messages": [HumanMessage(content=obs)],
                    "plan_progress": plan_progress,
                    "runtime_status": self._runtime_status_for_task(task_id),
                    "next_step": "generate",
                }

            runtime_ready, runtime_redirect = self._ensure_skill_runtime_ready(runtime_skills_state, task_id=task_id, code=code_to_run)
            if not runtime_ready and runtime_redirect:
                plan_progress = self._record_plan_current_evidence(task_id, "runtime_not_ready", runtime_redirect[:1000])
                return {
                    "messages": [AIMessage(content=runtime_redirect)],
                    "plan_progress": plan_progress,
                    "selected_skills": runtime_skills_state,
                    "active_skills": runtime_skills_state,
                    "runtime_status": self._runtime_status_for_task(task_id),
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
                    f.write("#!/bin/bash\nset -euo pipefail\n\n")
                f.write(code_to_run)
            output.print_once(f"script:{script_file}", f"💾 Script: {script_file}", Fore.GREEN)

            # 执行
            shim_path  = self.pm.get_shim_dir_str()
            start_time = time.time()
            try:
                if language == "Bash":
                    code_with_path = f'export PATH="{shim_path}:$PATH"\n{code_to_run}'
                    stdout, stderr, returncode = await run_bash_script(code_with_path)
                else:
                    code_with_path = (f"import os\nos.environ['PATH']="
                                      f"r'{shim_path}'+os.pathsep+os.environ.get('PATH','')\n{code_to_run}")
                    stdout, stderr, returncode = await run_python_repl(code_with_path)
            except Exception as e:
                stdout, stderr, returncode = "", str(e), 1

            duration = time.time() - start_time
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
            runtime_install_redirect, runtime_skills_after_execute = self._runtime_install_redirect_from_execution(
                code_to_run,
                stdout,
                stderr,
                runtime_skills_state,
                task_id,
            )
            runtime_missing = bool(runtime_install_redirect)

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

            runtime_status_block = self._format_runtime_status_for_observation(task_id)
            stdout_obs = self._clip_observation_text(stdout, 2000)
            stderr_obs = self._clip_observation_text(stderr, 1000)
            if runtime_missing:
                obs = (f"<observation>\n[{language}] ❌ Runtime missing ({duration:.1f}s)\n"
                       f"STDOUT:\n{stdout_obs}\n"
                       f"STDERR:\n{stderr_obs}"
                       f"{validation_block}"
                       f"{runtime_status_block}\n</observation>")
            elif returncode == 0 and not validation_failed:
                obs = (f"<observation>\n[{language}] ✅ Success ({duration:.1f}s)\n"
                       f"STDOUT:\n{stdout_obs}"
                       f"{validation_block}"
                       f"{runtime_status_block}\n</observation>")
            elif returncode == 0 and validation_failed:
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
            if runtime_missing:
                plan_progress = self._record_plan_current_evidence(
                    task_id,
                    "runtime_missing_after_execute",
                    f"{script_file}\n{runtime_install_redirect[:1000]}",
                )
            elif returncode == 0 and not validation_failed:
                plan_progress = self._record_plan_current_evidence(
                    task_id,
                    "execute_success",
                    f"{script_file}\nSTDOUT:\n{self._clip_observation_text(stdout, 1000)}",
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
            if runtime_missing:
                return {
                    "messages": [HumanMessage(content=obs), AIMessage(content=runtime_install_redirect)],
                    "plan_progress": plan_progress,
                    "selected_skills": runtime_skills_after_execute,
                    "active_skills": runtime_skills_after_execute,
                    "runtime_status": self._runtime_status_for_task(task_id),
                    "next_step": "install_subagent",
                }
            task_mode = str(state.get("task_mode") or "").strip().lower()
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
                "messages": [HumanMessage(content=obs)],
                "plan_progress": plan_progress,
                "runtime_status": self._runtime_status_for_task(task_id),
                "next_step": "finish" if task_mode in {"readonly", "simple_execute"} else "supervisor",
            }

        execute_node = error_recovery_middleware(
            self.llm,
            self.retriever,
            skill_manager=self.skill_manager,
        )(execute_logic)

        # ════════════════════════════════════════════
        # Node 4: Supervisor（新节点）
        # ════════════════════════════════════════════
        async def supervisor(state: AgentState):
            output.print_node_header("supervisor")
            update = await self.supervisor_node.run(state)
            task_id = state.get("task_id", "default")
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
                if verdict in {"PASS", "WARN"}:
                    plan_progress = self._mark_plan_current_done(task_id, f"supervisor_verdict: {verdict}")
                    update["plan_progress"] = plan_progress
                    if self._plan_tracker_is_complete(task_id):
                        update["next_step"] = "finish"
                elif verdict == "RETRY":
                    plan_progress = self._record_plan_current_evidence(task_id, "supervisor_verdict", verdict)
                    update["plan_progress"] = plan_progress
                elif verdict == "ESCALATE":
                    plan_progress = self._mark_plan_current_blocked(task_id, "supervisor_verdict: ESCALATE")
                    update["plan_progress"] = plan_progress
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
            return update

        # ════════════════════════════════════════════
        # Node 5: Install Sub-Agent
        # ════════════════════════════════════════════
        async def install_subagent(state: AgentState):
            output.print_node_header("install_subagent")
            last_msg = state["messages"][-1].content
            task_id  = state.get("task_id", "default")
            context_summary = state.get("requirements_summary", "No context.")
            current_plan_for_install = state.get("current_plan", "")
            selected_for_install = state.get("active_skills", []) or state.get("selected_skills", [])
            if current_plan_for_install:
                context_summary += f"\n\nCurrent approved plan:\n{current_plan_for_install[:2000]}"
            runtime_defaults = self._skill_runtime_defaults(selected_for_install)
            if self.skill_manager and selected_for_install:
                skill_ctx = self.skill_manager.load_skills_for_prompt(selected_for_install)
                cards = self._skill_execution_context(selected_for_install)
                if skill_ctx or cards:
                    context_summary += f"\n\nSelected skill runtime context:\n{cards}\n{skill_ctx[:4000]}"

            match = re.search(r"<install_tool(?:\s+env=[\"'](.*?)[\"'])?>(.*?)</install_tool>", last_msg, re.DOTALL)
            specified_env = match.group(1) if match else None
            tool_name     = match.group(2).strip() if match else runtime_defaults.get("tool_name", "skill_runtime")
            if not tool_name or tool_name == "unknown":
                tool_name = runtime_defaults.get("tool_name", "skill_runtime")
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
                    "next_step": "finish",
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
            self._install_attempt_counts[attempt_key] = self._install_attempt_counts.get(attempt_key, 0) + 1
            scope_key = ""
            if explicit_runtime_repair:
                skill_scope = ",".join(selected_skills or selected_for_install or [tool_name])
                scope_key = f"{task_id}::runtime_repair::{explicit_runtime_repair}::{target_env}::{skill_scope}"
                self._install_scope_attempt_counts[scope_key] = self._install_scope_attempt_counts.get(scope_key, 0) + 1
                if self._install_scope_attempt_counts[scope_key] > 2:
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
                        "next_step": "finish",
                    }
            if self._install_attempt_counts[attempt_key] > 2:
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
                    "next_step": "finish",
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

            sub_config   = {"configurable": {"thread_id": f"{task_id}_installer_{tool_name}"}}
            final_res    = "Installation Failed"
            snapshot     = self.installer_app.get_state(sub_config)
            current_payload = None if (snapshot.tasks and snapshot.tasks[0].interrupts) \
                              else {"tool_name": tool_name,
                                    "messages": [HumanMessage(content=initial_msg)]}

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
            if install_success and (selected_skills or selected_for_install):
                recheck_failures = []
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
                if recheck_failures:
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
                    "next_step": "generate" if install_success else "finish"}

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
        workflow.add_node("install_subagent", self._profile_node("install_subagent", install_subagent))

        workflow.add_edge(START, "triage")

        def triage_router(state):
            step = state.get("next_step")
            if step == "direct_chat": return "direct_chat"
            if step == "generate":    return "generate"
            return "consultant"

        workflow.add_conditional_edges("triage", triage_router,
                                       ["direct_chat", "generate", "consultant"])
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
        workflow.add_edge("planner",     "plan_review")

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
            step = state.get("next_step")
            if step == "finish":         return END
            if step == "ask_user":       return "ask_user"
            if step == "execute":        return "execute"
            if step == "install_subagent": return "install_subagent"
            return "generate"

        workflow.add_conditional_edges("generate", generate_router,
                                       ["ask_user", "execute", "install_subagent", "generate", END])

        # execute → supervisor → generate（managed 核心链路）；轻量任务直接结束。
        def execute_router(state):
            if state.get("next_step") == "finish":
                return END
            if state.get("next_step") == "install_subagent":
                return "install_subagent"
            if state.get("next_step") == "generate":
                return "generate"
            if str(state.get("task_mode") or "").strip().lower() in {"readonly", "simple_execute"}:
                return END
            return "supervisor"

        workflow.add_conditional_edges("execute", execute_router,
                                       ["generate", "install_subagent", "supervisor", END])

        def supervisor_router(state):
            step = state.get("next_step", "generate")
            if step == "finish": return END
            return "generate"

        workflow.add_conditional_edges("supervisor", supervisor_router,
                                       ["generate", END])

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
        steps = [step for step in state.get("steps", []) or [] if isinstance(step, dict)]
        if steps:
            statuses = {str(step.get("status") or "").strip().lower() for step in steps}
            if "blocked" in statuses:
                return "blocked"
            if all(status == "done" for status in statuses):
                return "completed"
            return "blocked"

        plan_progress = str((values or {}).get("plan_progress") or "")
        if "- [!]" in plan_progress or "Next step: BLOCKED:" in plan_progress:
            return "blocked"
        return ""

    def _terminal_status_from_snapshot(self, task_id: str, snapshot: Any) -> str:
        values = self._snapshot_values(snapshot)
        if self._snapshot_next_step(snapshot) == "human_input" or load_waiting_state(self.data_path, task_id):
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
            stable_record = self._get_or_create_stable_prompt(task_id, project_id)
            if not resume or not self.task_summary_renderer.task_exists(task_id):
                self.task_summary_renderer.initialize_task(task_id, str(prompt), status="running")
            if self.state_manager:
                if resume:
                    prompt = self._build_resume_prompt(task_id, prompt)
                else:
                    self.state_manager.create_task(task_id, prompt)
            elif resume:
                prompt = self._build_resume_prompt(task_id, prompt)
            runner_input = {
                "messages": [HumanMessage(content=prompt)],
                "task_id": task_id,
                "session_id": task_id,
                "project_id": project_id,
                "stable_system_prompt": stable_record["prompt"],
                "core_memory_loaded_at": stable_record["core_memory_loaded_at"],
                "runtime_status": self._runtime_status_for_task(task_id),
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
  /memory lint        Check Core/Wiki memory boundary and conflicts
  /memory promote     Promote safe wiki index refs into Core project memory
  /wiki               Show wiki memory status
  /wiki search <q>    Search LLM wiki memory
  /wiki ingest        Curate pending SessionArchive events into wiki pages
  /wiki ingest task   Curate pending events from the current task only
  /wiki compile       Rebuild wiki digest and claims cache
  /wiki lint          Check wiki links and source metadata
  /wiki page <id>     Show a wiki page by path or title
  /wiki review        Show pending wiki review items
  /task summarize [id] Render a task summary report from archive/wiki evidence
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

                # ── 系统命令 ─────────────────────────────────
                if user_input == "/exit":
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
                    wiki_lint = self.wiki_memory_manager.lint(project_id=self.default_project_id)
                    core_lint = self.core_memory_store.lint_project_memory(
                        self.default_project_id,
                        wiki_claims=self.wiki_memory_manager.read_claims_index(self.default_project_id),
                    )
                    output.page_text(
                        "Memory Lint",
                        f"{core_lint}\n\n{wiki_lint}",
                        Fore.CYAN,
                    )
                    continue

                if user_input == "/memory promote":
                    result = self.wiki_memory_manager.sync_project_index_fields(project_id=self.default_project_id)
                    output.page_text("Memory Promote", result, Fore.CYAN)
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
                        await self.go(Command(resume=user_response), task_id=tid, resume=True)
                    else:
                        await self.go("", task_id=tid, resume=True)
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
                while True:
                    await self.go(next_input, task_id=current_session_id)
                    config   = {"configurable": {"thread_id": current_session_id}}
                    snapshot = self.app.get_state(config)

                    if not (snapshot.tasks and snapshot.tasks[0].interrupts):
                        terminal_status = self._terminal_status_from_snapshot(current_session_id, snapshot)
                        if terminal_status == "waiting_user":
                            if self.state_manager:
                                self.state_manager.update_task_status(current_session_id, "waiting_user")
                            self.profiler.finish_task(current_session_id, "waiting_user")
                            break
                        if self.state_manager:
                            try:
                                self.state_manager.update_task_status(current_session_id, terminal_status)
                            except Exception:
                                pass
                        self.profiler.finish_task(current_session_id, terminal_status)
                        if terminal_status == "completed":
                            await self._maybe_offer_wiki_ingest(current_session_id, self.default_project_id)
                        break

                    payload = snapshot.tasks[0].interrupts[0].value
                    user_response = ""

                    # 代码审查
                    if isinstance(payload, dict) and payload.get("type") == "code_review":
                        risk_info = payload.get("risk", {})
                        risk = risk_info.get("level") or risk_info.get("risk_level") or "MEDIUM"
                        print(f"\n{Fore.YELLOW}⚠️  CODE REVIEW  |  Risk: {risk}{Style.RESET_ALL}")
                        wait_started = time.perf_counter()
                        wait_started_at = timestamp_now()
                        output.page_text("Code Review", payload.get('content', ''), Fore.CYAN)
                        try:
                            user_response = await self._aprompt_user(
                                current_session_id,
                                "code_review",
                                f"{Fore.YELLOW}[y/Enter=run, n=reject, q=quit, e:<code>=edit, text=feedback] > {Style.RESET_ALL}",
                                payload=payload,
                                started_at=wait_started_at,
                            )
                        except HumanInputUnavailable as exc:
                            self._record_human_wait(
                                current_session_id,
                                "code_review",
                                wait_started,
                                f"<waiting_user:{type(exc).__name__}>",
                                payload,
                                started_at=wait_started_at,
                            )
                            break
                        self._record_human_wait(
                            current_session_id,
                            "code_review",
                            wait_started,
                            user_response,
                            payload,
                            started_at=wait_started_at,
                        )

                    # 环境选择
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
                                current_session_id,
                                "env_selection",
                                f"Target env (Enter={dflt}) > ",
                                payload=payload,
                                started_at=wait_started_at,
                            )
                        except HumanInputUnavailable as exc:
                            self._record_human_wait(
                                current_session_id,
                                "env_selection",
                                wait_started,
                                f"<waiting_user:{type(exc).__name__}>",
                                payload,
                                started_at=wait_started_at,
                            )
                            break
                        raw_response = user_response
                        if not user_response.strip():
                            user_response = dflt
                        self._record_human_wait(
                            current_session_id,
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
                                current_session_id,
                                prompt_type,
                                f"{Fore.YELLOW}Response (q=quit) > {Style.RESET_ALL}",
                                payload=payload,
                                started_at=wait_started_at,
                            )
                        except HumanInputUnavailable as exc:
                            self._record_human_wait(
                                current_session_id,
                                prompt_type,
                                wait_started,
                                f"<waiting_user:{type(exc).__name__}>",
                                payload,
                                started_at=wait_started_at,
                            )
                            break
                        self._record_human_wait(
                            current_session_id,
                            prompt_type,
                            wait_started,
                            user_response,
                            payload,
                            started_at=wait_started_at,
                        )

                    # 通用中断
                    else:
                        wait_started = time.perf_counter()
                        wait_started_at = timestamp_now()
                        generic_payload = payload if isinstance(payload, dict) else {"content": payload}
                        try:
                            user_response = await self._aprompt_user(
                                current_session_id,
                                "interrupt",
                                f"{Fore.YELLOW}Response (q=quit) > {Style.RESET_ALL}",
                                payload=generic_payload,
                                started_at=wait_started_at,
                            )
                        except HumanInputUnavailable as exc:
                            self._record_human_wait(
                                current_session_id,
                                "interrupt",
                                wait_started,
                                f"<waiting_user:{type(exc).__name__}>",
                                generic_payload,
                                started_at=wait_started_at,
                            )
                            break
                        self._record_human_wait(
                            current_session_id,
                            "interrupt",
                            wait_started,
                            user_response,
                            generic_payload,
                            started_at=wait_started_at,
                        )

                    if user_response.strip().lower() in ["q", "quit", "exit"]:
                        self.profiler.finish_task(current_session_id, "cancelled")
                        break
                    next_input = Command(resume=user_response)

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
    restore_terminal_state()
    signal.signal(signal.SIGTSTP, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTSTP)
    signal.signal(signal.SIGTSTP, _handle_suspend)


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
        llm_model="qwen3-max",
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
