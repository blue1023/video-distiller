"""API 请求/响应模型。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CreateTaskRequest(BaseModel):
    url: str = Field(..., description="B 站视频链接（支持 BV 号、av 号、b23.tv 短链、带 ?p= 的分P）")
    note_style: Optional[str] = Field(
        None, description="笔记风格：concise / detailed / academic，留空用全局设置"
    )
    platform: Optional[str] = Field(None, description="指定平台，留空自动识别")
    refresh: bool = Field(False, description="忽略转写缓存，强制重新抓取字幕/转写")


class CreateBatchRequest(BaseModel):
    urls: List[str] = Field(default_factory=list, description="视频链接列表")
    text: Optional[str] = Field(
        None, description="多行文本，每行一个链接（与 urls 二选一，会合并去重）"
    )
    note_style: Optional[str] = Field(None, description="本批次统一的笔记风格")
    refresh: bool = Field(False, description="忽略转写缓存")


class TaskOut(BaseModel):
    id: str
    url: str
    platform: str
    video_id: Optional[str] = None
    part: int = 1
    title: Optional[str] = None
    uploader: Optional[str] = None
    duration: int = 0
    cover: Optional[str] = None
    status: str
    stage: Optional[str] = None
    progress: float = 0.0
    message: Optional[str] = None
    error: Optional[str] = None
    transcript_src: Optional[str] = None
    transcript_chars: int = 0
    batch_id: Optional[str] = None
    refresh: int = 0
    from_cache: int = 0
    created_at: float
    updated_at: float
    finished_at: Optional[float] = None
    note_id: Optional[str] = None


class NoteOut(BaseModel):
    id: str
    task_id: str
    title: str
    md_path: str
    mindmap_json: Optional[str] = None
    summary: Optional[str] = None
    tags: List[str] = []
    word_count: int = 0
    created_at: float
    video_id: Optional[str] = None
    url: Optional[str] = None
    uploader: Optional[str] = None
    duration: int = 0
    cover: Optional[str] = None


class SettingsPatch(BaseModel):
    llm_base_url: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_model: Optional[str] = None
    llm_mindmap_model: Optional[str] = None
    llm_temperature: Optional[float] = None
    llm_timeout: Optional[int] = None
    bili_cookie: Optional[str] = None
    bili_request_interval: Optional[float] = None
    asr_provider: Optional[str] = None
    asr_model: Optional[str] = None
    asr_base_url: Optional[str] = None
    asr_api_key: Optional[str] = None
    asr_local_model: Optional[str] = None
    asr_local_device: Optional[str] = None
    note_style: Optional[str] = None
    debug_llm: Optional[bool] = None
    cache_enabled: Optional[bool] = None
    cache_dir: Optional[str] = None
    max_concurrency: Optional[int] = None


class ApiMessage(BaseModel):
    ok: bool = True
    message: str = ""
    data: Dict[str, Any] = {}
