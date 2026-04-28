"""文本归一化。

归一化的目标：让引文与参考文献在「标点 / 繁简 / 异体字 / 空白」上**等价**，
但**保留字符级原貌**（用于后续 diff 显示原始文本）。

normalize() 输出仅用于 5-gram 倒排和 RapidFuzz 匹配；显示给用户的始终是原文。
"""
from __future__ import annotations

import re
import unicodedata

# 异体字映射表：以"现代通行简体"为标准。仅收录在本项目古籍中确实可能造成漏匹配的字。
# 后续 issue 报告里如果发现新案例再补。
_VARIANT_MAP = str.maketrans({
    # 繁简遗漏（OpenCC 已处理大部分，这里补盲点）
    "祇": "只", "衹": "只",
    # 古籍常见异体
    "鬭": "斗", "鬬": "斗", "敎": "教", "迺": "乃",
    # 数字异写：二/贰、三/叁 等用作"重复"或"数量"的语境互通
    "贰": "二", "叁": "三", "肆": "四", "伍": "五",
    # 其他常见同义换字（非异体，但古籍流传中常被互换）
    "于": "于", "於": "于",
    "无": "无", "無": "无",
})

# 中英标点 + 引号 + 各种空白字符。所有这些在归一化时直接删除。
# 显式列出 Unicode 码点，避免源文件被编辑器/系统折叠引号。
_PUNCT_CHARS = (
    "　"                                # 全角空格
    "“”‘’‟‛"  # “ ” ‘ ’ ‟ ‛
    "《》〈〉"              # 《》〈〉
    "「」『』"              # 「」『』
    "、。？！，"        # 、。？！，
    "；：（）"              # ；：（）
    "【】"                          # 【】
    "​‌‍﻿"              # 零宽字符
    "\""
    "'"
    "`"
)
_PUNCT_RE = re.compile(
    r"[\s" + re.escape(_PUNCT_CHARS) + r"]"
    r"|[!?.,;:()\[\]<>{}/\\|=+\-*&^%$#@~]"
    r"|[0-9]"                       # ASCII 数字（古籍 bib 中的脚注标号）
    r"|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]"  # 圆圈数字
    r"|[⑴⑵⑶⑷⑸⑹⑺⑻⑼⑽]"          # 括号数字
    r"|[⒈⒉⒊⒋⒌⒍⒎⒏⒐⒑]"          # 带点数字
)


_OPENCC = None


def _opencc():
    """惰性初始化 OpenCC（首次调用 ~50ms）。"""
    global _OPENCC
    if _OPENCC is None:
        from opencc import OpenCC
        _OPENCC = OpenCC("t2s")
    return _OPENCC


def normalize(text: str) -> str:
    """归一化用于匹配。返回的字符串只保留字符（无标点/空白），繁→简，异体折叠。"""
    if not text:
        return ""
    # 1. NFKC 统一全角半角与组合字符
    text = unicodedata.normalize("NFKC", text)
    # 2. 繁→简
    text = _opencc().convert(text)
    # 3. 异体字折叠
    text = text.translate(_VARIANT_MAP)
    # 4. 移除所有标点与空白
    text = _PUNCT_RE.sub("", text)
    return text


def normalize_keep_pos(text: str) -> tuple[str, list[int]]:
    """归一化并返回 normalized → original 的字符位置映射。

    用于在 normalized 里找到匹配后，能映射回原文做 diff 显示。
    返回 (normalized_text, idx_map)，其中 idx_map[i] 是 normalized[i] 对应的原文位置。

    严格按原文字符位置记录索引，**不**做 NFKC（NFKC 会改变长度，破坏位置映射）。
    NFKC 仅在不需要位置映射的 normalize() 里做。
    """
    if not text:
        return "", []
    cc = _opencc()
    out_chars: list[str] = []
    idx_map: list[int] = []
    for i, ch in enumerate(text):
        if _PUNCT_RE.match(ch):
            continue
        ch_simp = cc.convert(ch).translate(_VARIANT_MAP)
        if not ch_simp:
            continue
        for c in ch_simp:
            out_chars.append(c)
            idx_map.append(i)
    return "".join(out_chars), idx_map
