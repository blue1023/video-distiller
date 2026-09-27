#!/usr/bin/env python
"""联网自检：验证 B 站接口、WBI 签名、字幕解析是否真的可用。

用法：
    python tools/selftest_network.py                  # 用内置的候选视频
    python tools/selftest_network.py <B站链接或BV号>   # 指定视频

只打印结果，不写任何数据、不调用大模型。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

# 公开、长期存在、带字幕的候选视频（找不到可用项时由用户自行指定）
CANDIDATES = [
    "BV1GJ411x7h7",
    "BV1xx411c7mD",
    "BV17x411w7KC",
]

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


async def probe(url: str) -> bool:
    from src.core.config import get_settings
    from src.platforms.bilibili import BilibiliPlatform

    settings = get_settings()
    platform = BilibiliPlatform(settings)
    print(f"\n== 探测 {url} ==")
    try:
        parsed = platform.normalize(url)
        print(f"  解析结果：{parsed}")
        check("链接解析", bool(parsed.get("bvid") or parsed.get("aid")))

        info = await platform.fetch_info(parsed)
        print(f"  标题：{info.title}")
        print(f"  UP主：{info.uploader}  时长：{info.duration}s  分P数：{len(info.parts)}")
        check("获取视频信息", bool(info.title))
        check("分P列表非空", len(info.parts) >= 1)

        part = info.parts[0]
        subtitles = await platform.list_subtitles(info.aid or 0, info.video_id, part.cid or 0)
        print(f"  字幕候选：{len(subtitles)} 条")
        for item in subtitles:
            print(f"    - {item.get('lan_doc') or item.get('lan')} | ai={item.get('_is_ai')}")
        if subtitles:
            check("字幕接口可用", True)
        else:
            check(
                "字幕接口可访问（未登录故为空）",
                not settings.bili_cookie.strip(),
                "配置了 Cookie 却仍拿不到字幕，可能 Cookie 已过期",
            )
            print("  ⚠️ 未取到字幕：B 站字幕接口需要登录 Cookie，未配置时属正常现象")

        if subtitles:
            text, segments = await platform.fetch_subtitle_text(subtitles[0])
            print(f"  字幕正文：{len(text)} 字，{len(segments)} 段")
            print(f"  预览：{text[:160].replace(chr(10), ' / ')}")
            check("抓取到字幕文本", len(text) > 0)
        else:
            print("  → 继续验证音频流是否可取（语音转写的兜底链路）")

        # 无字幕时需要能拿到音频流，否则语音转写也无从下手
        try:
            audio = await platform.download_audio(parsed, info, part)
            size_mb = audio.stat().st_size / 1024 / 1024
            print(f"  音频流：{audio.name}（{size_mb:.1f} MB）")
            check("音频流可取", size_mb > 0.05)
            from src.core.utils import try_delete

            try_delete(audio)
        except Exception as exc:  # noqa: BLE001
            import traceback

            print(f"  音频下载失败：{type(exc).__name__}: {exc}")
            traceback.print_exc()
            check("音频流可取", False, str(exc))

        if settings.llm_api_key.strip():
            print("  已配置大模型 Key，可继续跑 run.py 走完整流程")
        else:
            print("  提示：尚未配置大模型 API Key，无法生成笔记")
        return True
    except Exception as exc:  # noqa: BLE001
        import traceback

        print(f"  出错：{type(exc).__name__}: {exc}")
        traceback.print_exc()
        return False
    finally:
        await platform.aclose()


async def main() -> int:
    urls = sys.argv[1:] or CANDIDATES
    for url in urls:
        if await probe(url):
            break
    print("\n" + "=" * 50)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  x {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
