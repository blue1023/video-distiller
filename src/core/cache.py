"""转写/字幕缓存。

同一期视频重复生成笔记时，最贵的一步是"抓字幕 + 下载音频 + 语音转写"，
这里按 (平台, 视频ID, 分P, ASR 配置指纹) 做缓存，命中就直接复用文本。

指纹里带上 mtime/size，保证本地视频文件被替换后缓存自动失效。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import CACHE_DIR, Settings, ensure_dirs, get_settings
from .utils import get_logger, try_delete
from ..platforms.base import Transcript

log = get_logger("cache")


def cache_root(settings: Optional[Settings] = None) -> Path:
    settings = settings or get_settings()
    if settings.cache_dir.strip():
        path = Path(settings.cache_dir.strip())
        if not path.is_absolute():
            from .config import BASE_DIR

            path = BASE_DIR / path
    else:
        path = CACHE_DIR
    (path / "transcripts").mkdir(parents=True, exist_ok=True)
    return path


def fingerprint(
    platform: str,
    video_id: str,
    part: int = 1,
    *,
    extra: str = "",
) -> str:
    raw = f"{platform}|{video_id}|{part}|{extra}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _path_for(
    platform: str,
    video_id: str,
    part: int,
    extra: str,
    settings: Optional[Settings] = None,
) -> Path:
    """缓存文件路径。

    ⚠️ settings 必须一路透传：否则自定义 cache_dir 会被忽略，
    写入与统计/清理会用两个不同目录（这个 bug 真的踩过）。
    """
    return (
        cache_root(settings)
        / "transcripts"
        / f"{fingerprint(platform, video_id, part, extra=extra)}.json"
    )


def load(
    platform: str,
    video_id: str,
    part: int = 1,
    *,
    extra: str = "",
    settings: Optional[Settings] = None,
) -> Optional[Transcript]:
    settings = settings or get_settings()
    if not settings.cache_enabled:
        return None
    path = _path_for(platform, video_id, part, extra, settings)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("缓存读取失败，忽略：%s", exc)
        try_delete(path)
        return None

    text = str(payload.get("text") or "")
    if not text.strip():
        return None
    log.info(
        "命中转写缓存：%s / %s P%s（%d 字，来源 %s）",
        platform,
        video_id,
        part,
        len(text),
        payload.get("source"),
    )
    return Transcript(
        text=text,
        source=str(payload.get("source") or "cache"),
        language=str(payload.get("language") or ""),
        segments=payload.get("segments") or [],
    )


def store(
    platform: str,
    video_id: str,
    part: int,
    transcript: Transcript,
    *,
    extra: str = "",
    settings: Optional[Settings] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Optional[Path]:
    settings = settings or get_settings()
    if not settings.cache_enabled or transcript.is_empty:
        return None
    ensure_dirs()
    path = _path_for(platform, video_id, part, extra, settings)
    payload = {
        "platform": platform,
        "video_id": video_id,
        "part": part,
        "source": transcript.source,
        "language": transcript.language,
        "chars": len(transcript.text),
        "text": transcript.text,
        # 片段可能很大，缓存里只留必要字段
        "segments": (transcript.segments or [])[:2000],
        "cached_at": time.time(),
        "meta": meta or {},
    }
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        log.info("已写入转写缓存：%s（%d 字）", path.name, len(transcript.text))
        return path
    except OSError as exc:
        log.warning("写缓存失败（不影响主流程）：%s", exc)
        return None


def stats(settings: Optional[Settings] = None) -> Dict[str, Any]:
    """缓存统计：条数、占用空间、最近写入时间。"""
    settings = settings or get_settings()
    root = cache_root(settings) / "transcripts"
    files = [p for p in root.glob("*.json") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    latest = max((p.stat().st_mtime for p in files), default=0)
    return {
        "count": len(files),
        "bytes": total,
        "mb": round(total / 1024 / 1024, 2),
        "latest": latest,
        "dir": str(root),
    }


def clear(settings: Optional[Settings] = None) -> Dict[str, Any]:
    settings = settings or get_settings()
    root = cache_root(settings) / "transcripts"
    removed = 0
    failed = 0
    for path in list(root.glob("*.json")):
        if try_delete(path, log):
            removed += 1
        else:
            failed += 1
    if failed:
        log.warning("有 %d 个缓存文件删除失败（可能被占用），已清空内容", failed)
    return {"removed": removed, "failed": failed, **stats(settings)}
