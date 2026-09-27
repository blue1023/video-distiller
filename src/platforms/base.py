"""平台适配器抽象层。

新增平台（YouTube / 抖音 / 小宇宙 …）只需：
  1. 继承 BasePlatform，实现 match / fetch_info / fetch_transcript；
  2. 在 registry.py 里注册。

上层 Pipeline 只认这套接口，不关心具体平台。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class VideoPart:
    """一个分 P / 一集。"""

    index: int
    title: str
    duration: int = 0
    cid: Optional[int] = None
    aid: Optional[int] = None
    bvid: Optional[str] = None


@dataclass
class VideoInfo:
    platform: str
    video_id: str  # BV 号 / 其它平台 ID
    title: str
    uploader: str = ""
    uploader_id: str = ""
    duration: int = 0
    description: str = ""
    cover: str = ""
    publish_time: Optional[int] = None
    view_count: Optional[int] = None
    like_count: Optional[int] = None
    tags: List[str] = field(default_factory=list)
    parts: List[VideoPart] = field(default_factory=list)
    url: str = ""
    aid: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "video_id": self.video_id,
            "title": self.title,
            "uploader": self.uploader,
            "duration": self.duration,
            "description": self.description,
            "cover": self.cover,
            "view_count": self.view_count,
            "like_count": self.like_count,
            "tags": self.tags,
            "parts": [p.__dict__ for p in self.parts],
            "url": self.url,
        }


@dataclass
class Transcript:
    """字幕 / 转写结果。"""

    text: str
    source: str  # subtitle_manual | subtitle_ai | asr_openai | asr_local
    language: str = ""
    segments: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.text or "").strip()


class PlatformError(RuntimeError):
    """平台层可预期的错误（链接不合法、视频不存在、风控等）。"""


class BasePlatform(abc.ABC):
    """平台能力接口。"""

    name: str = "base"
    display_name: str = "通用平台"
    #: 是否支持"无字幕时下载音频 + 转写"
    supports_audio: bool = True

    @abc.abstractmethod
    def match(self, url: str) -> bool:
        """判断这个链接是否属于本平台。"""

    @abc.abstractmethod
    def normalize(self, url: str) -> Dict[str, Any]:
        """把各种形态的链接解析成 {video_id, page, start_time, ...}。"""

    @abc.abstractmethod
    async def fetch_info(self, parsed: Dict[str, Any]) -> VideoInfo:
        """拉取视频元信息（标题、UP 主、分 P 列表…）。"""

    @abc.abstractmethod
    async def fetch_transcript(
        self,
        parsed: Dict[str, Any],
        info: VideoInfo,
        part: VideoPart,
        *,
        allow_asr: bool = True,
    ) -> Transcript:
        """获取字幕文本；平台无字幕时可按 allow_asr 决定是否回退到音频转写。"""

    async def aclose(self) -> None:  # pragma: no cover - 默认无需释放
        return None
