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

from core.api import check
from core.report import render


def main() -> int:
    p = argparse.ArgumentParser(prog="heimao", description="黑猫核校 — 中文古籍引文核校")
    sub = p.add_subparsers(dest="cmd", required=True)
    pc = sub.add_parser("check", help="核校书稿")
    pc.add_argument("manuscript", help="书稿路径（docx/txt/pdf）")
    pc.add_argument("--bib", nargs="+", required=True, help="参考文献路径列表")
    pc.add_argument("--out", default=None, help="输出文件路径（不指定则打印到 stdout）")
    pc.add_argument("--fmt", default="md", choices=["md", "html", "json"])
    pc.add_argument("--no-llm", action="store_true", help="跳过 LLM 阶段（只输出字面层判定）")
    args = p.parse_args()
    if args.cmd != "check":
        p.print_help()
        return 1

    bibs = []
    for pattern in args.bib:
        if "*" in pattern or "?" in pattern:
            bibs.extend(str(x) for x in sorted(Path().glob(pattern)))
        else:
            bibs.append(pattern)

    last = [time.time()]

    def on_progress(payload: dict) -> None:
        now = time.time()
        stage = payload.get("stage", "")
        extra = " ".join(f"{k}={v}" for k, v in payload.items() if k != "stage")
        print(f"[{now - last[0]:5.2f}s] {stage} {extra}", file=sys.stderr)
        last[0] = now

    report = check(
        args.manuscript,
        bibs,
        llm_key="" if args.no_llm else None,  # --no-llm 强制跳过；None 从 SILICONFLOW_API_KEY 环境变量读
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
