"""列出应用注册的所有路由（排查用）。"""

from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

from src.app import app  # noqa: E402

for route in app.routes:
    methods = getattr(route, "methods", None)
    label = ",".join(sorted(methods)) if methods else "-"
    print(f"{label:<26} {getattr(route, 'path', '?')}")
