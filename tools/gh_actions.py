#!/usr/bin/env python
"""查看 GitHub Actions 工作流与最近运行记录（只读，需要 GH_TOKEN）。"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

OWNER = os.environ.get("GH_OWNER", "blue1023")
REPO = os.environ.get("GH_REPO", "video-distiller")


def api(path: str, token: str) -> tuple[int, dict]:
    request = urllib.request.Request(f"https://api.github.com{path}")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "ci-checker")
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace"))
        except ValueError:
            return exc.code, {}


def main() -> int:
    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        print("缺少 GH_TOKEN", file=sys.stderr)
        return 2

    status, data = api(f"/repos/{OWNER}/{REPO}/actions/workflows", token)
    print("== 工作流 ==")
    if status != 200:
        print(f"  查询失败（HTTP {status}）：{data.get('message')}")
        return 1
    workflows = data.get("workflows") or []
    if not workflows:
        print("  还没有工作流被识别（可能刚推送，稍等几秒）")
    for workflow in workflows:
        print(f"  {workflow['name']} | 状态 {workflow['state']} | {workflow['path']}")

    status, runs = api(f"/repos/{OWNER}/{REPO}/actions/runs?per_page=5", token)
    print("\n== 最近运行 ==")
    if status != 200:
        print(f"  查询失败（HTTP {status}）：{runs.get('message')}")
        return 1
    total = runs.get("total_count", 0)
    print(f"  共 {total} 次")
    for run in runs.get("workflow_runs") or []:
        conclusion = run.get("conclusion") or "进行中"
        print(
            f"  #{run['run_number']} {run['name']} | {run['status']}/{conclusion} | "
            f"分支 {run['head_branch']} | {run['html_url']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
