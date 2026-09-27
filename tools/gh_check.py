#!/usr/bin/env python
"""校验 GitHub 仓库内容是否与本地一致（只读，需要 GH_TOKEN 环境变量）。"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

OWNER = os.environ.get("GH_OWNER", "blue1023")
REPO = os.environ.get("GH_REPO", "video-distiller")


def api(path: str, token: str) -> dict:
    request = urllib.request.Request(f"https://api.github.com{path}")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "repo-checker")
    with urllib.request.urlopen(request, timeout=40) as response:
        return json.load(response)


def main() -> int:
    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        print("缺少 GH_TOKEN", file=sys.stderr)
        return 2

    repo = api(f"/repos/{OWNER}/{REPO}", token)
    print(f"仓库：{repo['html_url']}")
    print(f"  私有：{repo['private']} | 默认分支：{repo['default_branch']} | 大小：{repo['size']} KB")

    branch = api(f"/repos/{OWNER}/{REPO}/branches/{repo['default_branch']}", token)
    commit = branch["commit"]
    print(f"  最新提交：{commit['sha'][:8]} {commit['commit']['message'].splitlines()[0]}")

    tree = api(f"/repos/{OWNER}/{REPO}/git/trees/{repo['default_branch']}?recursive=1", token)
    remote_files = {item["path"]: item.get("size", 0) for item in tree["tree"] if item["type"] == "blob"}
    print(f"  远端文件数：{len(remote_files)}")

    # 本地 git 跟踪的文件（排除 gitignore 的）
    # 注意：git 在 Windows 上默认把非 ASCII 文件名转义成八进制（core.quotepath），
    # 不关掉的话中文文件名会被误判成"仅在本地"。
    import subprocess

    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files"],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    local_files = [line.strip().strip('"') for line in result.stdout.splitlines() if line.strip()]

    missing = [f for f in local_files if f not in remote_files]
    extra = [f for f in remote_files if f not in local_files]
    print(f"  本地已提交文件数：{len(local_files)}")
    print(f"  仅在本地（未推送）：{missing if missing else '无'}")
    print(f"  仅在远端：{extra if extra else '无'}")

    print("\n远端文件清单：")
    for path in sorted(remote_files):
        print(f"  {remote_files[path]:>9,} B  {path}")

    ok = not missing and not extra
    print("\n" + ("✅ 本地与远端一致" if ok else "⚠️ 存在差异（见上）"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
