"""任务流水线：链接 -> 元信息 -> 字幕/转写 -> LLM -> 落盘产物。

任务在后台 asyncio 里跑，进度通过事件总线（SSE）推给前端。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .core.config import NOTES_DIR, get_settings
from .core import cache
from .core.db import create_note, delete_task, emit, get_task, update_task
from .core.utils import get_logger, safe_filename, to_json, tree_to_xmind
from .llm.notes import generate_note
from .platforms import resolve
from .platforms.base import PlatformError, Transcript, VideoInfo, VideoPart
from .platforms.local_file import LocalFilePlatform

log = get_logger("pipeline")

# 进度刻度（前端进度条就是按这个走的）
PROGRESS = {
    "queued": 0.02,
    "parsing": 0.05,
    "info": 0.15,
    "subtitle": 0.35,
    "cache": 0.45,
    "asr": 0.50,
    "structuring": 0.60,
    "rendering": 0.85,
    "mindmap": 0.92,
    "saving": 0.97,
    "done": 1.0,
}

_running: Dict[str, asyncio.Task] = {}

#: 全局并发闸门（同时跑几个任务）。见 configure_concurrency()。
_semaphore: Optional[asyncio.Semaphore] = None
_semaphore_limit: int = 0
_waiting: int = 0


def configure_concurrency(limit: Optional[int] = None) -> int:
    """按配置重建并发闸门；返回当前上限。"""
    global _semaphore, _semaphore_limit
    settings = get_settings()
    target = int(limit if limit is not None else (settings.max_concurrency or 1))
    target = max(1, min(8, target))
    if _semaphore is None or target != _semaphore_limit:
        _semaphore = asyncio.Semaphore(target)
        _semaphore_limit = target
        log.info("任务并发上限设为 %d", target)
    return target


def _gate() -> asyncio.Semaphore:
    if _semaphore is None:
        configure_concurrency()
    assert _semaphore is not None
    return _semaphore


def queue_status() -> Dict[str, int]:
    return {"running": len(_running), "waiting": _waiting, "limit": _semaphore_limit or 0}


@dataclass
class Artifacts:
    note_id: str
    markdown: str
    md_path: Path
    tree: Dict[str, Any] = field(default_factory=dict)
    mindmap_path: Optional[Path] = None
    xmind_path: Optional[Path] = None
    opml_path: Optional[Path] = None


def spawn_task(task_id: str) -> asyncio.Task:
    """把任务丢到后台执行（不阻塞 HTTP 请求）。"""
    task = asyncio.create_task(_run_guarded(task_id))
    _running[task_id] = task
    task.add_done_callback(lambda _t: _running.pop(task_id, None))
    return task


def is_running(task_id: str) -> bool:
    task = _running.get(task_id)
    return bool(task and not task.done())


def cancel_task(task_id: str) -> bool:
    task = _running.get(task_id)
    if task and not task.done():
        task.cancel()
        return True
    return False


async def _run_guarded(task_id: str) -> None:
    global _waiting
    try:
        # 并发闸门：排队期间任务状态保持 pending
        gate = _gate()
        if gate.locked():
            _waiting += 1
            await asyncio.to_thread(
                update_task,
                task_id,
                message=f"排队中（当前并发上限 {_semaphore_limit}）…",
            )
            await emit(
                task_id,
                "queued",
                PROGRESS["queued"],
                f"排队中，等待空闲额度（上限 {_semaphore_limit}）…",
                status="pending",
            )
        try:
            async with gate:
                await run_pipeline(task_id)
        finally:
            if gate.locked():
                _waiting = max(0, _waiting - 1)
    except asyncio.CancelledError:  # pragma: no cover - 用户取消
        await asyncio.to_thread(
            update_task, task_id, status="cancelled", stage="cancelled", message="已取消"
        )
        await emit(task_id, "cancelled", 0, "任务已取消", status="cancelled")
        raise
    except Exception as exc:  # noqa: BLE001 - 兜底：任何异常都要写到任务上
        log.exception("任务失败：%s", task_id)
        message = _humanize_error(exc)
        await asyncio.to_thread(
            update_task,
            task_id,
            status="failed",
            stage="failed",
            error=message,
            message=message,
            finished_at=time.time(),
        )
        await emit(task_id, "failed", 0, message, status="failed", error=message)


def _humanize_error(exc: Exception) -> str:
    if isinstance(exc, PlatformError):
        return str(exc)
    text = str(exc) or exc.__class__.__name__
    lowered = text.lower()
    if "api key" in lowered or "401" in text or "unauthorized" in lowered:
        return f"大模型鉴权失败，请检查 API Key 与接口地址：{text}"
    if "timeout" in lowered or "timed out" in lowered:
        return f"请求超时（视频过长或网络不稳定）：{text}"
    return text


async def run_pipeline(task_id: str) -> Artifacts:
    settings = get_settings()
    task = await asyncio.to_thread(get_task, task_id)
    if not task:
        raise RuntimeError(f"任务不存在：{task_id}")

    url = task["url"]
    # 任务级风格优先（提交时选的），否则用全局设置
    note_style = (task.get("note_style") or settings.note_style or "detailed")

    # 1. 解析链接
    await emit(task_id, "parsing", PROGRESS["parsing"], "正在解析链接…")
    platform = resolve(url, settings)
    parsed = platform.normalize(url)

    # 2. 元信息
    await emit(task_id, "info", PROGRESS["info"], f"正在获取视频信息（{platform.display_name}）…")
    info: VideoInfo = await platform.fetch_info(parsed)

    parts = info.parts or []
    page = int(parsed.get("page") or 1)
    part: Optional[VideoPart] = next((p for p in parts if p.index == page), None)
    if part is None:
        if len(parts) == 1:
            part = parts[0]
        else:
            raise PlatformError(
                f"视频共有 {len(parts)} 个分 P，p={page} 不存在。请检查链接里的 ?p= 参数。"
            )

    await asyncio.to_thread(
        update_task,
        task_id,
        video_id=info.video_id,
        title=info.title,
        uploader=info.uploader,
        duration=part.duration or info.duration,
        cover=info.cover,
        part=part.index,
        message=f"已获取：{info.title}",
    )
    can_asr = (settings.asr_provider or "none").lower() not in ("none", "", "off", "disabled")
    refresh = bool(task.get("refresh"))
    cache_extra = _cache_extra(settings, info, part)

    # 3. 字幕 / 转写（先查缓存）
    transcript: Optional[Transcript] = None
    if not refresh:
        cached = cache.load(info.platform, info.video_id, part.index, extra=cache_extra)
        if cached is not None:
            transcript = cached
            await asyncio.to_thread(update_task, task_id, from_cache=1)
            await emit(
                task_id,
                "cache",
                PROGRESS["cache"],
                f"命中转写缓存，跳过抓取（{len(cached.text)} 字）",
                from_cache=True,
            )

    if transcript is None:
        stage_message = (
            "正在获取字幕…" if not can_asr else "正在获取字幕（无字幕将自动转写音频）…"
        )
        await emit(task_id, "subtitle", PROGRESS["subtitle"], stage_message)

        transcript = await platform.fetch_transcript(
            parsed, info, part, allow_asr=can_asr
        )
        if transcript.is_empty:
            raise PlatformError("字幕内容为空，无法生成笔记")
        # 只有真正抓到的结果才写缓存
        cache.store(
            info.platform,
            info.video_id,
            part.index,
            transcript,
            extra=cache_extra,
            meta={"title": info.title, "uploader": info.uploader},
        )

    is_asr = transcript.source.startswith("asr")
    await asyncio.to_thread(
        update_task,
        task_id,
        transcript_src=transcript.source,
        transcript_chars=len(transcript.text),
    )
    if transcript.source != "cache":
        await emit(
            task_id,
            "asr" if is_asr else "subtitle",
            PROGRESS["asr"] if is_asr else PROGRESS["subtitle"] + 0.05,
            f"已获得文本 {len(transcript.text)} 字（{_source_label(transcript.source)}）",
            transcript_source=transcript.source,
            transcript_chars=len(transcript.text),
        )

    # 4. 大模型结构化
    await emit(
        task_id,
        "structuring",
        PROGRESS["structuring"],
        "正在调用大模型整理笔记结构…（长视频可能需要 1-3 分钟）",
    )
    note = await generate_note(
        transcript.text,
        info,
        part,
        transcript_source=transcript.source,
        note_style=note_style,
        settings=settings,
    )

    # 5. 落盘
    await emit(task_id, "rendering", PROGRESS["rendering"], "正在生成 Markdown 与思维导图…")
    artifacts = await asyncio.to_thread(
        save_artifacts, task_id, info, part, note, transcript
    )
    await emit(task_id, "saving", PROGRESS["saving"], "正在保存产物…")

    tokens = note.get("usage") or {}
    await asyncio.to_thread(
        update_task,
        task_id,
        status="success",
        stage="done",
        progress=1.0,
        message="笔记生成完成",
        finished_at=time.time(),
    )
    await emit(
        task_id,
        "done",
        PROGRESS["done"],
        "✅ 笔记生成完成",
        status="success",
        note_id=artifacts.note_id,
        title=info.title,
        model=note.get("model"),
        prompt_tokens=tokens.get("prompt_tokens", 0),
        completion_tokens=tokens.get("completion_tokens", 0),
    )
    return artifacts


def _source_label(source: str) -> str:
    return {
        "subtitle_manual": "视频自带字幕",
        "subtitle_ai": "B 站 AI 字幕",
        "asr_openai": "云端语音转写",
        "asr_faster-whisper": "本地语音转写",
        "asr_local": "本地语音转写",
        "cache": "转写缓存",
    }.get(source, source)


def _cache_extra(settings, info: VideoInfo, part: VideoPart) -> str:
    """缓存指纹的附加部分。

    - ASR 配置变了（换模型/换后端）应当重新转写，所以把配置写进指纹；
    - 本地文件用 mtime 保证"换了文件"时缓存自动失效。
    """
    if info.platform == LocalFilePlatform.name:
        path = Path(str((info.extra or {}).get("path") or ""))
        try:
            stat = path.stat()
            return f"local:{int(stat.st_mtime)}:{stat.st_size}"
        except OSError:
            return "local:missing"
    provider = (settings.asr_provider or "none").strip().lower()
    model = (
        settings.asr_local_model
        if provider in ("faster-whisper", "faster_whisper", "local")
        else settings.asr_model
    )
    return f"asr={provider}:{model}"


def save_artifacts(
    task_id: str,
    info: VideoInfo,
    part: VideoPart,
    note: Dict[str, Any],
    transcript: Transcript,
) -> Artifacts:
    """写 md / 导图 json / xmind / opml，并登记到数据库。"""
    from .core.utils import unique_path

    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = f"{stamp}_{safe_filename(info.title)}"
    if len(info.parts) > 1:
        stem += f"_P{part.index}"

    md_path = unique_path(NOTES_DIR, stem, ".md")
    md_path.write_text(note["markdown"], encoding="utf-8")

    tree = note.get("tree") or {}
    # 去掉以下划线开头的内部统计字段（_model / _tokens），对外只暴露纯树
    tree_clean = {k: v for k, v in tree.items() if not k.startswith("_")}
    mindmap_path = md_path.with_suffix(".mindmap.json")
    mindmap_path.write_text(to_json(tree_clean, indent=2), encoding="utf-8")

    xmind_path = md_path.with_suffix(".xmind")
    try:
        xmind_path.write_bytes(tree_to_xmind(tree_clean, sheet_title=info.title[:60]))
    except Exception as exc:  # noqa: BLE001 - 导图导出失败不该影响笔记
        log.warning("XMind 导出失败：%s", exc)
        xmind_path = None

    opml_path = md_path.with_suffix(".opml")
    try:
        from .core.utils import tree_to_opml

        opml_path.write_text(tree_to_opml(tree_clean, title=info.title), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("OPML 导出失败：%s", exc)
        opml_path = None

    structured = note.get("structured") or {}
    tags = [str(k) for k in (structured.get("keywords") or [])][:8]
    record = create_note(
        task_id=task_id,
        title=str(structured.get("title") or info.title),
        md_path=str(md_path.relative_to(NOTES_DIR.parent.parent)),
        mindmap_json=str(mindmap_path.relative_to(NOTES_DIR.parent.parent)),
        summary=str(structured.get("one_sentence") or structured.get("summary") or "")[:500],
        tags=tags,
        word_count=int(note.get("word_count") or 0),
    )
    log.info("产物已保存：%s", md_path.name)
    return Artifacts(
        note_id=record["id"],
        markdown=note["markdown"],
        md_path=md_path,
        tree=tree_clean,
        mindmap_path=mindmap_path,
        xmind_path=xmind_path,
        opml_path=opml_path,
    )
