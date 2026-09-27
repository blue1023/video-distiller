#!/usr/bin/env python
"""探测视频页面 HTML 里是否直接内嵌字幕信息（用于判断无 Cookie 时能否拿字幕）。"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

import httpx  # noqa: E402

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

KEYS = ["subtitle", "aisubtitle", "subtitle_url", "__INITIAL_STATE__", "player/v2", "wbi/v2"]


def main() -> int:
    bvids = sys.argv[1:] or ["BV1GJ411x7h7", "BV1xx411c7mD"]
    with httpx.Client(headers=HEADERS, timeout=25.0, follow_redirects=True) as client:
        for bvid in bvids:
            url = f"https://www.bilibili.com/video/{bvid}"
            try:
                response = client.get(url)
            except httpx.HTTPError as exc:
                print(f"{bvid}: 网络错误 {exc}")
                continue
            html = response.text
            print(f"\n=== {bvid} | HTTP {response.status_code} | HTML {len(html)} 字符")
            for key in KEYS:
                count = html.count(key)
                print(f"    {key:<20} 出现 {count} 次")
            for match in re.finditer(r'"(?:subtitle_url|aisubtitle)"\s*:\s*"([^"]{0,120})', html):
                print(f"    -> {match.group(0)[:140]}")
            # 尝试解析 __INITIAL_STATE__
            state = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});", html, re.S)
            if state:
                try:
                    data = json.loads(state.group(1).replace("undefined", "null"))
                    video_data = data.get("videoData") or {}
                    print(f"    INITIAL_STATE 解析成功，title={video_data.get('title')}")
                    subtitle = video_data.get("subtitle") or {}
                    print(f"    videoData.subtitle={json.dumps(subtitle, ensure_ascii=False)[:200]}")
                except Exception as exc:  # noqa: BLE001
                    print(f"    INITIAL_STATE 解析失败：{exc}")
                else:
                    full = video_data.get("subtitle") or {}
                    for item in full.get("list") or []:
                        print(
                            f"      · lan={item.get('lan')} doc={item.get('lan_doc')} "
                            f"type={item.get('type')} ai={item.get('ai_type')} "
                            f"lock={item.get('is_lock')} url={str(item.get('subtitle_url'))[:110]}"
                        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
