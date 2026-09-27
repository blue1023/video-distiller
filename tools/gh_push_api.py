#!/usr/bin/env python
"""用 GitHub REST API 推送当前分支（git push 因本机 TLS 环境不可用时的兜底方案）。

原理：读取本地已提交的 git 对象（blob / tree / commit），
复用远端已有的对象，自底向上补齐缺失对象，最后把分支 ref 指向新 commit。
只依赖标准库；需要环境变量 GH_TOKEN。

用法：
    $env:GH_TOKEN="..."; python tools/gh_push_api.py
"""

from __future__ import annotations

import base64
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


def github(path: str, token: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(f"{API}{path}", data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "dsh-git-api-push")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            body = response.read().decode("utf-8")
            return response.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"message": body[:400]}
    except urllib.error.URLError as exc:
        return 0, {"message": f"网络错误：{exc.reason}"}


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{result.stderr.strip()}")
    return result.stdout.strip()


def git_bytes(sha: str) -> bytes:
    """读取对象的原始内容。

    注意：tree 对象必须用 git cat-file tree 取原始二进制，
    用 -p 会得到带缩进的友好格式，无法解析。
    """
    kind = git("cat-file", "-t", sha)
    command = ["git", "cat-file", kind, sha]
    result = subprocess.run(command, cwd=BASE_DIR, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"读取对象 {sha}（{kind}）失败")
    return result.stdout


def parse_tree(raw: bytes) -> list[tuple[str, str, str]]:
    """解析原始 tree 对象，返回 [(mode, name, sha)]。

    tree 的二进制格式：<mode> <name>\\0<20 字节 sha>，循环排列。
    """
    entries: list[tuple[str, str, str]] = []
    index = 0
    length = len(raw)
    while index < length:
        space = raw.index(b" ", index)
        mode = raw[index:space].decode()
        nul = raw.index(b"\x00", space)
        name = raw[space + 1 : nul].decode("utf-8", "replace")
        sha = raw[nul + 1 : nul + 21].hex()
        entries.append((mode, name, sha))
        index = nul + 21
    return entries


def parse_commit(raw: bytes) -> tuple[str, list[str], str]:
    text = raw.decode("utf-8", "replace")
    header, _, message = text.partition("\n\n")
    tree_sha = ""
    parents: list[str] = []
    for line in header.splitlines():
        if line.startswith("tree "):
            tree_sha = line.split()[1]
        elif line.startswith("parent "):
            parents.append(line.split()[1])
    return tree_sha, parents, message


def main() -> int:
    token = os.environ.get("GH_TOKEN", "").strip()
    owner = os.environ.get("GH_OWNER", "blue1023")
    repo_name = os.environ.get("GH_REPO", "video-distiller")
    branch = os.environ.get("GH_BRANCH", "main")
    if not token:
        print("缺少环境变量 GH_TOKEN", file=sys.stderr)
        return 2

    head = git("rev-parse", "HEAD")
    print(f"本地 HEAD：{head[:8]}")

    status, ref = github(f"/repos/{owner}/{repo_name}/git/ref/heads/{branch}", token)
    remote_sha = ref.get("object", {}).get("sha") if status == 200 else None
    if remote_sha:
        print(f"远端 {branch}：{remote_sha[:8]}")
        if remote_sha == head:
            print("远端已是最新，无需推送")
            return 0
    else:
        print(f"远端 {branch} 不存在（HTTP {status}）")

    pending = (
        git("rev-list", "--reverse", f"{remote_sha}..{head}").splitlines() if remote_sha else [head]
    )
    print(f"待推送提交 {len(pending)} 个：{[c[:8] for c in pending]}")

    # 远端已有对象（按 sha 索引），并记录路径 -> sha 便于判断 blob 是否已存在
    remote_objects: set[str] = set()
    if remote_sha:
        status, resp = github(
            f"/repos/{owner}/{repo_name}/git/trees/{remote_sha}?recursive=1", token
        )
        if status == 200:
            for item in resp.get("tree", []):
                remote_objects.add(item["sha"])
        remote_objects.add(remote_sha)
    print(f"远端已知对象：{len(remote_objects)}")

    stats = {"blob": 0, "tree": 0, "commit": 0, "reused": 0}
    commit_map: dict[str, str] = {}

    def create_blob(sha: str) -> str:
        if sha in remote_objects:
            stats["reused"] += 1
            return sha
        payload = base64.b64encode(git_bytes(sha)).decode("ascii")
        status, body = github(
            f"/repos/{owner}/{repo_name}/git/blobs",
            token,
            method="POST",
            payload={"content": payload, "encoding": "base64"},
        )
        if status not in (200, 201):
            raise RuntimeError(f"创建 blob {sha[:8]} 失败：{body.get('message')}")
        stats["blob"] += 1
        remote_objects.add(body["sha"])
        return body["sha"]

    def create_tree(sha: str, path: str) -> str:
        entries = parse_tree(git_bytes(sha))
        items = []
        for mode, name, child_sha in entries:
            child_path = f"{path}/{name}" if path else name
            if mode == "160000":  # submodule，原样保留
                items.append({"path": name, "mode": mode, "type": "commit", "sha": child_sha})
            elif mode == "40000" or mode == "040000":
                items.append(
                    {
                        "path": name,
                        "mode": "040000",
                        "type": "tree",
                        "sha": create_tree(child_sha, child_path),
                    }
                )
            else:
                items.append(
                    {
                        "path": name,
                        "mode": mode,
                        "type": "blob",
                        "sha": create_blob(child_sha),
                    }
                )
        if sha in remote_objects:
            stats["reused"] += 1
            return sha
        status, body = github(
            f"/repos/{owner}/{repo_name}/git/trees",
            token,
            method="POST",
            payload={"tree": items},
        )
        if status not in (200, 201):
            raise RuntimeError(f"创建 tree {path or '/'} 失败：{body.get('message')}")
        stats["tree"] += 1
        remote_objects.add(body["sha"])
        return body["sha"]

    for commit_sha in pending:
        tree_sha, parents, message = parse_commit(git_bytes(commit_sha))
        new_tree = create_tree(tree_sha, "")
        new_parents = [commit_map.get(p, p) for p in parents]
        if not new_parents and remote_sha:
            # 首个本地提交在远端没有父节点时，接到远端现有 HEAD 上，保证历史连续
            new_parents = [remote_sha]
        status, body = github(
            f"/repos/{owner}/{repo_name}/git/commits",
            token,
            method="POST",
            payload={"message": message, "tree": new_tree, "parents": new_parents},
        )
        if status not in (200, 201):
            raise RuntimeError(f"创建 commit {commit_sha[:8]} 失败：{body.get('message')}")
        stats["commit"] += 1
        commit_map[commit_sha] = body["sha"]
        print(f"  提交 {commit_sha[:8]} -> {body['sha'][:8]}")

    new_head = commit_map[head]
    if remote_sha:
        status, body = github(
            f"/repos/{owner}/{repo_name}/git/refs/heads/{branch}",
            token,
            method="PATCH",
            payload={"sha": new_head, "force": False},
        )
    else:
        status, body = github(
            f"/repos/{owner}/{repo_name}/git/refs",
            token,
            method="POST",
            payload={"ref": f"refs/heads/{branch}", "sha": new_head},
        )
    if status not in (200, 201):
        print(f"更新分支失败（HTTP {status}）：{body.get('message')}")
        return 1

    print(
        f"\n✅ 推送完成：{branch} -> {new_head[:8]}"
        f"（新建 blob {stats['blob']} / tree {stats['tree']} / commit {stats['commit']}，复用 {stats['reused']}）"
    )
    print(f"   https://github.com/{owner}/{repo_name}")

    # 用 API 创建的 commit 与本地 commit 的 sha 不同（父提交被重写），
    # 这里把本地分支指到远端 commit，避免本地/远端历史分叉（两者内容完全一致）。
    if os.environ.get("SYNC_LOCAL_REF", "1") == "1":
        result = subprocess.run(
            ["git", "update-ref", f"refs/heads/{branch}", new_head],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"   本地 {branch} 已同步到远端 commit（内容与远端完全一致）")
        else:
            print(f"   本地分支同步失败（可忽略）：{result.stderr.strip()[:120]}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"推送失败：{exc}")
        raise SystemExit(1) from exc
