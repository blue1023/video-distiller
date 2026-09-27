"""FastAPI 应用：本地自用的 B 站链接笔记生成服务。

启动方式：
    python run.py                 # 默认 127.0.0.1:8848
    uvicorn src.app:app --reload  # 开发模式
"""

from __future__ import annotations

import contextlib
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .core.config import DATA_DIR, WEB_DIR, ensure_dirs, get_settings
from .core.db import init_db, mark_pending_as_failed
from .core.utils import get_logger, setup_logging
from .webapi import routes_notes, routes_system, routes_tasks

setup_logging()
log = get_logger("app")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    ensure_dirs()
    init_db()
    interrupted = mark_pending_as_failed()
    if interrupted:
        log.warning("有 %d 个任务因上次服务中断被标记为失败", interrupted)
    # 回收上次残留的临时音频
    from .core.config import MEDIA_DIR
    from .core.utils import cleanup_dir

    stale = cleanup_dir(MEDIA_DIR, logger=log)
    if stale:
        log.info("已清理 %d 个残留临时音频", stale)
    settings = get_settings()
    log.info(
        "启动完成 | 模型=%s | 语音转写=%s | 数据目录=%s",
        settings.llm_model or "未配置",
        settings.asr_provider,
        DATA_DIR,
    )
    yield
    # 关闭所有平台 httpx 客户端
    from .platforms import registry as platform_registry

    for name in list(platform_registry._INSTANCES):  # noqa: SLF001 - 内部清理
        with contextlib.suppress(Exception):
            await platform_registry._INSTANCES[name].aclose()  # noqa: SLF001
    platform_registry.reset_instances()


app = FastAPI(
    title="B 站链接笔记生成工具",
    description="粘贴 B 站视频链接，自动生成 Markdown 笔记与思维导图（本地自用）。",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 本地自用，允许浏览器扩展 / 其它本地页面调用
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes_tasks.router)
app.include_router(routes_notes.router)
app.include_router(routes_system.router)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
    log.exception("未处理异常 %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": f"服务器内部错误：{exc}"},
    )


# ---- 前端静态资源 ----
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    page = WEB_DIR / "index.html"
    if not page.exists():
        raise HTTPException(status_code=500, detail="前端文件缺失：web/index.html")
    return FileResponse(page, media_type="text/html; charset=utf-8")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    icon = WEB_DIR / "favicon.svg"
    if icon.exists():
        return FileResponse(icon, media_type="image/svg+xml")
    raise HTTPException(status_code=404)
