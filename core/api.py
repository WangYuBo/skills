"""黑猫核校 — 唯一对外入口。

`check()` 是整个系统对外暴露的全部 API。CLI 与 Web 共用同一个函数。

设计：
  - 永不抛异常。所有失败收集到 Report.warnings，调用方拿到的永远是合法 Report。
  - on_progress 是单一回调，CLI 用它打进度条，Web 用它推 SSE。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core.extract import Citation, extract
from core.index import Corpus, build_corpus
from core.match import Verdict, match
from core.parse import parse


ProgressFn = Callable[[dict], None]


@dataclass
class Report:
    verdicts: list[Verdict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)  # 阶段耗时（秒）

    @property
    def summary(self) -> dict:
        from core.report import _summary
        return _summary(self.verdicts)


def check(
    manuscript: str | Path,
    bibs: list[str | Path],
    *,
    llm_key: str | None = None,
    llm_model: str = "deepseek-ai/DeepSeek-V4-Flash",
    llm_base_url: str = "https://api.siliconflow.cn/v1",
    on_progress: ProgressFn | None = None,
) -> Report:
    """核校书稿引文，永不抛异常。

    Parameters
    ----------
    manuscript : 书稿路径（docx/txt/pdf）
    bibs       : 参考文献路径列表（txt/docx/pdf）
    llm_key    : SiliconFlow API key；不传则从 SILICONFLOW_API_KEY 环境变量读取；
                 仍无则跳过 LLM 阶段，只输出字面层判定（C/D 不会有 LLM 修正）
    llm_model  : 默认 deepseek-ai/DeepSeek-V4-Flash
    on_progress: 回调，参数为 {"stage": str, ...}
    """
    report = Report()
    progress = on_progress or _noop

    # Stage 1: 解析书稿
    t0 = time.time()
    try:
        progress({"stage": "parse_start", "path": str(manuscript)})
        doc = parse(manuscript)
    except Exception as e:
        report.warnings.append(f"解析书稿失败：{e}")
        return report
    report.timings["parse"] = time.time() - t0
    progress({"stage": "parse_done", "chars": doc.length})

    # Stage 2: 引文抽取
    t0 = time.time()
    try:
        citations = extract(doc)
    except Exception as e:
        report.warnings.append(f"引文抽取失败：{e}")
        return report
    report.timings["extract"] = time.time() - t0
    progress({"stage": "extract_done", "count": len(citations)})

    # Stage 3: bib 索引（部分失败不致命）
    t0 = time.time()
    valid_bibs = []
    for b in bibs:
        if not Path(b).exists():
            report.warnings.append(f"参考文献不存在：{b}")
            continue
        valid_bibs.append(b)
    if not valid_bibs:
        report.warnings.append("没有可用的参考文献")
        return report
    try:
        corpus = build_corpus(valid_bibs)
    except Exception as e:
        report.warnings.append(f"构建索引失败：{e}")
        return report
    report.timings["index"] = time.time() - t0
    progress({"stage": "index_done", "bibs": len(corpus.indexes)})

    # Stage 4: 匹配（含 LLM 阶段）
    llm = _make_llm(llm_key, llm_model, llm_base_url, report.warnings)
    t0 = time.time()
    try:
        verdicts = match(citations, corpus, llm=llm, on_progress=progress)
    except Exception as e:
        report.warnings.append(f"匹配失败：{e}")
        return report
    report.timings["match"] = time.time() - t0
    report.verdicts = verdicts
    progress({"stage": "done", "verdicts": len(verdicts)})
    return report


def _make_llm(key: str | None, model: str, base_url: str, warnings: list[str]):
    """创建 LLM 客户端；环境变量未设置或 SDK 未安装时返回 None（跳过 LLM 阶段）。"""
    if key == "":  # 显式跳过（--no-llm）
        return None
    key = key or os.environ.get("SILICONFLOW_API_KEY")
    if not key:
        return None
    try:
        from core.llm import LLMClient
        return LLMClient(api_key=key, model=model, base_url=base_url)
    except Exception as e:
        warnings.append(f"LLM 客户端创建失败：{e}（已跳过语义判定）")
        return None


def _noop(_payload: dict) -> None:
    pass
