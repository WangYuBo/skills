# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

"黑猫核校" — a citation verification tool for Chinese book manuscripts. Given a manuscript (100k–300k characters) and reference texts (1M–3M characters), the program extracts all direct quotes, citation notes, and their surrounding context from the manuscript, then compares them against the original reference texts to verify word-level accuracy, semantic consistency, and correct usage.

## Input/Output

- **Manuscript**: docx, txt, or non-scanned PDF (typically 100k–300k characters)
- **Reference texts (bib/)**: txt files (5.8MB total, 9 classical Chinese texts — 道德经, 论语, 孟子, 庄子, 黄帝内经, 诗经, 孙子兵法, 周易, 大学中庸)
- **Expected output**: A report listing each citation with its verification result (match/mismatch/partial-match) and details of discrepancies

## Domain-Specific Challenges

Chinese classical citations have unique verification challenges beyond simple string matching:

- **Interleaved commentary format**: Reference texts like 王弼注《道德经》mix original text with commentary. The system must distinguish which parts are the source text vs. annotations.
- **Variant characters**: Classical Chinese has variant character forms (异体字) across editions. "道可道" in one edition might use a variant of "道" in another.
- **Implicit citations**: Authors may paraphrase or concatenate passages without explicit quote markers.
- **Partial citations**: A citation may be a fragment of a longer passage, requiring fuzzy matching against the reference.
- **Citation note format**: Footnotes/endnotes in docx have structured formats (author, title, chapter, page) that need parsing to locate the correct reference passage.

## Design Principles
- 降低复杂度:通过深模块 + 信息隐藏 + 抽象分层 + 把复杂度向下压
- 书稿中的引文解析，宁多勿缺；
