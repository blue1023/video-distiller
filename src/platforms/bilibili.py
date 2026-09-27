"""B 站平台适配器。

要点 / 踩坑记录：
- 公开接口必须带 Referer=https://www.bilibili.com 与浏览器 UA，否则 -403/412。
- 新版接口（player/wbi/v2、playurl）需要 WBI 签名：img_key+sub_key 按固定
  换位表打乱后拼成 mixin key，再对参数做 md5。
- 字幕列表在 player 接口里，字幕内容是一个独立 json（旧版 body[]，新版
  jsonl 每行一条），两者都兼容。
- AI 字幕需要登录 Cookie；UP 主自己上传的字幕（CC）匿名也能拿。
- 没有任何字幕时，回退下载 DASH 音频流，交给 ASR 适配器转写。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..core.config import Settings, get_settings
from ..core.utils import format_duration, get_logger, safe_filename
from .base import BasePlatform, PlatformError, Transcript, VideoInfo, VideoPart

log = get_logger("platform.bilibili")

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"av(\d+)", re.IGNORECASE)
_B23_RE = re.compile(r"b23\.tv/([0-9A-Za-z]+)")


class BilibiliPlatform(BasePlatform):
    name = "bilibili"
    display_name = "哔哩哔哩"
    supports_audio = True

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self._client: Optional[httpx.AsyncClient] = None
        self._wbi_keys: Optional[Tuple[str, str]] = None
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ HTTP 基建

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {
                "User-Agent": self.settings.bili_user_agent,
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            if self.settings.bili_cookie.strip():
                headers["Cookie"] = self.settings.bili_cookie.strip()
            self._client = httpx.AsyncClient(
                headers=headers, timeout=30.0, follow_redirects=True, http2=False
            )
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _throttle(self) -> None:
        """请求间隔，避免高频触发风控。"""
        interval = max(0.0, float(self.settings.bili_request_interval or 0))
        if interval <= 0:
            return
        async with self._lock:
            wait = interval - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    async def _api(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        optional: bool = False,
        ignore_codes: Tuple[int, ...] = (),
    ) -> Dict[str, Any]:
        """请求 B 站接口。

        optional=True 时，业务错误码不抛异常而是返回 {}，让调用方优雅降级
        （典型场景：未登录拿不到字幕，但视频信息照常展示）。
        """
        client = await self._get_client()
        await self._throttle()
        try:
            response = await client.get(url, params=params)
        except httpx.HTTPError as exc:  # 网络层
            raise PlatformError(f"请求 B 站失败：{exc}") from exc
        if response.status_code == 412:
            raise PlatformError(
                "B 站返回 412（风控）。请在设置里填入浏览器 Cookie，或降低请求频率后重试。"
            )
        try:
            data = response.json()
        except ValueError as exc:
            if optional:
                return {}
            raise PlatformError(
                f"B 站返回了非 JSON 内容（HTTP {response.status_code}），通常是风控或需要登录。"
            ) from exc
        code = data.get("code")
        if code not in (0, None) and code not in ignore_codes:
            if optional:
                log.info("B 站接口 %s 返回 code=%s（已降级处理）", url, code)
                return {}
            raise PlatformError(f"B 站接口错误 code={code} message={data.get('message')}")
        return data.get("data") or data.get("result") or {}

    # ------------------------------------------------------------ 链接解析

    def match(self, url: str) -> bool:
        url = (url or "").lower()
        return any(
            key in url
            for key in ("bilibili.com", "b23.tv", "bvid=", "bv1", "av")
        ) and ("http" in url or url.startswith("BV") or url.lower().startswith("av"))

    def normalize(self, url: str) -> Dict[str, Any]:
        url = (url or "").strip()
        if not url:
            raise PlatformError("链接为空")

        # 纯 BV 号 / av 号
        if _BV_RE.fullmatch(url):
            url = f"https://www.bilibili.com/video/{url}"
        elif re.fullmatch(r"av\d+", url, re.IGNORECASE):
            url = f"https://www.bilibili.com/video/{url}"
        elif not url.startswith("http"):
            raise PlatformError("无法识别的链接，请粘贴完整的 B 站视频地址")

        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query or "")
        path = parsed.path or ""

        # 短链 b23.tv：需要跟随跳转拿真实地址（同步解析，避免在 async 上下文外调用）
        if "b23.tv" in parsed.netloc:
            url = _resolve_short_url(url)
            parsed = urllib.parse.urlparse(url)
            query = urllib.parse.parse_qs(parsed.query or "")
            path = parsed.path

        bvid = None
        aid = None
        match = _BV_RE.search(path) or _BV_RE.search(url)
        if match:
            bvid = match.group(1)
        else:
            match = _AV_RE.search(path)
            if match:
                aid = int(match.group(1))
        if not bvid and not aid:
            # 空间/动态/合集等暂不支持
            raise PlatformError("未在链接中找到 BV 号 / av 号，暂不支持该类型的 B 站页面")

        page = 1
        if query.get("p"):
            try:
                page = max(1, int(query["p"][0]))
            except (TypeError, ValueError):
                page = 1

        start_time = 0
        if query.get("t"):
            raw = query["t"][0]
            digits = re.sub(r"[^\d]", "", raw)
            if digits:
                start_time = int(digits)

        return {
            "platform": self.name,
            "url": url,
            "bvid": bvid,
            "aid": aid,
            "page": page,
            "start_time": start_time,
        }

    # ------------------------------------------------------------ 元信息

    async def _ensure_aid_bvid(self, parsed: Dict[str, Any]) -> Tuple[int, str]:
        aid, bvid = parsed.get("aid"), parsed.get("bvid")
        if aid and bvid:
            return int(aid), str(bvid)
        params = {"bvid": bvid} if bvid else {"aid": aid}
        data = await self._api("https://api.bilibili.com/x/web-interface/view", params)
        return int(data["aid"]), str(data["bvid"])

    async def fetch_info(self, parsed: Dict[str, Any]) -> VideoInfo:
        params: Dict[str, Any] = {}
        if parsed.get("bvid"):
            params["bvid"] = parsed["bvid"]
        else:
            params["aid"] = parsed["aid"]

        data = await self._api("https://api.bilibili.com/x/web-interface/view", params)
        if not data:
            raise PlatformError("视频不存在或已被删除（也可能是私密/仅粉丝可见）")

        pages = data.get("pages") or []
        parts: List[VideoPart] = []
        for page in pages:
            parts.append(
                VideoPart(
                    index=int(page.get("page") or len(parts) + 1),
                    title=(page.get("part") or "").strip() or f"P{len(parts) + 1}",
                    duration=int(page.get("duration") or 0),
                    cid=int(page.get("cid") or 0) or None,
                    aid=int(data.get("aid") or 0),
                    bvid=str(data.get("bvid") or ""),
                )
            )
        if not parts:
            parts.append(
                VideoPart(
                    index=1,
                    title=(data.get("title") or "视频").strip(),
                    duration=int(data.get("duration") or 0),
                    cid=int(data.get("cid") or 0) or None,
                    aid=int(data.get("aid") or 0),
                    bvid=str(data.get("bvid") or ""),
                )
            )

        stat = data.get("stat") or {}
        info = VideoInfo(
            platform=self.name,
            video_id=str(data.get("bvid") or parsed.get("bvid") or parsed.get("aid")),
            title=(data.get("title") or "未命名视频").strip(),
            uploader=(data.get("owner") or {}).get("name", ""),
            uploader_id=str((data.get("owner") or {}).get("mid", "")),
            duration=int(data.get("duration") or 0),
            description=(data.get("desc") or "").strip(),
            cover=(data.get("pic") or "").replace("http://", "https://"),
            publish_time=data.get("pubdate"),
            view_count=stat.get("view"),
            like_count=stat.get("like"),
            parts=parts,
            url=parsed.get("url") or f"https://www.bilibili.com/video/{data.get('bvid')}",
            aid=int(data.get("aid") or 0),
        )
        if info.description:
            info.tags = _extract_tags(info.description)
        return info

    # ------------------------------------------------------------ WBI 签名

    async def _get_wbi_keys(self) -> Tuple[str, str]:
        """获取 WBI 签名用的 img_key / sub_key。

        ⚠️ 注意：nav 接口在**未登录**时返回 code=-101，但 data 里依然带着 wbi_img，
        所以这里必须用 optional=True 降级处理，否则匿名使用时直接报"账号未登录"。
        """
        if self._wbi_keys:
            return self._wbi_keys

        data = await self._api(
            "https://api.bilibili.com/x/web-interface/nav",
            optional=True,
            ignore_codes=(-101, -400, -403),
        )
        wbi = (data or {}).get("wbi_img") or {}
        img_url = wbi.get("img_url") or ""
        sub_url = wbi.get("sub_url") or ""
        img_key = Path(urllib.parse.urlparse(img_url).path).stem
        sub_key = Path(urllib.parse.urlparse(sub_url).path).stem

        if not img_key or not sub_key:
            # 兜底：换一个同样会返回 wbi_img 的匿名接口
            for url in (
                "https://api.bilibili.com/x/web-interface/wbi/index/top/rcmd?fresh_type=3&ps=1",
                "https://api.bilibili.com/x/web-interface/wbi/search/square?limit=1",
            ):
                fallback = await self._api(url, optional=True, ignore_codes=(-101, -400))
                wbi = (fallback or {}).get("wbi_img") or {}
                img_key = Path(urllib.parse.urlparse(wbi.get("img_url", "")).path).stem
                sub_key = Path(urllib.parse.urlparse(wbi.get("sub_url", "")).path).stem
                if img_key and sub_key:
                    break

        if not img_key or not sub_key:
            raise PlatformError(
                "无法获取 B 站 WBI 签名密钥（nav 接口未返回 wbi_img）。"
                "通常是网络被拦截或触发了风控，稍后重试即可。"
            )
        self._wbi_keys = (img_key, sub_key)
        return self._wbi_keys

    async def _sign(self, params: Dict[str, Any]) -> Dict[str, Any]:
        img_key, sub_key = await self._get_wbi_keys()
        raw = img_key + sub_key
        mixin_key = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB if i < len(raw))[:32]
        signed = dict(params)
        signed["wts"] = int(time.time())
        items = sorted(signed.items())
        # 过滤 !'()* 字符，再 urlencode
        query = urllib.parse.urlencode(
            [
                (k, re.sub(r"[!'()*]", "", str(v)))
                for k, v in items
            ]
        )
        signed["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
        return signed

    # ------------------------------------------------------------ 字幕

    async def list_subtitles(
        self, aid: int, bvid: str, cid: int
    ) -> List[Dict[str, Any]]:
        """返回字幕列表，优先人工(CC)，其次 AI 生成。

        ⚠️ 重要：B 站的字幕接口（player/wbi/v2）在**未登录**时返回 -101，
        即匿名请求拿不到任何字幕。因此这里用 optional=True 降级，
        由调用方决定是回退语音转写还是提示用户配置 Cookie。
        """
        params = await self._sign({"aid": aid, "bvid": bvid, "cid": cid})
        data = await self._api(
            "https://api.bilibili.com/x/player/wbi/v2",
            params,
            optional=True,
            ignore_codes=(-101, -352, -404, -400),
        )
        subtitles = (data.get("subtitle") or {}).get("subtitles") or []

        if not subtitles:
            # 老接口兜底（部分视频只有 v2 返回字幕，反之亦然）
            fallback = await self._api(
                "https://api.bilibili.com/x/player/v2",
                {"aid": aid, "bvid": bvid, "cid": cid},
                optional=True,
                ignore_codes=(-101, -352, -404, -400),
            )
            subtitles = ((fallback.get("subtitle") or {}).get("subtitles")) or []

        def rank(item: Dict[str, Any]) -> int:
            # ai_type / ai_status: 0=人工上传, 1=AI 生成
            ai_type = item.get("ai_type", item.get("ai_status", 0))
            return 1 if ai_type == 0 else 0

        ordered = sorted(subtitles, key=rank)
        for item in ordered:
            item.setdefault("_is_ai", not (item.get("ai_type", 0) == 0))
        if ordered:
            log.info(
                "字幕候选 %d 条：%s",
                len(ordered),
                [(s.get("lan_doc") or s.get("lan")) for s in ordered],
            )
        else:
            log.info("未取到字幕（未登录 Cookie 时属正常现象）")
        return ordered

    async def fetch_subtitle_text(self, subtitle: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
        url = subtitle.get("subtitle_url") or subtitle.get("url") or ""
        if not url:
            return "", []
        if url.startswith("//"):
            url = "https:" + url
        client = await self._get_client()
        await self._throttle()
        response = await client.get(url)
        response.raise_for_status()
        raw = response.text
        segments: List[Dict[str, Any]] = []

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict) and payload.get("body"):
            for item in payload["body"]:
                segments.append(
                    {
                        "from": float(item.get("from") or 0),
                        "to": float(item.get("to") or 0),
                        "text": (item.get("content") or "").strip(),
                    }
                )
        elif isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                segments.append(
                    {
                        "from": float(item.get("from") or 0),
                        "to": float(item.get("to") or 0),
                        "text": (item.get("content") or "").strip(),
                    }
                )
        else:
            # 新版 jsonl：每行一个 JSON
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                content = (item.get("content") or "").strip()
                if content:
                    segments.append(
                        {
                            "from": float(item.get("from") or 0),
                            "to": float(item.get("to") or 0),
                            "text": content,
                        }
                    )

        segments = [s for s in segments if s["text"]]
        text = "\n".join(f"[{format_duration(s['from'])}] {s['text']}" for s in segments)
        return text, segments

    async def fetch_transcript(
        self,
        parsed: Dict[str, Any],
        info: VideoInfo,
        part: VideoPart,
        *,
        allow_asr: bool = True,
    ) -> Transcript:
        aid = int(part.aid or info.aid or 0)
        bvid = str(part.bvid or info.video_id)
        cid = part.cid
        if not cid:
            pages = await self._api(
                "https://api.bilibili.com/x/player/pagelist",
                {"bvid": bvid} if bvid.startswith("BV") else {"aid": aid},
            )
            for item in pages or []:
                if int(item.get("page") or 0) == part.index:
                    cid = int(item.get("cid"))
                    break
            cid = cid or int((pages or [{}])[0].get("cid") or 0)
        if not cid:
            raise PlatformError("拿不到 cid，无法获取字幕")

        part.cid = cid
        subtitles = await self.list_subtitles(aid, bvid, cid)
        for subtitle in subtitles:
            text, segments = await self.fetch_subtitle_text(subtitle)
            if text.strip():
                is_ai = bool(subtitle.get("_is_ai"))
                return Transcript(
                    text=text,
                    source="subtitle_ai" if is_ai else "subtitle_manual",
                    language=str(subtitle.get("lan_doc") or subtitle.get("lan") or ""),
                    segments=segments,
                )

        if not allow_asr:
            if not self.settings.bili_cookie.strip():
                raise PlatformError(
                    "拿不到字幕。B 站字幕接口需要登录态：请在右上角「设置」里粘贴浏览器 Cookie "
                    "（登录 B 站后 F12 → Network → 任意请求 → 复制 Cookie），"
                    "或在设置里开启「语音转写」作为兜底。"
                )
            raise PlatformError(
                "该视频没有任何字幕（人工 CC 与 B 站 AI 字幕都没有）。"
                "可在设置里开启「语音转写」；若视频有合集/多P，也请确认选对了分P。"
            )

        # 回退：下载音频 -> ASR
        from ..asr import transcribe_audio
        from ..core.utils import safe_filename, try_delete

        audio_path = await self.download_audio(parsed, info, part)
        try:
            transcript = await transcribe_audio(audio_path, self.settings)
            return transcript
        finally:
            # 转写用的音频属于临时文件，尽力删除但不影响主流程
            try:
                try_delete(audio_path, log)
                wav = audio_path.with_suffix(".wav")
                if wav != audio_path:
                    try_delete(wav, log)
            except Exception as exc:  # noqa: BLE001
                log.warning("清理临时音频失败：%s", exc)

    # ------------------------------------------------------------ 音频下载

    async def download_audio(
        self, parsed: Dict[str, Any], info: VideoInfo, part: VideoPart
    ) -> Path:
        """下载该分 P 的音频流（优先最低清晰度，够 ASR 用且体积小）。"""
        from ..core.config import MEDIA_DIR, ensure_dirs

        ensure_dirs()
        aid = int(part.aid or info.aid or 0)
        bvid = str(part.bvid or info.video_id)
        cid = int(part.cid or 0)
        if not cid:
            raise PlatformError("缺少 cid，无法下载音频")

        params = await self._sign({"avid": aid, "bvid": bvid, "cid": cid, "fnval": 16, "fourk": 1})
        data = await self._api(
            "https://api.bilibili.com/x/player/wbi/playurl",
            params,
            optional=True,
            ignore_codes=(-101, -10403),
        )
        dash = data.get("dash") or {}
        audios = dash.get("audio") or []
        if not audios:
            flac = (dash.get("flac") or {}).get("audio")
            audios = [flac] if flac else []
        if not audios:
            raise PlatformError(
                "无法获取音频流（可能是付费/会员专享视频，或需要 Cookie 登录）。"
            )

        # 选码率最低的，节省带宽
        best = sorted(audios, key=lambda a: int(a.get("bandwidth") or 0))[0]
        audio_url = best.get("baseUrl") or best.get("base_url")
        if not audio_url:
            raise PlatformError("音频流地址为空")

        client = await self._get_client()
        safe_title = safe_filename(f"{info.title}_{part.title}")[:60]
        target = MEDIA_DIR / f"{info.video_id}_p{part.index}_{safe_title}.m4s"

        headers = {
            "Referer": "https://www.bilibili.com/",
            "User-Agent": self.settings.bili_user_agent,
            "Range": "bytes=0-",
        }
        if self.settings.bili_cookie.strip():
            headers["Cookie"] = self.settings.bili_cookie.strip()

        await self._throttle()
        async with client.stream("GET", audio_url, headers=headers) as response:
            if response.status_code not in (200, 206):
                raise PlatformError(f"下载音频失败，HTTP {response.status_code}")
            with target.open("wb") as handle:
                async for chunk in response.aiter_bytes(1024 * 256):
                    handle.write(chunk)
        log.info("音频已下载：%s (%.1f MB)", target.name, target.stat().st_size / 1024 / 1024)
        return target


# ---------------------------------------------------------------- 辅助函数


def _resolve_short_url(url: str) -> str:
    """同步解析 b23.tv 短链（用一次性的 httpx 同步客户端）。"""
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            response = client.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    )
                },
            )
            return str(response.url)
    except httpx.HTTPError as exc:
        raise PlatformError(f"解析 b23.tv 短链失败：{exc}") from exc


def _extract_tags(description: str) -> List[str]:
    return [tag.strip() for tag in re.findall(r"#([^#\s]{1,20})#?", description)][:10]
