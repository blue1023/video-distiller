#!/usr/bin/env python
"""在 B 站搜索一批视频，找出真正带字幕的，用来验证字幕抓取链路。

用法：
    python tools/find_subtitle_video.py [关键词] [扫描数量]
"""

from __future__ import annotations

import asyncio
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
    "Origin": "https://www.bilibili.com",
}


async def search(client: httpx.AsyncClient, keyword: str, limit: int) -> list[dict]:
    # 搜索接口有反爬：先访问首页拿 buvid3 等匿名 Cookie，再带签名请求
    try:
        await client.get("https://www.bilibili.com/")
    except httpx.HTTPError:
        pass

    img_key, sub_key = await _wbi_keys(client)
    params = {
        "search_type": "video",
        "keyword": keyword,
        "page": 1,
        "page_size": min(limit, 50),
        "wts": int(__import__("time").time()),
    }
    params = _sign(params, img_key, sub_key)
    response = await client.get(
        "https://api.bilibili.com/x/web-interface/wbi/search/type", params=params
    )
    text = response.text
    try:
        payload = response.json()
    except ValueError:
        print(f"搜索接口返回非 JSON（HTTP {response.status_code}）：{text[:120]}")
        return []
    if payload.get("code") != 0:
        print(f"搜索失败：code={payload.get('code')} msg={payload.get('message')}")
        return []
    items = (payload.get("data") or {}).get("result") or []
    results = []
    for item in items[:limit]:
        results.append(
            {
                "bvid": item.get("bvid"),
                "title": (item.get("title") or "").replace('<em class="keyword">', "").replace("</em>", ""),
                "author": item.get("author"),
            }
        )
    return results


async def _wbi_keys(client: httpx.AsyncClient) -> tuple[str, str]:
    payload = (await client.get("https://api.bilibili.com/x/web-interface/nav")).json()
    wbi = (payload.get("data") or {}).get("wbi_img") or {}
    return (
        Path(urllib.parse.urlparse(wbi.get("img_url", "")).path).stem,
        Path(urllib.parse.urlparse(wbi.get("sub_url", "")).path).stem,
    )


def _sign(params: dict, img_key: str, sub_key: str) -> dict:
    import hashlib
    import re
    import time

    mixin_tab = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
        33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
        61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
        36, 20, 34, 44, 52,
    ]
    raw = img_key + sub_key
    mixin = "".join(raw[i] for i in mixin_tab if i < len(raw))[:32]
    signed = dict(params)
    signed.setdefault("wts", int(time.time()))
    query = urllib.parse.urlencode(
        [(k, re.sub(r"[!'()*]", "", str(v))) for k, v in sorted(signed.items())]
    )
    signed["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return signed


async def has_subtitle(client: httpx.AsyncClient, bvid: str) -> tuple[bool, int, str]:
    try:
        pages = (await client.get(
            "https://api.bilibili.com/x/player/pagelist", params={"bvid": bvid}
        )).json()
        cid = ((pages.get("data") or [{}])[0]).get("cid")
        if not cid:
            return False, 0, ""
        payload = (await client.get(
            "https://api.bilibili.com/x/player/v2", params={"bvid": bvid, "cid": cid}
        )).json()
        subs = ((payload.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
        return len(subs) > 0, len(subs), (subs[0].get("lan_doc") if subs else "")
    except Exception:  # noqa: BLE001
        return False, 0, ""


async def main() -> int:
    keyword = sys.argv[1] if len(sys.argv) > 1 else "字幕"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    async with httpx.AsyncClient(headers=HEADERS, timeout=25.0, follow_redirects=True) as client:
        items = await search(client, keyword, limit)
        print(f"搜索「{keyword}」得到 {len(items)} 条，逐条检查字幕…\n")
        found = []
        for item in items:
            if not item["bvid"]:
                continue
            ok, count, lang = await has_subtitle(client, item["bvid"])
            flag = f"✅ {count} 条 ({lang})" if ok else "—"
            print(f"  {item['bvid']}  {flag:<16} {item['title'][:44]}")
            if ok:
                found.append(item["bvid"])
        print("\n带字幕的视频：")
        for bvid in found[:10]:
            print(f"  https://www.bilibili.com/video/{bvid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
