#!/usr/bin/env python
"""探测 B 站各接口在"匿名"（无 Cookie）下的可用性，用于排查链路问题。

用法：python tools/probe_bili_api.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

import httpx  # noqa: E402

BV = "BV1GJ411x7h7"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
}

WBI_ENDPOINTS = [
    "https://api.bilibili.com/x/web-interface/nav",
    "https://api.bilibili.com/x/web-interface/wbi/index/top/rcmd?fresh_type=3&ps=1",
    "https://api.bilibili.com/x/web-interface/wbi/search/square?limit=1",
    "https://api.bilibili.com/x/web-interface/dynamic/region?ps=1&rid=1",
    "https://api.bilibili.com/x/h5/fingerprint",
]

NEED_CID_ENDPOINTS = [
    "https://api.bilibili.com/x/player/pagelist?bvid={bv}",
]


def show(label: str, response: httpx.Response) -> None:
    text = response.text
    try:
        payload = json.loads(text)
    except ValueError:
        print(f"{label:<62} HTTP {response.status_code}  非 JSON: {text[:80]}")
        return
    code = payload.get("code")
    message = payload.get("message")
    data = payload.get("data") or {}
    found = []
    if isinstance(data, dict):
        if isinstance(data.get("wbi_img"), dict):
            found.append("wbi_img")
        for key in ("wbi_img", "img_url", "sub_url"):
            if key in data and key != "wbi_img":
                found.append(key)
    print(
        f"{label:<62} HTTP {response.status_code}  code={code} msg={message} "
        f"| keys={list(data)[:8] if isinstance(data, dict) else type(data).__name__} {found}"
    )


def main() -> int:
    with httpx.Client(headers=HEADERS, timeout=20.0, follow_redirects=True) as client:
        print("== WBI 密钥候选接口（匿名）==")
        for url in WBI_ENDPOINTS:
            try:
                response = client.get(url)
                show(url.split("?")[0].replace("https://api.bilibili.com", ""), response)
            except httpx.HTTPError as exc:
                print(f"{url} -> 网络错误 {exc}")

        print("\n== 其它接口（匿名）==")
        others = [
            f"https://api.bilibili.com/x/web-interface/view?bvid={BV}",
            f"https://api.bilibili.com/x/player/pagelist?bvid={BV}",
            f"https://api.bilibili.com/x/player/v2?bvid={BV}&cid=0",
        ]
        for url in others:
            try:
                show(url.split("?")[0].replace("https://api.bilibili.com", ""), client.get(url))
            except httpx.HTTPError as exc:
                print(f"{url} -> 网络错误 {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
