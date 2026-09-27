#!/usr/bin/env python
"""导出源码结构（类/函数/行数），给文档核对用。

用法：python tools/dump_structure.py > structure.txt
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]

TARGETS = [
    "run.py",
    "src/app.py",
    "src/pipeline.py",
    "src/asr.py",
    "src/core/config.py",
    "src/core/db.py",
    "src/core/cache.py",
    "src/core/utils.py",
    "src/llm/client.py",
    "src/llm/notes.py",
    "src/platforms/base.py",
    "src/platforms/bilibili.py",
    "src/platforms/local_file.py",
    "src/platforms/registry.py",
    "src/webapi/schemas.py",
    "src/webapi/routes_tasks.py",
    "src/webapi/routes_notes.py",
    "src/webapi/routes_upload.py",
    "src/webapi/routes_system.py",
]

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass


def describe(node: ast.AST) -> str:
    if isinstance(node, ast.ClassDef):
        methods = []
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name.startswith("_") and not child.name.startswith("__"):
                    continue
                prefix = "async " if isinstance(child, ast.AsyncFunctionDef) else ""
                methods.append(prefix + child.name + "()")
        bases = ", ".join(ast.unparse(b) for b in node.bases)
        return f"class {node.name}({bases}):  " + ", ".join(methods)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
        return prefix + node.name + "()"
    return ""


def main() -> int:
    for rel in TARGETS:
        path = BASE_DIR / rel
        if not path.exists():
            print(f"!! 缺失 {rel}")
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        print(f"\n=== {rel}  ({len(source.splitlines())} 行)")
        for node in tree.body:
            text = describe(node)
            if text:
                print("   ", text)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = []
                for target in targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        names.append(target.id)
                if names:
                    print(f"    常量: {', '.join(names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
