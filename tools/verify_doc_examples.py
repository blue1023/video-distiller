"""验证文档里的代码示例是否真的可用（尤其是 05-改代码指南 的 demo 平台）。"""

from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

# 文档 05 里让读者新建的文件内容（原样复制）
DEMO_PLATFORM = '''"""演示平台：用一段写死的文本模拟一个视频，用来学习平台扩展机制。

URL 格式：demo://任意名字
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..core.config import Settings, get_settings
from .base import BasePlatform, Transcript, VideoInfo, VideoPart

SCHEME = "demo://"

FAKE_TRANSCRIPT = """[0:00] 欢迎来到演示平台，这是一段模拟的字幕文本。
[0:15] 第一，平台适配器的接口只有四个方法，实现它们就能接入新平台。
[0:40] 第二，match 负责认领链接，normalize 负责解析链接，不要混在一起。
[1:05] 第三，fetch_info 返回 VideoInfo，fetch_transcript 返回 Transcript。
[1:30] 第四，写完之后记得在 registry.py 里注册，注册后前端会自动出现。
[2:00] 以上就是演示内容，希望对你理解这个项目的架构有帮助。
"""


class DemoPlatform(BasePlatform):
    name = "demo"
    display_name = "演示平台"
    supports_audio = False

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()

    def match(self, url: str) -> bool:
        return (url or "").strip().startswith(SCHEME)

    def normalize(self, url: str) -> Dict[str, Any]:
        name = url.strip()[len(SCHEME):] or "未命名"
        return {"platform": self.name, "url": url, "video_id": name, "page": 1, "start_time": 0}

    async def fetch_info(self, parsed: Dict[str, Any]) -> VideoInfo:
        name = parsed["video_id"]
        return VideoInfo(
            platform=self.name,
            video_id=name,
            title=f"演示视频：{name}",
            uploader="演示账号",
            duration=150,
            description="这是演示平台生成的假数据，用于验证平台扩展机制。",
            parts=[VideoPart(index=1, title="第一部分", duration=150, cid=1)],
            url=parsed["url"],
            view_count=12345,
            like_count=678,
        )

    async def fetch_transcript(self, parsed, info, part, *, allow_asr: bool = True) -> Transcript:
        return Transcript(text=FAKE_TRANSCRIPT, source="subtitle_manual", language="zh-CN")

    async def aclose(self) -> None:
        return None
'''

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
    print("== 文档示例验证 ==")

    # 1. 把文档里的 demo.py 原样落到磁盘
    demo_path = BASE_DIR / "src" / "platforms" / "demo.py"
    demo_path.write_text(DEMO_PLATFORM, encoding="utf-8")
    print(f"  已写入 {demo_path.relative_to(BASE_DIR)}")
    try:
        import importlib

        module = importlib.import_module("src.platforms.demo")
        check("demo 平台可导入", hasattr(module, "DemoPlatform"))

        from src.core.config import Settings
        from src.platforms.registry import register, resolve

        register(module.DemoPlatform)
        settings = Settings()

        # 2. match / normalize
        platform = module.DemoPlatform(settings)
        check("match 认领 demo:// 链接", platform.match("demo://我的第一次扩展"))
        check("match 不抢 B 站链接", not platform.match("https://www.bilibili.com/video/BV1xx411c7mD"))
        parsed = platform.normalize("demo://我的第一次扩展")
        check("normalize 解析出 video_id", parsed.get("video_id") == "我的第一次扩展", str(parsed))

        # 3. 通过注册表解析（文档里说的"前端会自动出现"）
        resolved = resolve("demo://测试", settings)
        check("注册表能路由到 demo", resolved.name == "demo", resolved.name)

        from src.platforms import available_platforms

        names = [item["name"] for item in available_platforms()]
        check("平台列表包含 demo", "demo" in names, str(names))

        # 4. 跑一遍完整流水线（用假 LLM 服务，验证四个方法的契约正确）
        import asyncio

        async def run_pipeline_direct():
            info = await platform.fetch_info(parsed)
            part = info.parts[0]
            transcript = await platform.fetch_transcript(parsed, info, part)
            return info, transcript

        info, transcript = asyncio.run(run_pipeline_direct())
        check("fetch_info 返回标题", bool(info.title), info.title)
        check("fetch_info 有分P", len(info.parts) == 1)
        check("fetch_transcript 有文本", len(transcript.text) > 50, str(len(transcript.text)))

        # 5. 走真实 HTTP 接口（TestClient），确认平台能被完整使用
        import os

        os.environ["LLM_BASE_URL"] = ""
        from src.platforms.registry import reset_instances

        reset_instances()

        from fastapi.testclient import TestClient

        from src.app import app

        with TestClient(app) as client:
            status, platforms = 200, client.get("/api/platforms").json()
            names = [item["name"] for item in platforms["items"]]
            check("API /api/platforms 含 demo", "demo" in names, str(names))

            response = client.post("/api/tasks", json={"url": "demo://接口验证"})
            # 没配大模型时会在 LLM 阶段失败，但平台解析阶段应当通过
            body = response.json() if response.status_code == 200 else {}
            check("demo 链接能被提交为任务", response.status_code == 200, response.text[:160])
            if body.get("id"):
                import time

                task = {}
                for _ in range(20):
                    task = client.get(f"/api/tasks/{body['id']}").json()
                    if task.get("status") in ("success", "failed", "cancelled"):
                        break
                    time.sleep(0.5)
                print(f"  任务最终状态：{task.get('status')} | 平台 {task.get('platform')} | 阶段 {task.get('stage')}")
                check("任务识别为 demo 平台", task.get("platform") == "demo", str(task.get("platform")))
                check(
                    "平台解析与取信息阶段通过（失败点在大模型）",
                    task.get("stage") in ("structuring", "failed", "done"),
                    f"{task.get('stage')} / {task.get('error')}",
                )
                client.delete(f"/api/tasks/{body['id']}")
    finally:
        # 清理：删掉验证用的临时文件
        if demo_path.exists():
            demo_path.unlink()
            print(f"  已删除验证文件 {demo_path.relative_to(BASE_DIR)}")

    print("\n" + "=" * 46)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  x {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
