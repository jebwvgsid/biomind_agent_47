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

except ImportError:
    logger.warning("tiktoken not found – using char/4 estimation for token count.")

    def count_tokens(text: str) -> int:          # type: ignore[misc]
        return max(1, len(text) // 4)


def _msg_tokens(msg) -> int:
    """适配 LangChain BaseMessage / dict 两种格式"""
    if hasattr(msg, "content"):
        return count_tokens(str(msg.content))
    if isinstance(msg, dict):
        return count_tokens(str(msg.get("content", "")))
    return count_tokens(str(msg))


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
    ):
        self.llm = llm
        self.archive_dir = Path(archive_dir)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.max_tokens = max_tokens
        self.compress_threshold = compress_threshold
        self.keep_recent_tokens = keep_recent_tokens

        self._compression_count: int = 0
        self._chunks: List[MemoryChunk] = []
        self._index_file = self.archive_dir / "memory_index.json"
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

        print(f"\n{Fore.YELLOW}🗜️  [Memory] Token usage {total:,}/{self.max_tokens:,} "
              f"(≥{self.compress_threshold*100:.0f}%). Compressing...{Style.RESET_ALL}")

        compressed_msgs, archived_msgs = self._split_messages(messages)

        if not archived_msgs:
            logger.warning("Nothing to archive – messages list is too short.")
            return messages, False

        # 1. 生成摘要
        summary = await self._summarize(archived_msgs, task_id)

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
        self._save_index()
        self._compression_count += 1

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
        print(f"{Fore.GREEN}✓ Compressed: {total:,} → {new_total:,} tokens "
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
        print(f"\n📊 Memory: {s}")

    # ── 内部方法 ────────────────────────────────────────────

    def _split_messages(self, messages: List[Any]) -> Tuple[List[Any], List[Any]]:
        """
        从消息列表末尾保留 keep_recent_tokens 的内容，
        其余全部归档。
        保证：第一条 SystemMessage（系统 Prompt）永远保留。
        """
        from langchain_core.messages import SystemMessage

        # 找到最前面的 SystemMessage（主 Prompt）
        system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
        non_system = [m for m in messages if not isinstance(m, SystemMessage)]

        # 从后向前收集保留列表
        keep: List[Any] = []
        keep_tokens = 0
        for m in reversed(non_system):
            t = _msg_tokens(m)
            if keep_tokens + t > self.keep_recent_tokens:
                break
            keep.insert(0, m)
            keep_tokens += t

        to_archive = [m for m in non_system if m not in keep]

        # 保留消息 = 系统消息 + 最近内容
        compressed = system_msgs + keep
        return compressed, to_archive

    async def _summarize(self, messages: List[Any], task_id: str) -> str:
        """调用 LLM 对一段对话生成结构化摘要"""
        from langchain_core.messages import HumanMessage

        # 序列化消息为可读文本
        text_parts = []
        for m in messages:
            role = type(m).__name__.replace("Message", "")
            content = str(m.content) if hasattr(m, "content") else str(m)
            text_parts.append(f"[{role}]: {content[:800]}")   # 截断超长消息

        conversation_text = "\n\n".join(text_parts)

        prompt = f"""You are a memory archivist for a bioinformatics AI agent.
Summarize the following conversation segment into a CONCISE but INFORMATION-RICH record.

REQUIRED SECTIONS:
1. **Task Goal** – What was the user trying to accomplish?
2. **Steps Completed** – Which steps ran successfully? Include key file paths and commands.
3. **Current Status** – What is the last known state? Any pending steps?
4. **Key Artifacts** – Files generated, tool versions confirmed, env names.
5. **Errors Encountered** – Any errors and their resolutions (if resolved).

Keep the summary under 600 words. Be precise and factual.

---CONVERSATION SEGMENT---
Task ID: {task_id}
{conversation_text}
---END---

OUTPUT THE SUMMARY NOW:"""

        try:
            response = await self.llm.ainvoke([HumanMessage(content=prompt)])
            return response.content.strip()
        except Exception as e:
            logger.error(f"Summarization failed: {e}")
            # 降级：拼接最后几条消息的内容
            fallback = "\n".join(
                str(getattr(m, "content", m))[:200] for m in messages[-5:]
            )
            return f"[Summarization failed. Fallback excerpt]\n{fallback}"

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
