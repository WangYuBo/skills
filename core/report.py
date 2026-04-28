"""报告渲染。

render(verdicts, fmt) -> bytes。fmt ∈ {"json","md","html"}。同一份数据三种渲染。

设计：renderer 仅消费 Verdict + Citation 字段，不依赖任何索引/原文加载——所以可
以独立于 corpus 重渲染（用户改了输出格式不需要重跑匹配）。
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, is_dataclass
from typing import Iterable

from core.types import Verdict


GRADE_LABEL = {
    "A": "✅ 完全一致",
    "B": "🟡 轻微差异（不影响意义）",
    "C": "⚠ 文字或意义可能有问题",
    "D": "❌ 无法定位来源 / 疑似错引",
}


def render(verdicts: list[Verdict], fmt: str = "md", *, warnings: list[str] | None = None) -> bytes:
    fmt = fmt.lower()
    if fmt == "json":
        return _render_json(verdicts, warnings or [])
    if fmt == "md":
        return _render_md(verdicts, warnings or [])
    if fmt == "html":
        return _render_html(verdicts, warnings or [])
    raise ValueError(f"unsupported fmt: {fmt}")


def _render_json(verdicts: list[Verdict], warnings: list[str]) -> bytes:
    payload = {
        "summary": _summary(verdicts),
        "warnings": warnings,
        "verdicts": [_verdict_to_dict(v) for v in verdicts],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _verdict_to_dict(v: Verdict) -> dict:
    d = {
        "grade": v.grade,
        "grade_label": GRADE_LABEL[v.grade],
        "confidence": round(v.confidence, 3),
        "issues": v.issues,
        "suggestion": v.suggestion,
        "citation": {
            "quote": v.citation.quote,
            "location": v.citation.location,
            "source": v.citation.source,
            "book_hint": v.citation.book_hint,
            "chapter_hint": v.citation.chapter_hint,
            "is_concept": v.citation.is_concept,
            "context": v.citation.context,
        },
    }
    if v.evidence:
        d["evidence"] = {
            "bib_id": v.evidence.bib_id,
            "chapter": v.evidence.chapter,
            "score": round(v.evidence.score, 2),
            "raw_window": v.evidence.raw_window,
            "raw_offset_start": v.evidence.raw_offset_start,
            "raw_offset_end": v.evidence.raw_offset_end,
        }
    return d


def _render_md(verdicts: list[Verdict], warnings: list[str]) -> bytes:
    lines: list[str] = []
    s = _summary(verdicts)
    lines.append("# 黑猫核校报告")
    lines.append("")
    lines.append(f"- 引文总数：{s['total']}（其中概念词/强调引号：{s['concept']}，需核校：{s['real']}）")
    lines.append(f"- A 完全一致：{s['A']}　B 轻微差异：{s['B']}　C 可能有问题：{s['C']}　D 无法定位/疑似错引：{s['D']}")
    if warnings:
        lines.append("")
        lines.append("## ⚠ 警告")
        for w in warnings:
            lines.append(f"- {w}")
    # 按 grade 优先排序：D > C > B > A，让需要关注的在前面
    order = {"D": 0, "C": 1, "B": 2, "A": 3}
    sorted_v = sorted(verdicts, key=lambda v: (order[v.grade], v.citation.seq))
    for grade in ("D", "C", "B", "A"):
        items = [v for v in sorted_v if v.grade == grade]
        if not items:
            continue
        lines.append("")
        lines.append(f"## {GRADE_LABEL[grade]}（{len(items)} 条）")
        for v in items:
            lines.append("")
            lines.append(_md_one(v))
    return "\n".join(lines).encode("utf-8")


def _md_one(v: Verdict) -> str:
    c = v.citation
    out: list[str] = []
    head = f"### [{c.location}] {c.source}"
    if c.book_hint:
        head += f"　·　《{c.book_hint}》"
    if c.chapter_hint:
        head += f"　{c.chapter_hint}"
    out.append(head)
    out.append(f"- 引文：`{c.quote}`")
    if v.evidence:
        ev = v.evidence
        out.append(f"- 命中：《{_short(ev.bib_id)}》" + (f"　{ev.chapter}" if ev.chapter else ""))
        out.append(f"- 原文：`{ev.raw_window}`")
        out.append(f"- 匹配度：{ev.score:.1f}")
    if v.issues:
        out.append(f"- 问题：{'；'.join(v.issues)}")
    if v.suggestion:
        out.append(f"- 建议：{v.suggestion}")
    if c.context:
        out.append(f"- 上下文：{c.context}")
    return "\n".join(out)


def _render_html(verdicts: list[Verdict], warnings: list[str]) -> bytes:
    s = _summary(verdicts)
    rows: list[str] = []
    order = {"D": 0, "C": 1, "B": 2, "A": 3}
    sorted_v = sorted(verdicts, key=lambda v: (order[v.grade], v.citation.seq))
    for v in sorted_v:
        c = v.citation
        ev = v.evidence
        bib = _short(ev.bib_id) if ev else ""
        chap = ev.chapter or "" if ev else ""
        score = f"{ev.score:.1f}" if ev else ""
        raw = ev.raw_window if ev else ""
        issues = "；".join(v.issues)
        rows.append(
            f"<tr class='g-{v.grade}'>"
            f"<td>{v.grade}</td>"
            f"<td>{_h(c.location)}</td>"
            f"<td><code>{_h(c.quote)}</code></td>"
            f"<td>{_h(bib)}<br><small>{_h(chap)}</small></td>"
            f"<td><code>{_h(raw)}</code></td>"
            f"<td>{score}</td>"
            f"<td>{_h(issues)}</td>"
            f"<td>{_h(v.suggestion or '')}</td>"
            f"</tr>"
        )
    css = """
    body { font-family: -apple-system, "Segoe UI", "PingFang SC", sans-serif; padding: 20px; max-width: 1400px; margin: 0 auto; }
    h1 { border-bottom: 2px solid #333; padding-bottom: 8px; }
    .summary { background: #f0f4f8; padding: 12px 16px; border-radius: 6px; margin: 16px 0; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 6px 10px; border-bottom: 1px solid #e0e0e0; vertical-align: top; }
    th { background: #f7f7f7; text-align: left; }
    tr.g-A { background: #f0fff4; }
    tr.g-B { background: #fffbe6; }
    tr.g-C { background: #fff4e6; }
    tr.g-D { background: #ffece8; }
    code { font-family: "SF Mono", Consolas, monospace; background: rgba(0,0,0,0.04); padding: 1px 4px; border-radius: 3px; }
    """
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>黑猫核校报告</title>
<style>{css}</style></head><body>
<h1>黑猫核校报告</h1>
<div class="summary">
  引文总数：<b>{s['total']}</b>（概念词 {s['concept']}　需核校 {s['real']}）<br>
  A 完全一致 <b>{s['A']}</b>
  B 轻微差异 <b>{s['B']}</b>
  C 可能有问题 <b>{s['C']}</b>
  D 无法定位/疑似错引 <b>{s['D']}</b>
</div>
{('<h2>⚠ 警告</h2><ul>' + ''.join(f'<li>{_h(w)}</li>' for w in warnings) + '</ul>') if warnings else ''}
<table>
  <thead><tr>
    <th>等级</th><th>位置</th><th>引文</th><th>命中文献</th><th>原文</th><th>匹配度</th><th>问题</th><th>建议</th>
  </tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
</body></html>"""
    return html.encode("utf-8")


def _summary(verdicts: list[Verdict]) -> dict:
    grades = Counter(v.grade for v in verdicts)
    concept = sum(1 for v in verdicts if v.citation.is_concept)
    return {
        "total": len(verdicts),
        "concept": concept,
        "real": len(verdicts) - concept,
        "A": grades.get("A", 0),
        "B": grades.get("B", 0),
        "C": grades.get("C", 0),
        "D": grades.get("D", 0),
    }


def _short(bib_id: str) -> str:
    """把 'Dao De Jing Wang Bi Zhu Ben - Wang Bi' 缩成短书名（阅读方便）。"""
    table = [
        ("Dao De Jing", "道德经（王弼注）"),
        ("Lun Yu", "论语（杨伯峻译注）"),
        ("Meng Zi", "孟子（杨伯峻译注）"),
        ("Shi Jing", "诗经（周振甫译注）"),
        ("Sun Zi Bing Fa", "孙子兵法（陈曦译注）"),
        ("Zhou Yi", "周易（南怀瑾译注）"),
        ("Zhuang Zi", "庄子"),
        ("Huang Di Nei Jing", "黄帝内经"),
        ("Da Xue Zhong Yong", "大学中庸"),
    ]
    for k, v in table:
        if k in bib_id:
            return v
    return bib_id


def _h(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
