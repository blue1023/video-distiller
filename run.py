#!/usr/bin/env python
"""启动脚本：python run.py [--host 127.0.0.1] [--port 8848] [--reload]"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.core.config import ensure_dirs, get_settings  # noqa: E402
from src.core.utils import setup_logging  # noqa: E402


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="B 站链接笔记生成工具（本地服务）")
    parser.add_argument("--host", default=settings.host or "127.0.0.1")
    parser.add_argument("--port", type=int, default=int(settings.port or 8848))
    parser.add_argument("--reload", action="store_true", help="开发模式：代码改动自动重启")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args()

    setup_logging()
    ensure_dirs()

    import uvicorn

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    print("=" * 62)
    print("  B 站链接笔记生成工具")
    print(f"  网页地址：{url}")
    print(f"  接口文档：{url}/docs")
    print("  按 Ctrl+C 停止服务")
    print("=" * 62)

    if not args.no_browser:
        import threading

        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        "src.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
