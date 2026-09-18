"""
memory_manager.py  ―  Token-aware Long-term Memory for BioAnalyst Agent
==========================================================================
功能：
  1. 实时统计对话 token 数，超过阈值时自动压缩
  2. 将原始消息序列化后存入磁盘 (.jsonl 档案文件)
  3. 用关键词或向量相似度检索历史片段，按需注入上下文
  4. 提供 MemoryStatus 供 UI 层展示当前内存压力

依赖：
  pip install tiktoken colorama
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from colorama import Fore, Style

logger = logging.getLogger("BioMind.Memory")

# ─────────────────────────────────────────────────────────────
# 1.  Token 计数工具  (tiktoken 不可用时退回字符估算)
# ─────────────────────────────────────────────────────────────

try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")          # GPT-4 / Qwen 通用

    def count_tokens(text: str) -> int:
        return len(_enc.encode(text, disallowed_special=()))

except Exception as exc:
    logger.warning("tiktoken unavailable (%s) - using char/4 estimation for token count.", exc)

    def count_tokens(text: str) -> int:          # type: ignore[misc]
        return max(1, len(text) // 4)


def _msg_tokens(msg) -> int:
    """适配 LangChain BaseMessage / dict 两种格式"""
    if hasattr(msg, "content"):
        return count_tokens(str(msg.content))
    if isinstance(msg, dict):
        return count_tokens(str(msg.get("content", "")))
    return count_tokens(str(msg))


def _safe_print(text: str):
    logging.getLogger("BioMind.Memory").info(text)



# ─────────────────────────────────────────────────────────────
# 2.  数据类
# ─────────────────────────────────────────────────────────────

@dataclass
class MemoryStatus:
    used_tokens: int
    max_tokens: int
    compression_count: int
    archive_files: List[str]

    @property
    def usage_pct(self) -> float:
        return self.used_tokens / self.max_tokens * 100

    def __str__(self) -> str:
        bar_len = 30
        filled = int(bar_len * self.usage_pct / 100)
        bar = "█" * filled + "░" * (bar_len - filled)
        color = (Fore.GREEN if self.usage_pct < 60
                 else Fore.YELLOW if self.usage_pct < 85
                 else Fore.RED)
        return (f"{color}[{bar}] {self.usage_pct:.1f}%{Style.RESET_ALL} "
                f"({self.used_tokens:,}/{self.max_tokens:,} tokens | "
                f"compressed {self.compression_count}x | "
                f"{len(self.archive_files)} archive(s))")


@dataclass
class MemoryChunk:
    """归档后的一段压缩摘要"""
    chunk_id: str
    task_id: str
    created_at: str
    token_count_original: int
    summary: str
    keywords: List[str] = field(default_factory=list)
    archive_file: str = ""


# ─────────────────────────────────────────────────────────────
# 3.  MemoryManager 主类
# ─────────────────────────────────────────────────────────────

class MemoryManager:
    """
    用法示例:

        mm = MemoryManager(llm=llm, archive_dir="./memory_archives", max_tokens=200_000)

        # 每轮对话后调用：
        messages, was_compressed = await mm.maybe_compress(messages, task_id="task_001")

        # 需要检索历史时：
        snippets = mm.search_archives("samtools installation error", top_k=3)

        # UI 展示：
        print(mm.status())
    """

    def __init__(
        self,
        llm,
        archive_dir: str = "./memory_archives",
        max_tokens: int = 200_000,
        compress_threshold: float = 0.85,     # 85% 时触发压缩
        keep_recent_tokens: int = 40_000,     # 压缩后保留最近 40K tokens
        summary_max_words: Optional[int] = None,
        audit_compressions: bool = True,
    ):
        self.llm = llm
        self.archive_dir = Path(archive_dir)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.max_tokens = max_tokens
        self.compress_threshold = compress_threshold
        self.keep_recent_tokens = keep_recent_tokens
        self.summary_max_words = self._resolve_summary_max_words(summary_max_words)
        self.audit_compressions = audit_compressions

        self._compression_count: int = 0
        self._chunks: List[MemoryChunk] = []
        self._index_file = self.archive_dir / "memory_index.json"
        self._audit_file = self.archive_dir / "compression_audit.jsonl"
        self._load_index()

    # ── 公开接口 ────────────────────────────────────────────

    async def maybe_compress(
        self, messages: List[Any], task_id: str
    ) -> Tuple[List[Any], bool]:
        """
        检查 token 用量，如超过阈值则压缩并归档早期消息。
        返回 (新消息列表, 是否发生了压缩)
        """
        total = sum(_msg_tokens(m) for m in messages)
        threshold = int(self.max_tokens * self.compress_threshold)

        if total < threshold:
            return messages, False

        _safe_print(f"\n{Fore.YELLOW}🗜️  [Memory] Token usage {total:,}/{self.max_tokens:,} "
                    f"(≥{self.compress_threshold*100:.0f}%). Compressing...{Style.RESET_ALL}")

        compressed_msgs, archived_msgs = self._split_messages(messages)

        if not archived_msgs:
            logger.warning("Nothing to archive – messages list is too short.")
            return messages, False

        # 1. 生成摘要
        summary, prompt = await self._summarize_with_prompt(archived_msgs, task_id)

        # 2. 持久化原始消息
        archive_path = self._write_archive(archived_msgs, task_id)

        # 3. 提取关键词（简单频率法，无需额外库）
        keywords = self._extract_keywords(summary)

        # 4. 注册到内存索引
        chunk = MemoryChunk(
            chunk_id=f"{task_id}_{int(time.time())}",
            task_id=task_id,
            created_at=datetime.now().isoformat(),
            token_count_original=sum(_msg_tokens(m) for m in archived_msgs),
            summary=summary,
            keywords=keywords,
            archive_file=str(archive_path),
        )
        self._chunks.append(chunk)
        self._compression_count += 1
        self._save_index()
        # 5. 将摘要注入保留消息的最前方
        from langchain_core.messages import SystemMessage
        summary_msg = SystemMessage(
            content=(
                f"[Memory Compression #{self._compression_count}]\n"
                f"The following is a summary of the earlier conversation "
                f"(archived to: {archive_path.name}):\n\n{summary}"
            )
        )
        new_messages = [summary_msg] + compressed_msgs

        new_total = sum(_msg_tokens(m) for m in new_messages)
        if self.audit_compressions:
            self._write_compression_audit(
                chunk=chunk,
                prompt=prompt,
                summary=summary,
                total_tokens_before=total,
                total_tokens_after=new_total,
                archived_message_count=len(archived_msgs),
                retained_message_count=len(compressed_msgs),
            )
        _safe_print(f"{Fore.GREEN}✓ Compressed: {total:,} → {new_total:,} tokens "
                    f"({archive_path.name}){Style.RESET_ALL}")

        return new_messages, True

    def search_archives(self, query: str, top_k: int = 3) -> str:
        """
        关键词匹配检索历史摘要，返回拼接后的文本供 Prompt 注入。
        如果日后有 embedding 服务，可替换此方法内部实现。
        """
        if not self._chunks:
            return ""

        query_words = set(query.lower().split())

        scored: List[Tuple[float, MemoryChunk]] = []
        for chunk in self._chunks:
            kw_set = set(chunk.keywords)
            # 关键词重叠度
            overlap = len(query_words & kw_set) / max(len(query_words), 1)
            # 摘要全文命中（字符串包含）
            text_hits = sum(1 for w in query_words if w in chunk.summary.lower())
            score = overlap * 2 + text_hits * 0.5
            if score > 0:
                scored.append((score, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:top_k]

        if not top:
            return ""

        parts = [f"### Historical Context (from archive)"]
        for score, chunk in top:
            parts.append(
                f"\n[{chunk.created_at[:16]}] (relevance={score:.2f})\n{chunk.summary}"
            )
        return "\n".join(parts)

    def load_raw_archive(self, archive_file: str) -> List[Dict]:
        """按需加载原始消息记录（供调试或深度恢复）"""
        path = Path(archive_file)
        if not path.exists():
            path = self.archive_dir / archive_file
        if not path.exists():
            return []
        records = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return records

    def read_compression_audit(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Read compression audit records, newest last by default."""
        if not self._audit_file.exists():
            return []
        records: List[Dict[str, Any]] = []
        for line in self._audit_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                records.append(data)
        if limit is not None and limit >= 0:
            return records[-limit:]
        return records

    def status(self, messages: List[Any] = None) -> MemoryStatus:
        used = sum(_msg_tokens(m) for m in messages) if messages else 0
        return MemoryStatus(
            used_tokens=used,
            max_tokens=self.max_tokens,
            compression_count=self._compression_count,
            archive_files=[c.archive_file for c in self._chunks],
        )

    def print_status(self, messages: List[Any] = None):
        s = self.status(messages)
        _safe_print(f"\n📊 Memory: {s}")

    # ── 内部方法 ────────────────────────────────────────────

    @staticmethod
    def _resolve_summary_max_words(value: Optional[int]) -> int:
        if value is None:
            raw = os.getenv("BIOMIND_SUMMARY_MAX_WORDS", "").strip()
            if raw:
                try:
                    value = int(raw)
                except ValueError:
                    logger.warning("Invalid BIOMIND_SUMMARY_MAX_WORDS=%r; using 900.", raw)
                    value = None
        try:
            resolved = int(value) if value is not None else 900
        except (TypeError, ValueError):
            resolved = 900
        return max(200, resolved)

    @staticmethod
    def _is_memory_compression_message(message: Any) -> bool:
        content = str(getattr(message, "content", message))
        return content.lstrip().startswith("[Memory Compression #")

    @staticmethod
    def _is_protected_plan_message(message: Any) -> bool:
        content = str(getattr(message, "content", message))
        return content.lstrip().startswith("[Protected Plan]")

    @staticmethod
    def _is_system_message(message: Any) -> bool:
        try:
            from langchain_core.messages import SystemMessage
            if isinstance(message, SystemMessage):
                return True
        except Exception:
            pass

        message_type = str(getattr(message, "type", "") or "").lower()
        message_role = str(getattr(message, "role", "") or "").lower()
        if message_type == "system" or message_role == "system":
            return True
        return type(message).__name__ == "SystemMessage"

    @staticmethod
    def _clip_middle(text: str, limit: int) -> str:
        text = str(text)
        if len(text) <= limit:
            return text
        head = max(1, limit // 2)
        tail = max(1, limit - head)
        skipped = len(text) - limit
        return f"{text[:head]}\n... [truncated {skipped} chars] ...\n{text[-tail:]}"

    def _message_summary_excerpt(self, message: Any) -> str:
        role = type(message).__name__.replace("Message", "")
        content = str(message.content) if hasattr(message, "content") else str(message)
        lower = content.lower()

        if "<observation>" in lower or "stdout:" in lower or "stderr:" in lower:
            limit = 2_500
        elif "<execute>" in lower:
            limit = 2_500
        elif role == "Human":
            limit = 1_500
        elif role == "AI":
            limit = 900
        else:
            limit = 1_200
        return self._clip_middle(content, limit)

    def _split_messages(self, messages: List[Any]) -> Tuple[List[Any], List[Any]]:
        """
        从消息列表末尾保留 keep_recent_tokens 的内容，
        其余全部归档。
        保留：第一条稳定 SystemMessage、已有压缩摘要、最近对话。
        其他旧 SystemMessage 也会进入归档，避免长任务里系统反馈无限积累。
        """
        retained_indices: set[int] = set()
        stable_system_seen = False
        for idx, message in enumerate(messages):
            if not self._is_system_message(message):
                continue
            if self._is_memory_compression_message(message):
                retained_indices.add(idx)
                continue
            if self._is_protected_plan_message(message):
                retained_indices.add(idx)
                continue
            if not stable_system_seen:
                retained_indices.add(idx)
                stable_system_seen = True

        keep_tokens = 0
        for idx in range(len(messages) - 1, -1, -1):
            if idx in retained_indices:
                continue
            message = messages[idx]
            if self._is_system_message(message):
                continue
            t = _msg_tokens(message)
            if keep_tokens + t > self.keep_recent_tokens:
                break
            retained_indices.add(idx)
            keep_tokens += t

        compressed = [message for idx, message in enumerate(messages) if idx in retained_indices]
        to_archive = [message for idx, message in enumerate(messages) if idx not in retained_indices]
        return compressed, to_archive

    async def _summarize_with_prompt(self, messages: List[Any], task_id: str) -> Tuple[str, str]:
        prompt = self._build_summary_prompt(messages, task_id)
        from langchain_core.messages import HumanMessage
        try:
            from agent_core.context_lineage import ainvoke_with_snapshot
            response, _ = await ainvoke_with_snapshot(self.llm, [HumanMessage(content=prompt)],
                data_path=self.archive_dir, task_id=task_id,
                metadata={"node_name": "memory", "call_kind": "memory_summary"})
            return response.content.strip(), prompt
        except Exception as e:
            logger.error(f"Summarization failed: {e}")
            fallback = "\n".join(
                str(getattr(m, "content", m))[:200] for m in messages[-5:]
            )
            return f"[Summarization failed. Fallback excerpt]\n{fallback}", prompt

    async def _summarize(self, messages: List[Any], task_id: str) -> str:
        """调用 LLM 对一段对话生成结构化摘要"""
        summary, _prompt = await self._summarize_with_prompt(messages, task_id)
        return summary

    def _build_summary_prompt(self, messages: List[Any], task_id: str) -> str:
        # 序列化消息为可读文本
        text_parts = []
        for m in messages:
            role = type(m).__name__.replace("Message", "")
            text_parts.append(f"[{role}]: {self._message_summary_excerpt(m)}")

        conversation_text = "\n\n".join(text_parts)

        prompt = f"""You are BioMind's memory archivist for scientific and bioinformatics analysis tasks.

Summarize the archived conversation segment so a future agent can safely resume the work without rereading the raw log.

Rules:
- Be factual. Do not infer missing files, parameters, sample groups, biological context, or results.
- Preserve exact paths, dataset names, sample IDs, commands, parameters, environments, outputs, metrics, and errors when observed.
- Only include domain-specific details that appeared in the conversation.
- Do not force irrelevant fields such as genome build, annotation version, contrast design, or sample groups if they were not part of the task.
- If a core section has no useful information, write "not observed".
- Distinguish completed, failed, planned, and pending work.
- Record enough detail to avoid rerunning expensive successful steps.
- Do not include hidden reasoning or generic advice.

Required sections:
1. Task Goal
2. Data and Inputs
3. Analysis Context
4. Methods, Tools, and Environment
5. Commands and Parameters
6. Outputs and Artifacts
7. Metrics, Results, and Observations
8. Errors, Fixes, and Caveats
9. Current Status
10. Next Action

Domain-specific guidance:
- If the task involves sequencing/genomics, preserve reference genome, annotation, genome build, regions, variants, aligners, callers, and relevant thresholds.
- If the task involves scRNA-seq, preserve object paths, sample metadata, QC thresholds, normalization method, clustering resolution, annotation method, marker/DE settings.
- If the task involves methylation/epigenomics, preserve bigWig/BED/region files, methylation contexts, binning/window settings, genome coordinates, and plotting outputs.
- If the task involves chromatin/TAD analysis, preserve matrix files, resolutions, boundary/domain files, comparison pairs, tools, and output tables.
- If the task involves proteomics/metabolomics, preserve raw/input tables, identifiers, normalization, statistical contrasts, databases, and key output tables.
- If the task is tool installation/environment setup, preserve package names, env names, install/verify commands, versions, registry status, and failure fixes.
- If the task is general coding/data processing, preserve source files, scripts, APIs, schemas, transformations, tests, and produced files.

Keep the summary under {self.summary_max_words} words.

---CONVERSATION SEGMENT---
Task ID: {task_id}
{conversation_text}
---END---

OUTPUT THE SUMMARY NOW:"""
        return prompt

    def _write_compression_audit(
        self,
        *,
        chunk: MemoryChunk,
        prompt: str,
        summary: str,
        total_tokens_before: int,
        total_tokens_after: int,
        archived_message_count: int,
        retained_message_count: int,
    ) -> None:
        record = {
            "chunk_id": chunk.chunk_id,
            "task_id": chunk.task_id,
            "created_at": chunk.created_at,
            "archive_file": chunk.archive_file,
            "token_count_original": chunk.token_count_original,
            "total_tokens_before": total_tokens_before,
            "total_tokens_after": total_tokens_after,
            "archived_message_count": archived_message_count,
            "retained_message_count": retained_message_count,
            "summary_max_words": self.summary_max_words,
            "keywords": list(chunk.keywords),
            "prompt": prompt,
            "summary": summary,
        }
        with self._audit_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _extract_keywords(self, text: str) -> List[str]:
        """简单关键词提取（无需 NLP 库）"""
        import re
        # 去标点，转小写，分词
        words = re.findall(r'\b[a-zA-Z][a-zA-Z0-9_\-]{2,}\b', text.lower())
        # 停用词
        stopwords = {
            "the", "and", "for", "this", "that", "with", "are", "was",
            "were", "has", "have", "been", "from", "into", "user", "task",
            "step", "tool", "code", "file", "path", "using", "will", "can",
            "not", "any", "all", "one", "two", "three", "also", "then",
        }
        filtered = [w for w in words if w not in stopwords]
        # 频率统计，取前 20
        freq: Dict[str, int] = {}
        for w in filtered:
            freq[w] = freq.get(w, 0) + 1
        sorted_words = sorted(freq, key=lambda x: freq[x], reverse=True)
        return sorted_words[:20]

    def _write_archive(self, messages: List[Any], task_id: str) -> Path:
        """将消息列表序列化写入 .jsonl 文件"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"archive_{task_id}_{ts}.jsonl"
        path = self.archive_dir / filename

        with open(path, "w", encoding="utf-8") as f:
            for m in messages:
                role = type(m).__name__
                content = str(m.content) if hasattr(m, "content") else str(m)
                record = {
                    "role": role,
                    "content": content,
                    "timestamp": datetime.now().isoformat(),
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        logger.info(f"Archived {len(messages)} messages → {path}")
        return path

    def _load_index(self):
        if self._index_file.exists():
            try:
                data = json.loads(self._index_file.read_text(encoding="utf-8"))
                self._chunks = [MemoryChunk(**c) for c in data.get("chunks", [])]
                self._compression_count = data.get("compression_count", 0)
                logger.info(f"Loaded memory index: {len(self._chunks)} chunks")
            except Exception as e:
                logger.warning(f"Failed to load memory index: {e}")

    def _save_index(self):
        data = {
            "compression_count": self._compression_count,
            "chunks": [vars(c) for c in self._chunks],
        }
        self._index_file.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                    encoding="utf-8")
