"""纯 LLM 引文提取（异步并发流水线）。

架构：
  parse → 段落 → chunking(~1800 字/块) → 异步并发(默认 16) → 每块 LLM JSON 输出
        → 机械落库 (context/source/seq/is_concept) → 全局去重 → list[Citation]

对外公开：
  - extract(doc, *, llm, ...)        async 版本
  - extract_sync(doc, *, llm, ...)   asyncio.run() 包装的同步版本（api.py 用）

不再依赖 core/extract.py（正则层）。本模块负责所有提取逻辑；正则层仅在
api.py 检测到无 LLM key 时作为 fallback。
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Callable

from core.normalize import normalize
from core.types import Citation, Document


# ── 注释段识别 ──

_NOTE_HEADER_RE = re.compile(r"^\s*(注释|注)\s*[:：]?\s*$")
_NOTE_ITEM_RE = re.compile(r"^\s*(?:[(（]?\d+[)）.、]|[①②③④⑤⑥⑦⑧⑨⑩])\s*")
_BOOK_TOKEN_RE = re.compile(r"《([^《》\n]{1,40})》")


def _load_prompt() -> str:
    return (Path(__file__).parent.parent / "prompts" / "extract.txt").read_text(encoding="utf-8")


_SYSTEM_PROMPT = _load_prompt()


# ── Public API ──

ProgressFn = Callable[[dict], None]


async def extract(
    doc: Document,
    *,
    llm,
    concurrency: int = 16,
    chunk_chars: int = 1800,
    overlap: int = 1,
    on_progress: ProgressFn | None = None,
    warnings: list[str] | None = None,
) -> list[Citation]:
    """异步纯 LLM 提取。"""
    if llm is None or not doc.paragraphs:
        return []

    sources = _detect_sources(doc.paragraphs)
    chunks = _chunk_paragraphs(doc.paragraphs, sources, chunk_chars, overlap)

    if on_progress:
        on_progress({"stage": "extract_chunks_planned", "total": len(chunks)})

    results: list[list[dict]] = [[] for _ in chunks]
    sem = asyncio.Semaphore(max(1, concurrency))
    done_count = 0
    lock = asyncio.Lock()

    async def run_one(i: int, chunk: list[tuple[int, str, str]]) -> None:
        nonlocal done_count
        async with sem:
            try:
                results[i] = await _extract_chunk_with_retry(chunk, llm)
            except Exception as e:
                results[i] = []
                if warnings is not None:
                    span = f"段落 {chunk[0][0]}–{chunk[-1][0]}"
                    warnings.append(f"提取失败（{span}）：{e}")
        async with lock:
            done_count += 1
            if on_progress:
                on_progress({
                    "stage": "extract_chunk",
                    "done": done_count,
                    "total": len(chunks),
                    "citations": sum(len(r) for r in results),
                })

    await asyncio.gather(*(run_one(i, c) for i, c in enumerate(chunks)))

    flat: list[dict] = []
    for r in results:
        flat.extend(r)
    return _build_citations(flat, doc.paragraphs, sources)


def extract_sync(
    doc: Document,
    *,
    llm=None,
    concurrency: int = 16,
    chunk_chars: int = 1800,
    overlap: int = 1,
    on_progress: ProgressFn | None = None,
    warnings: list[str] | None = None,
) -> list[Citation]:
    """同步包装。如未传 llm，从 DEEPSEEK_API_KEY/SILICONFLOW_API_KEY 创建默认客户端。"""
    if llm is None:
        llm = _get_default_llm()
    if llm is None:
        return []
    return asyncio.run(extract(
        doc,
        llm=llm,
        concurrency=concurrency,
        chunk_chars=chunk_chars,
        overlap=overlap,
        on_progress=on_progress,
        warnings=warnings,
    ))


# ── 默认 LLM ──

def _get_default_llm():
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("SILICONFLOW_API_KEY")
    if not key:
        return None
    try:
        from core.llm import LLMClient
        return LLMClient(api_key=key)
    except Exception:
        return None


# ── Source detection ──

def _detect_sources(paragraphs: list[str]) -> list[str]:
    sources: list[str] = []
    in_note = False
    for para in paragraphs:
        if _NOTE_HEADER_RE.match(para):
            in_note = True
            sources.append("note")
            continue
        if in_note and len(para) < 30 and "“" not in para and not _NOTE_ITEM_RE.match(para):
            in_note = False
        sources.append("note" if (in_note or _NOTE_ITEM_RE.match(para)) else "body")
    return sources


# ── Chunking ──

def _chunk_paragraphs(
    paragraphs: list[str],
    sources: list[str],
    chunk_chars: int,
    overlap: int,
) -> list[list[tuple[int, str, str]]]:
    """按字符预算聚段。每条 chunk item: (para_index, source, text)。"""
    chunks: list[list[tuple[int, str, str]]] = []
    cur: list[tuple[int, str, str]] = []
    cur_len = 0
    for i, (p, s) in enumerate(zip(paragraphs, sources)):
        if cur and cur_len + len(p) > chunk_chars:
            chunks.append(cur)
            # 重叠：把上一个 chunk 的最后 `overlap` 段塞入新 chunk 头部
            tail = cur[-overlap:] if overlap > 0 else []
            cur = list(tail)
            cur_len = sum(len(t) for _, _, t in cur)
        cur.append((i, s, p))
        cur_len += len(p)
    if cur:
        chunks.append(cur)
    return chunks


# ── Per-chunk extraction with retry ──

async def _extract_chunk_with_retry(
    chunk: list[tuple[int, str, str]],
    llm,
    *,
    max_retries: int = 2,
) -> list[dict]:
    """单 chunk 异步提取 + 指数退避重试。"""
    backoffs = [1.0, 4.0]
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await _extract_chunk(chunk, llm)
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                wait = backoffs[min(attempt, len(backoffs) - 1)]
                # 429 限流：尝试读 Retry-After
                ra = _extract_retry_after(e)
                if ra is not None:
                    wait = max(wait, ra)
                await asyncio.sleep(wait)
    raise RuntimeError(f"chunk 提取失败：{last_err}")


def _extract_retry_after(err: Exception) -> float | None:
    msg = str(err)
    m = re.search(r"retry[- ]after['\"]?\s*[:=]\s*['\"]?(\d+)", msg, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


async def _extract_chunk(chunk: list[tuple[int, str, str]], llm) -> list[dict]:
    user = _format_chunk(chunk)
    raw = await llm.async_chat_json(_SYSTEM_PROMPT, user)
    items = _normalize_response(raw)
    para_lookup: dict[int, str] = {pi: text for pi, _src, text in chunk}
    valid: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        quote = item.get("quote")
        if not isinstance(quote, str):
            continue
        quote = quote.strip().strip("“”\"'「」『』 ")
        if len(quote) < 2:
            continue
        pi = _locate_paragraph(quote, para_lookup)
        if pi is None:
            continue
        valid.append({
            "pi": pi,
            "quote": quote,
            "context_llm": _str_or_empty(item.get("context")),
            "annotation": _str_or_empty(item.get("annotation")),
        })
    return valid


def _format_chunk(chunk: list[tuple[int, str, str]]) -> str:
    parts: list[str] = []
    for pi, src, text in chunk:
        tag = "正文" if src == "body" else "注释"
        parts.append(f"[段落{pi}·{tag}]\n{text}")
    return "\n\n".join(parts)


def _normalize_response(raw) -> list:
    """LLM 返回可能是 list / 包了一层 dict / flat 单对象 / 包装格式。统一转 list。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for k in ("citations", "results", "data", "items"):
            if k in raw and isinstance(raw[k], list):
                return raw[k]
        if "quote" in raw:
            return [raw]
        for v in raw.values():
            if isinstance(v, list):
                return v
    return []


def _locate_paragraph(quote: str, para_lookup: dict[int, str]) -> int | None:
    for pi, text in para_lookup.items():
        if quote in text:
            return pi
    qn = normalize(quote)
    if not qn:
        return None
    for pi, text in para_lookup.items():
        if qn in normalize(text):
            return pi
    return None


def _str_or_empty(v) -> str:
    return v if isinstance(v, str) else ""


# ── Build Citation objects ──

def _build_citations(
    raw: list[dict],
    paragraphs: list[str],
    sources: list[str],
) -> list[Citation]:
    out: list[Citation] = []
    seen: set[tuple[int, str]] = set()
    for r in raw:
        pi = r["pi"]
        quote = r["quote"]
        if pi >= len(paragraphs):
            continue
        norm = normalize(quote)
        if len(norm) < 2:
            continue
        key = (pi, norm)
        if key in seen:
            continue
        seen.add(key)

        para = paragraphs[pi]
        ctx_mech = _build_context_around(para, quote, paragraphs, pi, window=100)
        annotation = r.get("annotation", "")
        ctx = ctx_mech + (f"【作者解读】{annotation}" if annotation else "")

        source = sources[pi] if pi < len(sources) else "body"
        book = _infer_book_from_text(annotation) or _infer_book_from_text(r.get("context_llm", ""))
        # 进一步 fallback：在原段落中找紧邻 quote 前的《X》
        if not book:
            book = _infer_book_near_quote(para, quote)
        is_concept = source == "body" and book is None and len(norm) <= 4

        out.append(Citation(
            quote=quote,
            quote_norm=norm,
            context=ctx,
            location=f"{source}[{pi}]",
            source=source,
            book_hint=book,
            chapter_hint=None,
            seq=pi,
            is_concept=is_concept,
        ))
    out.sort(key=lambda c: (c.seq, c.quote_norm))
    return out


def _build_context_around(
    para: str,
    quote: str,
    paragraphs: list[str],
    pi: int,
    *,
    window: int = 100,
) -> str:
    idx = para.find(quote)
    if idx < 0:
        head = para[:window]
        tail = para[-window:] if len(para) > window else ""
        return f"{head}〖{quote}〗{tail}"
    left = para[max(0, idx - window):idx]
    right = para[idx + len(quote):idx + len(quote) + window]
    if len(left) < window * 0.8 and pi > 0:
        prev = paragraphs[pi - 1]
        gap = max(0, int(window * 0.8) - len(left))
        if gap > 0:
            left = prev[-gap:] + " ⏎ " + left
    if len(right) < window * 0.8 and pi + 1 < len(paragraphs):
        nxt = paragraphs[pi + 1]
        gap = max(0, int(window * 0.8) - len(right))
        if gap > 0:
            right = right + " ⏎ " + nxt[:gap]
    return f"{left}〖{quote}〗{right}"


def _infer_book_from_text(text: str) -> str | None:
    if not text:
        return None
    m = _BOOK_TOKEN_RE.search(text)
    return m.group(1) if m else None


def _infer_book_near_quote(para: str, quote: str) -> str | None:
    idx = para.find(quote)
    if idx < 0:
        return _infer_book_from_text(para)
    left = para[max(0, idx - 80):idx]
    right = para[idx + len(quote):idx + len(quote) + 40]
    return _infer_book_from_text(left) or _infer_book_from_text(right)
