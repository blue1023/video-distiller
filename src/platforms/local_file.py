"""本地视频文件的"平台"实现（预留扩展点②的落地）。

把上传到 data/uploads 的文件当成一个"单P视频"：
- 不需要网络，转写走 src/asr.py（建议用本地 faster-whisper，完全离线）；
- 输出的笔记结构、导图、导出流程与 B 站视频完全一致。

URL 约定：local://<视频ID>，其中视频ID 就是去掉扩展名的文件名。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.config import UPLOAD_DIR, Settings, get_settings
from ..core.utils import get_logger, safe_filename
from .base import BasePlatform, PlatformError, Transcript, VideoInfo, VideoPart

log = get_logger("platform.local")

VIDEO_SUFFIXES = {
    ".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm", ".m4v", ".ts",
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma",
}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma"}
SCHEME = "local://"


def is_local(url: str) -> bool:
    return (url or "").strip().startswith(SCHEME)


def upload_dir() -> Path:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return UPLOAD_DIR


def resolve_file(video_id: str) -> Path:
    """按视频ID 找到上传目录里的文件（防目录穿越）。"""
    cleaned = re.sub(r"[\\/]+", "_", (video_id or "").strip())
    if not cleaned:
        raise PlatformError("本地视频ID 为空")
    target = upload_dir() / cleaned
    root = upload_dir().resolve()
    try:
        resolved = target.resolve()
    except OSError as exc:
        raise PlatformError(f"无法解析本地文件路径：{exc}") from exc
    if not str(resolved).startswith(str(root)):
        raise PlatformError("非法的本地文件路径")
    if not resolved.exists() or not resolved.is_file():
        raise PlatformError(
            f"本地文件不存在：{cleaned}。可能已被删除或移动，请重新上传。"
        )
    return resolved


@dataclass
class LocalProbe:
    text: str
    source: str
    language: str = ""


class LocalFilePlatform(BasePlatform):
    name = "local"
    display_name = "本地视频"
    supports_audio = True

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()

    def match(self, url: str) -> bool:
        return is_local(url)

    def normalize(self, url: str) -> Dict[str, Any]:
        raw = (url or "").strip()
        if not raw.startswith(SCHEME):
            raise PlatformError("本地视频链接必须以 local:// 开头")
        video_id = raw[len(SCHEME) :].strip()
        if not video_id:
            raise PlatformError("本地视频ID 为空")
        path = resolve_file(video_id)
        return {
            "platform": self.name,
            "url": raw,
            "video_id": video_id,
            "path": str(path),
            "page": 1,
            "start_time": 0,
        }

    async def fetch_info(self, parsed: Dict[str, Any]) -> VideoInfo:
        path = Path(parsed["path"])
        stat = path.stat()
        title = path.stem
        if title.startswith("【") or len(title) > 60:
            title = title[:60]
        is_audio = path.suffix.lower() in AUDIO_SUFFIXES
        return VideoInfo(
            platform=self.name,
            video_id=parsed["video_id"],
            title=title,
            uploader="本地文件",
            duration=0,
            description=(
                f"本地{'音频' if is_audio else '视频'}文件：{path.name}"
                f"（{stat.st_size / 1024 / 1024:.1f} MB）"
            ),
            parts=[
                VideoPart(
                    index=1,
                    title=path.stem,
                    duration=0,
                    cid=1,
                )
            ],
            url=parsed["url"],
            extra={"path": str(path), "size": stat.st_size, "mtime": stat.st_mtime},
        )

    async def fetch_transcript(
        self,
        parsed: Dict[str, Any],
        info: VideoInfo,
        part: VideoPart,
        *,
        allow_asr: bool = True,
    ) -> Transcript:
        from ..asr import ASRError, transcribe_audio

        provider = (self.settings.asr_provider or "none").strip().lower()
        if provider in ("none", "", "off", "disabled"):
            raise PlatformError(
                "本地视频没有现成字幕，必须先开启语音转写。"
                "推荐在设置里选 faster-whisper（本地离线、免费），"
                "首次运行会自动下载模型文件。"
            )
        if provider in ("faster-whisper", "faster_whisper", "local"):
            try:
                import faster_whisper  # type: ignore  # noqa: F401
            except ImportError as exc:
                raise PlatformError(
                    "本地视频需要 faster-whisper 才能离线转写，请先安装："
                    "pip install faster-whisper"
                ) from exc

        path = Path(parsed["path"])
        try:
            transcript = await transcribe_audio(path, self.settings)
        except ASRError as exc:
            raise PlatformError(f"本地视频转写失败：{exc}") from exc
        log.info("本地文件转写完成：%s（%d 字）", path.name, len(transcript.text))
        return transcript

    async def aclose(self) -> None:  # pragma: no cover
        return None
