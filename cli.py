"""黑猫核校 CLI 薄壳。

用法:
  uv run python cli.py check <manuscript> --bib bib/*.txt [--out report.md] [--fmt md|html|json]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


def _load_env_local() -> None:
    """加载 env.local 中的 KEY=VALUE 行到 os.environ（不覆盖已存在的）。"""
    p = Path(__file__).parent / "env.local"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_env_local()

from core.api import check  # noqa: E402  必须在 env 加载后导入
from core.report import render  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(prog="heimao", description="黑猫核校 — 中文古籍引文核校")
    sub = p.add_subparsers(dest="cmd", required=True)
    pc = sub.add_parser("check", help="核校书稿")
    pc.add_argument("manuscript", help="书稿路径（docx/txt/pdf）")
    pc.add_argument("--bib", nargs="+", required=True, help="参考文献路径列表")
    pc.add_argument("--out", default=None, help="输出文件路径（不指定则打印到 stdout）")
    pc.add_argument("--fmt", default="md", choices=["md", "html", "json"])
    pc.add_argument("--no-llm", action="store_true", help="跳过 LLM 阶段（只输出字面层判定）")
    pc.add_argument("--extract-mode", default="llm", choices=["regex", "llm"], help="引文提取模式（默认 llm）")
    pc.add_argument("--concurrency", type=int, default=16, help="LLM 提取阶段并发数（默认 16）")
    pc.add_argument("--chunk-chars", type=int, default=1800, help="每个 chunk 的字符预算（默认 1800）")
    args = p.parse_args()
    if args.cmd != "check":
        p.print_help()
        return 1

    bibs = []
    for pattern in args.bib:
        p = Path(pattern)
        if "*" in pattern or "?" in pattern:
            if p.is_absolute():
                bibs.extend(str(x) for x in sorted(p.parent.glob(p.name)))
            else:
                bibs.extend(str(x) for x in sorted(Path().glob(pattern)))
        else:
            bibs.append(pattern)

    last = [time.time()]
    chunk_state = {"last_emit": 0.0}

    def on_progress(payload: dict) -> None:
        now = time.time()
        stage = payload.get("stage", "")
        # 高频 chunk 进度做节流：每 0.5s 至多打印一次，最后一条强制打印
        if stage == "extract_chunk":
            done = payload.get("done", 0)
            total = payload.get("total", 0)
            citations = payload.get("citations", 0)
            if done < total and now - chunk_state["last_emit"] < 0.5:
                return
            chunk_state["last_emit"] = now
            print(
                f"[{now - last[0]:5.2f}s] extract_chunk [{done}/{total}] +{citations}",
                file=sys.stderr,
            )
            last[0] = now
            return
        extra = " ".join(f"{k}={v}" for k, v in payload.items() if k != "stage")
        print(f"[{now - last[0]:5.2f}s] {stage} {extra}", file=sys.stderr)
        last[0] = now

    report = check(
        args.manuscript,
        bibs,
        llm_key="" if args.no_llm else None,
        extract_mode=args.extract_mode,
        concurrency=args.concurrency,
        chunk_chars=args.chunk_chars,
        on_progress=on_progress,
    )

    body = render(report.verdicts, fmt=args.fmt, warnings=report.warnings)
    if args.out:
        Path(args.out).write_bytes(body)
        s = report.summary
        print(
            f"\n报告写入：{args.out}　"
            f"总 {s['total']}（概念 {s['concept']}，需核校 {s['real']}）　"
            f"A {s['A']}　B {s['B']}　C {s['C']}　D {s['D']}",
            file=sys.stderr,
        )
        if report.timings:
            tot = sum(report.timings.values())
            parts = "　".join(f"{k} {v:.2f}s" for k, v in report.timings.items())
            print(f"耗时：{parts}　合计 {tot:.2f}s", file=sys.stderr)
    else:
        sys.stdout.buffer.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
