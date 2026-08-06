"""Deterministic, loss-aware packing for step-local LLM context.

The packer operates on typed blocks before a prompt is rendered.  It never
slices the final prompt.  Optional blocks may be compressed with their own
strategy or removed as a whole; required blocks either remain structurally
valid or raise ``RequiredContextOverflow``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence


CompressFn = Callable[[str, int], str]


class RequiredContextOverflow(RuntimeError):
    """Raised when required context cannot fit without silent information loss."""

    def __init__(self, message: str, *, report: Optional[Dict] = None):
        super().__init__(message)
        self.report = dict(report or {})


def build_prompt_budget_telemetry(
    messages: Sequence[object],
    *,
    component_chars: Optional[Dict[str, int]] = None,
    context_limit_tokens: int = 200_000,
    warning_ratio: float = 0.75,
) -> Dict:
    """Return provider-neutral prompt size telemetry without storing content."""
    texts = [str(getattr(item, "content", item) or "") for item in messages]
    total_chars = sum(len(value) for value in texts)
    estimated_tokens = max(1, (total_chars + 3) // 4) if total_chars else 0
    seen: set[str] = set()
    duplicate_chars = 0
    for value in texts:
        normalized = re.sub(r"\s+", " ", value).strip()
        if not normalized:
            continue
        digest = hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()
        if digest in seen:
            duplicate_chars += len(value)
        else:
            seen.add(digest)
    limit = max(1, int(context_limit_tokens or 1))
    ratio = estimated_tokens / limit
    return {
        "message_count": len(texts),
        "input_chars": total_chars,
        "estimated_input_tokens": estimated_tokens,
        "context_limit_tokens": limit,
        "remaining_headroom_tokens": max(0, limit - estimated_tokens),
        "usage_ratio": round(ratio, 6),
        "warning": ratio >= max(0.0, min(float(warning_ratio), 1.0)),
        "duplicate_chars": duplicate_chars,
        "component_chars": {
            str(key): max(0, int(value or 0))
            for key, value in dict(component_chars or {}).items()
        },
    }


@dataclass
class ContextBlock:
    block_id: str
    category: str
    content: str
    priority: int
    pinned: bool = False
    droppable: bool = True
    min_chars: int = 0
    source_ref: Optional[str] = None
    step_id: Optional[str] = None
    revision: Optional[int] = None
    compress_fn: Optional[CompressFn] = None

    def normalized_content(self) -> str:
        return str(self.content or "").strip()


@dataclass
class ContextPackResult:
    contents: Dict[str, str]
    report: Dict = field(default_factory=dict)


def _marker(omitted: int, label: str = "content") -> str:
    return f"... [{omitted} chars of {label} omitted] ..."


def compress_lines(text: str, max_chars: int) -> str:
    """Keep whole lines from the head and tail; never cut a line in half."""
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    lines = value.splitlines()
    if len(lines) <= 1:
        return value
    marker = _marker(max(0, len(value) - max_chars), "lines")
    kept_head: List[str] = []
    kept_tail: List[str] = []
    used = len(marker) + 2
    left = 0
    right = len(lines) - 1
    take_head = True
    while left <= right:
        index = left if take_head else right
        candidate = lines[index]
        extra = len(candidate) + 1
        if used + extra > max_chars:
            break
        if take_head:
            kept_head.append(candidate)
            left += 1
        else:
            kept_tail.append(candidate)
            right -= 1
        used += extra
        take_head = not take_head
    if left > right:
        return "\n".join(kept_head + list(reversed(kept_tail)))
    return "\n".join([*kept_head, marker, *reversed(kept_tail)]).strip()


def compress_paragraphs(text: str, max_chars: int) -> str:
    """Keep complete paragraphs, preferring the beginning and the latest tail."""
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", value) if part.strip()]
    if len(paragraphs) <= 1:
        return compress_lines(value, max_chars)
    marker = _marker(max(0, len(value) - max_chars), "paragraphs")
    selected_head: List[str] = []
    selected_tail: List[str] = []
    used = len(marker) + 4
    left = 0
    right = len(paragraphs) - 1
    take_head = True
    while left <= right:
        index = left if take_head else right
        paragraph = paragraphs[index]
        if used + len(paragraph) + 2 > max_chars:
            break
        if take_head:
            selected_head.append(paragraph)
            left += 1
        else:
            selected_tail.append(paragraph)
            right -= 1
        used += len(paragraph) + 2
        take_head = not take_head
    if left > right:
        return "\n\n".join(selected_head + list(reversed(selected_tail)))
    return "\n\n".join([*selected_head, marker, *reversed(selected_tail)]).strip()


def compress_log(text: str, max_chars: int) -> str:
    """Keep log head, error neighborhoods, and tail as complete lines."""
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    lines = value.splitlines()
    error_indexes = [
        index
        for index, line in enumerate(lines)
        if re.search(r"error|exception|fatal|traceback|failed|killed|segmentation", line, re.IGNORECASE)
    ]
    indexes = set(range(min(20, len(lines))))
    indexes.update(range(max(0, len(lines) - 40), len(lines)))
    for index in error_indexes[:12]:
        indexes.update(range(max(0, index - 3), min(len(lines), index + 4)))
    selected = [lines[index] for index in sorted(indexes)]
    compressed = "\n".join(selected)
    if len(compressed) > max_chars:
        compressed = compress_lines(compressed, max_chars)
    omitted_lines = max(0, len(lines) - len(selected))
    if omitted_lines and len(compressed) + 48 <= max_chars:
        compressed += f"\n... [{omitted_lines} log lines omitted] ..."
    return compressed


def compress_tagged_text(text: str, max_chars: int) -> str:
    """Compress one outer-tagged message while preserving its tag contract."""
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    wrapper = re.match(
        r"^\s*(?P<open><(?P<tag>[A-Za-z_][\w.-]*)(?:\s+[^>]*)?>)\s*(?P<body>.*)\s*(?P<close></(?P=tag)>)\s*$",
        value,
        re.DOTALL,
    )
    if not wrapper:
        return compress_paragraphs(value, max_chars)
    open_tag = wrapper.group("open")
    close_tag = wrapper.group("close")
    inner_budget = max_chars - len(open_tag) - len(close_tag) - 2
    if inner_budget <= 0:
        return value
    compressor = compress_log if wrapper.group("tag").lower() == "observation" else compress_paragraphs
    body = compressor(wrapper.group("body"), inner_budget)
    return f"{open_tag}\n{body}\n{close_tag}"


def compress_json(text: str, max_chars: int) -> str:
    """Compact JSON without ever returning a partial JSON document."""
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    return compact if len(compact) <= max_chars else value


def compress_markdown_sections(text: str, max_chars: int) -> str:
    """Select complete Markdown sections, prioritizing executable guidance."""
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    wrapper = re.match(
        r"^\s*(?P<open><(?P<tag>[A-Za-z_][\w.-]*)[^>]*>)\s*(?P<body>.*)\s*(?P<close></(?P=tag)>)\s*$",
        value,
        re.DOTALL,
    )
    if wrapper:
        open_tag = wrapper.group("open")
        close_tag = wrapper.group("close")
        inner_budget = max_chars - len(open_tag) - len(close_tag) - 2
        if inner_budget <= 0:
            return value
        inner = compress_markdown_sections(wrapper.group("body"), inner_budget)
        return f"{open_tag}\n{inner}\n{close_tag}"
    lines = value.splitlines()
    starts = [index for index, line in enumerate(lines) if re.match(r"^#{1,6}\s+", line)]
    if not starts:
        return compress_lines(value, max_chars)
    if starts[0] != 0:
        starts.insert(0, 0)
    sections: List[str] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        section = "\n".join(lines[start:end]).strip()
        if section:
            sections.append(section)
    preferred = re.compile(
        r"runtime|quick start|input|execution|command|validation|output|error|troubleshoot|constraint|禁止|输入|执行|验证|错误",
        re.IGNORECASE,
    )
    ordered = sorted(
        enumerate(sections),
        key=lambda item: (0 if item[0] == 0 else 1, 0 if preferred.search(item[1].splitlines()[0]) else 1, item[0]),
    )
    selected: List[tuple[int, str]] = []
    used = 0
    for index, section in ordered:
        if used + len(section) + 2 <= max_chars:
            selected.append((index, section))
            used += len(section) + 2
    if not selected:
        return compress_lines(sections[0], max_chars)
    selected.sort(key=lambda item: item[0])
    rendered = "\n\n".join(section for _, section in selected)
    omitted = len(sections) - len(selected)
    note = f"\n\n... [{omitted} lower-priority Markdown sections omitted] ..." if omitted else ""
    return rendered + note if len(rendered) + len(note) <= max_chars else rendered


class ContextBudgetManager:
    """Pack typed blocks into a deterministic character budget."""

    def pack(self, blocks: Sequence[ContextBlock], max_chars: int) -> ContextPackResult:
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        working = [
            {
                "block": block,
                "content": block.normalized_content(),
                "original_chars": len(block.normalized_content()),
                "action": "kept",
            }
            for block in blocks
        ]
        self._deduplicate(working)
        self._compress_to_budget(working, max_chars, include_pinned=False)
        self._drop_to_budget(working, max_chars)
        self._compress_to_budget(working, max_chars, include_pinned=True)

        total = self._total(working)
        report = self._report(working, max_chars, total)
        if total > max_chars:
            required = [
                item["block"].block_id
                for item in working
                if item["content"] and (item["block"].pinned or not item["block"].droppable)
            ]
            report["overflow"] = True
            report["required_blocks"] = required
            raise RequiredContextOverflow(
                "Required context exceeds the available prompt budget: " + ", ".join(required),
                report=report,
            )
        return ContextPackResult(
            contents={item["block"].block_id: str(item["content"]) for item in working},
            report=report,
        )

    @staticmethod
    def _total(items: Iterable[Dict]) -> int:
        return sum(len(str(item.get("content") or "")) for item in items)

    @staticmethod
    def _deduplicate(items: List[Dict]) -> None:
        seen: Dict[str, Dict] = {}
        for item in sorted(items, key=lambda value: (-value["block"].priority, not value["block"].pinned)):
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            fingerprint = re.sub(r"\s+", " ", content).strip().lower()
            if fingerprint in seen and item["block"].droppable and not item["block"].pinned:
                item["content"] = ""
                item["action"] = f"deduplicated_against:{seen[fingerprint]['block'].block_id}"
            else:
                seen[fingerprint] = item

    def _compress_to_budget(self, items: List[Dict], max_chars: int, *, include_pinned: bool) -> None:
        candidates = sorted(
            (
                item
                for item in items
                if item["content"]
                and item["block"].compress_fn is not None
                and (include_pinned or not item["block"].pinned)
            ),
            key=lambda item: (item["block"].pinned, item["block"].priority, -len(item["content"])),
        )
        for item in candidates:
            excess = self._total(items) - max_chars
            if excess <= 0:
                return
            block = item["block"]
            current = str(item["content"])
            target = max(int(block.min_chars or 0), len(current) - excess)
            if target >= len(current):
                continue
            compressed = str(block.compress_fn(current, target) or "").strip()
            if compressed and len(compressed) < len(current):
                item["content"] = compressed
                item["action"] = "compressed"

    def _drop_to_budget(self, items: List[Dict], max_chars: int) -> None:
        candidates = sorted(
            (
                item
                for item in items
                if item["content"] and item["block"].droppable and not item["block"].pinned
            ),
            key=lambda item: (item["block"].priority, -len(item["content"])),
        )
        for item in candidates:
            if self._total(items) <= max_chars:
                return
            item["content"] = ""
            item["action"] = "dropped"

    @staticmethod
    def _report(items: List[Dict], max_chars: int, total: int) -> Dict:
        return {
            "budget_chars": max_chars,
            "packed_chars": total,
            "overflow": total > max_chars,
            "blocks": [
                {
                    "id": item["block"].block_id,
                    "category": item["block"].category,
                    "priority": item["block"].priority,
                    "pinned": item["block"].pinned,
                    "action": item["action"],
                    "original_chars": item["original_chars"],
                    "final_chars": len(str(item["content"] or "")),
                    "source_ref": item["block"].source_ref or "",
                }
                for item in items
            ],
        }
