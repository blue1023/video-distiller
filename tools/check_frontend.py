#!/usr/bin/env python
"""前端一致性检查（不需要浏览器）：

1. index.html 里没有重复的 id；
2. app.js 里 $("xxx") 引用的 id 都真实存在；
3. index.html 引用的 css/js 文件都在磁盘上；
4. vendor 依赖的关键入口文件齐全。

用法：python tools/check_frontend.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
WEB = BASE_DIR / "web"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

PASSED = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  [OK]   {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


def main() -> int:
    html_path = WEB / "index.html"
    if not html_path.exists():
        print("缺少 web/index.html")
        return 1
    html = html_path.read_text(encoding="utf-8")

    print("== HTML 结构 ==")
    ids = re.findall(r'\bid="([^"]+)"', html)
    duplicates = {value for value in ids if ids.count(value) > 1}
    check("没有重复 id", not duplicates, str(duplicates))
    check("id 数量合理", len(ids) > 30, str(len(ids)))

    # 引用的静态资源
    assets = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    missing_assets = [a for a in assets if not (WEB / a.replace("/static/", "")).exists()]
    check("静态资源都存在", not missing_assets, str(missing_assets))
    print(f"    引用资源 {len(assets)} 个")

    print("\n== JS 引用的元素 ==")
    js_files = sorted((WEB / "js").glob("*.js"))
    check("存在 JS 文件", bool(js_files), str(len(js_files)))
    referenced: set[str] = set()
    for path in js_files:
        source = path.read_text(encoding="utf-8")
        referenced.update(re.findall(r'\$\("([^"]+)"\)', source))
        referenced.update(re.findall(r'getElementById\("([^"]+)"\)', source))
    known = set(ids)
    missing = sorted(item for item in referenced if item not in known)
    check("JS 引用的 id 都存在", not missing, f"缺失 {missing}")
    print(f"    JS 共引用 {len(referenced)} 个 id")

    print("\n== 设置面板字段 ==")
    # app.js 的 SETTING_FIELDS 每一项都应该有对应的 s-xxx 输入框
    app_source = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    block = re.search(r"var SETTING_FIELDS = \[(.*?)\];", app_source, re.S)
    fields = re.findall(r'"([^"]+)"', block.group(1)) if block else []
    check("解析到设置字段", len(fields) >= 10, str(fields))
    missing_fields = [f for f in fields if f"s-{f}" not in known]
    check("每个设置字段都有输入框", not missing_fields, f"缺失 {missing_fields}")

    print("\n== vendor 依赖 ==")
    for rel in (
        "vendor/d3/d3.min.js",
        "vendor/markmap-view/browser/index.js",
        "vendor/markmap-lib/browser/index.iife.js",
        "vendor/marked/marked.min.js",
        "vendor/manifest.json",
    ):
        check(f"存在 {rel}", (WEB / rel).exists())

    print("\n" + "=" * 46)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  x {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
