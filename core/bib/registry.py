"""bib parser 插件注册表。

每个 bib 解析器自我注册（通过 @register 装饰器）；index.py 拿到 filename 后调
lookup(filename) 得到合适的 parser，永远不需要 import 各个 parser 模块或写 if/elif。

加新 bib：在 core/bib/parsers/ 下新建一个文件，写 @register("filename pattern")
即可。core 不需要任何改动。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from core.bib.types import ParsedBib

Parser = Callable[[str, str], ParsedBib]  # (filename, raw_text) -> ParsedBib

_REGISTRY: list[tuple[str, Parser]] = []
_DEFAULT: Parser | None = None


def register(filename_pattern: str):
    """子串匹配 filename。第一个 match 的 parser 生效。"""
    def deco(fn: Parser) -> Parser:
        _REGISTRY.append((filename_pattern, fn))
        return fn
    return deco


def register_default(fn: Parser) -> Parser:
    """注册兜底 parser。文件名不匹配任何 pattern 时调用。"""
    global _DEFAULT
    _DEFAULT = fn
    return fn


def lookup(filename: str) -> Parser:
    name = Path(filename).name
    for pattern, parser in _REGISTRY:
        if pattern in name:
            return parser
    if _DEFAULT is None:
        raise RuntimeError("no default bib parser registered")
    return _DEFAULT


def parse_bib(path: str | Path) -> ParsedBib:
    """读 bib 文件并用合适的 parser 解析。"""
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    parser = lookup(p.name)
    return parser(p.name, raw)


_LOADED = False


def ensure_loaded() -> None:
    """触发 core/bib/parsers/ 下所有 .py 模块加载，让它们的 @register 装饰器执行。"""
    global _LOADED
    if _LOADED:
        return
    import importlib
    parsers_dir = Path(__file__).parent / "parsers"
    for f in sorted(parsers_dir.glob("*.py")):
        if f.name.startswith("_"):
            continue
        importlib.import_module(f"core.bib.parsers.{f.stem}")
    _LOADED = True
