"""笔记相关路由：列表、详情、下载、导出。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response

from ..core.config import BASE_DIR, NOTES_DIR
from ..core.db import get_note, get_note_by_task, list_notes
from ..core.utils import safe_filename, tree_to_opml, tree_to_xmind

router = APIRouter(prefix="/api/notes", tags=["notes"])

_MEDIA_TYPES = {
    "md": "text/markdown; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "xmind": "application/x-xmind",
    "opml": "text/x-opml; charset=utf-8",
}


def _resolve_path(raw: Optional[str]) -> Optional[Path]:
    if not raw:
        return None
    candidate = (BASE_DIR / raw).resolve()
    # 防目录穿越：只允许读 data/ 下的文件
    if not str(candidate).startswith(str(NOTES_DIR.parent.resolve())):
        return None
    return candidate


def _load_tree(note: Dict[str, Any]) -> Dict[str, Any]:
    path = _resolve_path(note.get("mindmap_json"))
    if path and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


@router.get("")
async def index(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    items = []
    for note in list_notes(limit=limit, offset=offset):
        row = dict(note)
        try:
            row["tags"] = json.loads(row.get("tags") or "[]")
        except ValueError:
            row["tags"] = []
        row["exists"] = _path_exists(row.get("md_path"))
        items.append(row)
    return {"items": items}


def _path_exists(raw: Optional[str]) -> bool:
    path = _resolve_path(raw)
    return bool(path and path.exists())


@router.get("/by-task/{task_id}")
async def by_task(task_id: str) -> Dict[str, Any]:
    note = get_note_by_task(task_id)
    if not note:
        raise HTTPException(status_code=404, detail="该任务还没有生成笔记")
    return _note_payload(note)


@router.get("/{note_id}")
async def detail(note_id: str) -> Dict[str, Any]:
    note = get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="笔记不存在")
    return _note_payload(note)


def _note_payload(note: Dict[str, Any]) -> Dict[str, Any]:
    path = _resolve_path(note.get("md_path"))
    content = ""
    if path and path.exists():
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            content = ""
    try:
        tags = json.loads(note.get("tags") or "[]")
    except ValueError:
        tags = []
    return {
        **note,
        "tags": tags,
        "content": content,
        "tree": _load_tree(note),
        "exists": bool(path and path.exists()),
    }


@router.get("/{note_id}/content")
async def content(note_id: str) -> Dict[str, Any]:
    note = get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="笔记不存在")
    return _note_payload(note)


def _disposition(filename: str) -> Dict[str, str]:
    """构造 Content-Disposition。

    ⚠️ HTTP 头只能是 latin-1，中文文件名直接塞进去会抛 UnicodeEncodeError。
    按 RFC 6266/5987 处理：ASCII 回退名 + filename*=UTF-8''<percent-encoded>。
    """
    from urllib.parse import quote

    ascii_fallback = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "download"
    encoded = quote(filename, safe="")
    return {
        "Content-Disposition": (
            f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"
        )
    }


@router.get("/{note_id}/download")
async def download(note_id: str, kind: str = Query("md", pattern="^(md|json|xmind|opml)$")) -> Response:
    note = get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="笔记不存在")

    raw_title = note.get("title") or "note"
    stem = safe_filename(raw_title)

    if kind == "md":
        path = _resolve_path(note.get("md_path"))
        if not path or not path.exists():
            raise HTTPException(status_code=404, detail="Markdown 文件已丢失")
        return FileResponse(
            path,
            media_type=_MEDIA_TYPES["md"],
            headers=_disposition(f"{stem}.md"),
        )

    tree = _load_tree(note)
    if not tree:
        raise HTTPException(status_code=404, detail="该笔记没有思维导图数据")

    if kind == "json":
        return Response(
            content=json.dumps(tree, ensure_ascii=False, indent=2),
            media_type=_MEDIA_TYPES["json"],
            headers=_disposition(f"{stem}.mindmap.json"),
        )
    if kind == "xmind":
        payload = tree_to_xmind(tree, sheet_title=str(raw_title)[:60])
        return Response(
            content=payload,
            media_type=_MEDIA_TYPES["xmind"],
            headers=_disposition(f"{stem}.xmind"),
        )
    opml = tree_to_opml(tree, title=str(raw_title))
    return Response(
        content=opml,
        media_type=_MEDIA_TYPES["opml"],
        headers=_disposition(f"{stem}.opml"),
    )


@router.get("/{note_id}/raw", response_class=PlainTextResponse)
async def raw(note_id: str) -> str:
    """纯文本返回 Markdown，方便直接 curl / 复制。"""
    note = get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="笔记不存在")
    path = _resolve_path(note.get("md_path"))
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Markdown 文件已丢失")
    return path.read_text(encoding="utf-8")


@router.delete("/{note_id}")
async def remove(note_id: str, purge: bool = Query(False, description="同时删除磁盘产物")) -> Dict[str, Any]:
    note = get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="笔记不存在")
    if purge:
        from ..core.utils import try_delete

        for key in ("md_path", "mindmap_json"):
            path = _resolve_path(note.get(key))
            if path:
                try_delete(path)
        md_path = _resolve_path(note.get("md_path"))
        if md_path:
            for suffix in (".xmind", ".opml"):
                try_delete(md_path.with_suffix(suffix))
    from ..core.db import delete_note

    delete_note(note_id)
    return {"ok": True, "message": "笔记已删除"}


@router.get("/{note_id}/print", response_class=HTMLResponse)
async def print_view(note_id: str) -> str:
    """极简打印视图：想直接打印成 PDF 时用。"""
    note = get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="笔记不存在")
    payload = _note_payload(note)
    import html as _html

    body = _html.escape(payload.get("content") or "")
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{_html.escape(note.get('title') or '笔记')}</title>
<style>
body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;max-width:860px;
margin:40px auto;padding:0 20px;line-height:1.7;color:#222}}
pre{{white-space:pre-wrap;word-break:break-word;font-family:inherit}}
</style></head><body><pre>{body}</pre></body></html>"""
