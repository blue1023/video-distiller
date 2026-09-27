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
from .schemas import CreateBatchRequest, CreateTaskRequest, TaskOut

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

STYLE_LABELS = {"concise": "精简", "detailed": "详尽", "academic": "学术"}
#: 一批里最多提交多少个链接，避免一次性把 B 站请求打爆
MAX_BATCH = 50


def _serialize(task: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not task:
        return None
    from ..core.db import get_note_by_task

    note = get_note_by_task(task["id"])
    data = dict(task)
    data["note_id"] = note["id"] if note else None
    data["running"] = is_running(task["id"])
    data["queued"] = data.get("status") == "pending"
    return data


def split_urls(text: Optional[str], urls: Optional[List[str]] = None) -> List[str]:
    """把多行文本 / 数组里的链接解析成去重后的列表（保持原顺序）。"""
    candidates: List[str] = []
    if urls:
        candidates.extend(urls)
    if text:
        for line in text.replace(",", "\n").splitlines():
            line = line.strip()
            if not line:
                continue
            # 允许 "标题 https://..." 这种粘贴形式，只取链接部分
            for token in line.split():
                if token.startswith(("http://", "https://", "local://", "BV", "av")):
                    candidates.append(token)
                    break
            else:
                candidates.append(line)

    cleaned: List[str] = []
    seen: set = set()
    for item in candidates:
        value = item.strip().strip("，,;；")
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(value)
    return cleaned


def _prepare_task(url: str, note_style: str, batch_id: Optional[str], refresh: bool):
    """校验链接并落库。返回 (task_dict, error_str)。"""
    from ..core.db import update_task

    settings = get_settings()
    platform = resolve(url, settings)
    # 本地文件先确认还在
    if platform.name == "local":
        from ..platforms.local_file import resolve_file

        resolve_file(url[len("local://") :])
    task = create_task(
        url=url,
        platform=platform.name,
        note_style=note_style,
        batch_id=batch_id,
        refresh=refresh,
    )
    if refresh:
        update_task(task["id"], message="已加入队列（忽略缓存，强制重新抓取）")
    return task


@router.post("", response_model=TaskOut)
async def create(payload: CreateTaskRequest) -> Dict[str, Any]:
    url = (payload.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="请填写视频链接")

    settings = get_settings()
    note_style = payload.note_style or settings.note_style or "detailed"
    try:
        task = _prepare_task(url, note_style, None, bool(payload.refresh))
    except PlatformError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    spawn_task(task["id"])
    return _serialize(get_task(task["id"]))  # type: ignore[return-value]


@router.post("/batch")
async def create_batch(payload: CreateBatchRequest) -> Dict[str, Any]:
    """批量提交：一次粘贴多条链接，按全局并发上限依次执行。"""
    from ..core.db import create_batch as db_create_batch
    from ..core.db import update_task

    raw_urls = split_urls(payload.text, payload.urls)
    if not raw_urls:
        raise HTTPException(status_code=400, detail="没有解析到任何链接")
    if len(raw_urls) > MAX_BATCH:
        raise HTTPException(
            status_code=400, detail=f"一次最多提交 {MAX_BATCH} 条链接，当前 {len(raw_urls)} 条"
        )

    settings = get_settings()
    note_style = payload.note_style or settings.note_style or "detailed"
    batch = db_create_batch(raw_urls, note_style=note_style, raw=payload.text)

    created: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    for url in raw_urls:
        try:
            task = _prepare_task(url, note_style, batch["id"], bool(payload.refresh))
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响整批
            skipped.append({"url": url, "reason": str(exc)})
            continue
        update_task(task["id"], message=f"批次 {batch['id'][-6:]} 已入队")
        created.append(task)
        spawn_task(task["id"])

    if not created:
        raise HTTPException(
            status_code=400,
            detail="这批链接都没有通过校验：" + "；".join(f"{s['url']}（{s['reason']}）" for s in skipped[:3]),
        )
    return {
        "batch_id": batch["id"],
        "total": len(raw_urls),
        "created": len(created),
        "skipped": skipped,
        "tasks": [_serialize(t) for t in created],
    }


@router.get("/batches")
async def batches(limit: int = Query(30, ge=1, le=200)) -> Dict[str, Any]:
    from ..core.db import list_batches

    return {"items": list_batches(limit=limit)}


@router.get("/batches/{batch_id}")
async def batch_detail(batch_id: str) -> Dict[str, Any]:
    from ..core.db import get_batch

    batch = get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    batch["tasks"] = [_serialize(t) for t in batch["tasks"]]
    return batch


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


@router.post("/{task_id}/refresh")
async def refresh(task_id: str, use_cache: bool = Query(False, description="是否允许复用转写缓存")) -> Dict[str, Any]:
    """基于原任务重新生成（默认忽略缓存，强制重新抓字幕/转写）。"""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        new_task = _prepare_task(
            task["url"],
            task.get("note_style") or get_settings().note_style,
            task.get("batch_id"),
            not use_cache,
        )
    except PlatformError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    spawn_task(new_task["id"])
    return _serialize(get_task(new_task["id"]))  # type: ignore[return-value]


@router.delete("/{task_id}")
async def remove(task_id: str) -> Dict[str, Any]:
    if is_running(task_id):
        cancel_task(task_id)
        await asyncio.sleep(0.2)
    from ..core.db import delete_task

    delete_task(task_id)
    return {"ok": True, "message": "任务已删除"}
