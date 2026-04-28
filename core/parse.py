"""文档解析层。

对外只暴露 parse(path) -> Document。docx/txt/pdf 自动识别。docx 同时取正文 +
Word 脚注/尾注（若有）；本项目实测脚注通常以"注释:"等标记内嵌正文，由 extract
层识别，不在解析层区分。
"""
from __future__ import annotations

import re
from pathlib import Path

from core.types import Document


def parse(path: str | Path) -> Document:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".docx":
        text = _read_docx(p)
    elif suffix == ".pdf":
        text = _read_pdf(p)
    elif suffix in (".txt", ".md"):
        text = p.read_text(encoding="utf-8")
    else:
        raise ValueError(f"unsupported format: {suffix}")
    paragraphs, offsets = _split_paragraphs(text)
    return Document(text=text, paragraphs=paragraphs, source=p, para_offsets=offsets)


def _read_docx(path: Path) -> str:
    from docx2python import docx2python
    r = docx2python(str(path))
    body = r.text
    notes = ""
    if r.footnotes:
        notes += "\n\n" + _flatten_runs(r.footnotes)
    if r.endnotes:
        notes += "\n\n" + _flatten_runs(r.endnotes)
    return body + notes


def _flatten_runs(nested) -> str:
    """docx2python 返回深度嵌套 list[list[list[list[str]]]]，扁平化为字符串。"""
    out: list[str] = []
    def walk(x):
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, (list, tuple)):
            for y in x:
                walk(y)
    walk(nested)
    return "\n".join(s for s in out if s)


def _read_pdf(path: Path) -> str:
    import pdfplumber
    pages: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            pages.append(t)
    return "\n\n".join(pages)


_PARA_SPLIT = re.compile(r"\n\s*\n+")


def _split_paragraphs(text: str) -> tuple[list[str], list[int]]:
    paragraphs: list[str] = []
    offsets: list[int] = []
    pos = 0
    for chunk in _PARA_SPLIT.split(text):
        chunk = chunk.strip()
        if not chunk:
            pos = text.find(chunk, pos) if chunk else pos
            continue
        idx = text.find(chunk, pos)
        if idx < 0:
            idx = pos
        offsets.append(idx)
        paragraphs.append(chunk)
        pos = idx + len(chunk)
    return paragraphs, offsets
