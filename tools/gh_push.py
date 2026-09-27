#!/usr/bin/env python
"""GitHub 上传助手：校验 Token / 创建仓库 / 推送代码。

用法（token 通过环境变量传入，避免出现在命令行历史与日志里）：
    $env:GH_TOKEN="..."   (Windows PowerShell)
    export GH_TOKEN=...   (bash)
    python tools/gh_push.py --repo video-distiller --visibility public

安全说明：脚本只打印掩码后的 token，不会把完整 token 写入任何文件。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

API = "https://api.github.com"


def mask(token: str) -> str:
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}...{token[-4:]}"


def api(path: str, token: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
    url = f"{API}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "video-distiller-uploader")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body = response.read().decode("utf-8")
            return response.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"message": body[:300]}
    except urllib.error.URLError as exc:
        raise SystemExit(f"网络错误：{exc.reason}")


def run_git(args: list[str], token: str | None = None, redact: str | None = None) -> tuple[int, str]:
    """执行 git 命令；输出中的 token 会被掩码。"""
    env = dict(os.environ)
    if token:
        # 让 git 不弹凭据窗口（无交互环境）
        env["GIT_TERMINAL_PROMPT"] = "0"
    process = subprocess.run(
        ["git", *args],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    output = (process.stdout or "") + (process.stderr or "")
    if redact:
        output = output.replace(redact, mask(redact))
    return process.returncode, output.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 GitHub Token 并推送仓库")
    parser.add_argument("--repo", default="video-distiller")
    parser.add_argument("--visibility", default="public", choices=["public", "private"])
    parser.add_argument("--branch", default="main")
    parser.add_argument("--dry-run", action="store_true", help="只做校验，不推送")
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        print("缺少环境变量 GH_TOKEN", file=sys.stderr)
        return 2
    print(f"使用 Token：{mask(token)}")

    # 1. 校验 token
    status, user = api("/user", token)
    if status != 200:
        print(f"Token 校验失败（HTTP {status}）：{user.get('message')}")
        return 1
    login = user.get("login")
    print(f"Token 有效，登录用户：{login}")
    scopes = ""
    print(f"账号类型：{user.get('type')}，公开仓库数：{user.get('public_repos')}")

    # 2. 检查仓库
    status, repo = api(f"/repos/{login}/{args.repo}", token)
    if status == 404:
        print(f"仓库 {login}/{args.repo} 不存在，正在创建（{args.visibility}）…")
        status, repo = api(
            "/user/repos",
            token,
            method="POST",
            payload={
                "name": args.repo,
                "description": "B 站链接笔记生成工具：粘贴链接 → Markdown 笔记 + 思维导图（XMind/PNG 导出）",
                "private": args.visibility == "private",
                "has_issues": True,
                "has_wiki": False,
                "auto_init": False,
            },
        )
        if status not in (200, 201):
            print(f"创建仓库失败（HTTP {status}）：{repo.get('message')}")
            print("提示：Token 需要 repo 权限（classic）或 Contents+Administration 写权限（fine-grained）")
            return 1
        print(f"仓库已创建：{repo.get('html_url')}")
    elif status == 200:
        print(f"仓库已存在：{repo.get('html_url')}（默认分支 {repo.get('default_branch')}，私有={repo.get('private')}）")
    else:
        print(f"查询仓库失败（HTTP {status}）：{repo.get('message')}")
        return 1

    clone_url = f"https://{login}:{token}@github.com/{login}/{args.repo}.git"

    # 3. 本地提交状态
    code, out = run_git(["log", "--oneline", "-1"])
    print(f"本地最新提交：{out}" if code == 0 else f"没有本地提交：{out}")

    code, out = run_git(["remote", "get-url", "origin"], redact=token)
    if code == 0 and out:
        run_git(["remote", "set-url", "origin", clone_url], redact=token)
        print("已更新 origin 地址")
    else:
        run_git(["remote", "add", "origin", clone_url], redact=token)
        print("已添加 origin")

    # 4. 推送
    print(f"推送分支 {args.branch} …")
    code, out = run_git(["push", "-u", "origin", args.branch], token=token, redact=token)
    print(out)
    if code != 0:
        print("推送失败。常见原因：Token 缺少 repo 权限 / 仓库已有内容产生冲突 / 网络受限。")
        return 1

    status, repo = api(f"/repos/{login}/{args.repo}", token)
    print(f"\n✅ 推送完成：{repo.get('html_url')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
