"""引文抽取层。

extract(doc) -> list[Citation]。识别书稿中的引文及其上下文：
- 引号引文：「""」「''」「「」」「『』」内容
- 注释段引文：以"注释:"等开头的段落里的引号内容（带 source attribution）
- 隐式引文：作者写《X》第N章后接的 50-150 字（弱置信度，先不上 v1）

每条 Citation 自带归一化文本与 ±100 字上下文，下游 match 不再回头读 doc。
"""
from __future__ import annotations

import re

from core.normalize import normalize
from core.types import Citation, Document


# Chinese double quotes only (U+201C/U+201D). Min 2 chars, max 200 to avoid runaway matches.
_QUOTE_RE = re.compile(r"“([^“”\n]{2,200}?)”")
# Book name 《》 capture
_BOOK_RE = re.compile(r"《([^《》\n]{1,40})》")
# Chapter / fascicle hints
_CHAPTER_RE = re.compile(r"(第[一-龥零一二三四五六七八九十百千万0-9]+[章节篇卷回])")
# Note section markers (paragraph starts with 注释 / 注 / ① / 1) / 1、 / 1.)
_NOTE_HEADER_RE = re.compile(r"^\s*(注释|注)\s*[:：]?\s*$")
# Numbered note item start (e.g. "1)\t" "1、" "1.")
_NOTE_ITEM_RE = re.compile(r"^\s*(?:[(（]?\d+[)）.、]|[①②③④⑤⑥⑦⑧⑨⑩])\s*")


def extract(doc: Document) -> list[Citation]:
    """从 Document 抽取所有引文。"""
    citations: list[Citation] = []
    paragraphs = doc.paragraphs
    n = len(paragraphs)
    in_note_section = False
    for i, para in enumerate(paragraphs):
        if _NOTE_HEADER_RE.match(para):
            in_note_section = True
            continue
        # 进入新章节标记 / 短标题（< 30 字且不含引号）→ 退出 note section
        if in_note_section and len(para) < 30 and "“" not in para and not _NOTE_ITEM_RE.match(para):
            in_note_section = False
        kind = "note" if (in_note_section or _NOTE_ITEM_RE.match(para)) else "body"
        # 在该段落中找所有引号引文
        for m in _QUOTE_RE.finditer(para):
            raw = m.group(1)
            if len(raw) < 2:
                continue
            norm = normalize(raw)
            if len(norm) < 2:
                continue
            ctx = _build_context(paragraphs, i, m.start(), m.end())
            # 注释段：扫整段（注释的本职是给出处）；正文段：仅看引文附近 ±50 字
            scope = para if kind == "note" else para[max(0, m.start() - 50): m.end() + 50]
            book, chapter = _hints(scope)
            # 概念词识别：≤ 4 字 + 无 book hint + 正文段 → 多半是作者强调用引号
            # 例："活化石"、"源代码"、"太阳"。这些不是引文，不参与核校。
            is_concept = (
                kind == "body"
                and book is None
                and len(norm) <= 4
            )
            citations.append(
                Citation(
                    quote=raw,
                    quote_norm=norm,
                    context=ctx,
                    location=f"{kind}[{i}]",
                    source=kind,
                    book_hint=book,
                    chapter_hint=chapter,
                    seq=i,
                    is_concept=is_concept,
                )
            )
    return _dedup(citations)


def _build_context(paragraphs: list[str], i: int, start: int, end: int) -> str:
    para = paragraphs[i]
    left = para[max(0, start - 100):start]
    right = para[end:end + 100]
    # 若当前段落本身上下文不够，再带上前一段尾部 / 下一段开头
    if len(left) < 80 and i > 0:
        left = paragraphs[i - 1][-(80 - len(left)):] + " ⏎ " + left
    if len(right) < 80 and i + 1 < len(paragraphs):
        right = right + " ⏎ " + paragraphs[i + 1][:80 - len(right)]
    return f"{left}〖{para[start:end]}〗{right}"


def _hints(scope: str) -> tuple[str | None, str | None]:
    book = None
    chapter = None
    bm = _BOOK_RE.search(scope)
    if bm:
        book = bm.group(1)
    cm = _CHAPTER_RE.search(scope)
    if cm:
        chapter = cm.group(1)
    return book, chapter


def _dedup(citations: list[Citation]) -> list[Citation]:
    """同一段内同一引文文本只保留一条；不同段保留（位置不同）。"""
    seen: set[tuple[int, str]] = set()
    out: list[Citation] = []
    for c in citations:
        key = (c.seq, c.quote_norm)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out
