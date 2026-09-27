"""本地视频上传接口。

上传后的文件会落到 data/uploads/，任务链接用 local://<文件名>，
后续走进同一套流水线（转写 → 大模型 → 笔记 → 导图）。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ..core.config import get_settings
from ..core.utils import get_logger, safe_filename, try_delete
from ..platforms.local_file import AUDIO_SUFFIXES, VIDEO_SUFFIXES, upload_dir

router = APIRouter(prefix="/api/upload", tags=["upload"])
log = get_logger("api.upload")

#: 单个文件上限（可通过反向代理/环境再收紧）
MAX_FILE_MB = 4096


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


@router.post("")
async def upload(files: List[UploadFile] = File(...), title: Optional[str] = Form(None)) -> Dict[str, Any]:
    """上传一到多个视频/音频文件，返回可直接提交的任务链接。"""
    if not files:
        raise HTTPException(status_code=400, detail="没有收到文件")

    settings = get_settings()
    directory = upload_dir()
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []

    for index, item in enumerate(files):
        original = item.filename or f"upload-{index + 1}"
        suffix = "." + original.rsplit(".", 1)[-1].lower() if "." in original else ""
        if suffix not in VIDEO_SUFFIXES:
            errors.append(
                {
                    "name": original,
                    "reason": f"不支持的文件类型 {suffix or '(无扩展名)'}，支持："
                    + "、".join(sorted(VIDEO_SUFFIXES)),
                }
            )
            continue

        stem = safe_filename(original.rsplit(".", 1)[0], max_length=70, fallback="video")
        if title and len(files) == 1:
            stem = safe_filename(title, max_length=70, fallback=stem)
        # 加时间戳避免同名覆盖
        filename = f"{int(time.time())}_{stem}{suffix}"
        target = directory / filename

        written = 0
        try:
            with target.open("wb") as handle:
                while True:
                    chunk = await item.read(1024 * 1024 * 4)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_FILE_MB * 1024 * 1024:
                        raise ValueError(f"文件超过 {MAX_FILE_MB} MB 上限")
                    handle.write(chunk)
        except Exception as exc:  # noqa: BLE001
            try_delete(target)
            errors.append({"name": original, "reason": str(exc)})
            continue
        finally:
            await item.close()

        if written == 0:
            try_delete(target)
            errors.append({"name": original, "reason": "文件内容为空"})
            continue

        is_audio = suffix in AUDIO_SUFFIXES
        log.info("已接收上传：%s（%s）", filename, _human_size(written))
        results.append(
            {
                "filename": filename,
                "url": f"local://{filename}",
                "size": written,
                "size_text": _human_size(written),
                "kind": "audio" if is_audio else "video",
            }
        )

    if not results:
        raise HTTPException(
            status_code=400,
            detail="；".join(f"{e['name']}：{e['reason']}" for e in errors) or "上传失败",
        )

    # 本地视频必须靠语音转写，这里给出明确提示（不自动改配置，避免意外开销）
    hint = None
    provider = (settings.asr_provider or "none").strip().lower()
    if provider in ("none", "", "off", "disabled"):
        hint = (
            "本地视频需要语音转写才能出笔记：请到设置里把「语音转写」切到 "
            "faster-whisper（本地离线，需 pip install faster-whisper）或 openai。"
        )
    return {"items": results, "errors": errors, "hint": hint}


@router.get("")
async def list_uploads() -> Dict[str, Any]:
    directory = upload_dir()
    items = []
    for path in sorted(directory.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
        if not path.is_file():
            continue
        stat = path.stat()
        items.append(
            {
                "filename": path.name,
                "url": f"local://{path.name}",
                "size": stat.st_size,
                "size_text": _human_size(stat.st_size),
                "mtime": stat.st_mtime,
            }
        )
    return {"items": items}


@router.delete("/{filename}")
async def remove_upload(filename: str) -> Dict[str, Any]:
    directory = upload_dir().resolve()
    target = (directory / filename).resolve()
    if not str(target).startswith(str(directory)):
        raise HTTPException(status_code=400, detail="非法文件名")
    if not target.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    ok = try_delete(target, log)
    return {
        "ok": ok,
        "message": "文件已删除" if ok else "文件被占用，已清空内容但未能删除",
    }
