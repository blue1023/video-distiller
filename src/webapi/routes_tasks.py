"""任务相关路由：创建、查询、SSE 进度、取消。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..core.config import get_settings
from ..core.db import bus, count_tasks, create_task, get_task, list_tasks
from ..pipeline import cancel_task, is_running, spawn_task
from ..platforms import resolve
from ..platforms.base import PlatformError
from .schemas import CreateTaskRequest, TaskOut

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

STYLE_LABELS = {"concise": "精简", "detailed": "详尽", "academic": "学术"}


def _serialize(task: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not task:
        return None
    from ..core.db import get_note_by_task

    note = get_note_by_task(task["id"])
    data = dict(task)
    data["note_id"] = note["id"] if note else None
    data["running"] = is_running(task["id"])
    return data


@router.post("", response_model=TaskOut)
async def create(payload: CreateTaskRequest) -> Dict[str, Any]:
    url = (payload.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="请填写视频链接")

    settings = get_settings()
    platform_name = payload.platform
    if not platform_name:
        try:
            platform_name = resolve(url, settings).name
        except PlatformError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    note_style = payload.note_style or settings.note_style or "detailed"
    task = create_task(
        url=url, platform=platform_name, note_style=note_style
    )
    spawn_task(task["id"])
    created = get_task(task["id"])
    return _serialize(created)  # type: ignore[return-value]


@router.get("")
async def index(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    tasks = [_serialize(t) for t in list_tasks(limit=limit, offset=offset)]
    return {"total": count_tasks(), "items": tasks}


@router.get("/{task_id}", response_model=TaskOut)
async def detail(task_id: str) -> Dict[str, Any]:
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _serialize(task) or {}


@router.get("/{task_id}/events")
async def events(task_id: str, request: Request) -> StreamingResponse:
    """SSE 进度流：先补发历史事件，再实时推送。"""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    queue = bus.subscribe(task_id)

    async def generator():
        try:
            snapshot = {
                "stage": task.get("stage"),
                "progress": task.get("progress", 0),
                "message": task.get("message"),
                "status": task.get("status"),
                "replay": True,
            }
            yield f"data: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
            for event in bus.history(task_id):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            if task.get("status") in ("success", "failed", "cancelled"):
                yield "event: close\ndata: {}\n\n"
                return

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("status") in ("success", "failed", "cancelled") or event.get(
                    "stage"
                ) in ("done", "failed", "cancelled"):
                    yield "event: close\ndata: {}\n\n"
                    break
        finally:
            bus.unsubscribe(task_id, queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{task_id}/cancel")
async def cancel(task_id: str) -> Dict[str, Any]:
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not is_running(task_id):
        return {"ok": False, "message": "任务当前不在运行中"}
    cancel_task(task_id)
    return {"ok": True, "message": "已请求取消"}


@router.delete("/{task_id}")
async def remove(task_id: str) -> Dict[str, Any]:
    if is_running(task_id):
        cancel_task(task_id)
        await asyncio.sleep(0.2)
    from ..core.db import delete_task

    delete_task(task_id)
    return {"ok": True, "message": "任务已删除"}
