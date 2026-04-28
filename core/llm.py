"""LLM 客户端 + 语义判定阶段。

默认走 DeepSeek 官方 API（OpenAI 兼容协议）。封装在本模块后，未来切换 provider 只需
改本模块。

提供两套调用接口：
  - chat_json(system, user) — 同步，judge 阶段用
  - async_chat_json(system, user) — 异步，extract 阶段并发用
"""
from __future__ import annotations

import concurrent.futures as _cf
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.types import Verdict


@dataclass
class LLMClient:
    api_key: str
    model: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com/v1"
    timeout: float = 45.0
    max_retries: int = 1

    _client: Any = None
    _async_client: Any = None

    def __post_init__(self) -> None:
        from openai import OpenAI, AsyncOpenAI
        # max_retries=0 关掉 SDK 内置重试，避免一次调用叠加多层指数退避（实测上游 500
        # 时单批可被拖到 480s）。控制权交给本类的外层 max_retries。
        self._client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=0,
        )
        self._async_client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=0,
        )

    def chat_json(self, system: str, user: str) -> dict | list:
        """同步：调一次 chat completion，要求 JSON 输出。失败抛异常。"""
        import time as _t
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=0.1,
                )
                content = resp.choices[0].message.content or "{}"
                return _extract_json(content)
            except Exception as e:
                last_err = e
                if attempt < self.max_retries:
                    _t.sleep(1.0)
        raise RuntimeError(f"LLM 调用失败：{last_err}")

    async def async_chat_json(self, system: str, user: str) -> dict | list:
        """异步：调一次 chat completion。失败抛异常，由调用方决定重试策略。"""
        resp = await self._async_client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.1,
        )
        content = resp.choices[0].message.content or "{}"
        return _extract_json(content)

    async def aclose(self) -> None:
        """关闭异步客户端的连接池。"""
        if self._async_client is not None:
            await self._async_client.close()


def _extract_json(content: str) -> dict | list:
    """优先按 JSON 直接解析；失败时尝试从 ```json fence 中抽取。"""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"```(?:json)?\s*(.+?)\s*```", content, re.DOTALL)
        if m:
            return json.loads(m.group(1))
        m = re.search(r"(\{.*\}|\[.*\])", content, re.DOTALL)
        if m:
            return json.loads(m.group(1))
        raise


# ---------- 批量判定 ----------

def _load_judge_prompt() -> str:
    return (Path(__file__).parent.parent / "prompts" / "judge.txt").read_text(encoding="utf-8")

_SYSTEM_PROMPT = _load_judge_prompt()


def judge_batch(
    verdicts: list[Verdict],
    corpus,
    llm: LLMClient,
    *,
    batch_size: int = 8,
    max_workers: int = 4,
    on_progress=None,
) -> None:
    """对 needs_llm=True 的 Verdict 批量调用 LLM，原地更新其 grade/issues/suggestion。"""
    targets: list[Verdict] = [v for v in verdicts if v.needs_llm]
    if not targets:
        return
    if on_progress:
        on_progress({"stage": "llm_start", "count": len(targets)})
    batches = [targets[i : i + batch_size] for i in range(0, len(targets), batch_size)]
    done = 0
    with _cf.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = [pool.submit(_judge_one_batch, batch, llm) for batch in batches]
        for fut in _cf.as_completed(futs):
            try:
                fut.result()  # _judge_one_batch 内部已就地修改 Verdict
            except Exception as e:
                # 该批失败：留 issue 标记，等用户手工复核
                # （无法精确知道是哪一批，遍历全部 needs_llm=True 还在的，加一条 issue）
                for v in targets:
                    if v.needs_llm:
                        v.issues.append(f"LLM 失败: {e}")
                        v.needs_llm = False
            done += 1
            if on_progress:
                on_progress({"stage": "llm_progress", "done": done, "total": len(batches)})
    if on_progress:
        on_progress({"stage": "llm_done"})


def _normalize_batch_response(parsed: dict | list) -> list:
    """将 DeepSeek json_object 模式的多种返回格式统一为 list[dict]。

    可能的格式：
      - [{"id":0,...}]        已正确，直接返回
      - {"id":0,"verdict":...}  flat 单对象 → 包成 [obj]
      - {"results":[...],...}  包装格式 → 提取列表
    """
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        # 包装格式：尝试常见 wrapper key
        for k in ("results", "verdicts", "data", "items"):
            if k in parsed and isinstance(parsed[k], list):
                return parsed[k]
        # flat 单对象：{"id": 0, "verdict": "FAITHFUL", ...}
        if "id" in parsed and "verdict" in parsed:
            return [parsed]
        # 其他 dict：查任意值为 list 的 key
        for v in parsed.values():
            if isinstance(v, list):
                return v
        # 退路：整个 dict 包一层
        return [parsed]
    return []


def _judge_one_batch(batch: list[Verdict], llm: LLMClient) -> None:
    """对一个 batch 调用 LLM 并就地更新 batch 中每条 Verdict。"""
    payload = []
    for i, v in enumerate(batch):
        item = {
            "id": i,
            "quote": v.citation.quote,
            "context": _trim(v.citation.context, 200),
            "book_hint": v.citation.book_hint or "",
        }
        if v.evidence:
            ev = v.evidence
            item["matched_passage"] = _trim(ev.raw_window, 200)
            item["matched_book"] = ev.bib_id
            item["matched_chapter"] = ev.chapter or ""
            item["score"] = round(ev.score, 1)
        else:
            item["matched_passage"] = ""
            item["matched_book"] = ""
            item["matched_chapter"] = ""
            item["score"] = 0
        payload.append(item)
    user = f"请判定下列 {len(payload)} 条引文：\n" + json.dumps(payload, ensure_ascii=False)
    parsed = llm.chat_json(_SYSTEM_PROMPT, user)
    # json_object 模式下 DeepSeek 可能返回多种格式；统一归一化为 list[dict]
    parsed = _normalize_batch_response(parsed)
    if not isinstance(parsed, list):
        raise RuntimeError(f"LLM 返回不是数组：{type(parsed).__name__}")
    for r in parsed:
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        if not isinstance(rid, int) or rid < 0 or rid >= len(batch):
            continue
        _apply_llm_result(batch[rid], r)


def _apply_llm_result(v: Verdict, r: dict) -> None:
    verdict = (r.get("verdict") or "").upper()
    issues = list(r.get("issues") or [])
    suggestion = r.get("suggestion") or None
    if verdict == "FAITHFUL":
        v.grade = "A"
        v.issues = issues if issues else []
    elif verdict == "VARIANT_OK":
        v.grade = "B"
        v.issues = issues
    elif verdict == "PARAPHRASE":
        v.grade = "B"
        v.issues = ["意译/缩写"] + issues
    elif verdict == "MISCITED":
        v.grade = "C"
        v.issues = issues
    elif verdict == "NOT_FOUND":
        v.grade = "D"
        v.issues = issues if issues else ["未找到对应原文"]
    else:
        v.issues.append(f"LLM 返回未知 verdict: {verdict}")
    if suggestion:
        v.suggestion = suggestion
    v.needs_llm = False


def _trim(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[: n // 2] + " … " + text[-n // 2 :]
