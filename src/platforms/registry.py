"""平台注册表：链接进来 -> 找到能处理它的平台实例。

扩展新平台只需在 _FACTORIES 里加一行。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Type

from ..core.config import Settings, get_settings
from ..core.utils import get_logger
from .base import BasePlatform, PlatformError

log = get_logger("platforms")

# 平台类在首次使用时才实例化，避免导入即读配置
_FACTORIES: Dict[str, Type[BasePlatform]] = {}
_INSTANCES: Dict[str, BasePlatform] = {}
_ORDER: List[str] = []


def register(platform_cls: Type[BasePlatform]) -> Type[BasePlatform]:
    _FACTORIES[platform_cls.name] = platform_cls
    if platform_cls.name not in _ORDER:
        _ORDER.append(platform_cls.name)
    return platform_cls


def _load_builtin() -> None:
    if _FACTORIES:
        return
    from .bilibili import BilibiliPlatform

    register(BilibiliPlatform)


def available_platforms() -> List[Dict[str, str]]:
    _load_builtin()
    return [
        {
            "name": name,
            "display_name": _FACTORIES[name].display_name,
            "supports_local_upload": "false",
            "supports_audio": str(_FACTORIES[name].supports_audio).lower(),
        }
        for name in _ORDER
    ]


def get_platform(name: str, settings: Optional[Settings] = None) -> BasePlatform:
    _load_builtin()
    factory = _FACTORIES.get(name)
    if factory is None:
        raise PlatformError(f"未注册的平台：{name}")
    instance = _INSTANCES.get(name)
    if instance is None:
        instance = factory(settings or get_settings())
        _INSTANCES[name] = instance
    return instance


def resolve(url: str, settings: Optional[Settings] = None) -> BasePlatform:
    """按链接特征挑选平台。"""
    _load_builtin()
    errors: List[str] = []
    for name in _ORDER:
        platform = get_platform(name, settings)
        try:
            if platform.match(url):
                return platform
        except Exception as exc:  # pragma: no cover - 匹配逻辑不应抛错
            errors.append(f"{name}: {exc}")
    detail = f"（{'; '.join(errors)}）" if errors else ""
    raise PlatformError(f"没有平台能处理这个链接，目前仅支持 B 站视频{detail}")


def reset_instances() -> None:
    """设置变更后让平台实例用新配置重建。"""
    _INSTANCES.clear()
