#!/usr/bin/env python
"""把前端依赖（markmark / markmap / echarts / marked）下载到 web/vendor，实现离线可用。

用法：python tools/fetch_vendor.py
- 只依赖标准库（urllib + tarfile + zipfile）；
- 优先 npmmirror（国内快），失败自动回退 registry.npmjs.org；
- 下载后把每个 npm 包的 dist/ 内容摊平到 web/vendor/<pkg>/，并生成 manifest.json。
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
VENDOR = BASE_DIR / "web" / "vendor"

NPM_MIRRORS = [
    "https://registry.npmmirror.com",
    "https://registry.npmjs.org",
]

PACKAGES = [
    ("markmap-lib", "0.18.12"),
    ("markmap-view", "0.18.12"),
    ("d3", "7.9.0"),
    ("marked", "12.0.2"),
]

# 这些文件是运行时真正需要的（其余 d.ts / map 之类不用搬）
KEEP_SUFFIX = (".js", ".mjs", ".cjs", ".css", ".woff", ".woff2")


def http_get(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (vendor-fetcher)"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def resolve_tarball(name: str, version: str) -> str:
    errors = []
    for mirror in NPM_MIRRORS:
        meta_url = f"{mirror}/{name}/{version}"
        try:
            meta = json.loads(http_get(meta_url).decode("utf-8"))
            tarball = (meta.get("dist") or {}).get("tarball")
            if not tarball:
                tarball = f"{mirror}/{name}/-/{name}-{version}.tgz"
            return tarball
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{mirror}: {exc}")
    raise RuntimeError(f"无法获取 {name}@{version} 地址（{'；'.join(errors)}）")


def extract_package(name: str, version: str) -> int:
    tarball = resolve_tarball(name, version)
    print(f"  ↓ {name}@{version}")
    payload = http_get(tarball)
    target_dir = VENDOR / name
    target_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        members = [m for m in tar.getmembers() if m.isfile()]
        has_dist = any("dist" in Path(m.name).parts for m in members)
        for member in members:
            parts = Path(member.name).parts
            if len(parts) < 2:
                continue
            relative = Path(*parts[1:])  # 去掉顶层 package/
            if relative.suffix.lower() not in KEEP_SUFFIX:
                continue
            if has_dist and "dist" not in relative.parts and name not in ("d3", "echarts"):
                continue
            if not has_dist and "lib" in relative.parts and relative.suffix in (".js", ".mjs"):
                # 没有 dist 的包（如 marked），只取根目录下的打包文件，避免拖一堆源码
                continue
            # 摊平：去掉 dist / src / umd / lib 前缀，便于浏览器按文件名引用
            flat_parts = [
                part for part in relative.parts if part not in ("dist", "src", "umd", "lib")
            ]
            if not flat_parts:
                continue
            destination = target_dir.joinpath(*flat_parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                continue
            destination.write_bytes(source.read())
            copied += 1
    print(f"    -> {copied} 个文件")
    return copied


def main() -> int:
    VENDOR.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, list[str]] = {}
    for name, version in PACKAGES:
        try:
            extract_package(name, version)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {name} 下载失败：{exc}", file=sys.stderr)
            continue
        files = sorted(
            str(p.relative_to(VENDOR)).replace("\\", "/")
            for p in (VENDOR / name).rglob("*")
            if p.is_file()
        )
        manifest[name] = {"version": version, "files": files}
    (VENDOR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n完成，共 {len(manifest)}/{len(PACKAGES)} 个包，清单写入 {VENDOR / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
