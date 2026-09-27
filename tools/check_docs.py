#!/usr/bin/env python
"""检查 docs/ 里的文档内部链接是否有效（避免文档互相引用到不存在的文件）。

用法：python tools/check_docs.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
DOCS = BASE_DIR / "docs"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

PASSED = 0
FAILED: list[str] = []

#: 文档里明确让读者「自己新建」的文件，检查时跳过
PLANNED_FILES = {"src/platforms/demo.py"}

FENCE = re.compile(r"```.*?```", re.S)
INLINE_CODE = re.compile(r"`[^`\n]*`")


def strip_code(text: str) -> str:
    """去掉围栏代码块与行内代码。

    否则代码/示例里的 markdown 链接语法（例如 f-string 拼出来的 `[文字](链接)`）
    会被当成真实链接，产生误报 —— 这个检查器自己就踩过。
    """
    return INLINE_CODE.sub("", FENCE.sub("", text))


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  [OK]   {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


def main() -> int:
    files = sorted(DOCS.rglob("*.md"))
    print(f"== 找到 {len(files)} 个文档 ==")
    check("文档数量合理", len(files) >= 6, str([f.name for f in files]))

    print("\n== 内部链接 ==")
    broken: list[str] = []
    # 只匹配"看起来像真实链接"的目标：不含空格/花括号/换行。
    # 否则代码片段里的伪链接语法会产生误报。
    link_re = re.compile(r"\[([^\]\n]+)\]\(([^)\s{}\n]+)\)")
    for path in files:
        text = strip_code(path.read_text(encoding="utf-8"))
        for label, target in link_re.findall(text):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            clean = target.split("#")[0].strip()
            if not clean:
                continue
            resolved = (path.parent / clean).resolve()
            if not resolved.exists():
                broken.append(f"{path.name} -> {target}")
    check("所有内部链接有效", not broken, "; ".join(broken[:5]))

    print("\n== 文档里提到的源码文件 ==")
    referenced: set[str] = set()
    for path in files:
        text = path.read_text(encoding="utf-8")
        referenced.update(re.findall(r"`(src/[\w/\.]+\.py)`", text))
        referenced.update(re.findall(r"`(web/[\w/\.]+\.(?:js|css|html))`", text))
        referenced.update(re.findall(r"`(tools/[\w/\.]+\.py)`", text))
    missing = sorted(r for r in referenced if not (BASE_DIR / r).exists() and r not in PLANNED_FILES)
    check("提到的源码文件都存在", not missing, str(missing))
    planned = sorted(r for r in referenced if r in PLANNED_FILES)
    print(f"    共引用 {len(referenced)} 个源文件路径（其中 {len(planned)} 个是练习让读者新建的）")

    print("\n== 文档里提到的关键函数 ==")
    known_symbols = {
        "run_pipeline": "src/pipeline.py",
        "spawn_task": "src/pipeline.py",
        "configure_concurrency": "src/pipeline.py",
        "save_artifacts": "src/pipeline.py",
        "get_settings": "src/core/config.py",
        "save_settings": "src/core/config.py",
        "init_db": "src/core/db.py",
        "create_task": "src/core/db.py",
        "get_batch": "src/core/db.py",
        "EventBus": "src/core/db.py",
        "markdown_to_tree": "src/core/utils.py",
        "tree_to_xmind": "src/core/utils.py",
        "tree_to_opml": "src/core/utils.py",
        "safe_filename": "src/core/utils.py",
        "split_transcript": "src/core/utils.py",
        "transcribe_audio": "src/asr.py",
        "resolve_file": "src/platforms/local_file.py",
        "chat_json": "src/llm/client.py",
        "generate_note": "src/llm/notes.py",
        "generate_mindmap": "src/llm/notes.py",
        "render_markdown": "src/llm/notes.py",
        "_tree_from_outline": "src/llm/notes.py",
        "split_urls": "src/webapi/routes_tasks.py",
        "_prepare_task": "src/webapi/routes_tasks.py",
    }
    all_docs = "\n".join(p.read_text(encoding="utf-8") for p in files)
    absent = [name for name in known_symbols if name not in all_docs]
    check("主要函数在文档里被提及", len(absent) <= 3, f"未提及 {absent}")

    print("\n" + "=" * 46)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  x {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
