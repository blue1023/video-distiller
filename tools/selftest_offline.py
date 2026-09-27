#!/usr/bin/env python
"""离线自检：不联网、不依赖第三方库，验证核心纯函数是否正常。

用法：python tools/selftest_offline.py
覆盖：配置加载、SQLite 读写与迁移、Markdown 解析、导图剪枝、XMind/OPML 生成、
      字幕切块、文件名清洗、笔记 Markdown 模板渲染、B 站链接解析。
"""

from __future__ import annotations

import json
import sys
import uuid
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

# 测试用临时目录放在项目内，避免系统临时目录的权限问题
TEST_TMP = BASE_DIR / ".tmp" / "selftest"
TEST_TMP.mkdir(parents=True, exist_ok=True)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

PASSED = 0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [OK]   {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


def test_config() -> None:
    print("\n== 配置 ==")
    from src.core.config import Settings, get_settings

    settings = get_settings()
    check("默认配置可加载", isinstance(settings, Settings))
    public = settings.public()
    check("密钥被打码", public["llm_api_key"] in ("", "***"))


def tempdir() -> str:
    """在项目内建一个临时目录。

    刻意不使用 tempfile.TemporaryDirectory / mkdtemp：
    ① 某些沙箱环境拒绝对名称以 tmp 开头的新建目录写入；
    ② 受限环境下删除目录可能被拒绝，导致测试在清理阶段误报。
    测试产物留在 .tmp/selftest 下，随时可手工清空。
    """
    target = TEST_TMP / uuid.uuid4().hex[:10]
    target.mkdir(parents=True, exist_ok=True)
    return str(target)


def test_db() -> None:
    print("\n== SQLite ==")
    import src.core.db as db_module

    tmp = tempdir()
    original = db_module.DB_PATH
    db_module.DB_PATH = Path(tmp) / "test.db"
    try:
        db_module.init_db()
        task = db_module.create_task(
            "https://www.bilibili.com/video/BV1xx411c7mD", note_style="concise"
        )
        check("创建任务", bool(task and task["id"]))
        check("风格落库", task["note_style"] == "concise")
        db_module.update_task(task["id"], status="success", progress=1.0)
        reloaded = db_module.get_task(task["id"])
        check("更新任务", reloaded["status"] == "success")
        note = db_module.create_note(
            task_id=task["id"],
            title="测试笔记",
            md_path="data/notes/x.md",
            mindmap_json="data/notes/x.mindmap.json",
            summary="一句话",
            tags=["a", "b"],
            word_count=12,
        )
        check("创建笔记", bool(note and note["id"]))
        check("按任务查笔记", db_module.get_note_by_task(task["id"])["id"] == note["id"])
        check("任务计数", db_module.count_tasks() == 1)
        check("笔记列表带详情", db_module.list_notes()[0].get("title") == "测试笔记")
        marked = db_module.mark_pending_as_failed()
        check("中断任务清理不误伤", marked == 0, f"实际 {marked}")
        db_module.delete_note(note["id"])
        db_module.delete_task(task["id"])
        check("删除任务", db_module.get_task(task["id"]) is None)
    finally:
        db_module.DB_PATH = original


def test_utils() -> None:
    print("\n== 工具函数 ==")
    from src.core.utils import (
        count_words,
        format_duration,
        markdown_to_tree,
        prune_tree,
        safe_filename,
        split_transcript,
        tree_to_opml,
        tree_to_xmind,
    )

    check("时长格式化", format_duration(3725) == "1:02:05", format_duration(3725))
    check("短时长格式化", format_duration(65) == "1:05")
    check("安全文件名", safe_filename('a/b:c*d?"e|f') == "a_b_c_d_e_f", safe_filename('a/b:c*d?"e|f'))
    check("空标题兜底", safe_filename("   ") == "note")
    check("中英字数", count_words("中文abc") == 3, str(count_words("中文abc")))

    long_text = "这是一句话。" * 2000
    chunks = split_transcript(long_text, max_chars=500, overlap=50)
    check("长文本切块", len(chunks) > 1, f"{len(chunks)} 块")
    check("切块长度受控", all(len(c) <= 700 for c in chunks))

    markdown = """# 标题

> 一句话总结

## 第一节

- 要点一
  - 子要点
- 要点二

## 第二节

1. 顺序要点

| a | b |
| --- | --- |
| 1 | 2 |

```python
print("这段不应进导图")
```
"""
    tree = markdown_to_tree(markdown, "根")
    flat = json.dumps(tree, ensure_ascii=False)
    check("导图根节点", tree["content"] == "根")
    check("标题进入导图", "第一节" in flat and "第二节" in flat)
    check("嵌套要点进入导图", "子要点" in flat)
    check("代码块被忽略", "print" not in flat)
    check("引用被清理", "一句话总结" in flat)
    pruned = prune_tree(tree, max_depth=2, max_children=2)
    check("剪枝有结果", bool(pruned["content"]))

    xmind_bytes = tree_to_xmind(tree, sheet_title="测试")
    check("XMind 是 zip", xmind_bytes[:2] == b"PK")
    tmp = tempdir()
    path = Path(tmp) / "t.xmind"
    path.write_bytes(xmind_bytes)
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        check("XMind 含 content.json", "content.json" in names, str(names))
        content = json.loads(zf.read("content.json").decode("utf-8"))
        check("XMind 结构可解析", content[0]["rootTopic"]["title"] == "根")
        check("XMind 子节点存在", bool(content[0]["rootTopic"].get("children")))

    opml = tree_to_opml(tree, title="测试")
    check("OPML 头部", opml.startswith("<?xml") and "<opml" in opml)
    check("OPML 含标题", "第一节" in opml)


def test_render() -> None:
    print("\n== Markdown 模板渲染 ==")
    import types

    from src.llm.notes import render_markdown

    info = types.SimpleNamespace(
        title="原始视频标题",
        url="https://www.bilibili.com/video/BV1xx411c7mD",
        uploader="某UP主",
        duration=600,
        view_count=12345,
        video_id="BV1xx411c7mD",
        parts=[1, 2],
    )
    part = types.SimpleNamespace(index=2, title="第二P", duration=300)
    data = {
        "title": "蒸馏后的标题",
        "one_sentence": "一句话总结。",
        "summary": "摘要正文。",
        "keywords": ["关键词1", "关键词2"],
        "outline": [
            {"heading": "章节一", "level": 2, "start": 12, "end": 130, "points": ["要点A", "- 子要点B"]},
        ],
        "highlights": ["结论X"],
        "quotes": [{"text": "金句", "time": "00:12"}],
        "action_items": ["做点什么"],
        "open_questions": ["还有什么"],
    }
    markdown = render_markdown(
        data,
        info,
        part,
        transcript_source="subtitle_ai",
        note_style="detailed",
        model="test-model",
        generated_at="2025-01-01 00:00:00",
        stats={"transcript_chars": 100},
    )
    for expected in (
        "# 蒸馏后的标题",
        "一句话总结",
        "| 分P | 第 2 P · 第二P |",
        "B 站 AI 字幕",
        "章节一",
        "子要点B",
        "结论X",
        "金句",
        "行动清单",
        "t=12",
    ):
        check(f"渲染包含「{expected}」", expected in markdown, markdown[:200])


def test_mindmap_fallback() -> None:
    print("\n== 导图兜底结构 ==")
    from src.llm.notes import _tree_from_outline

    tree = _tree_from_outline(
        {
            "one_sentence": "一句话",
            "outline": [{"heading": "章节", "points": ["要点1", "要点2"]}],
            "highlights": ["结论"],
        },
        "根标题",
    )
    check("兜底根节点", tree["content"] == "根标题")
    flat = json.dumps(tree, ensure_ascii=False)
    check("兜底含章节", "章节" in flat and "要点1" in flat)


def test_bilibili_parse() -> None:
    print("\n== B 站链接解析（纯函数，不联网）==")
    try:
        from src.platforms.bilibili import BilibiliPlatform
    except ImportError as exc:
        print(f"  [SKIP] 需要 httpx：{exc}")
        return

    platform = BilibiliPlatform.__new__(BilibiliPlatform)

    cases = [
        ("https://www.bilibili.com/video/BV1GJ411x7h7", "BV1GJ411x7h7", 1, 0),
        ("https://www.bilibili.com/video/BV1GJ411x7h7?p=3", "BV1GJ411x7h7", 3, 0),
        ("https://www.bilibili.com/video/BV1GJ411x7h7?p=3&t=125", "BV1GJ411x7h7", 3, 125),
        ("BV1GJ411x7h7", "BV1GJ411x7h7", 1, 0),
        ("https://www.bilibili.com/video/av170001", None, 1, 0),
    ]
    for url, bvid, page, start in cases:
        try:
            parsed = platform.normalize(url)
        except Exception as exc:  # noqa: BLE001
            check(f"解析 {url}", False, str(exc))
            continue
        check(
            f"解析 {url}",
            parsed.get("bvid") == bvid
            and parsed.get("page") == page
            and parsed.get("start_time") == start,
            str(parsed),
        )
        check(f"match {url}", platform.match(parsed["url"]))

    check("非法链接被拒", _raises(platform.normalize, "https://www.bilibili.com/space/12345"))
    check("match 排除非 B 站", not platform.match("https://www.youtube.com/watch?v=xxx"))


def _raises(func, *args) -> bool:
    try:
        func(*args)
        return False
    except Exception:  # noqa: BLE001
        return True


def main() -> int:
    print("离线自检开始…")
    for test in (
        test_config,
        test_db,
        test_utils,
        test_render,
        test_mindmap_fallback,
        test_bilibili_parse,
    ):
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            FAILED.append(f"{test.__name__} 抛出异常：{exc}")
    print("\n" + "=" * 50)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  x {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
