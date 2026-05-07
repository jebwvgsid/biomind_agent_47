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
    LLM_DEFAULT_MODEL, LLM_BASE_URL, BIO_SOFTWARE_ROOT, BIO_SOURCE_INSTALL_DIR,
    DEFAULT_PROJECT_ID,
)

try:
    from package_manager import PackageManager
except ImportError:
    sys.path.append(str(project_root))
    from package_manager import PackageManager

from agent_core.state import AgentState
from agent_core.installer import InstallerAgent
from agent_core.retriever import RAGFlowRetriever
from agent_core.router import determine_intent
from agent_core.skill_manager import SkillManager
from agent_core.middleware import HITLMiddleware
from execution.executor import detect_script_language, normalize_script_code, run_bash_script, run_python_repl
from execution.validator import ResultValidator
from execution.monitoring import ExecutionMonitor, LogCleaner, ErrorRecoverySystem
from agent_core.middleware import error_recovery_middleware
from memory.persistence import PersistentStateManager
from memory.md_memory import MarkdownMemoryManager
from memory.core_store import CoreMemoryStore
from memory.session_archive import SessionArchive
from memory.wiki_memory import WikiMemoryManager
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
                cmd = input("Scroll command > ").strip().lower()
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
        except (EOFError, KeyboardInterrupt):
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
        clean = re.sub(r"<[^>]+>.*?</[^>]+>", "", content, flags=re.DOTALL).strip()
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
            api_key=os.environ.get("OPENAI_API_KEY"),
            callbacks=[VibeThinkingHandler()],
            temperature=0.7,
            model_kwargs={"extra_body": {"enable_thinking": False}},
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
        self.runtime_root = Path(data_path) / ".biomind"
        self.runtime_memory_dir = self.runtime_root / "memory"
        migrated = WikiMemoryManager.migrate_legacy_runtime_memory(
            Path(data_path) / "memory",
            self.runtime_memory_dir,
        )
        if migrated:
            logger.info("Migrated legacy runtime memory files into %s", self.runtime_memory_dir)

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
        self.md_memory_manager = MarkdownMemoryManager(
            llm=self.llm,
            memory_dir=self.runtime_memory_dir,
            core_store=self.core_memory_store,
            core_project_id=self.default_project_id,
        )
        self.wiki_memory_manager = WikiMemoryManager(
            llm=self.llm,
            memory_dir=self.runtime_memory_dir,
            default_project_id=self.default_project_id,
        )

        # ── SupervisorNode（新）───────────────────────────────
        self.supervisor_node = SupervisorNode(
            llm=self.llm,
            output_dir=os.path.join(data_path, "task_outputs"),
            max_retries=3,
            use_llm_review=use_llm_supervisor,
        )
        self._install_env_choices: Dict[str, str] = {}

        # ── 子智能体 ──────────────────────────────────────────
        _installer = InstallerAgent(self.llm, work_dir=data_path, package_manager=self.pm)
        self.installer_app = _installer.get_graph()

        # ── SkillManager ──────────────────────────────────────
        try:
            from config.config import SKILLS_DIR
            self.skill_manager = SkillManager(SKILLS_DIR)
        except Exception:
            self.skill_manager = None

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

    async def _observe_md_memory(
        self,
        task_id: str,
        stage: str,
        messages: Optional[List[BaseMessage]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        messages = messages or []
        payload = payload or {}
        event_type = str(payload.get("event_type") or f"{stage}_event")
        project_id = str(payload.get("project_id") or self.default_project_id)
        archive_id = None
        try:
            self.md_memory_manager.append_event(
                task_id=task_id,
                stage=stage,
                messages=messages,
                payload=payload,
            )
            if self.session_archive:
                recent = messages[-4:] if messages else []
                content_parts = [
                    f"{type(msg).__name__}: {str(getattr(msg, 'content', msg))[:2000]}"
                    for msg in recent
                ]
                if payload:
                    content_parts.append("payload: " + json.dumps(payload, ensure_ascii=False, default=str)[:4000])
                content = "\n\n".join(content_parts) or stage
                summary = str(payload.get("summary") or content[:1200])
                archive_id = self.session_archive.append_event(
                    task_id=task_id,
                    session_id=task_id,
                    stage=stage,
                    event_type=event_type,
                    content=content,
                    summary=summary,
                    metadata=payload,
                )
        except Exception as e:
            logger.warning(f"Markdown memory observe failed at {stage}: {e}")
        try:
            source_refs = [f"task:{task_id}"]
            if archive_id is not None:
                source_refs.append(f"session_archive:{archive_id}")
            self.wiki_memory_manager.append_raw_event(
                task_id=task_id,
                session_id=task_id,
                stage=stage,
                event_type=event_type,
                messages=messages,
                payload=payload,
                project_id=project_id,
                source_refs=source_refs,
            )
        except Exception as e:
            logger.warning(f"Wiki memory raw append failed at {stage}: {e}")

    async def _finalize_md_memory(
        self,
        task_id: str,
        status: str,
        messages: Optional[List[BaseMessage]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            await self.md_memory_manager.finalize_task(
                task_id=task_id,
                status=status,
                messages=messages or [],
                payload=payload or {},
            )
        except Exception as e:
            logger.warning(f"Markdown memory finalize failed for {task_id}: {e}")

    async def _md_memory_prompt_context(self, task_id: str, query: str) -> Dict[str, str]:
        try:
            return await self.md_memory_manager.build_prompt_context_async(task_id, query)
        except Exception as e:
            logger.warning(f"Markdown memory retrieval failed for {task_id}: {e}")
            return {"long_term_memory": "", "task_memory": "", "related_memory": ""}

    def _md_resume_context(self, task_id: str) -> str:
        try:
            task_memory = self.md_memory_manager.read_task_memory(task_id)
        except Exception as e:
            logger.warning(f"Markdown memory resume read failed for {task_id}: {e}")
            return ""
        if not task_memory.strip():
            return ""
        return (
            "**Markdown Task Memory (preferred resume context):**\n"
            f"{task_memory}\n\n"
            "Use this task memory first, then use execution records below as supporting evidence.\n"
        )

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

    def _archive_prompt_context(self, task_id: str, query: str) -> str:
        if not query.strip():
            return ""
        try:
            hits = self.session_archive.search(query, task_id=task_id, limit=4)
            return self.session_archive.format_hits(hits, title="Retrieved Session Archive")
        except Exception as e:
            logger.warning(f"Session archive retrieval failed for {task_id}: {e}")
            return ""

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

    def _ephemeral_prompt_context(
        self,
        state: AgentState,
        messages: List[BaseMessage],
        current_plan: str,
        loaded_skills: str,
        md_memory_context: Dict[str, str],
        wiki_context: str,
        archive_context: str,
        supervisor_context: str,
    ) -> str:
        rag_history_list = state.get("rag_history", []) or []
        inherited_rag = "\n".join(rag_history_list)
        return PromptManager.build_ephemeral_context(
            rag_context=inherited_rag,
            history=messages,
            loaded_skills_instructions=loaded_skills,
            long_term_memory=md_memory_context.get("long_term_memory", ""),
            task_memory=md_memory_context.get("task_memory", ""),
            related_memory=md_memory_context.get("related_memory", ""),
            wiki_context=wiki_context,
            archive_context=archive_context,
            supervisor_context=supervisor_context,
            current_plan=current_plan,
        )

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

    def _skill_execution_context(self, skill_ids: List[str]) -> str:
        if not self.skill_manager or not skill_ids:
            return ""
        cards = self.skill_manager.get_execution_cards(skill_ids)
        return f"\n[MANDATORY SKILL EXECUTION CARDS]\n{cards}\n" if cards else ""

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
            return None
        payload = install_re.group(1) or install_re.group(2) or ""
        raw_tokens = [t for t in re.split(r"\s+", payload.strip()) if t]
        tokens = []
        skip_next = False
        for token in raw_tokens:
            if skip_next:
                skip_next = False
                continue
            if token in {"-n", "--name", "-p", "--prefix"}:
                skip_next = True
                continue
            if token.startswith("-"):
                continue
            tokens.append(token)
        package = next((t for t in tokens if not re.match(r"^(?:python=|env=)", t, re.I)), "python-package")
        package = re.split(r"[<>=!~]", package)[0]

        defaults = self._skill_runtime_defaults(selected_skills)
        env = defaults.get("env", "")
        if not env:
            env = f"bio_{package.lower().replace('-', '_')}"

        tool_name = defaults.get("tool_name") if defaults.get("selected") else package
        return f'<install_tool env="{env}">{tool_name}</install_tool>'

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

    def _ensure_skill_runtime_ready(self, selected_skills: Optional[List[str]]) -> tuple:
        if not self.skill_manager or not self.pm:
            return True, ""
        for sid in self._selected_skill_ids(selected_skills):
            requirements = self._skill_runtime_requirements(sid)
            env = requirements.get("env") or requirements.get("default_env", "")
            if not env:
                continue
            if not any([requirements.get("packages"), requirements.get("capabilities"), requirements.get("language"), requirements.get("healthcheck")]):
                continue
            ok, detail = self.pm.verify_environment(
                env_name=env,
                language=requirements.get("language", ""),
                healthcheck=requirements.get("healthcheck", ""),
            )
            status = "verified" if ok else "broken"
            self.skill_manager.record_environment(
                sid,
                env=env,
                packages=requirements.get("packages", []),
                install_command=requirements.get("install_command", ""),
                status=status,
                notes=detail or ("Environment healthcheck passed." if ok else "Environment healthcheck failed."),
                language=requirements.get("language", ""),
                capabilities=requirements.get("capabilities", []),
                healthcheck=requirements.get("healthcheck", ""),
            )
            if not ok:
                redirect_name = requirements.get("tool_name") or self._skill_runtime_defaults(selected_skills).get("tool_name", "skill_runtime")
                reason = detail or f"Healthcheck failed for environment {env}"
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

    # ────────────────────────────────────────────────────────────
    #  图构建
    # ────────────────────────────────────────────────────────────

    def _build_graph(self):
        workflow = StateGraph(AgentState)

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
            load_actions = [
                str(m.content).strip()
                for m in messages
                if isinstance(m, AIMessage) and str(m.content).strip().startswith("LOAD_")
            ]
            validation_errors = [
                str(m.content)
                for m in messages
                if isinstance(m, SystemMessage) and "<consultant_validation_error>" in str(m.content)
            ]

            # 内存压缩检查
            messages, compressed = await self.memory_manager.maybe_compress(messages, state.get("task_id", "default"))
            if compressed:
                output.print_once("mem_compress", "🗜️ Memory compressed and archived.", Fore.YELLOW)

            if self.skill_manager and (len(load_actions) >= 6 or len(validation_errors) >= 3):
                fallback_raw = []
                for action in load_actions:
                    parts = action.split(maxsplit=2)
                    if len(parts) >= 2:
                        fallback_raw.append(parts[1])
                context_l = consultant_context.lower()
                if any(term in context_l for term in ["导入", "ingest", "10x", "mtx", "h5"]):
                    fallback_raw.append("scrna/ingest/data-download")
                if any(term in context_l for term in ["qc", "质控", "过滤", "filter"]):
                    fallback_raw.append("scrna/qc/filter")
                if any(term in context_l for term in ["preprocess", "normalize", "normalization", "标准化", "预处理", "variable feature", "高变基因", "sctransform"]):
                    fallback_raw.append("scrna/preprocess/basic")
                if any(term in context_l for term in ["cluster", "clustering", "聚类", "pca", "umap", "tsne", "邻近图", "降维"]):
                    fallback_raw.append("scrna/cluster/basic")
                if any(term in context_l for term in ["marker", "markers", "markergene", "差异marker", "marker基因", "标记基因"]):
                    fallback_raw.append("scrna/markers/find")
                if any(term in context_l for term in ["annotate", "annotation", "cell type", "celltype", "注释", "细胞类型", "singler"]):
                    fallback_raw.append("scrna/annotate/basic")
                if any(term in context_l for term in ["differential", "difference expression", "deg", "de ", "差异表达", "contrast", "ctrl", "stim"]):
                    fallback_raw.append("scrna/de/contrast")
                if not fallback_raw:
                    fallback_raw = self.skill_manager.match_skills(consultant_context, max_results=consultant_max_skills)
                selected = self.skill_manager.normalize_selected_skills(fallback_raw, max_results=consultant_max_skills)
                if selected:
                    summary = (
                        "[SELECTED_SKILLS: " + ", ".join(selected) + "]\n"
                        "[MISSING_INPUTS]\n"
                        "- none detected before consultant loop guard triggered\n"
                        "[/MISSING_INPUTS]\n"
                        "[QUESTIONS_FOR_USER]\n"
                        "- none\n"
                        "[/QUESTIONS_FOR_USER]\n"
                        "[REQUIREMENTS_SUMMARY]\n"
                        "- Consultant loop guard triggered after repeated internal skill-loading or validation retries.\n"
                        "- Proceed with the selected local skills inferred from the verified request context.\n"
                        "- Planner and Generate must re-check exact manifest paths and use loaded skill docs before execution.\n"
                        "[/REQUIREMENTS_SUMMARY]"
                    )
                    output.print_once(
                        "consultant_loop_guard",
                        "⚠️ Consultant repeated internal actions; using the current skill context and handing to Planner.",
                        Fore.YELLOW,
                    )
                    return {
                        "messages": [AIMessage(content="FINISH\n" + summary)],
                        "selected_skills": selected,
                        "selected_skills_raw": list(fallback_raw),
                        "skill_selection_source": "consultant_loop_guard",
                        "requirements_summary": summary,
                        "next_step": "planner",
                    }

            kb_list_desc = ", ".join(KB_MAP.keys())
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
            )

            response = await self.llm.ainvoke([SystemMessage(content=consultant_prompt)] + messages)
            content = response.content.strip()
            task_id = state.get("task_id", "default")
            await self._observe_md_memory(
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

                user_response = await asyncio.to_thread(
                    self.confirmation_manager.request_confirmation,
                    code=clean_code, language="Bash",
                )
                if user_response == 'y':
                    shim_path = self.pm.get_shim_dir_str()
                    stdout, stderr, ret = await run_bash_script(f'export PATH="{shim_path}:$PATH"\n{clean_code}')
                    obs = f"<observation>\n{'✅' if ret==0 else '❌'}\n{stdout[:1000]}\n{stderr[:500]}\n</observation>"
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
                print(f"{Fore.GREEN}🗣️  {question}{Style.RESET_ALL}")
                user_answer = interrupt(question)
                return {
                    "messages": [AIMessage(content=content),
                                 HumanMessage(content=f"Answer to '{question}': {user_answer}")],
                    "next_step": "consultant",
                }

            # FINISH 动作
            if content.startswith("FINISH"):
                summary = content[len("FINISH"):].strip()
                if self.skill_manager:
                    if "[SELECTED_SKILLS:" not in summary:
                        obs = (
                            "<consultant_validation_error>\n"
                            "FINISH must include a [SELECTED_SKILLS: ...] marker, even if the value is None.\n"
                            "</consultant_validation_error>"
                        )
                        output.print_once(
                            f"consultant_validation:{len(validation_errors)}",
                            "⚠️ Consultant FINISH was missing [SELECTED_SKILLS]; asking it to repair the handoff.",
                            Fore.YELLOW,
                        )
                        return {"messages": [AIMessage(content=content), SystemMessage(content=obs)], "next_step": "consultant"}

                    raw_selected = self.skill_manager.parse_selected_skill_ids(summary)
                    selected = self.skill_manager.normalize_selected_skills(raw_selected, max_results=consultant_max_skills)
                    issues = self.skill_manager.validate_selected_skills(raw_selected, max_results=consultant_max_skills) if raw_selected else []
                    if issues:
                        repair_raw = []
                        context_l = (consultant_context + "\n" + summary).lower()
                        if any(term in context_l for term in ["导入", "ingest", "10x", "mtx", "h5"]):
                            repair_raw.append("scrna/ingest/data-download")
                        if any(term in context_l for term in ["qc", "质控", "过滤", "filter"]):
                            repair_raw.append("scrna/qc/filter")
                        if any(term in context_l for term in ["preprocess", "normalize", "normalization", "标准化", "预处理", "variable feature", "高变基因", "sctransform"]):
                            repair_raw.append("scrna/preprocess/basic")
                        if any(term in context_l for term in ["cluster", "clustering", "聚类", "pca", "umap", "tsne", "邻近图", "降维"]):
                            repair_raw.append("scrna/cluster/basic")
                        if any(term in context_l for term in ["marker", "markers", "markergene", "差异marker", "marker基因", "标记基因"]):
                            repair_raw.append("scrna/markers/find")
                        if any(term in context_l for term in ["annotate", "annotation", "cell type", "celltype", "注释", "细胞类型", "singler"]):
                            repair_raw.append("scrna/annotate/basic")
                        if any(term in context_l for term in ["differential", "difference expression", "deg", "de ", "差异表达", "contrast", "ctrl", "stim"]):
                            repair_raw.append("scrna/de/contrast")
                        if not repair_raw:
                            repair_raw = self.skill_manager.match_skills(
                                consultant_context + "\n" + summary,
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
                                "next_step": "planner",
                            }
                        return {"messages": [AIMessage(content=content), SystemMessage(content=obs)], "next_step": "consultant"}
                else:
                    raw_selected = []
                    selected = []
                output.print_once("consultant_finish", "✓ Consultant complete. Handing to Planner.", Fore.GREEN)
                return {
                    "messages": [AIMessage(content=content)],
                    "selected_skills": selected,
                    "selected_skills_raw": list(raw_selected),
                    "skill_selection_source": "consultant_finish",
                    "requirements_summary": summary,
                    "next_step": "planner",
                }
            output.print_ai(content)

            return {"messages": [response], "next_step": "human_input"}

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
            selected_skills = state.get("selected_skills", []) or []
            selected_skills_raw = state.get("selected_skills_raw", []) or list(selected_skills)
            skill_selection_source = state.get("skill_selection_source", "consultant_finish")
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

            # 构建详细规划 Prompt（包含脚本预生成指令）
            detailed_plan_prompt = f"""You are the Planner for BioAnalyst V4.

User Goal: {user_msg}
{f"Consultant Summary: {summary}" if summary else ""}

[ENVIRONMENT]
Installed Tools:
{installed}

[KNOWLEDGE BASE CONTEXT]
{rag_knowledge[:2000] if rag_knowledge else "None"}

[AVAILABLE SKILLS]
{skills_summary}

[SELECTED SKILLS]
{selected_skills_text}

[ACTIVE SKILL DOCS]
{active_skill_docs if active_skill_docs else "No skill selected. If you choose a skill, use only IDs from AVAILABLE SKILLS."}
{skill_reference_index if skill_reference_index else ""}
{skill_execution_context}

==========================================================
TASK: Generate a DETAILED PLAN with 3 sections:
==========================================================

You are only planning. Do NOT generate bash scripts, Python scripts, or runnable code blocks.
Do NOT invent commands for skills. If a matching skill is present in ACTIVE SKILL DOCS,
plan to use its documented script path and documented arguments exactly.

## Section 1: Requirement Analysis
- Input files, reference genomes, parameters needed
- Tools required (check installed list above)
- Estimated runtime and resource needs (RAM, CPU, disk)

## Section 2: Step-by-Step Execution Plan
For EACH step, provide:
  Step N: [Action Name]
    - Tool: [tool name + version if known]
    - Execution guidance: [plain-language instruction; include an exact command only when it is copied from ACTIVE SKILL DOCS and filled with user-provided paths]
    - Input: [expected input files/data]
    - Output: [expected output files/format]
    - Success criteria: [how to verify this step worked]
    - Risk: [LOW/MEDIUM/HIGH – and why]

## Section 3: Validation Checklist
- What final outputs to verify?
- QC metrics to check (mapping rate, coverage, etc.)?
- Common failure modes to watch for?

RULES:
- If a tool is in Installed Tools, use it directly. DO NOT add install steps.
- If using any skill, append: [SELECTED_SKILLS: skill_id_1, skill_id_2]
- You may only select skill IDs listed in AVAILABLE SKILLS. Do not translate, rename, or invent skill IDs.
- SELECTED SKILLS are already finalized. Do not add extra candidate skills beyond that set.
- If MANDATORY SKILL EXECUTION CARDS are present, they override your prior knowledge and all plan wording.
- If ACTIVE SKILL DOCS are present, you MUST read them and use their real script path(s). Never describe the skill as an abstract module or pre-built global command.
- Never write "Command template" for a selected skill. Use "Execution guidance".
- Never use placeholder wrapper commands or undocumented script names.
- **PATH STRICTNESS**: You MUST use the EXACT absolute paths provided in the Consultant Summary. 
- **NO GUESSING**: Do NOT guess file names or naming patterns (e.g., do not assume '.CG.bw' if the summary says '_CG.Chr10.bw'). 
- If a required path is missing from the summary, your plan MUST include a step to find it first.
- Skill name is not a runnable shell command unless the skill body explicitly defines such a command.
- Do not include a "Script Skeleton" section.
"""

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
                response = await self.llm.ainvoke([HumanMessage(content=prompt_to_send)])
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

            # 保存计划文件
            task_id  = state.get("task_id", "default")
            plan_dir = os.path.join(self.data_path, "task_outputs", task_id)
            os.makedirs(plan_dir, exist_ok=True)
            plan_file = os.path.join(plan_dir, "plan.md")
            with open(plan_file, "w", encoding="utf-8") as f:
                f.write(
                    "# Task Plan\n"
                    f"Generated: {datetime.now().isoformat()}\n"
                    f"Selected Skills: {', '.join(selected_skills) if selected_skills else 'None'}\n"
                    f"Selected Skills Raw: {', '.join(selected_skills_raw) if selected_skills_raw else 'None'}\n"
                    f"Skill Selection Source: {skill_selection_source}\n\n"
                    f"{plan_content}"
                )

            # 写入思维链日志
            log_plan_to_cot(self.supervisor_node, task_id, plan_content)
            await self._observe_md_memory(
                task_id,
                "planner",
                messages + [AIMessage(content=plan_content)],
                {
                    "plan_file": plan_file,
                    "selected_skills": selected_skills,
                    "skill_selection_source": skill_selection_source,
                    "plan": plan_content[:4000],
                },
            )

            return {
                "current_plan": plan_content,
                "selected_skills": selected_skills,
                "selected_skills_raw": selected_skills_raw,
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
            print(f"\n{Fore.CYAN}{'='*60}")
            print(f"  📋  PLAN REVIEW  |  Task: {task_id}")
            print(f"{'='*60}{Style.RESET_ALL}")
            output.page_text(f"Plan Review | Task: {task_id}", plan, Fore.CYAN)
            print(f"{'─'*60}")

            try:
                user_input = input(
                    f"\n{Fore.YELLOW}Approve? [y/n/q] or type feedback > {Style.RESET_ALL}"
                ).strip()
            except EOFError:
                user_input = "n"

            il = user_input.lower()

            if il in ["y", "yes", ""]:
                print(f"{Fore.GREEN}✓ Plan approved. Executing...{Style.RESET_ALL}")
                await self._observe_md_memory(
                    task_id,
                    "plan_review",
                    messages + [HumanMessage(content="Plan approved. Execute Step 1.")],
                    {"decision": "approved", "plan_excerpt": plan[:2000]},
                )
                return {
                    "messages": [HumanMessage(content="Plan approved. Execute Step 1.")],
                    "next_step": "generate",
                }
            elif il in ["q", "quit", "exit"]:
                print(f"{Fore.MAGENTA}🛑 Task cancelled.{Style.RESET_ALL}")
                await self._finalize_md_memory(
                    task_id,
                    "cancelled",
                    messages + [HumanMessage(content="Cancelled.")],
                    {"decision": "cancelled_at_plan_review"},
                )
                return {"messages": [HumanMessage(content="Cancelled.")], "next_step": "finish"}
            else:
                feedback = user_input if il not in ["n", "no"] else ""
                if not feedback:
                    feedback = input(f"{Fore.YELLOW}Feedback (Enter=auto-retry): {Style.RESET_ALL}").strip()
                    if not feedback:
                        feedback = "Rejected. Try a different approach."
                print(f"{Fore.CYAN}📡 Feedback captured: {feedback}{Style.RESET_ALL}")
                await self._observe_md_memory(
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
                    "skill_selection_source": "",
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

            # 熔断（连续 5 次 AI 消息含错误）
            recent_errors = sum(
                1 for m in messages[-8:]
                if isinstance(m, AIMessage) and
                any(k in str(m.content) for k in ["❌", "Failed", "Error"])
            )
            if recent_errors >= 5:
                return {
                    "messages": [AIMessage(content="<solution>❌ System Aborted: Too many errors.</solution>")],
                    "next_step": "finish",
                }

            # 技能加载
            loaded_skills = ""
            if self.skill_manager and current_plan:
                selected = state.get("selected_skills", [])
                if selected:
                    loaded = self.skill_manager.load_skills_for_prompt(selected)
                    if loaded:
                        loaded_skills = loaded.replace("<active_skill_docs>", "<active_skills>").replace("</active_skill_docs>", "</active_skills>")
                        ref_index = self.skill_manager.get_skill_reference_index_prompt(selected)
                        if ref_index:
                            loaded_skills += "\n" + ref_index
                        loaded_skills += "\n" + self._skill_execution_context(selected)

            # 检索 Markdown 记忆。Legacy MemoryManager remains token compression only.
            last_msg_content = str(messages[-1].content) if messages else ""
            md_query = "\n".join(
                part for part in [last_msg_content[:600], str(current_plan)[:800]]
                if part
            )
            md_memory_context = await self._md_memory_prompt_context(task_id, md_query)
            archive_context = self._archive_prompt_context(task_id, md_query)
            session_id = state.get("session_id") or task_id
            project_id = state.get("project_id") or self.default_project_id
            wiki_context = self._wiki_prompt_context(task_id, project_id, md_query)

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
                md_memory_context=md_memory_context,
                wiki_context=wiki_context,
                archive_context=archive_context,
                supervisor_context=supervisor_instr,
            )
            call_msgs = [SystemMessage(content=stable_sys + "\n\n" + ephemeral_context)] + messages
            response  = await self.llm.ainvoke(call_msgs)
            content   = response.content

            # 打印（去重）
            output.print_ai(content)

            # 解析
            search_match  = re.search(r"<search>(.*?)</search>", content, re.DOTALL)
            install_match = re.search(r"<install_tool.*?>.*?</install_tool>", content, re.DOTALL)
            execute_match = re.search(r"<execute>(.*?)</execute>", content, re.DOTALL)
            answer_match  = re.search(r"<solution>(.*?)</solution>", content, re.DOTALL)

            generated_action = (
                "finish" if answer_match else
                "install" if install_match else
                "execute" if execute_match else
                "search" if search_match else
                "continue"
            )
            await self._observe_md_memory(
                task_id,
                "generate",
                messages + [response],
                {
                    "action": generated_action,
                    "content": content[:3000],
                    "current_plan_excerpt": str(current_plan)[:1200],
                },
            )
            if answer_match:
                await self._finalize_md_memory(
                    task_id,
                    "completed",
                    messages + [response],
                    {"solution": answer_match.group(1).strip()[:3000]},
                )

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
                tool_msg = HumanMessage(content=f"<rag_result>\n{query}\n{rag_result}\n</rag_result>")
                return {"messages": [response, tool_msg], "next_step": "generate"}

            next_step = "generate"
            if answer_match:    next_step = "finish"
            elif install_match: next_step = "install_subagent"
            elif execute_match:
                next_step = "execute" if execute_match.group(1).strip() else "generate"

            return {"messages": [response], "next_step": next_step, "retry_count": 0}

        # ════════════════════════════════════════════
        # Node 3: Execute
        # ════════════════════════════════════════════
        async def execute_logic(state: AgentState) -> AgentState:
            output.print_node_header("execute")
            last_message = state["messages"][-1].content
            task_id      = state.get("task_id", "default")

            match = re.search(r"<execute>(.*?)</execute>", last_message, re.DOTALL)
            if not match:
                return {"messages": [HumanMessage(content="<observation>❌ No code found.</observation>")],
                        "next_step": "generate"}

            original_code = match.group(1).strip()
            language = detect_script_language(original_code)
            code = normalize_script_code(original_code)
            install_redirect = self._redirect_install_command(code, state.get("current_plan", "") or "")
            if install_redirect:
                return {
                    "messages": [AIMessage(content=install_redirect)],
                    "next_step": "install_subagent",
                }

            # 中间件审查
            action, final_code = self.middleware.review_code(code, language)
            if action == "reject":
                return {"messages": [HumanMessage(content="<observation>❌ Rejected by user.</observation>")],
                        "next_step": "generate"}
            if action == "quit":
                await self._finalize_md_memory(
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
                    return {"messages": [HumanMessage(content=feedback_msg)], "next_step": "generate"}
                else:   
                    # 这是修改后的代码，可以执行
                    code_to_run = normalize_script_code(final_code)
                    language = detect_script_language(code_to_run)
            else:
                code_to_run = normalize_script_code(final_code)  # approve 的情况  

            selected_skills_state = state.get("selected_skills", [])
            install_redirect = self._redirect_install_command(code_to_run, selected_skills_state)
            if install_redirect:
                return {
                    "messages": [AIMessage(content=install_redirect)],
                    "next_step": "install_subagent",
                }

            runtime_ready, runtime_redirect = self._ensure_skill_runtime_ready(selected_skills_state)
            if not runtime_ready and runtime_redirect:
                return {
                    "messages": [AIMessage(content=runtime_redirect)],
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

            # 持久化
            if self.state_manager:
                self.state_manager.save_execution(task_id, step_index, code_to_run, language,
                                                  stdout, stderr, returncode, duration)
                if returncode == 0:
                    self._auto_save_results(task_id, step_index, code_to_run, stdout)
                    self._record_successful_skill_execution(
                        selected_skills_state,
                        code_to_run,
                        stdout,
                    )

            if returncode == 0:
                obs = (f"<observation>\n[{language}] ✅ Success ({duration:.1f}s)\n"
                       f"STDOUT:\n{stdout[:2000]}\n</observation>")
            else:
                obs = (f"<observation>\n[{language}] ❌ Failed ({duration:.1f}s)\n"
                       f"STDOUT:\n{stdout[:2000]}\n"
                       f"STDERR:\n{stderr[:1000]}\n</observation>")

            output.print_observation(obs)
            await self._observe_md_memory(
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
                },
            )
            return {"messages": [HumanMessage(content=obs)], "next_step": "supervisor"}

        execute_node = error_recovery_middleware(self.llm, self.retriever)(execute_logic)

        # ════════════════════════════════════════════
        # Node 4: Supervisor（新节点）
        # ════════════════════════════════════════════
        async def supervisor(state: AgentState):
            output.print_node_header("supervisor")
            update = await self.supervisor_node.run(state)
            task_id = state.get("task_id", "default")
            update_messages = update.get("messages", []) if isinstance(update, dict) else []
            await self._observe_md_memory(
                task_id,
                "supervisor",
                state.get("messages", []) + update_messages,
                {
                    "next_step": update.get("next_step") if isinstance(update, dict) else "",
                    "retry_count": update.get("retry_count") if isinstance(update, dict) else "",
                    "supervisor_verdict": update.get("supervisor_verdict") if isinstance(update, dict) else "",
                },
            )
            if isinstance(update, dict) and update.get("next_step") == "finish":
                await self._finalize_md_memory(
                    task_id,
                    "failed",
                    state.get("messages", []) + update_messages,
                    {"reason": "Supervisor ended the task."},
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
            selected_for_install = state.get("selected_skills", [])
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
            choice_key = self._install_choice_key(task_id, tool_name)
            selected_skills = runtime_defaults.get("selected", []) or []
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
            reuse_match = None
            if self.pm and any([
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
                        self._record_installed_skill_environment(selected_for_install, reuse_match["env"], final_res)
                        await self._observe_md_memory(
                            task_id,
                            "install_subagent",
                            state.get("messages", []) + [AIMessage(content=f"<observation>{final_res}</observation>")],
                            {
                                "tool_name": tool_name,
                                "target_env": reuse_match["env"],
                                "result": final_res,
                            },
                        )
                        return {
                            "messages": [AIMessage(content=f"<observation>{final_res}</observation>")],
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

            packages = runtime_defaults.get("packages", [])
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

            self._record_installed_skill_environment(selected_for_install, target_env, final_res)
            self._install_env_choices.pop(choice_key, None)
            await self._observe_md_memory(
                task_id,
                "install_subagent",
                state.get("messages", []) + [AIMessage(content=f"<observation>{final_res}</observation>")],
                {
                    "tool_name": tool_name,
                    "target_env": target_env,
                    "result": final_res[:3000],
                },
            )

            return {"messages": [AIMessage(content=f"<observation>{final_res}</observation>")],
                    "next_step": "generate"}

        # ════════════════════════════════════════════
        # 节点注册 & 连线
        # ════════════════════════════════════════════
        workflow.add_node("consultant",      consultant)
        workflow.add_node("planner",         planner)
        workflow.add_node("plan_review",     plan_review)
        workflow.add_node("generate",        generate)
        workflow.add_node("execute",         execute_node)
        workflow.add_node("supervisor",      supervisor)          # ← 新节点
        workflow.add_node("install_subagent", install_subagent)

        workflow.add_edge(START, "consultant")

        def consultant_router(state):
            step = state.get("next_step")
            if step == "planner":     return "planner"
            if step == "human_input": return END
            return "consultant"

        workflow.add_conditional_edges("consultant", consultant_router,
                                       ["planner", "consultant", END])

        workflow.add_edge("planner",     "plan_review")
        workflow.add_edge("install_subagent", "generate")

        def plan_review_router(state):
            step = state.get("next_step")
            if step == "planner": return "planner"
            if step == "finish":  return END
            return "generate"

        workflow.add_conditional_edges("plan_review", plan_review_router,
                                       ["generate", "planner", END])

        def generate_router(state):
            step = state.get("next_step")
            if step == "finish":         return END
            if step == "execute":        return "execute"
            if step == "install_subagent": return "install_subagent"
            return "generate"

        workflow.add_conditional_edges("generate", generate_router,
                                       ["execute", "install_subagent", "generate", END])

        # execute → supervisor → generate（核心新链路）
        workflow.add_edge("execute", "supervisor")

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

    async def go(
        self,
        input_data: Any,
        task_id: Optional[str] = None,
        resume: bool = False,
    ):
        if not task_id:
            task_id = f"task_{int(time.time())}"
        config = {"configurable": {"thread_id": task_id}}

        output.reset_for_task()

        if isinstance(input_data, Command):
            runner_input = input_data
            print(f"\n🔄 Resuming: {task_id}")
        else:
            prompt = input_data
            project_id = self.default_project_id
            stable_record = self._get_or_create_stable_prompt(task_id, project_id)
            md_resume = self._md_resume_context(task_id) if resume else ""
            if not resume or not self.md_memory_manager.task_exists(task_id):
                self.md_memory_manager.initialize_task(task_id, str(prompt), status="running")
            if self.state_manager:
                if resume:
                    prompt = md_resume + self.state_manager.resume_task(task_id) + "\n" + prompt
                else:
                    self.state_manager.create_task(task_id, prompt)
            elif resume:
                prompt = self._md_resume_context(task_id) + "\n" + prompt
            runner_input = {
                "messages": [HumanMessage(content=prompt)],
                "task_id": task_id,
                "session_id": task_id,
                "project_id": project_id,
                "stable_system_prompt": stable_record["prompt"],
                "core_memory_loaded_at": stable_record["core_memory_loaded_at"],
            }
            print(f"\n🚀 Task: {task_id}")

        # 内存状态展示
        self.memory_manager.print_status()

        async for event in self.app.astream(runner_input, config=config):
            pass   # 所有打印由 OutputRouter 在节点内处理，此处不重复

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
  /memory search <q>  Search Markdown memory
  /wiki               Show wiki memory status
  /wiki search <q>    Search LLM wiki memory
  /wiki ingest        Curate pending raw events into wiki pages
  /wiki lint          Check wiki links and source metadata
  /wiki page <id>     Show a wiki page by path or title
  /wiki review        Show pending wiki review items
  /dream              Batch-curate pending memory events
  /dream-log          Show Dream memory log
  /dream-restore      Restore latest pre-Dream memory snapshot
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
                user_input = await asyncio.to_thread(
                    input, f"\n{Fore.CYAN}[{current_session_id[:20]}]{Fore.WHITE} {cwd_name}> {Style.RESET_ALL}"
                )
                user_input = user_input.strip()
                if not user_input:
                    continue

                # ── 系统命令 ─────────────────────────────────
                if user_input == "/exit":
                    break

                if user_input.startswith("/memory search "):
                    query = user_input.split(" ", 2)[2].strip()
                    result = await self.md_memory_manager.asearch(query, task_id=current_session_id)
                    output.page_text(f"Markdown Memory Search | {query}", result, Fore.CYAN)
                    continue

                if user_input == "/memory":
                    config = {"configurable": {"thread_id": current_session_id}}
                    snap   = self.app.get_state(config)
                    msgs   = snap.values.get("messages", []) if snap.values else []
                    self.memory_manager.print_status(msgs)
                    print(self.core_memory_store.status_text(self.default_project_id))
                    print(self.md_memory_manager.status_text(current_session_id))
                    output.page_text(
                        "Long-term Markdown Memory",
                        self.md_memory_manager.read_long_term(),
                        Fore.CYAN,
                    )
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

                if user_input == "/wiki ingest":
                    result = await self.wiki_memory_manager.ingest_pending(project_id=self.default_project_id)
                    output.page_text("Wiki Ingest", result, Fore.CYAN)
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

                if user_input == "/dream":
                    result = await self.md_memory_manager.curate_pending(task_id=current_session_id)
                    output.page_text("Dream Memory Curation", result, Fore.CYAN)
                    continue

                if user_input == "/dream-log":
                    output.page_text(
                        "Dream Log",
                        self.md_memory_manager.read_dream_log(),
                        Fore.CYAN,
                    )
                    continue

                if user_input == "/dream-restore":
                    result = self.md_memory_manager.restore_latest_snapshot()
                    output.page_text("Dream Restore", result, Fore.CYAN)
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
                    await self._finalize_md_memory(
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
                    await self._finalize_md_memory(
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
                while True:
                    await self.go(next_input, task_id=current_session_id)
                    config   = {"configurable": {"thread_id": current_session_id}}
                    snapshot = self.app.get_state(config)

                    if not (snapshot.tasks and snapshot.tasks[0].interrupts):
                        break

                    payload = snapshot.tasks[0].interrupts[0].value
                    user_response = ""

                    # 代码审查
                    if isinstance(payload, dict) and payload.get("type") == "code_review":
                        risk = payload.get("risk", {}).get("level", "MEDIUM")
                        print(f"\n{Fore.YELLOW}⚠️  CODE REVIEW  |  Risk: {risk}{Style.RESET_ALL}")
                        output.page_text("Code Review", payload.get('content', ''), Fore.CYAN)
                        user_response = await asyncio.to_thread(
                            input, f"{Fore.YELLOW}[y/n/q] > {Style.RESET_ALL}"
                        )

                    # 环境选择
                    elif isinstance(payload, dict) and payload.get("type") == "env_selection":
                        tool = payload.get("tool_name")
                        dflt = payload.get("default_env")
                        reason = payload.get("reason", "")
                        print(f"\n{Fore.CYAN}🔧 Install '{tool}' → env [{dflt}]?{Style.RESET_ALL}")
                        if reason:
                            print(f"{Fore.CYAN}{reason}{Style.RESET_ALL}")
                        user_response = await asyncio.to_thread(
                            input, f"Target env (Enter={dflt}) > "
                        )
                        if not user_response.strip():
                            user_response = dflt

                    # 通用中断
                    else:
                        user_response = await asyncio.to_thread(
                            input, f"{Fore.YELLOW}Response (q=quit) > {Style.RESET_ALL}"
                        )

                    if user_response.strip().lower() in ["q", "quit", "exit"]:
                        break
                    next_input = Command(resume=user_response)

            except KeyboardInterrupt:
                print(f"\n{Fore.YELLOW}⚡ Interrupted{Style.RESET_ALL}")
                if self.state_manager:
                    self.state_manager.update_task_status(current_session_id, "interrupted")
            except Exception:
                import traceback
                traceback.print_exc()


# ════════════════════════════════════════════════════════════════
#  启动入口
# ════════════════════════════════════════════════════════════════

def restore_terminal():
    try:
        if os.name == "posix":
            os.system("stty echo")
    except Exception:
        pass
    print("\n👋 Terminal restored.")


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
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bye")
    except Exception:
        import traceback
        traceback.print_exc()
