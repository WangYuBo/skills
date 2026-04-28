"""核心匹配引擎。对外只暴露 match()。

内部三阶段全部私有：
  _stage_exact   — Aho-Corasick 一次扫描所有引文 vs 全 bib（O(N+M+hits)）
  _stage_fuzzy   — 5-gram 倒排召回 + RapidFuzz 局部对齐
  _stage_diff    — 字符级 diff，按 score 分级 A/B/C/D

LLM 阶段（_stage_llm）在 core/llm.py 里实现，本模块只负责调用。
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Literal

from rapidfuzz import fuzz
from rapidfuzz.distance import Indel, Levenshtein

from core.extract import Citation
from core.index import GRAM, BibIndex, Corpus


Grade = Literal["A", "B", "C", "D"]


@dataclass
class MatchEvidence:
    bib_id: str
    chapter: str | None
    raw_offset_start: int        # 在 bib.raw 中的偏移
    raw_offset_end: int
    raw_window: str              # bib.raw 中匹配到的原始文本
    score: float                 # 0-100，RapidFuzz 分数
    edit_ops: list[tuple[str, int, int]] = field(default_factory=list)
    # 字符 diff：[("equal"/"replace"/"insert"/"delete", quote_idx, raw_idx), ...]


@dataclass
class Verdict:
    citation: Citation
    grade: Grade                 # A 完全一致 / B 轻微差异 / C 文字或意义有问题 / D 无法定位
    evidence: MatchEvidence | None
    issues: list[str] = field(default_factory=list)
    suggestion: str | None = None
    confidence: float = 1.0
    needs_llm: bool = False      # 由 _stage_diff 标记，由 _stage_llm 消费


def match(
    citations: list[Citation],
    corpus: Corpus,
    *,
    llm=None,
    on_progress=None,
) -> list[Verdict]:
    if not citations:
        return []
    # 概念词不参与匹配，直接给特殊判定
    concept_verdicts: list[Verdict] = []
    real_citations: list[Citation] = []
    for c in citations:
        if c.is_concept:
            concept_verdicts.append(Verdict(
                citation=c, grade="A", evidence=None,
                issues=["概念词或强调引号，非引文"],
                needs_llm=False, confidence=1.0,
            ))
        else:
            real_citations.append(c)
    citations = real_citations
    # Stage 1: 精确匹配（Aho-Corasick 一次扫描）
    exact = _stage_exact(citations, corpus)
    # Stage 2: 未命中的走 fuzzy
    fuzzy = _stage_fuzzy(citations, exact, corpus)
    # Stage 3: 合并结果，做字符 diff，分级
    verdicts = _stage_diff(citations, exact, fuzzy, corpus)
    if on_progress:
        on_progress({"stage": "match_done", "verdicts": len(verdicts)})
    # Stage 4: LLM 仅对 needs_llm 项
    if llm is not None:
        from core.llm import judge_batch
        judge_batch(verdicts, corpus, llm, on_progress=on_progress)
    # 把概念词判定加回结果，按原始引文顺序排（方便报告阅读）
    all_verdicts = verdicts + concept_verdicts
    all_verdicts.sort(key=lambda v: (v.citation.raw_para_idx, v.citation.quote))
    return all_verdicts


# ---------- Stage 1: 精确匹配 ----------

def _stage_exact(citations: list[Citation], corpus: Corpus) -> list[MatchEvidence | None]:
    """对每条 citation，在选定 bib 的归一化文本中找完全相等的子串。

    使用 pyahocorasick 一次扫描所有 bib：把所有 citation 的 quote_norm 加入自动机，
    一次过遍历每个 bib 的 text_norm，记录命中。
    """
    import ahocorasick

    # 同一 quote_norm 可能对应多个 citation；按字符串去重，命中后扇出回所有 owners
    a = ahocorasick.Automaton()
    owners: dict[str, list[int]] = {}
    for i, c in enumerate(citations):
        if len(c.quote_norm) >= 4:
            owners.setdefault(c.quote_norm, []).append(i)
    for qn, _ in owners.items():
        a.add_word(qn, qn)
    a.make_automaton()

    results: list[MatchEvidence | None] = [None] * len(citations)
    for idx in corpus.indexes:
        for end_pos, qn in a.iter(idx.text_norm):
            start_pos = end_pos - len(qn) + 1
            ev = _make_evidence(idx, start_pos, end_pos + 1, score=100.0)
            for i in owners[qn]:
                if results[i] is None:
                    results[i] = ev
    return results


# ---------- Stage 2: 模糊召回 ----------

def _stage_fuzzy(
    citations: list[Citation],
    exact: list[MatchEvidence | None],
    corpus: Corpus,
) -> list[MatchEvidence | None]:
    """对未精确命中的引文做 q-gram 召回 + RapidFuzz 局部对齐。"""
    out: list[MatchEvidence | None] = list(exact)
    for i, c in enumerate(citations):
        if out[i] is not None:
            continue
        out[i] = _fuzzy_one(c, corpus)
    return out


def _fuzzy_one(c: Citation, corpus: Corpus) -> MatchEvidence | None:
    qn = c.quote_norm
    if len(qn) < GRAM:
        return None
    bib_pool = corpus.select(c.book_hint)
    best: MatchEvidence | None = None
    for idx in bib_pool:
        ev = _fuzzy_in_bib(qn, idx)
        if ev and (best is None or ev.score > best.score):
            best = ev
    # hint 命中但分数低：尝试全库兜底
    if c.book_hint and (best is None or best.score < 80):
        for idx in corpus.indexes:
            if idx in bib_pool:
                continue
            ev = _fuzzy_in_bib(qn, idx)
            if ev and (best is None or ev.score > best.score):
                best = ev
    return best


def _fuzzy_in_bib(qn: str, idx: BibIndex) -> MatchEvidence | None:
    # 取最稀有的 K 个 5-gram 来做候选位置初筛
    grams = [qn[i : i + GRAM] for i in range(0, len(qn) - GRAM + 1)]
    # 去重并按 posting 长度升序（最稀有优先）
    uniq = list({g: idx.gram_index.get(g, ()) for g in grams}.items())
    uniq.sort(key=lambda kv: len(kv[1]) if kv[1] else 1 << 30)
    # 取前 K 个稀有 gram，合并它们的 posting 作为候选锚点
    K = 4
    anchors: set[int] = set()
    for g, pos in uniq[:K]:
        if not pos:
            continue
        if len(pos) > 500:
            continue
        g_start_in_q = qn.find(g)
        for p in pos:
            anchor = max(0, p - g_start_in_q)
            anchors.add(anchor)
    # 兜底：5-gram 全无命中 → 用 partial_ratio 在整个 bib 文本里扫一遍
    # 数据量 ~20-200 万字符，RapidFuzz 是 SIMD 加速 + score_cutoff 过滤，几十到几百毫秒
    if not anchors and len(qn) >= 6:
        al = fuzz.partial_ratio_alignment(qn, idx.text_norm, score_cutoff=55.0)
        if al is not None and al.score >= 55:
            return _make_evidence(idx, al.dest_start, al.dest_end, score=al.score)
        return None
    if not anchors:
        return None
    anchors_sorted = sorted(anchors)[:60]
    # 在 anchor 附近用多档窗口大小，取 Levenshtein 比例最高者。
    # 关键：对长引文（≥ 12 字）额外做"5-gram 子序列覆盖率"测算 — 处理"原文+注疏混排"
    # 即 src 字符在 dest 中按序出现但被注疏文字割裂的情况（partial_ratio 会低估）。
    best_score = -1.0
    best_start = best_end = 0
    qlen = len(qn)
    cap = 60.0
    long_quote = qlen >= 12  # 短引文不做 LCS 兜底（避免假阳性）
    for a in anchors_sorted:
        for mult in (1.0, 1.3, 1.6):
            wlen = int(qlen * mult) + 4
            for shift in (-4, 0, 4):
                s = max(0, a + shift)
                e = min(len(idx.text_norm), s + wlen)
                if e - s < qlen - 2:
                    continue
                window = idx.text_norm[s:e]
                score = fuzz.ratio(qn, window, score_cutoff=cap)
                if score > best_score:
                    best_score = score
                    al = fuzz.partial_ratio_alignment(qn, window)
                    if al is None:
                        best_start, best_end = s, e
                    else:
                        best_start = s + al.dest_start
                        best_end = s + al.dest_end
                    cap = best_score - 1
        if long_quote and best_score < 95:
            # 用更大窗口测 LCS 覆盖率（src 是否作为子序列存在于 dest 中）
            big_e = min(len(idx.text_norm), a + qlen * 4)
            big_window = idx.text_norm[a : big_e]
            indel_sim = Indel.normalized_similarity(qn, big_window)  # = LCS-based
            # LCS 长度
            lcs_len = (qlen + len(big_window) - Indel.distance(qn, big_window)) / 2
            coverage = lcs_len / qlen if qlen else 0
            if coverage >= 0.95:
                # 原文（大致）完整出现于 dest 中；分数压在 C 区（85-94），由 LLM 兜底判定
                lcs_score = 85 + min(9, int(coverage * 10) - 9)
                if lcs_score > best_score:
                    best_score = lcs_score
                    # 边界：找到 src 在 dest 中的"覆盖区间" — 简化为 anchor 到包含最后一个 src char 的 dest 位置
                    best_start = a
                    best_end = min(big_e, a + int(qlen * 2.5))
    if best_score < 60:
        return None
    return _make_evidence(idx, best_start, best_end, score=best_score)


# ---------- Stage 3: 字符 diff + 分级 ----------

def _stage_diff(
    citations: list[Citation],
    exact: list[MatchEvidence | None],
    fuzzy: list[MatchEvidence | None],
    corpus: Corpus,
) -> list[Verdict]:
    verdicts: list[Verdict] = []
    for c, ev_exact, ev_fuzzy in zip(citations, exact, fuzzy):
        ev = ev_exact or ev_fuzzy
        if ev is None:
            verdicts.append(Verdict(citation=c, grade="D", evidence=None,
                                    issues=["未在任何参考文献中找到"],
                                    needs_llm=True, confidence=0.0))
            continue
        # 字符级 diff（在归一化空间做，避免被标点/异体字干扰）
        bib_idx = _find_bib(corpus, ev.bib_id)
        if bib_idx is None:
            verdicts.append(Verdict(citation=c, grade="D", evidence=ev,
                                    issues=["内部错误：bib_id 找不到"], needs_llm=True))
            continue
        norm_window = bib_idx.text_norm[
            _raw_to_norm(bib_idx, ev.raw_offset_start) : _raw_to_norm(bib_idx, ev.raw_offset_end)
        ]
        ops = Levenshtein.editops(c.quote_norm, norm_window or ev.raw_window)
        ev.edit_ops = [(op.tag, op.src_pos, op.dest_pos) for op in ops]
        score = ev.score
        # 分级
        if score >= 99.5 and not ops:
            grade: Grade = "A"
            issues: list[str] = []
            needs_llm = False
        elif score >= 95:
            grade = "B"
            issues = _classify_diffs(c.quote_norm, norm_window or ev.raw_window, ops)
            # 若全是标点/异体字差异（diff 结果来自异体字源/已被归一化吃掉），实际不会出现
            # 这里 score>=95 但有 ops 说明确实有少量字差
            needs_llm = False
        elif score >= 80:
            grade = "C"
            issues = _classify_diffs(c.quote_norm, norm_window or ev.raw_window, ops)
            needs_llm = True
        else:
            grade = "D"
            issues = ["匹配度过低，疑似错引或未找到正确出处"]
            needs_llm = True
        # 加上章节信息（按 raw_offset 二分查找最近的章节锚点）
        ev.chapter = _chapter_at(bib_idx, ev.raw_offset_start)
        verdicts.append(Verdict(
            citation=c, grade=grade, evidence=ev,
            issues=issues, needs_llm=needs_llm,
            confidence=score / 100.0,
        ))
    return verdicts


def _classify_diffs(quote: str, window: str, ops) -> list[str]:
    issues: list[str] = []
    inserts = sum(1 for op in ops if op.tag == "insert")
    deletes = sum(1 for op in ops if op.tag == "delete")
    replaces = sum(1 for op in ops if op.tag == "replace")
    if deletes:
        issues.append(f"缺字 {deletes}")
    if inserts:
        issues.append(f"多字 {inserts}")
    if replaces:
        issues.append(f"换字 {replaces}")
    return issues


# ---------- 工具 ----------

def _make_evidence(idx: BibIndex, norm_start: int, norm_end: int, score: float) -> MatchEvidence:
    raw_start = _norm_to_raw(idx, norm_start)
    raw_end = _norm_to_raw(idx, norm_end - 1) + 1 if norm_end > norm_start else raw_start
    raw_window = idx.raw[raw_start:raw_end]
    return MatchEvidence(
        bib_id=idx.bib_id,
        chapter=None,  # _stage_diff 里再补
        raw_offset_start=raw_start,
        raw_offset_end=raw_end,
        raw_window=raw_window,
        score=score,
    )


def _norm_to_raw(idx: BibIndex, norm_pos: int) -> int:
    if not idx.norm_to_raw:
        return norm_pos
    if norm_pos >= len(idx.norm_to_raw):
        return len(idx.raw)
    return idx.norm_to_raw[norm_pos]


def _raw_to_norm(idx: BibIndex, raw_pos: int) -> int:
    """raw 偏移 → norm 偏移；二分查找最接近的 norm 索引。"""
    return bisect.bisect_left(idx.norm_to_raw, raw_pos)


def _chapter_at(idx: BibIndex, raw_pos: int) -> str | None:
    if not idx.chapters:
        return None
    offsets = [c[0] for c in idx.chapters]
    j = bisect.bisect_right(offsets, raw_pos) - 1
    if j < 0:
        return None
    return idx.chapters[j][1]


def _find_bib(corpus: Corpus, bib_id: str) -> BibIndex | None:
    for idx in corpus.indexes:
        if idx.bib_id == bib_id:
            return idx
    return None
