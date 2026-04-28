"""通用 bib parser：章节标题切分 + 注疏剥离 + 书名自动识别。

适用于无特殊结构的 bib，或作为兜底。每个具体 bib parser 也可以委托给
default parse_chapters() / aliases_for()，再补充自定义剥离逻辑。
"""
from __future__ import annotations

import re

from core.bib.registry import register_default
from core.bib.types import ParsedBib


# ---------- 章节检测 ----------

_CHAPTER_LINE_RE = re.compile(
    r"^[\s　]*"
    r"(第[一二三四五六七八九十百千零0-9]+[章节篇卷回则]"     # 第N章/篇/...
    r"|[一二三四五六七八九十]+[、.](?:.{0,30}))"            # 一、xxx
    r"[\s　]*$",
    re.MULTILINE,
)


def parse_chapters(text: str) -> list[tuple[int, str]]:
    """提取章节锚点列表 [(offset, name), ...]，offset 是 raw 文本中的偏移。"""
    out: list[tuple[int, str]] = []
    for m in _CHAPTER_LINE_RE.finditer(text):
        out.append((m.start(), m.group(1).strip()))
    return out


# ---------- 别名生成 ----------

# 硬编码映射（常见古籍文件名 → 别名）
_KNOWN_ALIASES: list[tuple[str, list[str]]] = [
    ("dao de jing", ["道德经", "老子", "五千言", "道德真经"]),
    ("lun yu", ["论语"]),
    ("meng zi", ["孟子"]),
    ("shi jing", ["诗经", "毛诗"]),
    ("sun zi bing fa", ["孙子兵法", "孙子", "兵法"]),
    ("zhou yi", ["周易", "易经", "易"]),
    ("zhuang zi", ["庄子", "南华经", "南华真经"]),
    ("huang di nei jing", ["黄帝内经", "内经", "素问", "灵枢"]),
    ("da xue zhong yong", ["大学", "中庸", "大学中庸"]),
]

# 从正文前部提取书名
_BOOK_TITLE_RE = re.compile(r"《([^》]{1,20})》")
_FIRST_HEADING_RE = re.compile(
    r"^[\s　]*([一-鿿]{2,20}(?:译注|注译|今注|今译|集注|集解|正义|注疏|章句|本义|直解|评注))",
    re.MULTILINE,
)


def _extract_title_from_text(text: str) -> list[str]:
    """从正文前 1000 字中提取候选书名。"""
    preview = text[:1000]
    titles: list[str] = []
    for m in _BOOK_TITLE_RE.finditer(preview):
        t = m.group(1).strip()
        if t and t not in titles:
            titles.append(t)
    # 也尝试匹配行首的"XXX译注"等格式
    for m in _FIRST_HEADING_RE.finditer(preview):
        t = m.group(1).strip()
        if t and t not in titles:
            titles.append(t)
    return titles


def aliases_for(filename: str, text_preview: str = "") -> list[str]:
    """根据文件名 + 正文前部自动提取书名别名。"""
    name = filename.lower()
    # 1. 硬编码映射
    for key, aliases in _KNOWN_ALIASES:
        if key in name:
            return aliases
    # 2. 从正文提取
    if text_preview:
        auto = _extract_title_from_text(text_preview)
        if auto:
            return auto
    return []


# ---------- 注疏剥离 ----------

# 注释/翻译/评点标记（独立成行，触发 block 剥离模式）
_BLOCK_MARKER_RE = re.compile(
    r"^[\s　]*"
    r"【[^】]*(?:译文|注释|今译|今注|白话|评|解|疏|按语|校|译|注)[^】]*】"
    r"[\s　]*$",
)

# 行首即是标记且同行带内容（如 论语/孟子 的"【译文】孔子说：..."）
# 疑似原文行（用于在注疏 block 内识别正文恢复点）
# 大学中庸格式：知 [6] 止 [7] 而后有定 / 物 [13] 有本末 [14]
_ORIGINAL_LINE_HINT_RE = re.compile(
    r"[一-鿿]+\s*\[\d+\]\s*[一-鿿]"
)

_INLINE_MARKER_RE = re.compile(
    r"^[\s　]*"
    r"【[^】]*(?:译文|今译|白话|评|解|疏|按语|校|译|注)[^】]*】"
    r"(?![\s　]*$)"    # 后面还有内容（不是独立成行）
    r"|^(?:译文|注释|今译|白话|评注|按语|校记)[：:]\s"   # "译文：..."等
    r"|^[\s　]*〔[^〕]*〕"                                 # 〔1〕等脚注行
    r"|^[\s　]*[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]"         # 圈码数字行
)


def _strip_commentary(text: str) -> str:
    """移除注疏/翻译/评点 block。

    格式 A（论语/孟子）：行首"【译文】孔子说：..."→ 同行内容一并删（始终启用）
    格式 B（大学中庸）：【注】【译】【评】独立成行 → 跳过直到下个 marker 或
      正文特征行（含 [N] 引用标记）。仅当正文存在 [N] 模式时才启用，
      避免对黄帝内经等无标记格式误伤。
    """
    lines = text.splitlines()
    has_bracket_hint = any(_ORIGINAL_LINE_HINT_RE.search(l) for l in lines[:2000])

    keep: list[str] = []
    skip = False
    blank_streak = 0
    for line in lines:
        is_block = has_bracket_hint and bool(_BLOCK_MARKER_RE.match(line))
        is_inline = not is_block and bool(_INLINE_MARKER_RE.match(line))
        if is_block:
            skip = True
            blank_streak = 0
            continue
        if is_inline:
            continue
        if skip:
            if is_block:
                blank_streak = 0
                continue
            if not line.strip():
                blank_streak += 1
                if blank_streak >= 2:
                    skip = False
                    blank_streak = 0
                    keep.append(line)
                continue
            if _ORIGINAL_LINE_HINT_RE.search(line):
                skip = False
                blank_streak = 0
                keep.append(line)
                continue
            blank_streak = 0
            continue
        keep.append(line)
    return "\n".join(keep)


# ---------- 兜底 parser ----------

@register_default
def default_parse(filename: str, raw: str) -> ParsedBib:
    cleaned = _strip_commentary(raw)
    stripped = len(cleaned) < len(raw) * 0.95
    if stripped:
        raw = cleaned
        chapters = parse_chapters(cleaned)
    else:
        chapters = parse_chapters(raw)
    return ParsedBib(
        bib_id=filename.rsplit(".", 1)[0],
        raw=raw,
        aliases=aliases_for(filename, text_preview=raw),
        chapters=chapters,
    )
