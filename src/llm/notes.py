"""笔记与思维导图的生成逻辑（Prompt 工程都在这里）。

两阶段策略：
  阶段 A  结构化理解：把字幕（必要时 map-reduce 分块压缩）变成一个规整的
          JSON——摘要、关键词、章节大纲（带时间戳）、要点、金句。
  阶段 B  渲染：Markdown 由 Python 按模板拼装（格式稳定、可控），
          思维导图由 A 的结果 + 轻量二次调用来生成层级。
好处：Markdown 结构永远正确，导图也不依赖模型"写 markdown"的水平。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from ..core.config import Settings, get_settings
from ..core.utils import count_words, format_duration, get_logger
from ..platforms.base import VideoInfo, VideoPart
from .client import LLMError, LLMResult, chat_json
log = get_logger("llm.notes")

# ---------------------------------------------------------------- Prompt

STYLE_GUIDE = {
    "concise": "风格要求：极度精简，只保留主干结论与关键数字，每个小节 2-3 条即可。",
    "detailed": "风格要求：详尽完整，保留推理链条、案例、数据与结论，适合当作学习笔记复习。",
    "academic": "风格要求：学术化表达，突出概念定义、方法论、论证结构与局限性，术语准确。",
}

#: 给渲染用的中文风格名
STYLE_LABELS = {"concise": "精简", "detailed": "详尽", "academic": "学术"}

SYSTEM_STRUCT = """你是一位顶级的知识整理专家，专门把视频字幕整理成高质量结构化笔记。
你必须在忠实于原视频内容的前提下做结构化，不得编造视频中没有的信息。

输出严格为 JSON 对象，结构如下（字段名必须完全一致）：
{
  "title": "笔记标题，需体现视频核心主题，可对原标题做提炼，30 字以内",
  "one_sentence": "一句话总结整期视频（60 字以内，要有信息量，不要空话）",
  "summary": "200-400 字的整体摘要，说明视频讲了什么、怎么讲的、结论是什么",
  "keywords": ["3-8 个关键词或主题词"],
  "outline": [
    {
      "heading": "章节标题（若视频有明确章节请沿用其叫法，否则自行归纳）",
      "level": 2,
      "start": 0,
      "end": 120,
      "points": [
        "该章节的要点，一条一个独立事实/结论/步骤，尽量保留具体数字、名词、方法名",
        "要点可以嵌套一层，用 Markdown 列表语法写在字符串里：'- 子要点'"
      ]
    }
  ],
  "highlights": ["3-6 条全片最值得记住的结论或洞察"],
  "quotes": [{"text": "视频里原话或高度接近原话的句子", "time": "12:34"}],
  "action_items": ["可执行的下一步建议（如视频本身就是教程）；没有就给空数组"],
  "open_questions": ["视频没有解答但值得思考的问题；没有就给空数组"]
}

硬性规则：
1. outline 必须覆盖视频从头到尾的内容，章节之间时间不重叠，start/end 为秒数（整数）。
2. points 里的每一条都要具体，禁止"讲解了相关内容"这类空话。
3. 全部使用中文输出（专有名词、代码、英文术语保留原文）。
4. 不要输出 markdown 代码块围栏，只输出 JSON 本身。"""

SYSTEM_MERGE = """你是一位知识整理专家。下面是同一期视频多个片段的摘要 JSON 列表。
请把它们合并成一份完整、连贯、不重复的结构化笔记 JSON，覆盖视频全程。

输出结构与输入单条完全一致（title / one_sentence / summary / keywords / outline /
highlights / quotes / action_items / open_questions），要求：
1. outline 按时间顺序排列，合并相邻同类内容，不要出现重复章节；
2. 保留每一条有价值的具体信息（数字、方法名、结论），不要因为合并而丢信息；
3. 时间戳单位是秒（整数）；
4. 全部中文输出，只输出 JSON，不要代码块围栏。"""

SYSTEM_CHUNK = """你在为长视频做分块速记。下面是视频字幕的其中一段（带 [mm:ss] 时间戳）。
请只针对这一段，输出 JSON：
{
  "title": "这一段讲的主题",
  "summary": "这一段的内容摘要，200 字以内",
  "keywords": ["关键词"],
  "outline": [{"heading": "小节标题", "level": 2, "start": 0, "end": 120, "points": ["具体要点"]}],
  "highlights": ["这一段里最重要的结论"],
  "quotes": [{"text": "原话", "time": "12:34"}]
}
规则：start/end 使用这一段内的真实秒数（依据字幕时间戳推算），points 必须具体，
全部中文，只输出 JSON。"""

SYSTEM_MINDMAP = """你是思维导图结构设计师。请把给定的笔记 JSON 改写成适合"脑图"阅读的层级结构。

输出 JSON：
{"root": "根节点文字（视频主题，12 字以内）", "children": [{"name": "一级节点", "children": [{"name": "二级节点", "children": [{"name": "三级节点"}]}]}]}

规则：
1. 层级最多 4 层，节点总数控制在 {max_nodes} 个以内；
2. 每个节点文字不超过 20 字，是名词短语或结论，不要整句解释；
3. 结构要 MECE（相互独立、完全穷尽），同一父节点下的子节点数量不要超过 7 个；
4. 不要丢关键信息，但可以合并同类项；
5. 全部中文，只输出 JSON，不要代码块围栏。"""


# ---------------------------------------------------------------- 阶段 A


def _video_header(info: VideoInfo, part: VideoPart, transcript_source: str) -> str:
    lines = [
        f"视频标题：{info.title}",
        f"分P：第 {part.index} P — {part.title}（时长 {format_duration(part.duration or info.duration)}）",
        f"UP 主：{info.uploader or '未知'}",
        f"字幕来源：{transcript_source}",
    ]
    if info.description:
        lines.append(f"视频简介（节选）：{info.description[:600]}")
    tags = [t for t in (info.tags or []) if t]
    if tags:
        lines.append(f"原始标签：{'、'.join(tags)}")
    return "\n".join(lines)


def _truncate_transcript(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.6)]
    tail = text[-int(limit * 0.35) :]
    return f"{head}\n\n……（中间内容因超长被省略，请基于首尾内容做归纳）……\n\n{tail}"


async def _summarize_chunk(
    index: int,
    total: int,
    chunk: str,
    header: str,
    settings: Settings,
) -> Dict[str, Any]:
    messages = [
        {"role": "system", "content": SYSTEM_CHUNK},
        {
            "role": "user",
            "content": (
                f"{header}\n\n这是第 {index}/{total} 段字幕：\n"
                f"<字幕>\n{chunk}\n</字幕>"
            ),
        },
    ]
    result = await chat_json(messages, settings, temperature=0.2)
    data = result.data
    data.setdefault("_tokens", {})
    data["_tokens"] = {
        "prompt": result.prompt_tokens,
        "completion": result.completion_tokens,
    }
    return data


async def _structure_transcript(
    transcript: str,
    header: str,
    settings: Settings,
    note_style: str,
) -> LLMResult:
    """把字幕变成结构化 JSON；超长时自动 map-reduce。"""
    style = STYLE_GUIDE.get(note_style, STYLE_GUIDE["detailed"])
    max_chars = 24000 if "qwen" in settings.llm_model.lower() else 30000
    chunks = [transcript] if len(transcript) <= max_chars else _split_soft(transcript, max_chars)

    if len(chunks) <= 1:
        messages = [
            {"role": "system", "content": SYSTEM_STRUCT},
            {
                "role": "user",
                "content": (
                    f"{header}\n\n{style}\n\n"
                    f"请根据下面的视频字幕生成结构化笔记 JSON。\n"
                    f"字幕中的 [mm:ss] 是本条字幕出现的时刻，用它推算章节 start/end。\n\n"
                    f"<字幕>\n{_truncate_transcript(transcript, 120000)}\n</字幕>"
                ),
            },
        ]
        return await chat_json(messages, settings)

    log.info("字幕超长（%d 字），分 %d 块做 map-reduce", len(transcript), len(chunks))

    # map：并发分块速记（限流 3）
    semaphore = asyncio.Semaphore(3)

    async def worker(idx: int, chunk: str) -> Dict[str, Any]:
        async with semaphore:
            try:
                return await _summarize_chunk(idx, len(chunks), chunk, header, settings)
            except LLMError as exc:
                log.warning("第 %d 块摘要失败：%s", idx, exc)
                return {"title": f"第 {idx} 段", "summary": chunk[:500], "outline": []}

    partials = await asyncio.gather(*(worker(i, c) for i, c in enumerate(chunks, 1)))

    # 分块太多时先两两粗合并，避免合并请求本身超长
    while len(partials) > 8:
        merged_round: List[Dict[str, Any]] = []
        for i in range(0, len(partials), 4):
            group = partials[i : i + 4]
            merged_round.append(await _merge_partials(group, header, settings))
        partials = merged_round

    merged = await _merge_partials(partials, header, settings, style=style)
    return merged


def _split_soft(text: str, max_chars: int) -> List[str]:
    from ..core.utils import split_transcript

    return split_transcript(text, max_chars=max_chars, overlap=200)


async def _merge_partials(
    partials: List[Dict[str, Any]],
    header: str,
    settings: Settings,
    style: str = "",
) -> LLMResult:
    import json

    payload = json.dumps(partials, ensure_ascii=False)
    if len(payload) > 90000:
        payload = payload[:90000]
    messages = [
        {"role": "system", "content": SYSTEM_MERGE},
        {
            "role": "user",
            "content": f"{header}\n\n{style}\n\n片段摘要列表：\n{payload}",
        },
    ]
    return await chat_json(messages, settings, temperature=0.2)


# ---------------------------------------------------------------- Markdown 渲染


def _fmt_time(seconds: Any) -> str:
    try:
        return format_duration(int(float(seconds)))
    except (TypeError, ValueError):
        return "0:00"


def _chapter_link(info: VideoInfo, part: VideoPart, seconds: Any) -> str:
    """带时间跳转的链接：点开直接回到视频对应位置。"""
    try:
        start = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        start = 0
    base = info.url or f"https://www.bilibili.com/video/{info.video_id}"
    separator = "&" if "?" in base else "?"
    if part.index > 1:
        base = f"{base}{separator}p={part.index}&t={start}"
    else:
        base = f"{base}{separator}t={start}"
    return base


def render_markdown(
    data: Dict[str, Any],
    info: VideoInfo,
    part: VideoPart,
    *,
    transcript_source: str,
    note_style: str,
    model: str,
    generated_at: str,
    stats: Optional[Dict[str, Any]] = None,
) -> str:
    title = str(data.get("title") or info.title).strip()
    one_sentence = str(data.get("one_sentence") or "").strip()
    summary = str(data.get("summary") or "").strip()
    keywords = [str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()]
    outline = data.get("outline") or []
    highlights = [str(h).strip() for h in (data.get("highlights") or []) if str(h).strip()]
    quotes = data.get("quotes") or []
    actions = [str(a).strip() for a in (data.get("action_items") or []) if str(a).strip()]
    questions = [str(q).strip() for q in (data.get("open_questions") or []) if str(q).strip()]

    source_label = {
        "subtitle_manual": "视频自带字幕（人工上传）",
        "subtitle_ai": "B 站 AI 字幕",
        "asr_openai": "语音转写（云端 Whisper）",
        "asr_faster-whisper": "语音转写（本地 faster-whisper）",
        "asr_local": "语音转写（本地）",
    }.get(transcript_source, transcript_source or "未知")

    lines: List[str] = []
    lines.append(f"# {title}")
    lines.append("")
    if one_sentence:
        lines.append(f"> **一句话总结**：{one_sentence}")
        lines.append("")

    # 元信息表
    lines.append("| 项目 | 内容 |")
    lines.append("| --- | --- |")
    lines.append(f"| 原视频 | [{info.title}]({info.url}) |")
    if len(info.parts) > 1:
        lines.append(f"| 分P | 第 {part.index} P · {part.title} |")
    lines.append(f"| UP 主 | {info.uploader or '未知'} |")
    lines.append(f"| 时长 | {format_duration(part.duration or info.duration)} |")
    if info.view_count is not None:
        lines.append(f"| 播放量 | {info.view_count:,} |")
    lines.append(f"| 字幕来源 | {source_label} |")
    lines.append(f"| 笔记风格 | {STYLE_LABELS.get(note_style, note_style)} |")
    lines.append(f"| 生成时间 | {generated_at} |")
    lines.append(f"| 生成模型 | {model} |")
    lines.append("")

    if summary:
        lines.append("## 📌 内容摘要")
        lines.append("")
        lines.append(summary)
        lines.append("")

    if keywords:
        lines.append("## 🏷️ 关键词")
        lines.append("")
        lines.append(" ".join(f"`{k}`" for k in keywords))
        lines.append("")

    # 目录
    if outline:
        lines.append("## 🧭 目录")
        lines.append("")
        for index, section in enumerate(outline, 1):
            heading = str(section.get("heading") or f"第 {index} 节").strip()
            start = section.get("start")
            lines.append(f"{index}. [{heading}](#{_anchor(heading)}) · [{_fmt_time(start)}]({_chapter_link(info, part, start)})")
        lines.append("")

    # 正文
    if outline:
        lines.append("## 📖 详细笔记")
        lines.append("")
        for index, section in enumerate(outline, 1):
            heading = str(section.get("heading") or f"第 {index} 节").strip()
            start = section.get("start")
            end = section.get("end")
            lines.append(f"### {index}. {heading}")
            lines.append("")
            span = f"{_fmt_time(start)} - {_fmt_time(end)}" if end else _fmt_time(start)
            lines.append(f"⏱ [{span}]({_chapter_link(info, part, start)})")
            lines.append("")
            for point in section.get("points") or []:
                point_text = str(point).strip()
                if not point_text:
                    continue
                if point_text.startswith(("-", "*", "•")):
                    lines.append(f"  {point_text}")
                else:
                    lines.append(f"- {point_text}")
            lines.append("")

    if highlights:
        lines.append("## 💡 核心结论")
        lines.append("")
        for item in highlights:
            lines.append(f"- {item}")
        lines.append("")

    if quotes:
        lines.append("## 💬 金句摘录")
        lines.append("")
        for quote in quotes:
            if isinstance(quote, dict):
                text = str(quote.get("text") or "").strip()
                when = str(quote.get("time") or "").strip()
            else:
                text, when = str(quote).strip(), ""
            if not text:
                continue
            suffix = f" —— `{when}`" if when else ""
            lines.append(f"> {text}{suffix}")
            lines.append("")

    if actions:
        lines.append("## ✅ 行动清单")
        lines.append("")
        for item in actions:
            lines.append(f"- [ ] {item}")
        lines.append("")

    if questions:
        lines.append("## ❓ 遗留问题")
        lines.append("")
        for item in questions:
            lines.append(f"- {item}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        f"*由「B 站链接笔记生成工具」自动生成 · 字幕 {stats.get('transcript_chars', 0) if stats else 0} 字"
        f" · 笔记 {count_words(chr(10).join(lines))} 字*"
    )
    lines.append("")
    return "\n".join(lines)


def _anchor(heading: str) -> str:
    import re

    text = re.sub(r"[^\w\u4e00-\u9fff\s-]", "", heading).strip().lower()
    return re.sub(r"\s+", "-", text)


# ---------------------------------------------------------------- 阶段 B：导图


def _tree_from_outline(data: Dict[str, Any], root_title: str) -> Dict[str, Any]:
    """把结构化笔记直接映射成脑图（不额外调用模型也能出图）。"""
    root: Dict[str, Any] = {"content": root_title, "children": []}
    one = str(data.get("one_sentence") or "").strip()
    if one:
        root["children"].append({"content": one[:40], "children": []})

    for section in data.get("outline") or []:
        heading = str(section.get("heading") or "").strip()
        if not heading:
            continue
        node: Dict[str, Any] = {"content": heading, "children": []}
        for point in (section.get("points") or [])[:8]:
            text = str(point).strip().lstrip("-*• ").strip()
            if not text:
                continue
            if len(text) > 48:
                text = text[:48] + "…"
            node["children"].append({"content": text, "children": []})
        root["children"].append(node)

    for item in (data.get("highlights") or [])[:6]:
        text = str(item).strip()
        if text:
            root["children"].append(
                {
                    "content": "核心结论",
                    "children": [{"content": text[:48], "children": []}],
                }
            )
            break
    return root


def _normalize_model_tree(raw: Dict[str, Any], fallback_title: str) -> Dict[str, Any]:
    def walk(node: Any, depth: int) -> Optional[Dict[str, Any]]:
        if depth > 6 or not isinstance(node, dict):
            return None
        name = str(node.get("name") or node.get("content") or node.get("title") or "").strip()
        if not name:
            return None
        children: List[Dict[str, Any]] = []
        for child in node.get("children") or []:
            if isinstance(child, str):
                if child.strip():
                    children.append({"content": child.strip(), "children": []})
                continue
            converted = walk(child, depth + 1)
            if converted:
                children.append(converted)
        return {"content": name, "children": children}

    root_name = str(raw.get("root") or raw.get("name") or fallback_title).strip()
    root: Dict[str, Any] = {"content": root_name or fallback_title, "children": []}
    for child in raw.get("children") or []:
        if isinstance(child, str):
            if child.strip():
                root["children"].append({"content": child.strip(), "children": []})
            continue
        converted = walk(child, 2)
        if converted:
            root["children"].append(converted)
    return root


async def generate_mindmap(
    structured: Dict[str, Any],
    info: VideoInfo,
    part: VideoPart,
    settings: Optional[Settings] = None,
    *,
    max_nodes: int = 60,
) -> Dict[str, Any]:
    """优先让模型重排层级；失败则退回"结构化笔记直出"的兜底树。"""
    settings = settings or get_settings()
    root_title = str(structured.get("title") or info.title)[:30]
    fallback = _tree_from_outline(structured, root_title)

    import json

    compact = {
        "title": structured.get("title"),
        "one_sentence": structured.get("one_sentence"),
        "keywords": structured.get("keywords"),
        "outline": [
            {
                "heading": s.get("heading"),
                "points": [str(p)[:120] for p in (s.get("points") or [])[:10]],
            }
            for s in (structured.get("outline") or [])
        ],
        "highlights": structured.get("highlights"),
    }
    messages = [
        {"role": "system", "content": SYSTEM_MINDMAP.replace("{max_nodes}", str(max_nodes))},
        {"role": "user", "content": json.dumps(compact, ensure_ascii=False)},
    ]
    try:
        result = await chat_json(
            messages,
            settings,
            model=settings.llm_mindmap_model or settings.llm_model,
            temperature=0.2,
        )
        tree = _normalize_model_tree(result.data, root_title)
        if len(tree.get("children") or []) < 2:
            log.warning("模型导图节点过少，回退到结构化直出")
            return fallback
        tree["_model"] = result.model
        tree["_tokens"] = {
            "prompt": result.prompt_tokens,
            "completion": result.completion_tokens,
        }
        return tree
    except LLMError as exc:
        log.warning("导图生成失败，使用兜底结构：%s", exc)
        return fallback


# ---------------------------------------------------------------- 对外入口


async def generate_note(
    transcript_text: str,
    info: VideoInfo,
    part: VideoPart,
    *,
    transcript_source: str = "",
    note_style: str = "detailed",
    settings: Optional[Settings] = None,
) -> Dict[str, Any]:
    """完整生成流程：结构化 -> Markdown -> 导图树。"""
    settings = settings or get_settings()
    header = _video_header(info, part, transcript_source)
    structured_result = await _structure_transcript(
        transcript_text, header, settings, note_style
    )
    structured = structured_result.data

    import time as _time

    generated_at = _time.strftime("%Y-%m-%d %H:%M:%S")
    stats = {"transcript_chars": len(transcript_text)}
    markdown = render_markdown(
        structured,
        info,
        part,
        transcript_source=transcript_source,
        note_style=note_style,
        model=structured_result.model,
        generated_at=generated_at,
        stats=stats,
    )
    tree = await generate_mindmap(structured, info, part, settings)

    return {
        "structured": structured,
        "markdown": markdown,
        "tree": tree,
        "model": structured_result.model,
        "usage": {
            "prompt_tokens": structured_result.prompt_tokens,
            "completion_tokens": structured_result.completion_tokens,
        },
        "generated_at": generated_at,
        "word_count": count_words(markdown),
    }
