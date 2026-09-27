#!/usr/bin/env python
"""生成一份样例产物（用假数据），用于人工检查排版质量。

用法：python tools/make_sample_artifact.py [输出目录]
默认写到 .tmp/sample/，不会污染 data/notes。
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

from src.core.utils import tree_to_opml, tree_to_xmind  # noqa: E402
from src.llm.notes import _tree_from_outline, render_markdown  # noqa: E402

OUT_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE_DIR / ".tmp" / "sample"
OUT_DIR.mkdir(parents=True, exist_ok=True)

info = types.SimpleNamespace(
    title="3 小时讲透：从零实现一个 RAG 系统（含向量库、召回与重排）",
    url="https://www.bilibili.com/video/BV1xx411c7mD",
    uploader="某技术UP主",
    duration=10800,
    view_count=128456,
    video_id="BV1xx411c7mD",
    parts=[1, 2],
)
part = types.SimpleNamespace(index=1, title="第一讲：整体架构与数据准备", duration=3600)

STRUCTURED: dict = {
    "title": "从零实现 RAG 系统：架构、向量库与召回重排",
    "one_sentence": (
        "作者用 3 小时从零搭了一套可用的 RAG 系统，"
        "重点是分块策略、混合召回与重排这三处工程细节。"
    ),
    "summary": (
        "视频先从「为什么朴素 RAG 效果差」切入，讲清 RAG 的四个环节（解析、分块、召回、生成），"
        "再逐个拆解工程实现：用递归分块结合标题感知来保留语义边界；"
        "用 BM25 与向量检索做混合召回，再用 cross-encoder 重排；"
        "最后给出评估方法与三个常见坑（分块过大、只做向量召回、忽略元数据过滤）。"
    ),
    "keywords": ["RAG", "向量检索", "BM25", "重排", "分块策略", "评估"],
    "outline": [
        {
            "heading": "为什么朴素 RAG 不好用",
            "level": 2,
            "start": 125,
            "end": 900,
            "points": [
                "朴素 RAG 的三个失效场景：多跳问题、术语指代、需要跨文档汇总",
                "把 embedding 当成理解是最大的误解，它只是把语义压到一个点上",
                "实测：同一份文档换一种分块方式，召回命中率能差 20 个百分点以上",
            ],
        },
        {
            "heading": "数据准备与分块策略",
            "level": 2,
            "start": 900,
            "end": 2100,
            "points": [
                "先用 pdfplumber 抽文本，再按标题层级建立章节路径元数据，例如 第三章 > 3.2 > 3.2.1",
                "递归分块：优先在段落边界切，其次句子，最后才硬切；块大小 512 token、重叠 64 token",
                "- 重叠不是为了记住上下文，而是为了保证跨块的句子不被截断",
                "- 中文场景用字符数估算会偏大，建议用 tokenizer 实测",
                "每个块都要带上来源文件、章节路径、页码、时间戳，后续过滤与引用全靠它",
            ],
        },
        {
            "heading": "混合召回：BM25 加向量",
            "level": 2,
            "start": 2100,
            "end": 3300,
            "points": [
                "只做向量召回时，专有名词与编号（如 3.2.1、RFC 7231）几乎必然漏检",
                "BM25 负责字面命中，向量负责语义泛化，两路各取 top 50 再融合",
                "融合用 RRF（倒数排名融合），不需要调权重，实践中最省事",
                "元数据过滤要前置：先按章节或时间缩小范围再排序，提升比换模型更明显",
            ],
        },
        {
            "heading": "重排与生成",
            "level": 2,
            "start": 3300,
            "end": 4200,
            "points": [
                "cross-encoder 重排把 top 50 压到 top 5，延迟增加约 200ms 但准确率提升显著",
                "重排模型选百兆级的小模型就够，用大模型重排性价比很低",
                "Prompt 里要求只依据给定片段回答并标注来源编号，可大幅降低幻觉",
            ],
        },
        {
            "heading": "评估方法与常见坑",
            "level": 2,
            "start": 4200,
            "end": 5400,
            "points": [
                "先建 50 条人工标注的问答集，比追求自动化指标更实际",
                "分开评估召回是否命中与生成是否正确，否则无法定位问题",
                "三个坑：分块过大导致召回噪声、只做向量召回、忽略元数据过滤",
            ],
        },
    ],
    "highlights": [
        "分块策略对效果的影响大于换 embedding 模型",
        "混合召回加 RRF 融合是低成本高收益的组合",
        "元数据过滤应该前置，而不是在生成阶段补救",
        "评估要拆成召回与生成两段分别看",
    ],
    "quotes": [
        {"text": "embedding 不是理解，它只是把语义压成一个点，别指望它替你推理。", "time": "08:12"},
        {"text": "你把文档切碎了，就要负责把上下文还回去。", "time": "21:40"},
    ],
    "action_items": [
        "给自己的文档库加上章节路径元数据",
        "在现有检索链路里加一路 BM25，用 RRF 融合",
        "标注 50 条问答作为回归测试集",
    ],
    "open_questions": ["中文长文档的最佳块大小是否与领域强相关？"],
}

markdown = render_markdown(
    STRUCTURED,
    info,
    part,
    transcript_source="subtitle_ai",
    note_style="detailed",
    model="deepseek-chat",
    generated_at="2025-01-01 12:00:00",
    stats={"transcript_chars": 24800},
)

tree = _tree_from_outline(STRUCTURED, STRUCTURED["title"][:30])
clean_tree = {k: v for k, v in tree.items() if not k.startswith("_")}

(OUT_DIR / "sample.md").write_text(markdown, encoding="utf-8")
(OUT_DIR / "sample.mindmap.json").write_text(
    json.dumps(clean_tree, ensure_ascii=False, indent=2), encoding="utf-8"
)
(OUT_DIR / "sample.xmind").write_bytes(tree_to_xmind(clean_tree, sheet_title="RAG 笔记"))
(OUT_DIR / "sample.opml").write_text(tree_to_opml(clean_tree, title="RAG 笔记"), encoding="utf-8")

print(f"样例产物已写入：{OUT_DIR}")
print(f"  Markdown {len(markdown)} 字")
print("\n---- Markdown 预览（前 55 行）----")
for line in markdown.splitlines()[:55]:
    print(line)
