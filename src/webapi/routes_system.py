"""系统相关路由：健康检查、平台列表、设置读写、大模型自检。"""

from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from ..core.config import get_settings, llm_ready, save_settings
from ..core.db import count_tasks
from ..core.utils import get_logger
from ..llm.client import LLMError, chat
from ..platforms import available_platforms
from ..platforms import registry as platform_registry
from .schemas import SettingsPatch

router = APIRouter(prefix="/api", tags=["system"])
log = get_logger("api.system")


@router.get("/health")
async def health() -> Dict[str, Any]:
    settings = get_settings()
    return {
        "ok": True,
        "time": time.time(),
        "tasks": count_tasks(),
        "llm_configured": llm_ready(settings),
        "llm_model": settings.llm_model,
        "asr_provider": settings.asr_provider,
    }


@router.get("/platforms")
async def platforms() -> Dict[str, Any]:
    return {"items": available_platforms()}


@router.get("/settings")
async def read_settings() -> Dict[str, Any]:
    settings = get_settings()
    return {"settings": settings.public(), "llm_ready": llm_ready(settings)}


@router.patch("/settings")
async def patch_settings(payload: SettingsPatch) -> Dict[str, Any]:
    patch = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    if not patch:
        return {"settings": get_settings().public(), "message": "没有需要更新的字段"}
    settings = save_settings(patch)
    # 平台实例持有旧配置（Cookie / UA / 间隔），需要重建
    platform_registry.reset_instances()
    return {
        "settings": settings.public(),
        "llm_ready": llm_ready(settings),
        "message": "设置已保存并生效",
    }


@router.post("/settings/test-llm")
async def test_llm() -> Dict[str, Any]:
    settings = get_settings()
    if not llm_ready(settings):
        raise HTTPException(status_code=400, detail="请先填写大模型 API Key")
    try:
        result = await chat(
            [
                {"role": "system", "content": "你是连通性测试助手，只输出 JSON。"},
                {"role": "user", "content": '请返回 {"ok": true, "msg": "连接正常"}'},
            ],
            settings,
            max_tokens=64,
        )
    except LLMError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "model": result.get("model"),
        "elapsed": round(float(result.get("elapsed") or 0), 2),
        "preview": (result.get("content") or "")[:200],
    }


@router.post("/settings/test-bilibili")
async def test_bilibili() -> Dict[str, Any]:
    """用一条公开视频验证 B 站接口是否可达、Cookie 是否有效。"""
    from ..platforms import get_platform
    from ..platforms.base import PlatformError

    settings = get_settings()
    platform = get_platform("bilibili", settings)
    try:
        parsed = platform.normalize("https://www.bilibili.com/video/BV1GJ411x7h7")
        info = await platform.fetch_info(parsed)
    except PlatformError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await platform.aclose()
    return {
        "ok": True,
        "title": info.title,
        "uploader": info.uploader,
        "cookie": bool(settings.bili_cookie.strip()),
    }
