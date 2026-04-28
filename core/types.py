"""核心数据类型。层间契约——只定义结构，不含行为。

parse  → Document
extract → list[Citation]
match  → list[Verdict]
report ← list[Verdict]

任何实现只要产出/消费这些类型，就可替换对应模块。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


# ── parse 层产出 ──

@dataclass
class Document:
    text: str                 # 全文（段落以 \n 分隔）
    paragraphs: list[str]     # 段落列表，按出现顺序
    source: Path              # 原始文件
    para_offsets: list[int] = field(default_factory=list)  # 每段在 text 中起始偏移

    @property
    def length(self) -> int:
        return len(self.text)


# ── extract 层产出 ──

@dataclass
class Citation:
    quote: str                # 原始引文（已去引号字符）
    quote_norm: str           # 归一化后用于匹配
    context: str              # 上下文片段（前后各 100 字内）
    location: str             # "body[5]" / "note[9]"
    source: str               # 'body' | 'note'
    book_hint: str | None = None
    chapter_hint: str | None = None
    seq: int = -1             # 在文档中的出现顺序（排序/去重用，提取实现保证递增唯一）
    is_concept: bool = False  # True 表示概念词/强调引号，不需要核校


# ── match 层产出 ──

Grade = Literal["A", "B", "C", "D"]


@dataclass
class MatchEvidence:
    bib_id: str
    chapter: str | None
    raw_offset_start: int
    raw_offset_end: int
    raw_window: str
    score: float              # 0-100，RapidFuzz 分数
    edit_ops: list[tuple[str, int, int]] = field(default_factory=list)


@dataclass
class Verdict:
    citation: Citation
    grade: Grade
    evidence: MatchEvidence | None
    issues: list[str] = field(default_factory=list)
    suggestion: str | None = None
    confidence: float = 1.0
    needs_llm: bool = False
