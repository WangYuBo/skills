"""参考文献索引层。

对外只暴露 build_corpus(bib_paths) -> Corpus。内部实现：
  1. 用 bib/registry 解析每个 bib 文件
  2. 归一化全文，构建 5-gram 倒排索引
  3. 按文件 sha256 缓存到 ~/.cache/heimao_index/<sha256>.pkl
  4. 多个 bib 装进一个 Corpus，对外只暴露查询接口

设计选择：纯 Python dict 倒排索引（不用 BM25/ES/FAISS）——理由见 plan 附录。
"""
from __future__ import annotations

import hashlib
import os
import pickle
from dataclasses import dataclass, field
from pathlib import Path

from core.bib.registry import ensure_loaded, parse_bib
from core.bib.types import ParsedBib
from core.normalize import normalize_keep_pos


CACHE_DIR = Path(os.environ.get("HEIMAO_CACHE_DIR", Path.home() / ".cache" / "heimao_index"))
INDEX_VERSION = 5  # 索引格式版本，用于 invalidate（normalize 规则变化时 +1）
GRAM = 5


@dataclass
class BibIndex:
    bib_id: str
    sha256: str
    aliases: list[str]
    raw: str                                  # 原始全文（含注疏，用于显示与 diff）
    text_norm: str                            # 归一化全文（用于匹配）
    norm_to_raw: list[int]                    # text_norm[i] → raw 偏移
    gram_index: dict[str, list[int]]          # 5-gram → text_norm 偏移列表
    chapters: list[tuple[int, str]]           # (raw_offset, chapter_name)


@dataclass
class Corpus:
    indexes: list[BibIndex] = field(default_factory=list)

    def by_alias(self, alias: str) -> BibIndex | None:
        for idx in self.indexes:
            if alias in idx.aliases or alias == idx.bib_id:
                return idx
        return None

    def select(self, alias: str | None) -> list[BibIndex]:
        """alias 命中则只返回该 bib，否则返回全部（用于全局兜底搜索）。"""
        if alias:
            hit = self.by_alias(alias)
            if hit:
                return [hit]
        return self.indexes


def build_corpus(bib_paths: list[str | Path]) -> Corpus:
    ensure_loaded()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    indexes: list[BibIndex] = []
    for p in bib_paths:
        indexes.append(_build_or_load_one(Path(p)))
    return Corpus(indexes=indexes)


def _build_or_load_one(path: Path) -> BibIndex:
    sha = _sha256(path)
    cache = CACHE_DIR / f"{sha}.v{INDEX_VERSION}.pkl"
    if cache.exists():
        try:
            with cache.open("rb") as f:
                obj = pickle.load(f)
            if isinstance(obj, BibIndex) and obj.sha256 == sha:
                return obj
        except Exception:
            pass  # 缓存损坏 → 重建
    parsed = parse_bib(path)
    idx = _build(parsed, sha)
    try:
        with cache.open("wb") as f:
            pickle.dump(idx, f, protocol=pickle.HIGHEST_PROTOCOL)
    except OSError:
        pass  # 缓存写失败不影响主流程
    return idx


def _build(parsed: ParsedBib, sha: str) -> BibIndex:
    text_norm, norm_to_raw = normalize_keep_pos(parsed.raw)
    gram_index = _build_grams(text_norm)
    return BibIndex(
        bib_id=parsed.bib_id,
        sha256=sha,
        aliases=parsed.aliases,
        raw=parsed.raw,
        text_norm=text_norm,
        norm_to_raw=norm_to_raw,
        gram_index=gram_index,
        chapters=parsed.chapters,
    )


def _build_grams(text: str) -> dict[str, list[int]]:
    """对归一化文本建 5-gram 倒排。"""
    gram_index: dict[str, list[int]] = {}
    n = len(text) - GRAM + 1
    for i in range(n):
        g = text[i : i + GRAM]
        gram_index.setdefault(g, []).append(i)
    return gram_index


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(1 << 16)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()
