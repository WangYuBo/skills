"""将 report-full.md 转换为 CSV 文件。"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

INPUT = Path(__file__).parent / "report-full.md"
OUTPUT = Path(__file__).parent / "report-full.csv"

GRADE_MAP = {
    "❌ 无法定位来源 / 疑似错引": "D",
    "⚠ 文字或意义可能有问题": "C",
    "🟡 轻微差异（不影响意义）": "B",
    "✅ 完全一致": "A",
}


def parse_report(text: str) -> list[dict]:
    entries: list[dict] = []
    current_grade = ""
    current: dict | None = None

    lines = text.splitlines()

    for line in lines:
        # Detect grade section
        for key, grade in GRADE_MAP.items():
            if key in line:
                current_grade = grade
                break
        else:
            # entry header: ### [location] type　·　《source》
            m = re.match(r"^###\s+\[(.+?)\]\s+(\S+).*?《(.+)》$", line)
            if m:
                if current:
                    entries.append(current)
                current = {
                    "等级": current_grade,
                    "位置": m.group(1),
                    "类型": m.group(2),
                    "出处声明": m.group(3).strip(),
                    "引文": "",
                    "命中文献": "",
                    "命中章节": "",
                    "原文": "",
                    "匹配度": "",
                    "问题": "",
                    "建议": "",
                    "上下文": "",
                }
                continue

        if current is None:
            continue

        # 引文
        m = re.match(r"^- 引文[：:]\s*`(.+)`$", line)
        if m:
            current["引文"] = m.group(1)
            continue

        # 命中
        m = re.match(r"^- 命中[：:]\s*《(.+?)》\s*(.*)$", line)
        if m:
            current["命中文献"] = m.group(1).strip()
            current["命中章节"] = m.group(2).strip()
            continue

        # 原文 (start)
        m = re.match(r"^- 原文[：:]\s*`(.+)`$", line)
        if m:
            current["原文"] = m.group(1)
            # Check if this is a multi-line原文 (just the opening backtick)
            continue

        # 原文 continuation or already in 原文 mode — look for backtick-quoted text
        # 原文 can span multiple lines after the `- 原文：` line
        if line.startswith("- 匹配度"):
            m = re.match(r"^- 匹配度[：:]\s*(\d+\.?\d*)", line)
            if m:
                current["匹配度"] = m.group(1)
            continue

        if line.startswith("- 问题"):
            m = re.match(r"^- 问题[：:]\s*(.+)$", line)
            if m:
                current["问题"] = m.group(1)
            continue

        if line.startswith("- 建议"):
            m = re.match(r"^- 建议[：:]\s*(.+)$", line)
            if m:
                current["建议"] = m.group(1)
            continue

        if line.startswith("- 上下文"):
            m = re.match(r"^- 上下文[：:]\s*(.+)$", line)
            if m:
                current["上下文"] = m.group(1)
            continue

        # Handle continuations of multi-line fields
        # After 原文：`... line, subsequent non-key lines are part of 原文
        # After 上下文： line, subsequent non-key lines are part of 上下文
        # After 问题： line, subsequent non-key lines are part of 问题

    # Don't forget the last entry
    if current:
        entries.append(current)

    return entries


def parse_report_v2(text: str) -> list[dict]:
    """State-machine based parser that handles multi-line fields."""
    entries: list[dict] = []
    current_grade = ""
    current: dict | None = None
    # Which multi-line field are we accumulating (if any)
    acc_field: str | None = None

    lines = text.splitlines()

    def flush_entry() -> None:
        nonlocal current
        if current:
            # Trim trailing whitespace/backticks from multi-line fields
            for f in ("原文", "上下文", "问题"):
                val = current[f]
                if val:
                    val = val.rstrip()
                    # remove trailing backtick if present
                    if val.endswith("`"):
                        val = val[:-1]
                    current[f] = val
            entries.append(current)
            current = None

    i = 0
    while i < len(lines):
        line = lines[i]

        # Detect grade section
        for key, grade in GRADE_MAP.items():
            if key in line:
                flush_entry()
                current_grade = grade
                acc_field = None
                break
        else:
            # entry header: two formats
            #   ### [location] type　·　《source》
            #   ### [location] type
            m = re.match(r"^###\s+\[(.+?)\]\s+(\S+)(?:.*?《(.+?)》)?$", line)
            if m:
                flush_entry()
                source = m.group(3).strip() if m.group(3) else ""
                current = {
                    "等级": current_grade,
                    "位置": m.group(1),
                    "类型": m.group(2),
                    "出处声明": source,
                    "引文": "",
                    "命中文献": "",
                    "命中章节": "",
                    "原文": "",
                    "匹配度": "",
                    "问题": "",
                    "建议": "",
                    "上下文": "",
                }
                acc_field = None
                i += 1
                continue

        if current is None:
            i += 1
            continue

        # Field detection
        m_quote = re.match(r"^- 引文[：:]\s*`(.+)`$", line)
        m_hit = re.match(r"^- 命中[：:]\s*《(.+?)》\s*(.*)$", line)
        m_orig = re.match(r"^- 原文[：:]\s*`(.*)$", line)
        m_score = re.match(r"^- 匹配度[：:]\s*(\d+\.?\d*)", line)
        m_issue = re.match(r"^- 问题[：:]\s*(.*)$", line)
        m_sug = re.match(r"^- 建议[：:]\s*(.*)$", line)
        m_ctx = re.match(r"^- 上下文[：:]\s*(.*)$", line)

        if m_quote:
            current["引文"] = m_quote.group(1)
            acc_field = None
        elif m_hit:
            current["命中文献"] = m_hit.group(1).strip()
            current["命中章节"] = m_hit.group(2).strip()
            acc_field = None
        elif m_orig:
            val = m_orig.group(1)
            # Check if the value ends with a closing backtick on the same line
            if val.rstrip().endswith("`"):
                current["原文"] = val.rstrip()[:-1]
                acc_field = None
            else:
                current["原文"] = val + "\n"
                acc_field = "原文"
        elif m_score:
            current["匹配度"] = m_score.group(1)
            acc_field = None
        elif m_issue:
            current["问题"] = m_issue.group(1)
            acc_field = "问题"
        elif m_sug:
            current["建议"] = m_sug.group(1)
            acc_field = None
        elif m_ctx:
            current["上下文"] = m_ctx.group(1)
            acc_field = "上下文"
        elif acc_field:
            # Continuation of a multi-line field
            current[acc_field] += line + "\n"
        # else: blank line or ignored line

        i += 1

    flush_entry()
    return entries


def main() -> int:
    text = INPUT.read_text(encoding="utf-8")
    entries = parse_report_v2(text)

    if not entries:
        print("ERROR: No entries parsed!", file=sys.stderr)
        return 1

    # Verify count
    print(f"Parsed {len(entries)} entries", file=sys.stderr)
    by_grade = {}
    for e in entries:
        g = e["等级"]
        by_grade[g] = by_grade.get(g, 0) + 1
    for g in ["A", "B", "C", "D"]:
        print(f"  {g}: {by_grade.get(g, 0)}", file=sys.stderr)

    columns = ["等级", "位置", "类型", "出处声明", "引文", "命中文献", "命中章节", "原文", "匹配度", "问题", "建议", "上下文"]

    with open(OUTPUT, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(entries)

    print(f"\nCSV written: {OUTPUT} ({len(entries)} rows)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
