#!/usr/bin/env python
"""深入探测：匿名情况下 player/v2 与 player/wbi/v2 到底能返回什么。"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

import httpx  # noqa: E402

MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
}


def wbi_keys(client: httpx.Client) -> tuple[str, str]:
    payload = client.get("https://api.bilibili.com/x/web-interface/nav").json()
    wbi = (payload.get("data") or {}).get("wbi_img") or {}
    img_key = Path(urllib.parse.urlparse(wbi.get("img_url", "")).path).stem
    sub_key = Path(urllib.parse.urlparse(wbi.get("sub_url", "")).path).stem
    return img_key, sub_key


def sign(params: dict, img_key: str, sub_key: str) -> dict:
    raw = img_key + sub_key
    mixin = "".join(raw[i] for i in MIXIN_TAB if i < len(raw))[:32]
    signed = dict(params)
    signed["wts"] = int(time.time())
    query = urllib.parse.urlencode(
        [(k, re.sub(r"[!'()*]", "", str(v))) for k, v in sorted(signed.items())]
    )
    signed["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return signed


def summarize(label: str, payload: dict, depth: int = 0) -> None:
    code = payload.get("code")
    data = payload.get("data") or {}
    print(f"\n--- {label} | HTTP code={code} msg={payload.get('message')}")
    if not isinstance(data, dict):
        print(f"    data 类型：{type(data).__name__}")
        return
    print(f"    data 顶层字段：{list(data)[:15]}")
    subtitle = data.get("subtitle")
    if isinstance(subtitle, dict):
        print(f"    subtitle.allow_submit={subtitle.get('allow_submit')} "
              f"subtitles={len(subtitle.get('subtitles') or [])}")
        for item in (subtitle.get("subtitles") or [])[:5]:
            print(f"      - {item.get('lan_doc') or item.get('lan')} ai_type={item.get('ai_type')} "
                  f"url={(item.get('subtitle_url') or '')[:70]}")
    elif subtitle is not None:
        print(f"    subtitle 类型异常：{type(subtitle).__name__}")


def main() -> int:
    bvid = sys.argv[1] if len(sys.argv) > 1 else "BV1GJ411x7h7"
    with httpx.Client(headers=HEADERS, timeout=25.0, follow_redirects=True) as client:
        pages = client.get(
            "https://api.bilibili.com/x/player/pagelist", params={"bvid": bvid}
        ).json()
        cid = (pages.get("data") or [{}])[0].get("cid")
        print(f"bvid={bvid} cid={cid}")

        view = client.get(
            "https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid}
        ).json()
        aid = (view.get("data") or {}).get("aid")
        print(f"aid={aid}  title={(view.get('data') or {}).get('title')}")

        summarize(
            "player/v2 (bvid+cid)",
            client.get(
                "https://api.bilibili.com/x/player/v2",
                params={"bvid": bvid, "cid": cid},
            ).json(),
        )
        summarize(
            "player/v2 (aid+cid)",
            client.get(
                "https://api.bilibili.com/x/player/v2", params={"aid": aid, "cid": cid}
            ).json(),
        )

        img_key, sub_key = wbi_keys(client)
        print(f"\nwbi img_key={img_key[:12]}... sub_key={sub_key[:12]}...")
        signed = sign({"aid": aid, "bvid": bvid, "cid": cid}, img_key, sub_key)
        summarize(
            "player/wbi/v2 (已签名)",
            client.get("https://api.bilibili.com/x/player/wbi/v2", params=signed).json(),
        )

        # 尝试取一条字幕内容，验证 CDN 是否公开可读
        response = client.get(
            "https://api.bilibili.com/x/player/v2", params={"bvid": bvid, "cid": cid}
        ).json()
        subs = ((response.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
        if subs:
            url = subs[0].get("subtitle_url") or ""
            if url.startswith("//"):
                url = "https:" + url
            content = client.get(url)
            print(f"\n字幕 CDN：HTTP {content.status_code}，前 200 字：\n{content.text[:200]}")
        else:
            print("\n无字幕候选，跳过 CDN 测试")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
