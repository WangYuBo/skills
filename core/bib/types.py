"""bib 解析后的数据结构。所有 parser 都返回 ParsedBib。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ParsedBib:
    bib_id: str                          # 文件名 stem
    raw: str                             # 原始全文（用于显示与 diff）
    aliases: list[str]                   # 别名表，例 ["道德经", "老子"]
    chapters: list[tuple[int, str]] = field(default_factory=list)
    # (raw_offset, chapter_name)，按 offset 升序

    # 注疏剥离（v1 留空；v2 按需填充）
    has_separated_original: bool = False
    original_raw: str = ""               # 仅原文（剥除注疏后）
