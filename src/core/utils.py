"""通用工具：日志、文件名清洗、文本切分、Markdown 解析、XMind 生成。"""

from __future__ import annotations

import logging
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# ---------------------------------------------------------------- 日志

_LEVEL = logging.INFO


def setup_logging(level: int = _LEVEL) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", "%H:%M:%S")
    )
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# ---------------------------------------------------------------- 文件 / 字符串


_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_filename(text: str, max_length: int = 80, fallback: str = "note") -> str:
    """把视频标题变成安全的文件名（Windows / macOS / Linux 通用）。"""
    text = unicodedata.normalize("NFKC", text or "").strip()
    text = _ILLEGAL.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if len(text) > max_length:
        text = text[:max_length].rstrip(" .")
    return text or fallback


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{stem}{suffix}"
    index = 2
    while candidate.exists():
        candidate = directory / f"{stem} ({index}){suffix}"
        index += 1
    return candidate


def try_delete(path: Path, logger: Optional[logging.Logger] = None) -> bool:
    """尽力删除文件。

    Windows 上文件可能被杀软 / 下载器短暂占用，直接 unlink 会抛
    PermissionError；这里退化为"清空内容 + 改名标记"，既不报错也便于人工清理。
    """
    path = Path(path)
    if not path.exists():
        return True
    try:
        path.unlink()
        return True
    except (OSError, PermissionError):
        pass
    try:
        path.write_bytes(b"")
        stale = path.with_suffix(path.suffix + ".stale")
        try:
            path.rename(stale)
        except OSError:
            pass
        if logger:
            logger.warning("无法删除 %s，已清空内容并保留为 .stale", path.name)
        return False
    except OSError as exc:
        if logger:
            logger.warning("清理 %s 失败：%s", path.name, exc)
        return False


def cleanup_dir(directory: Path, *, suffix: str = ".m4s", logger: Optional[logging.Logger] = None) -> int:
    """清理目录下的临时媒体文件（服务启动时调用，兜底回收残留）。"""
    if not directory.exists():
        return 0
    removed = 0
    for item in directory.iterdir():
        if not item.is_file():
            continue
        if suffix and not (item.name.endswith(suffix) or item.suffix in (".m4s", ".wav", ".mp4", ".m4a", ".stale")):
            continue
        if try_delete(item, logger):
            removed += 1
    return removed


# ---------------------------------------------------------------- 时间 / 文本


def format_duration(seconds: Optional[int]) -> str:
    seconds = int(seconds or 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_timestamp(ts: Optional[float]) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def count_words(text: str) -> int:
    """中英混排的粗略字数统计（中文按字，英文按词）。"""
    if not text:
        return 0
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    english = len(re.findall(r"[A-Za-z]+", text))
    return chinese + english


def split_transcript(
    text: str, max_chars: int = 6000, overlap: int = 200
) -> List[str]:
    """把字幕按长度切块，尽量在句子边界切开，块间留少量重叠防上下文断裂。"""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return [text] if text else []

    chunks: List[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + max_chars, length)
        if end < length:
            window = text[start:end]
            # 优先句末标点，其次换行/逗号
            for pattern in ("\n", "。", "！", "？", ". ", "! ", "? ", "；", "，", " "):
                pos = window.rfind(pattern)
                if pos > max_chars * 0.5:
                    end = start + pos + len(pattern)
                    break
        chunks.append(text[start:end].strip())
        if end >= length:
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


def to_json(data: Any, indent: Optional[int] = None) -> str:
    import json

    return json.dumps(data, ensure_ascii=False, indent=indent)


# ---------------------------------------------------------------- Markdown 解析


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")


def _clean_inline(text: str) -> str:
    """去掉 markdown 行内语法，留纯文本给思维导图节点。"""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # 图片
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)       # 链接
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)             # 粗体
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", text)    # 斜体
    text = re.sub(r"`([^`]+)`", r"\1", text)                   # 行内代码
    text = re.sub(r"~~([^~]+)~~", r"\1", text)                 # 删除线
    text = re.sub(r"^>\s*", "", text)                          # 引用
    return text.strip()


def markdown_to_tree(markdown: str, root_title: str = "笔记") -> Dict[str, Any]:
    """把 Markdown 标题 / 列表层级解析成 markmap 需要的树结构。

    规则：
    - 标题按层级嵌套；
    - 列表项挂到最近的标题下，用缩进判断嵌套；
    - 代码块内的内容整体忽略；
    - 段落首句若无标题承接，作为正文节点保留（过长则截断）。
    """
    root: Dict[str, Any] = {"content": root_title, "children": []}
    heading_stack: List[tuple[int, Dict[str, Any]]] = [(0, root)]
    bullet_stack: List[tuple[int, Dict[str, Any]]] = []
    in_code = False

    for raw_line in (markdown or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code = not in_code
            continue
        if in_code or not stripped:
            continue

        heading = _HEADING.match(stripped)
        if heading:
            level = len(heading.group(1))
            content = _clean_inline(heading.group(2))
            if not content:
                continue
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            parent = heading_stack[-1][1] if heading_stack else root
            node = {"content": content, "children": []}
            parent.setdefault("children", []).append(node)
            heading_stack.append((level, node))
            bullet_stack.clear()
            continue

        bullet = _BULLET.match(line)
        if bullet:
            indent = len(bullet.group(1).expandtabs(4))
            content = _clean_inline(bullet.group(3))
            if not content:
                continue
            while bullet_stack and bullet_stack[-1][0] >= indent:
                bullet_stack.pop()
            parent = (
                bullet_stack[-1][1]
                if bullet_stack
                else (heading_stack[-1][1] if heading_stack else root)
            )
            node = {"content": content, "children": []}
            parent.setdefault("children", []).append(node)
            bullet_stack.append((indent, node))
            continue

        # 普通段落：跳过表格分隔行，其余作为叶子节点（限制长度）
        if re.match(r"^\|?[\s:|-]+\|?$", stripped) and "|" in stripped:
            continue
        paragraph = _clean_inline(stripped)
        if not paragraph or paragraph.startswith("---"):
            continue
        if len(paragraph) > 60:
            paragraph = paragraph[:60] + "…"
        parent = heading_stack[-1][1] if heading_stack else root
        parent.setdefault("children", []).append({"content": paragraph, "children": []})

    return root


def prune_tree(node: Dict[str, Any], max_depth: int = 4, max_children: int = 12) -> Dict[str, Any]:
    """限制导图规模：太深的层级和过多的同级节点会让可视化变成一坨毛线。"""

    def walk(item: Dict[str, Any], depth: int) -> Optional[Dict[str, Any]]:
        content = (item.get("content") or "").strip()
        if not content:
            return None
        children_in = item.get("children") or []
        children: List[Dict[str, Any]] = []
        if depth < max_depth:
            for child in children_in[:max_children]:
                trimmed = walk(child, depth + 1)
                if trimmed:
                    children.append(trimmed)
            if len(children_in) > max_children:
                children.append(
                    {"content": f"…还有 {len(children_in) - max_children} 条", "children": []}
                )
        return {"content": content, "children": children}

    return walk(node, 1) or {"content": node.get("content", "笔记"), "children": []}


# ---------------------------------------------------------------- XMind 导出


def tree_to_xmind(tree: Dict[str, Any], sheet_title: str = "Sheet 1") -> bytes:
    """生成 .xmind 文件（XMind ZEN / XMind 2020+ 的 JSON 格式，本质是个 zip）。

    XMind 新版文件结构：
      content.json  —— 导图主体
      metadata.json —— 元信息
      manifest.json —— 清单
    用标准库 zipfile 拼即可，不引入额外依赖。
    """
    import json
    import zipfile
    from io import BytesIO

    def convert(node: Dict[str, Any]) -> Dict[str, Any]:
        children = [convert(c) for c in (node.get("children") or []) if c.get("content")]
        result: Dict[str, Any] = {"id": new_node_id(), "title": node.get("content", "")}
        if children:
            result["children"] = {"attached": children}
        return result

    root_topic = convert(tree)
    content = [
        {
            "id": new_node_id(),
            "class": "sheet",
            "title": sheet_title,
            "rootTopic": root_topic,
            "topicPositioning": "fixed",
        }
    ]
    metadata = {"creator": {"name": "BiliNote", "version": "0.1.0"}}
    manifest = {"file-entries": {"content.json": {}, "metadata.json": {}}}

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.json", json.dumps(content, ensure_ascii=False))
        zf.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False))
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
    return buffer.getvalue()


_node_counter = 0


def new_node_id() -> str:
    global _node_counter
    _node_counter += 1
    return f"{int(time.time() * 1000):x}{_node_counter:04x}"


def tree_to_opml(tree: Dict[str, Any], title: str = "笔记") -> str:
    """备选导出：OPML（XMind / MindManager / 幕布 都能导入）。"""

    def esc(text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def walk(node: Dict[str, Any], indent: int) -> List[str]:
        pad = "  " * indent
        children = node.get("children") or []
        lines = [f'{pad}<outline text="{esc(node.get("content", ""))}">']
        for child in children:
            lines.extend(walk(child, indent + 1))
        lines.append(f"{pad}</outline>")
        return lines

    body: List[str] = []
    for child in tree.get("children") or []:
        body.extend(walk(child, 1))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<opml version="2.0">\n'
        f"  <head><title>{esc(title)}</title></head>\n"
        "  <body>\n" + "\n".join(body) + "\n  </body>\n</opml>\n"
    )
