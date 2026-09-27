#!/usr/bin/env python
"""端到端自检（不需要真实 API Key）。

做法：起一个"假的 OpenAI 兼容服务"（同时支持 chat/completions 与
audio/transcriptions），把 LLM 与转写地址都指向它，再用 FastAPI TestClient
打真实接口，跑完 建任务 → 抓字幕/音频 → 转写 → 生成笔记 → 导出 全流程。

用法：
    python tools/selftest_e2e.py                # 用内置测试视频
    python tools/selftest_e2e.py <B站链接>       # 指定视频
    python tools/selftest_e2e.py --mock-only    # 只起假服务，手工 curl 调试
"""

from __future__ import annotations

import io
import json
import sys
import threading
import time
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

import uvicorn
from fastapi import FastAPI, Request

TEST_TMP = BASE_DIR / ".tmp" / "e2e"
TEST_TMP.mkdir(parents=True, exist_ok=True)

MOCK_PORT = 18765

PASSED = 0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [OK]   {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


# ---------------------------------------------------------------- 假模型服务

NOTE_JSON = {
    "title": "端到端测试笔记",
    "one_sentence": "这是一次自动化端到端验证。",
    "summary": "本视频用于验证抓取、调用大模型、渲染与导出链路是否通畅。",
    "keywords": ["自动化测试", "流水线", "Markdown"],
    "outline": [
        {"heading": "第一个章节", "level": 2, "start": 0, "end": 60, "points": ["要点一：链路通畅", "要点二：产物落盘"]},
        {"heading": "第二个章节", "level": 2, "start": 60, "end": 120, "points": ["要点三：导图可导出"]},
    ],
    "highlights": ["整条流水线可用"],
    "quotes": [{"text": "测试通过", "time": "00:30"}],
    "action_items": ["打开 data/notes 查看产物"],
    "open_questions": [],
}

MINDMAP_JSON = {
    "root": "端到端测试笔记",
    "children": [
        {"name": "第一个章节", "children": [{"name": "要点一"}, {"name": "要点二"}]},
        {"name": "第二个章节", "children": [{"name": "要点三"}]},
    ],
}

MOCK_ASR_TEXT = (
    "[0:00] 这是模拟语音转写返回的第一段内容，用于验证无字幕视频的兜底链路。\n"
    "[0:30] 第二段内容包含一些具体信息：三个步骤、两个数字、一个结论。\n"
    "[1:00] 第三段内容用于凑够长度，让后续的大模型调用有足够输入。"
)

REPORT: dict = {}

mock_app = FastAPI(title="mock-openai")


@mock_app.post("/v1/chat/completions")
async def mock_completions(request: Request):
    body = await request.json()
    system = ""
    for message in body.get("messages", []):
        if message.get("role") == "system":
            system = message.get("content", "")
            break
    REPORT["calls"] = REPORT.get("calls", 0) + 1
    REPORT["last_model"] = body.get("model")
    if "思维导图结构设计师" in system:
        REPORT["saw_mindmap_prompt"] = True
        content = json.dumps(MINDMAP_JSON, ensure_ascii=False)
    else:
        REPORT["saw_note_prompt"] = True
        content = json.dumps(NOTE_JSON, ensure_ascii=False)
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "model": body.get("model") or "mock-model",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 123, "completion_tokens": 456},
    }


@mock_app.post("/v1/audio/transcriptions")
async def mock_transcriptions(request: Request):
    REPORT["asr_called"] = REPORT.get("asr_called", 0) + 1
    size = 0
    try:
        form = await request.form()
        upload = form.get("file")
        if upload is not None and hasattr(upload, "read"):
            size = len(await upload.read())
    except Exception as exc:  # noqa: BLE001
        print(f"[mock] 读取表单失败：{exc}", flush=True)
    REPORT["asr_upload_bytes"] = REPORT.get("asr_upload_bytes", 0) + size
    print(f"[mock] 转写请求：{size} 字节", flush=True)
    return {"text": MOCK_ASR_TEXT}


def _fake_wav_bytes(seconds: float = 1.0, rate: int = 8000) -> bytes:
    """生成一个最小的合法 WAV（用于测试本地上传链路，避免依赖巨大视频文件）。"""
    import math
    import struct

    frames = int(rate * seconds)
    samples = bytearray()
    for index in range(frames):
        value = int(12000 * math.sin(2 * math.pi * 440 * index / rate))
        samples += struct.pack("<h", value)
    data_size = len(samples)
    header = b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", data_size)
    return header + bytes(samples)


def _serve_mock(port: int) -> None:
    uvicorn.run(mock_app, host="127.0.0.1", port=port, log_level="error")


def start_mock(port: int) -> None:
    import httpx

    threading.Thread(target=_serve_mock, args=(port,), daemon=True).start()
    for _ in range(80):
        try:
            response = httpx.post(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                json={"model": "probe", "messages": [{"role": "system", "content": "x"}]},
                timeout=3,
            )
            if response.status_code == 200:
                return
            print(f"  假服务返回 {response.status_code}：{response.text[:160]}")
            raise SystemExit(1)
        except SystemExit:
            raise
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    raise SystemExit("假服务启动失败")


# ---------------------------------------------------------------- 主流程


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    url = args[0] if args else "https://www.bilibili.com/video/BV1GJ411x7h7"

    if "--mock-only" in sys.argv:
        # 独立跑假服务：给 run.py 手工联调时用（PORT 可用环境变量覆盖）
        import os

        port = int(os.environ.get("MOCK_PORT", MOCK_PORT))
        start_mock(port)
        print(f"假服务已就绪：http://127.0.0.1:{port}/v1（Ctrl+C 退出）")
        while True:
            time.sleep(5)

    from src.core.config import save_settings

    # 让被测服务把请求都打给假服务：同时开启 openai 转写，验证无字幕兜底链路
    save_settings(
        {
            "llm_base_url": f"http://127.0.0.1:{MOCK_PORT}/v1",
            "llm_api_key": "mock-key",
            "llm_model": "mock-model",
            "asr_provider": "openai",
            "asr_base_url": f"http://127.0.0.1:{MOCK_PORT}/v1",
            "asr_api_key": "mock-key",
            "asr_model": "mock-whisper",
            "cache_enabled": True,
            "max_concurrency": 2,
        }
    )

    print("启动假的大模型 / 转写服务…")
    start_mock(MOCK_PORT)

    from fastapi.testclient import TestClient

    from src.app import app

    with TestClient(app) as client:
        print("\n== 健康检查 ==")
        health = client.get("/api/health").json()
        check("health 可用", health.get("ok") is True)
        check("识别到已配置大模型", health.get("llm_configured") is True)

        print("\n== 大模型连通性自检接口 ==")
        test_llm = client.post("/api/settings/test-llm")
        check("test-llm 返回 200", test_llm.status_code == 200, test_llm.text[:160])

        print("\n== 提交任务 ==")
        response = client.post("/api/tasks", json={"url": url, "note_style": "concise"})
        check("创建任务返回 200", response.status_code == 200, response.text[:200])
        task_id = (response.json() or {}).get("id")
        check("任务有 id", bool(task_id))

        deadline = time.time() + 420
        final: dict = {}
        while time.time() < deadline:
            final = client.get(f"/api/tasks/{task_id}").json()
            if final.get("status") in ("success", "failed", "cancelled"):
                break
            time.sleep(1.0)

        print(
            f"  最终状态：{final.get('status')} | 阶段：{final.get('stage')} | "
            f"进度：{final.get('progress')} | 字幕来源：{final.get('transcript_src')}"
        )
        if final.get("status") != "success":
            print(f"  错误信息：{final.get('error')}")
        check("任务成功结束", final.get("status") == "success", str(final.get("error")))
        check("字幕来源已记录", bool(final.get("transcript_src")))
        check("标题已回填", bool(final.get("title")))
        check("进度到 100%", float(final.get("progress") or 0) >= 1.0)
        check("关联了笔记 id", bool(final.get("note_id")))

        print("\n== 兜底链路（音频 → 转写）==")
        print(
            f"  转写调用 {REPORT.get('asr_called')} 次，"
            f"上传音频 {REPORT.get('asr_upload_bytes', 0) / 1024:.1f} KB"
        )
        if str(final.get("transcript_src", "")).startswith("asr"):
            check("走了语音转写兜底", bool(REPORT.get("asr_called")))
            check("音频确实上传到转写接口", int(REPORT.get("asr_upload_bytes", 0)) > 1024)
        else:
            check("直接命中字幕（无需转写）", str(final.get("transcript_src")).startswith("subtitle"))

        print("\n== 笔记接口 ==")
        notes = client.get("/api/notes").json().get("items") or []
        check("笔记列表非空", len(notes) >= 1)
        note_ids: list[str] = []
        if notes:
            note_id = notes[0]["id"]
            note_ids.append(note_id)
            detail = client.get(f"/api/notes/{note_id}").json()
            content = detail.get("content") or ""
            check("笔记有 Markdown 正文", len(content) > 200, str(len(content)))
            check("正文含摘要章节", "内容摘要" in content)
            check("正文含时间戳跳转", "t=60" in content or "t=0" in content)
            check("笔记有导图树", bool((detail.get("tree") or {}).get("children")))
            check("标签已解析", isinstance(detail.get("tags"), list) and len(detail["tags"]) >= 1)

            md = client.get(f"/api/notes/{note_id}/download?kind=md")
            check("下载 Markdown", md.status_code == 200 and "端到端测试笔记" in md.text, str(md.status_code))

            xmind = client.get(f"/api/notes/{note_id}/download?kind=xmind")
            check("下载 XMind", xmind.status_code == 200 and xmind.content[:2] == b"PK")
            if xmind.content[:2] == b"PK":
                with zipfile.ZipFile(io.BytesIO(xmind.content)) as zf:
                    xmind_content = json.loads(zf.read("content.json").decode("utf-8"))
                check(
                    "XMind 根节点正确",
                    xmind_content[0]["rootTopic"]["title"] == "端到端测试笔记",
                    str(xmind_content[0]["rootTopic"]["title"]),
                )

            opml = client.get(f"/api/notes/{note_id}/download?kind=opml")
            check("下载 OPML", opml.status_code == 200 and "<opml" in opml.text)

            raw = client.get(f"/api/notes/{note_id}/raw")
            check("raw 接口返回纯文本", raw.status_code == 200 and raw.text.startswith("#"))

        print("\n== 转写缓存 ==")
        stats = client.get("/api/cache").json()
        print(f"  缓存条目 {stats.get('count')} 条，{stats.get('mb')} MB")
        check("首次生成写入了缓存", stats.get("count", 0) >= 1, str(stats))
        asr_before = int(REPORT.get("asr_called") or 0)

        second = client.post("/api/tasks", json={"url": url})
        check("第二次任务创建成功", second.status_code == 200, second.text[:160])
        second_id = (second.json() or {}).get("id")
        deadline = time.time() + 300
        second_final: dict = {}
        while time.time() < deadline:
            second_final = client.get(f"/api/tasks/{second_id}").json()
            if second_final.get("status") in ("success", "failed", "cancelled"):
                break
            time.sleep(1.0)
        print(
            f"  第二次：{second_final.get('status')} | 来源 {second_final.get('transcript_src')} | "
            f"from_cache={second_final.get('from_cache')}"
        )
        check("第二次任务成功", second_final.get("status") == "success", str(second_final.get("error")))
        check("第二次命中缓存", int(second_final.get("from_cache") or 0) == 1)
        check(
            "命中缓存后不再调用转写接口",
            int(REPORT.get("asr_called") or 0) == asr_before,
            f"{asr_before} -> {REPORT.get('asr_called')}",
        )
        second_note = second_final.get("note_id")
        if second_note:
            note_ids.append(second_note)

        cleared = client.delete("/api/cache")
        check("清空缓存接口可用", cleared.status_code == 200 and cleared.json().get("removed", 0) >= 1, cleared.text[:160])
        check("清空后缓存为空", client.get("/api/cache").json().get("count") == 0)

        print("\n== 批量提交 ==")
        batch = client.post(
            "/api/tasks/batch",
            json={
                "text": f"{url}\n{url}\nhttps://example.com/not-a-video",
                "note_style": "concise",
            },
        )
        check("批量接口返回 200", batch.status_code == 200, batch.text[:200])
        batch_data = batch.json() if batch.status_code == 200 else {}
        check("批次 id 存在", bool(batch_data.get("batch_id")))
        check("去重后提交 1 条", batch_data.get("created") == 1, str(batch_data.get("created")))
        check("非法链接被跳过", len(batch_data.get("skipped") or []) == 1, str(batch_data.get("skipped")))
        if batch_data.get("batch_id"):
            detail_batch = client.get(f"/api/tasks/batches/{batch_data['batch_id']}").json()
            check("批次详情含任务", len(detail_batch.get("tasks") or []) == 1)
            for task in detail_batch.get("tasks") or []:
                deadline = time.time() + 300
                while time.time() < deadline:
                    current = client.get(f"/api/tasks/{task['id']}").json()
                    if current.get("status") in ("success", "failed", "cancelled"):
                        break
                    time.sleep(1.0)
                if current.get("note_id"):
                    note_ids.append(current["note_id"])
                check("批次任务完成", current.get("status") == "success", str(current.get("error")))

        print("\n== 本地视频上传 ==")
        wav = _fake_wav_bytes(1.0)
        upload = client.post(
            "/api/upload",
            files={"files": ("测试音频.wav", wav, "audio/wav")},
        )
        check("上传接口返回 200", upload.status_code == 200, upload.text[:200])
        uploaded = (upload.json().get("items") or [{}])[0] if upload.status_code == 200 else {}
        print(f"  上传结果：{uploaded.get('filename')}（{uploaded.get('size_text')}）")
        check("返回 local:// 链接", str(uploaded.get("url", "")).startswith("local://"), str(uploaded.get("url")))

        listings = client.get("/api/upload").json().get("items") or []
        check("上传列表可见", any(item["url"] == uploaded.get("url") for item in listings))

        if uploaded.get("url"):
            local_task = client.post("/api/tasks", json={"url": uploaded["url"]})
            check("本地上传可作为任务提交", local_task.status_code == 200, local_task.text[:200])
            local_id = (local_task.json() or {}).get("id")
            deadline = time.time() + 300
            local_final: dict = {}
            while time.time() < deadline:
                local_final = client.get(f"/api/tasks/{local_id}").json()
                if local_final.get("status") in ("success", "failed", "cancelled"):
                    break
                time.sleep(1.0)
            print(
                f"  本地任务：{local_final.get('status')} | 平台 {local_final.get('platform')} | "
                f"来源 {local_final.get('transcript_src')}"
            )
            check("本地任务成功", local_final.get("status") == "success", str(local_final.get("error")))
            check("识别为 local 平台", local_final.get("platform") == "local")
            check("本地任务走了转写", str(local_final.get("transcript_src", "")).startswith("asr"))
            if local_final.get("note_id"):
                note_ids.append(local_final["note_id"])

        print("\n== 重新生成（忽略缓存）==")
        regen = client.post(f"/api/tasks/{task_id}/refresh")
        check("重生成返回 200", regen.status_code == 200, regen.text[:160])
        check("新任务的 refresh 标记为 1", int((regen.json() or {}).get("refresh") or 0) == 1)
        asr_before_regen = int(REPORT.get("asr_called") or 0)
        regen_id = (regen.json() or {}).get("id")
        deadline = time.time() + 300
        while time.time() < deadline:
            current = client.get(f"/api/tasks/{regen_id}").json()
            if current.get("status") in ("success", "failed", "cancelled"):
                break
            time.sleep(1.0)
        check("重生成任务成功", current.get("status") == "success", str(current.get("error")))
        check("重生成忽略了缓存", int(current.get("from_cache") or 0) == 0)
        check(
            "重生成重新调用了转写",
            int(REPORT.get("asr_called") or 0) > asr_before_regen,
            f"{asr_before_regen} -> {REPORT.get('asr_called')}",
        )
        if current.get("note_id"):
            note_ids.append(current["note_id"])

        print("\n== 磁盘产物 ==")
        from src.core.config import NOTES_DIR

        recent = sorted(
            p.name for p in NOTES_DIR.glob("*") if p.stat().st_mtime > time.time() - 900
        )
        print(f"  最近产物 {len(recent)} 个：{recent[:4]}…")
        check("落盘了 md 文件", any(name.endswith(".md") for name in recent))
        check("落盘了 mindmap.json", any(name.endswith(".mindmap.json") for name in recent))
        check("落盘了 xmind", any(name.endswith(".xmind") for name in recent))

        print("\n== 假模型调用统计 ==")
        print(
            f"  调用 {REPORT.get('calls')} 次 | 笔记提示词 {REPORT.get('saw_note_prompt')} | "
            f"导图提示词 {REPORT.get('saw_mindmap_prompt')} | 转写 {REPORT.get('asr_called')} 次"
        )
        check("调用过笔记生成", bool(REPORT.get("saw_note_prompt")))
        check("调用过导图生成", bool(REPORT.get("saw_mindmap_prompt")))

        print("\n== 队列状态 ==")
        queue = client.get("/api/queue").json()
        print(f"  {queue}")
        check("队列接口返回上限", queue.get("limit", 0) >= 1, str(queue))

        print("\n== 错误处理 ==")
        bad = client.post("/api/tasks", json={"url": "https://example.com/not-bilibili"})
        check("非法链接被拒绝", bad.status_code == 400, str(bad.status_code))
        check("不存在的笔记返回 404", client.get("/api/notes/n_notexist").status_code == 404)
        check(
            "空批量提交被拒绝",
            client.post("/api/tasks/batch", json={"text": "   "}).status_code == 400,
        )

        print("\n== 清理测试数据 ==")
        for note_id in note_ids:
            if note_id:
                client.delete(f"/api/notes/{note_id}?purge=true")
        if task_id:
            client.delete(f"/api/tasks/{task_id}")
        # 清掉本批次与本次测试产生的任务/上传文件
        remaining = client.get("/api/tasks?limit=200").json().get("items") or []
        for task in remaining:
            client.delete(f"/api/tasks/{task['id']}")
        for item in client.get("/api/upload").json().get("items") or []:
            client.delete(f"/api/upload/{item['filename']}")
        check("测试笔记已清理", all(
            client.get(f"/api/notes/{nid}").status_code == 404 for nid in note_ids if nid
        ))

    print("\n" + "=" * 52)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  x {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
