"""平台包。"""

from .base import BasePlatform, PlatformError, Transcript, VideoInfo, VideoPart  # noqa: F401
from .registry import available_platforms, get_platform, register, resolve  # noqa: F401
