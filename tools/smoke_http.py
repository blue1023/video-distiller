#!/usr/bin/env python
"""真实 HTTP 冒烟测试：对着正在运行的服务打接口（含批量、上传、导出）。

用法：
    python tools/smoke_http.py                      # 默认 http://127.0.0.1:8848
    python tools/smoke_http.py http://127.0.0.1:9000
"""

from __future__ import annotations

import io
import json
import math
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

PASSED = 0
FAILED: list[str] = []

TEST_URL = "https://www.bilibili.com/video/BV1GJ411x7h7"


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  [OK]   {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


def call(path: str, method: str = "GET", payload: dict | None = None, raw: bytes | None = None,
         content_type: str = "application/json", timeout: int = 120):
    # 路径里可能含中文文件名（如上传/删除），必须 URL 编码
    safe_path = urllib.parse.quote(path, safe="/?&=%:")
    url = f"{BASE}{safe_path}"
    data = raw if raw is not None else (json.dumps(payload).encode("utf-8") if payload is not None else None)
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            ctype = response.headers.get("Content-Type", "")
            # http.client 返回的 header 名是小写，这里统一成小写键方便取用
            headers = {k.lower(): v for k, v in response.headers.items()}
            if "application/json" in ctype:
                return response.status, json.loads(body.decode("utf-8")), headers
            return response.status, body, headers
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        headers = {k.lower(): v for k, v in exc.headers.items()}
        try:
            return exc.code, json.loads(body), headers
        except ValueError:
            return exc.code, {"detail": body[:200]}, headers


def multipart(file_field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----dsh" + uuid.uuid4().hex
    body = io.BytesIO()
    body.write(f"--{boundary}\r\n".encode())
    body.write(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode("utf-8")
    )
    body.write(b"Content-Type: application/octet-stream\r\n\r\n")
    body.write(content)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    return body.getvalue(), f"multipart/form-data; boundary={boundary}"


def wav_bytes(seconds: float = 1.0, rate: int = 8000) -> bytes:
    frames = int(rate * seconds)
    samples = bytearray()
    for index in range(frames):
        samples += struct.pack("<h", int(8000 * math.sin(2 * math.pi * 330 * index / rate)))
    header = b"RIFF" + struct.pack("<I", 36 + len(samples)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(samples))
    return header + bytes(samples)


def wait_task(task_id: str, timeout: int = 420) -> dict:
    deadline = time.time() + timeout
    task: dict = {}
    while time.time() < deadline:
        status, task, _ = call(f"/api/tasks/{task_id}")
        if status != 200 or task.get("status") in ("success", "failed", "cancelled"):
            break
        time.sleep(2)
    return task


def main() -> int:
    print(f"目标服务：{BASE}\n")

    print("== 健康与静态资源 ==")
    status, health, _ = call("/api/health")
    check("health 可用", status == 200 and health.get("ok"), str(health))
    for path in ("/", "/static/js/app.js", "/static/css/app.css", "/static/vendor/markmap-lib/browser/index.iife.js"):
        status, body, _ = call(path)
        size = len(body) if isinstance(body, bytes) else len(str(body))
        check(f"GET {path} 可用（{size} B）", status == 200)
    status, platforms, _ = call("/api/platforms")
    names = [item["name"] for item in (platforms.get("items") or [])]
    check("平台列表含 bilibili 与 local", {"bilibili", "local"} <= set(names), str(names))

    print("\n== 队列与缓存 ==")
    status, queue, _ = call("/api/queue")
    check("队列状态可读", status == 200 and "limit" in queue, str(queue))
    status, cache_info, _ = call("/api/cache")
    check("缓存统计可读", status == 200 and "count" in cache_info, str(cache_info))

    print("\n== 批量提交 ==")
    status, batch, _ = call(
        "/api/tasks/batch",
        method="POST",
        payload={"text": f"{TEST_URL}\nhttps://example.com/not-video", "note_style": "concise"},
    )
    check("批量接口 200", status == 200, str(batch))
    batch_id = batch.get("batch_id")
    check("创建了批次", bool(batch_id))
    check("跳过了非法链接", len(batch.get("skipped") or []) == 1)
    status, batch_detail, _ = call(f"/api/tasks/batches/{batch_id}")
    check("批次详情可读", status == 200 and len(batch_detail.get("tasks") or []) == 1)
    task_ids = [t["id"] for t in (batch_detail.get("tasks") or [])]

    if task_ids:
        print("\n== 等待任务完成（并发上限内）==")
        final = wait_task(task_ids[0])
        print(
            f"  {final.get('status')} | 来源 {final.get('transcript_src')} | "
            f"from_cache={final.get('from_cache')} | 笔记 {final.get('note_id')}"
        )
        check("任务成功", final.get("status") == "success", str(final.get("error")))

        print("\n== 导出下载（中文文件名头）==")
        note_id = final.get("note_id")
        if note_id:
            for kind in ("md", "xmind", "opml", "json"):
                status, body, headers = call(f"/api/notes/{note_id}/download?kind={kind}")
                disposition = headers.get("content-disposition", "")
                ok = status == 200 and "filename*=UTF-8''" in disposition
                check(f"下载 {kind} 且文件名已编码", ok, f"{status} {disposition[:80]}")
            status, note, _ = call(f"/api/notes/{note_id}")
            check("笔记正文非空", status == 200 and len(note.get("content") or "") > 100)
            check("导图树存在", bool((note.get("tree") or {}).get("children")))

        print("\n== 缓存复用 ==")
        status, again, _ = call("/api/tasks", method="POST", payload={"url": TEST_URL})
        check("再次提交成功", status == 200, str(again))
        second = wait_task(again.get("id"))
        check("第二次命中缓存", int(second.get("from_cache") or 0) == 1, str(second.get("from_cache")))
        if second.get("note_id"):
            call(f"/api/notes/{second['note_id']}?purge=true", method="DELETE")

    print("\n== 本地上传 ==")
    body, content_type = multipart("files", "冒烟测试.wav", wav_bytes(0.5))
    status, upload, _ = call("/api/upload", method="POST", raw=body, content_type=content_type)
    check("上传 200", status == 200, str(upload)[:200])
    items = upload.get("items") or [{}]
    local_url = items[0].get("url") if items else None
    check("返回 local:// 链接", str(local_url).startswith("local://"), str(local_url))
    if local_url:
        status, local_task, _ = call("/api/tasks", method="POST", payload={"url": local_url})
        check("本地任务提交成功", status == 200, str(local_task)[:160])
        local_final = wait_task(local_task.get("id"))
        print(f"  本地任务：{local_final.get('status')} | 平台 {local_final.get('platform')} | 来源 {local_final.get('transcript_src')}")
        check("本地任务完成", local_final.get("status") == "success", str(local_final.get("error")))
        check("平台识别为 local", local_final.get("platform") == "local")
        if local_final.get("note_id"):
            call(f"/api/notes/{local_final['note_id']}?purge=true", method="DELETE")
        call(f"/api/upload/{items[0]['filename']}", method="DELETE")

    print("\n== 清理 ==")
    status, tasks, _ = call("/api/tasks?limit=200")
    for task in tasks.get("items") or []:
        call(f"/api/tasks/{task['id']}", method="DELETE")
    status, notes, _ = call("/api/notes?limit=200")
    for note in notes.get("items") or []:
        call(f"/api/notes/{note['id']}?purge=true", method="DELETE")
    for item in (call("/api/upload")[1].get("items") or []):
        call(f"/api/upload/{item['filename']}", method="DELETE")
    call("/api/cache", method="DELETE")
    status, left_tasks, _ = call("/api/tasks?limit=10")
    check("任务已清理", not (left_tasks.get("items") or []))
    check("缓存已清理", call("/api/cache")[1].get("count") == 0)

    print("\n" + "=" * 50)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  x {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
