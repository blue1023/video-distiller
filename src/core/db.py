"""SQLite 持久层 + 进程内事件总线。

- SQLite 只存"索引与元数据"：任务状态、笔记元信息。
  真正的产物（md / json / xmind）落在 data/notes/ 目录，方便直接翻看和 git 管理。
- 事件总线用于任务进度实时推送（SSE）：每个任务一个 asyncio 队列。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

from .config import DB_PATH, ensure_dirs

# ---------------------------------------------------------------- 建表

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    platform      TEXT NOT NULL DEFAULT 'bilibili',
    video_id      TEXT,
    part          INTEGER DEFAULT 1,
    title         TEXT,
    uploader      TEXT,
    duration      INTEGER DEFAULT 0,
    cover         TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    stage         TEXT,
    progress      REAL NOT NULL DEFAULT 0,
    message       TEXT,
    error         TEXT,
    transcript_src TEXT,
    transcript_chars INTEGER DEFAULT 0,
    note_style    TEXT DEFAULT 'detailed',
    batch_id      TEXT,
    refresh       INTEGER DEFAULT 0,
    from_cache    INTEGER DEFAULT 0,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    finished_at   REAL
);

CREATE TABLE IF NOT EXISTS notes (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    title       TEXT NOT NULL,
    md_path     TEXT NOT NULL,
    mindmap_json TEXT,
    summary     TEXT,
    tags        TEXT,
    word_count  INTEGER DEFAULT 0,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id         TEXT PRIMARY KEY,
    note_style TEXT DEFAULT 'detailed',
    total      INTEGER DEFAULT 0,
    raw        TEXT,
    created_at REAL NOT NULL
);"""

# 索引单独一份：必须等 _migrate() 补完列之后再建，否则老库升级会报
# "no such column"（这个坑真的踩过，有回归测试兜着）。
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_batch   ON tasks(batch_id);
CREATE INDEX IF NOT EXISTS idx_notes_task    ON notes(task_id);
CREATE INDEX IF NOT EXISTS idx_notes_created ON notes(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_batches_created ON batches(created_at DESC);
"""


def _connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db() -> Iterable[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        # 索引必须在补列之后建
        conn.executescript(INDEXES)


def _migrate(conn: sqlite3.Connection) -> None:
    """轻量迁移：给老库补上后来新增的列。"""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    additions = {
        "note_style": "TEXT DEFAULT 'detailed'",
        "batch_id": "TEXT",
        "refresh": "INTEGER DEFAULT 0",
        "from_cache": "INTEGER DEFAULT 0",
    }
    for column, definition in additions.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------- 任务


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def create_task(
    url: str,
    platform: str = "bilibili",
    video_id: Optional[str] = None,
    part: int = 1,
    note_style: str = "detailed",
    batch_id: Optional[str] = None,
    refresh: bool = False,
) -> Dict[str, Any]:
    task_id = new_id("t_")
    now = time.time()
    with db() as conn:
        conn.execute(
            """INSERT INTO tasks (id, url, platform, video_id, part, status, stage,
                                  progress, message, note_style, batch_id, refresh,
                                  created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id,
                url,
                platform,
                video_id,
                part,
                "pending",
                "queued",
                0.0,
                f"已加入队列（风格：{note_style}）",
                note_style,
                batch_id,
                1 if refresh else 0,
                now,
                now,
            ),
        )
    return get_task(task_id)  # type: ignore[return-value]


def update_task(task_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    columns = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [task_id]
    with db() as conn:
        conn.execute(f"UPDATE tasks SET {columns} WHERE id=?", values)


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    with db() as conn:
        return _row_to_dict(
            conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        )


def list_tasks(limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def count_tasks() -> int:
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()
    return int(row["c"]) if row else 0


def delete_task(task_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))


def mark_pending_as_failed() -> int:
    """服务重启后，把上次没跑完的任务标记为失败，避免一直转圈。"""
    now = time.time()
    with db() as conn:
        cur = conn.execute(
            """UPDATE tasks SET status='failed', error='服务重启，任务中断',
                                stage='interrupted', updated_at=?, finished_at=?
               WHERE status IN ('pending','running')""",
            (now, now),
        )
        return cur.rowcount


# ---------------------------------------------------------------- 笔记


def create_note(
    task_id: str,
    title: str,
    md_path: str,
    mindmap_json: Optional[str] = None,
    summary: Optional[str] = None,
    tags: Optional[List[str]] = None,
    word_count: int = 0,
) -> Dict[str, Any]:
    note_id = new_id("n_")
    now = time.time()
    with db() as conn:
        conn.execute(
            """INSERT INTO notes (id, task_id, title, md_path, mindmap_json, summary,
                                  tags, word_count, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                note_id,
                task_id,
                title,
                md_path,
                mindmap_json,
                summary,
                json.dumps(tags or [], ensure_ascii=False),
                word_count,
                now,
            ),
        )
    return get_note(note_id)  # type: ignore[return-value]


def get_note(note_id: str) -> Optional[Dict[str, Any]]:
    with db() as conn:
        row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    return _row_to_dict(row)


def get_note_by_task(task_id: str) -> Optional[Dict[str, Any]]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM notes WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    return _row_to_dict(row)


def list_notes(limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """SELECT n.*, t.video_id, t.url, t.uploader, t.duration, t.cover
               FROM notes n LEFT JOIN tasks t ON t.id = n.task_id
               ORDER BY n.created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_note(note_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM notes WHERE id=?", (note_id,))


# ---------------------------------------------------------------- 批次


def create_batch(
    urls: List[str], note_style: str = "detailed", raw: Optional[str] = None
) -> Dict[str, Any]:
    batch_id = new_id("b_")
    now = time.time()
    with db() as conn:
        conn.execute(
            """INSERT INTO batches (id, note_style, total, raw, created_at)
               VALUES (?,?,?,?,?)""",
            (batch_id, note_style, len(urls), raw or "\n".join(urls), now),
        )
    return get_batch(batch_id)  # type: ignore[return-value]


def get_batch(batch_id: str) -> Optional[Dict[str, Any]]:
    with db() as conn:
        row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not row:
            return None
        # 注意：所有查询都必须在 with 块内完成，出了块连接就关了
        tasks = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM tasks WHERE batch_id=? ORDER BY created_at ASC", (batch_id,)
            ).fetchall()
        ]
    batch = dict(row)
    batch["tasks"] = tasks
    batch["done"] = sum(1 for t in tasks if t["status"] == "success")
    batch["failed"] = sum(1 for t in tasks if t["status"] == "failed")
    return batch


def list_batches(limit: int = 30) -> List[Dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM batches ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_batch(batch_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM batches WHERE id=?", (batch_id,))


# ---------------------------------------------------------------- 事件总线


class EventBus:
    """任务进度事件：先落内存，再广播给所有订阅者（SSE）。

    单机自用场景不追求跨进程，内存队列足够；重连时用任务快照补齐。
    """

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._history: Dict[str, List[Dict[str, Any]]] = {}

    def publish(self, task_id: str, event: Dict[str, Any]) -> None:
        event.setdefault("ts", time.time())
        event.setdefault("task_id", task_id)
        history = self._history.setdefault(task_id, [])
        history.append(event)
        if len(history) > 200:
            del history[:-200]
        for queue in list(self._subscribers.get(task_id, [])):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - 慢消费者直接丢
                pass

    def subscribe(self, task_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.setdefault(task_id, []).append(queue)
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue) -> None:
        queues = self._subscribers.get(task_id)
        if not queues:
            return
        if queue in queues:
            queues.remove(queue)
        if not queues:
            self._subscribers.pop(task_id, None)

    def history(self, task_id: str) -> List[Dict[str, Any]]:
        return list(self._history.get(task_id, []))


bus = EventBus()


async def emit(task_id: str, stage: str, progress: float, message: str, **extra: Any) -> None:
    """更新任务状态 + 广播事件（Pipeline 里统一走这个）。"""
    payload: Dict[str, Any] = {
        "stage": stage,
        "progress": round(float(progress), 4),
        "message": message,
        "status": extra.pop("status", "running"),
    }
    payload.update(extra)
    await asyncio.to_thread(
        update_task,
        task_id,
        status=payload["status"],
        stage=stage,
        progress=payload["progress"],
        message=message,
    )
    bus.publish(task_id, payload)
