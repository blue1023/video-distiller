"""全局配置：环境变量 / .env 优先，其次 data/settings.json（由网页设置面板写入）。

优先级：环境变量 > data/settings.json > 代码默认值。

之所以做成"运行时可改"，是因为这个工具是本地自用：
网页上直接改 API Key 比每次编辑 .env 重启服务舒服得多。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- 路径

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
NOTES_DIR = DATA_DIR / "notes"
MEDIA_DIR = DATA_DIR / "media"
UPLOAD_DIR = DATA_DIR / "uploads"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "app.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
WEB_DIR = BASE_DIR / "web"


def ensure_dirs() -> None:
    for d in (DATA_DIR, NOTES_DIR, MEDIA_DIR, UPLOAD_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 设置模型


class Settings(BaseModel):
    """一份可持久化的配置。字段名与 .env 变量名一一对应（大小写不敏感）。"""

    # 大模型
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_mindmap_model: str = ""
    llm_temperature: float = 0.3
    llm_timeout: int = 300

    # B 站
    bili_cookie: str = ""
    bili_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    bili_request_interval: float = 1.0

    # 语音转写
    asr_provider: str = "none"  # none | openai | faster-whisper
    asr_model: str = "whisper-1"
    asr_base_url: str = ""
    asr_api_key: str = ""
    asr_local_model: str = "small"
    asr_local_device: str = "auto"

    # 服务 / 生成偏好
    host: str = "127.0.0.1"
    port: int = 8848
    note_style: str = "detailed"  # concise | detailed | academic

    # 缓存与并发
    cache_enabled: bool = True
    cache_dir: str = ""  # 留空用 data/cache
    max_concurrency: int = 2  # 同时跑几个任务（批量和并发都在此限制内）

    # 是否把 LLM 请求体/响应写日志（排错用）
    debug_llm: bool = False

    def public(self) -> Dict[str, Any]:
        """给前端看的版本：密钥打码，只暴露"是否已配置"。"""
        data = self.model_dump()
        for key in ("llm_api_key", "asr_api_key", "bili_cookie"):
            value = data.get(key) or ""
            data[key] = "***" if value else ""
        return data


# 环境变量 -> 字段 的映射
_ENV_MAP: Dict[str, str] = {
    "LLM_BASE_URL": "llm_base_url",
    "LLM_API_KEY": "llm_api_key",
    "LLM_MODEL": "llm_model",
    "LLM_MINDMAP_MODEL": "llm_mindmap_model",
    "LLM_TEMPERATURE": "llm_temperature",
    "LLM_TIMEOUT": "llm_timeout",
    "BILI_COOKIE": "bili_cookie",
    "BILI_USER_AGENT": "bili_user_agent",
    "BILI_REQUEST_INTERVAL": "bili_request_interval",
    "ASR_PROVIDER": "asr_provider",
    "ASR_MODEL": "asr_model",
    "ASR_BASE_URL": "asr_base_url",
    "ASR_API_KEY": "asr_api_key",
    "ASR_LOCAL_MODEL": "asr_local_model",
    "ASR_LOCAL_DEVICE": "asr_local_device",
    "HOST": "host",
    "PORT": "port",
    "NOTE_STYLE": "note_style",
    "CACHE_ENABLED": "cache_enabled",
    "CACHE_DIR": "cache_dir",
    "MAX_CONCURRENCY": "max_concurrency",
    "DEBUG_LLM": "debug_llm",
}

_SECRET_FIELDS = {"llm_api_key", "asr_api_key", "bili_cookie"}

_lock = threading.RLock()
_cached: Optional[Settings] = None


def _read_env_file(path: Path) -> Dict[str, str]:
    """极简 .env 解析（不引入 python-dotenv，少一个依赖）。"""
    result: Dict[str, str] = {}
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            result[key] = value
    return result


def _load_overrides() -> Dict[str, Any]:
    """合并 .env 与 data/settings.json（后者由网页写入）。"""
    overrides: Dict[str, Any] = {}

    file_values: Dict[str, str] = {}
    file_values.update(_read_env_file(BASE_DIR / ".env"))
    # 真实环境变量优先级最高
    for env_key, field in _ENV_MAP.items():
        if os.environ.get(env_key) not in (None, ""):
            file_values[env_key] = os.environ[env_key]

    for env_key, raw in file_values.items():
        field = _ENV_MAP.get(env_key)
        if not field:
            continue
        overrides[field] = _coerce(field, raw)

    # 网页保存的设置覆盖 .env（但密钥留空时不覆盖，避免误清空）
    if SETTINGS_PATH.exists():
        try:
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        if isinstance(saved, dict):
            for key, value in saved.items():
                if key in _SECRET_FIELDS and value in (None, "", "***"):
                    continue
                if key in Settings.model_fields:
                    overrides[key] = value

    # 网页里填了 "***" 表示"保持原值不变"，此处剔除脏值
    for key in _SECRET_FIELDS:
        if overrides.get(key) == "***":
            overrides.pop(key)
    return overrides


def _coerce(field: str, raw: str) -> Any:
    """按模型字段类型转换字符串。"""
    annotation = Settings.model_fields[field].annotation
    if annotation is float:
        try:
            return float(raw)
        except ValueError:
            return 0.0
    if annotation is int:
        try:
            return int(raw)
        except ValueError:
            return 0
    if annotation is bool:
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    return raw


def get_settings(refresh: bool = False) -> Settings:
    """取当前设置（带缓存）。refresh=True 强制重新读盘。"""
    global _cached
    with _lock:
        if _cached is None or refresh:
            _cached = Settings(**_load_overrides())
        return _cached


def save_settings(patch: Dict[str, Any]) -> Settings:
    """把 patch 合并进 data/settings.json 并刷新缓存。"""
    ensure_dirs()
    with _lock:
        current: Dict[str, Any] = {}
        if SETTINGS_PATH.exists():
            try:
                current = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                current = {}
        if not isinstance(current, dict):
            current = {}

        for key, value in patch.items():
            if key not in Settings.model_fields:
                continue
            if key in _SECRET_FIELDS and value in (None, "", "***"):
                # 空值 / *** 视为"不修改"，保留原值
                continue
            current[key] = value

        SETTINGS_PATH.write_text(
            json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return get_settings(refresh=True)


def llm_ready(settings: Optional[Settings] = None) -> bool:
    s = settings or get_settings()
    return bool(s.llm_api_key.strip()) or "127.0.0.1" in s.llm_base_url or "localhost" in s.llm_base_url
