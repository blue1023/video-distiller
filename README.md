# 🧪 视频蒸馏器 · B 站链接笔记生成工具

> 粘贴一条 B 站视频链接 → 自动抓字幕 → 调用大模型蒸馏 → 得到 **Markdown 笔记** + **可交互思维导图**（可导出 XMind / PNG / SVG）。

本项目是**本地自用的 Web 服务**：没有登录、没有次数限制、没有云端存储，所有产物都躺在你自己的硬盘上。

---

## ✨ 功能一览

| 能力 | 说明 |
| --- | --- |
| 🔗 链接输入 | 支持完整链接、BV 号、av 号、b23.tv 短链、带 `?p=3&t=120` 的分 P / 时间戳链接 |
| 📝 字幕抓取 | 优先人工 CC 字幕 → B 站 AI 字幕 → 无字幕时自动下载音频并语音转写 |
| 🤖 大模型蒸馏 | 任意 **OpenAI 兼容**接口（DeepSeek / 通义 / Kimi / 硅基流动 / OpenAI / 本地 Ollama） |
| 📄 Markdown 产物 | 摘要、关键词、带时间戳跳转的目录、分章节详细笔记、核心结论、金句、行动清单 |
| 🧠 思维导图 | 网页内可缩放/拖拽/折叠，一键导出 **图片(PNG)**、**矢量(SVG)**、**XMind**、**OPML** |
| ⏱ 实时进度 | SSE 推送每个阶段（解析 → 字幕 → AI 整理 → 落盘），长视频不用干等 |
| 💾 本地归档 | 笔记落盘 `data/notes/`，SQLite 建索引，网页里可回看、重下、删除 |
| 🧩 预留扩展 | 平台适配器 + 转写适配器 + LLM 适配器三层解耦，加 YouTube/抖音只需新增一个类 |
| 🌙 深色模式 | 跟随系统，离线可用（前端依赖已本地化，无网也能跑） |

---

## 🚀 快速开始

### 1. 环境要求

- **Python 3.10+**（开发环境为 3.12）
- 可选：**ffmpeg**（无字幕视频走语音转写时，用于把音频转成 16k wav；非必需）
- 不需要 Node.js（前端是原生 JS，依赖已放进 `web/vendor/`）

### 2. 安装

```bash
git clone <你的仓库地址>
cd 视频蒸馏器

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. 配置大模型

复制 `.env.example` 为 `.env` 并填写，或者**启动后直接在网页右上角「⚙️ 设置」里填**（推荐，改完立即生效，不用重启）。

最小配置只有一个 Key：

```ini
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-xxxxxxxx
LLM_MODEL=deepseek-chat
```

常见服务商填法：

| 服务 | `LLM_BASE_URL` | `LLM_MODEL` 示例 |
| --- | --- | --- |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 阿里通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 月之暗面 Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-32k` |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `deepseek-ai/DeepSeek-V3` |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | `qwen2.5:14b` |

> 💡 想省钱：`LLM_MODEL` 用便宜的大模型，`LLM_MINDMAP_MODEL` 用更小更快的模型专门做导图结构化。

### 4. 启动

```bash
python run.py
```

浏览器会自动打开 <http://127.0.0.1:8848>，把 B 站链接丢进输入框即可。

常用参数：

```bash
python run.py --port 9000        # 换端口
python run.py --no-browser       # 不自动开浏览器
python run.py --reload           # 开发模式，改代码自动重启
```

---

## 🧭 使用说明

1. **粘贴链接** → 选风格（精简 / 详尽 / 学术）→ 点「生成笔记」。
2. 左栏出现任务卡片，进度条会实时走；短字幕视频约 20-40 秒，1 小时长视频约 1-3 分钟。
3. 完成后右栏显示**思维导图**，切到 **Markdown** 页签看全文。
4. 右上按钮：`🖼 导出图片`（PNG，2 倍图）、`SVG`、`🧠 XMind`、`⬇️ 下载 MD`。
5. 生成的产物同时落盘在：

```
data/notes/20250101-120000_视频标题.md            # Markdown 笔记
data/notes/20250101-120000_视频标题.mindmap.json  # 导图树结构
data/notes/20250101-120000_视频标题.xmind         # 可编辑的 XMind 文件
data/notes/20250101-120000_视频标题.opml          # 通用大纲格式
```

---

## ⚙️ 配置项总览

所有项都可以写在 `.env`，也可以由网页设置面板写入 `data/settings.json`（**环境变量优先级更高**）。

### 大模型
| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI 兼容接口地址 |
| `LLM_API_KEY` | 空 | API Key；本地 Ollama 可留空 |
| `LLM_MODEL` | `deepseek-chat` | 主模型 |
| `LLM_MINDMAP_MODEL` | 空 | 专用于导图结构化的模型，留空复用主模型 |
| `LLM_TEMPERATURE` | `0.3` | 采样温度 |
| `LLM_TIMEOUT` | `300` | 单次请求超时（秒） |

### B 站
| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `BILI_COOKIE` | 空 | 遇到 `412` 风控、或想拿 AI 字幕时，把浏览器 Cookie 整串贴进来 |
| `BILI_USER_AGENT` | Chrome UA | 请求头 UA |
| `BILI_REQUEST_INTERVAL` | `1.0` | 请求间隔，避免触发风控 |

### 语音转写（无字幕视频的兜底）
| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ASR_PROVIDER` | `none` | `none` / `openai` / `faster-whisper` |
| `ASR_MODEL` | `whisper-1` | OpenAI 兼容转写模型名 |
| `ASR_BASE_URL` / `ASR_API_KEY` | 空 | 转写专用接口与 Key，留空复用大模型配置 |
| `ASR_LOCAL_MODEL` | `small` | 本地模型规格：`tiny`/`base`/`small`/`medium`/`large-v3` |
| `ASR_LOCAL_DEVICE` | `auto` | `auto`/`cuda`/`cpu` |

启用本地转写：

```bash
pip install faster-whisper
```

然后在网页设置里把「语音转写」改成 `faster-whisper`（首次运行会自动下载模型，`small` 约 500MB）。

### 服务与生成偏好
| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HOST` / `PORT` | `127.0.0.1` / `8848` | 监听地址与端口 |
| `NOTE_STYLE` | `detailed` | 默认笔记风格：`concise` / `detailed` / `academic` |

---

## 🧱 项目结构

```
视频蒸馏器/
├── run.py                     # 启动入口（python run.py）
├── requirements.txt
├── .env.example               # 配置模板
├── src/
│   ├── app.py                 # FastAPI 应用（含静态资源挂载）
│   ├── pipeline.py            # 核心流水线：链接→字幕→LLM→落盘
│   ├── asr.py                 # 语音转写适配器（openai / faster-whisper）
│   ├── core/
│   │   ├── config.py          # 配置加载（env > settings.json > 默认）
│   │   ├── db.py              # SQLite 持久层 + SSE 事件总线
│   │   └── utils.py           # 文件名清洗、Markdown 解析、XMind/OPML 生成
│   ├── platforms/
│   │   ├── base.py            # 平台抽象接口（扩展点 ①）
│   │   ├── bilibili.py        # B 站实现（WBI 签名 / 字幕 / 音频）
│   │   └── registry.py        # 平台注册表
│   ├── llm/
│   │   ├── client.py          # OpenAI 兼容客户端（重试 / JSON 抽取）
│   │   └── notes.py           # 两阶段 Prompt 工程 + Markdown 模板
│   └── webapi/                # REST + SSE 路由
├── web/                       # 前端（原生 JS，无需构建）
│   ├── index.html
│   ├── css/app.css
│   ├── js/{app,api,markdown,mindmap,fallback}.js
│   └── vendor/                # d3 / markmap / marked（离线可用）
├── tools/                     # 自检与调试脚本（见 tools/README.md）
│   ├── selftest_offline.py    # 离线自检：纯函数 / 数据库 / 模板渲染
│   ├── selftest_network.py    # 联网自检：B 站接口 / 字幕 / 音频
│   ├── selftest_e2e.py        # 端到端：假模型跑完整流水线（无需 API Key）
│   ├── fetch_vendor.py        # 重新拉取前端依赖到 web/vendor
│   ├── make_sample_artifact.py# 生成样例产物，检查排版
│   └── probe_bili_*.py        # B 站接口可用性排查
└── data/                      # 运行产物（已 gitignore）
    ├── app.db
    ├── settings.json
    └── notes/
```

---

## 🔌 扩展指南

### ① 新增视频平台（YouTube / 抖音 / 小宇宙…）

```python
# src/platforms/youtube.py
from .base import BasePlatform, Transcript, VideoInfo, VideoPart

class YouTubePlatform(BasePlatform):
    name = "youtube"
    display_name = "YouTube"

    def match(self, url: str) -> bool:
        return "youtube.com" in url or "youtu.be" in url

    def normalize(self, url: str) -> dict:
        ...  # 解析出 video_id / page

    async def fetch_info(self, parsed) -> VideoInfo:
        ...

    async def fetch_transcript(self, parsed, info, part, *, allow_asr=True) -> Transcript:
        ...
```

然后在 `src/platforms/registry.py` 的 `_load_builtin()` 里 `register(YouTubePlatform)`。
流水线与前端**不需要任何改动**，平台列表会自动出现在 `/api/platforms`。

### ② 接入本地视频上传

`BasePlatform` 的接口天然支持：新增一个 `LocalFilePlatform`，`match()` 判断 URL 是否是 `file://` 或自定义 scheme，`fetch_transcript()` 直接调用 `src/asr.py` 的 `transcribe_audio()`。前端的上传入口只需加一个 `<input type="file">` 并调用现有的 `/api/tasks`。

### ③ 换 ASR 后端

在 `src/asr.py` 里加一个分支函数（例如 `_transcribe_whispercpp`），在 `transcribe_audio()` 里按 `settings.asr_provider` 分发即可。

### ④ 换思维导图库

前端 `web/js/mindmap.js` 已经把所有 markmap 细节封装成 5 个方法（`init/render/fit/rescale/exportSvg/exportPng`）。换成 ECharts 树图或 AntV G6，只要保持这几个方法签名，`app.js` 无需改动。

---

## 🌐 API 速查

服务启动后可访问 <http://127.0.0.1:8848/docs> 查看交互式文档。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/tasks` | 创建任务 `{url, note_style}` |
| `GET` | `/api/tasks` | 任务列表 |
| `GET` | `/api/tasks/{id}` | 任务详情 |
| `GET` | `/api/tasks/{id}/events` | **SSE** 实时进度流 |
| `POST` | `/api/tasks/{id}/cancel` | 取消任务 |
| `GET` | `/api/notes` | 笔记列表 |
| `GET` | `/api/notes/{id}` | 笔记详情（含 Markdown 正文与导图树） |
| `GET` | `/api/notes/{id}/download?kind=md\|json\|xmind\|opml` | 下载产物 |
| `PATCH` | `/api/settings` | 更新设置 |
| `POST` | `/api/settings/test-llm` | 大模型连通性自检 |
| `POST` | `/api/settings/test-bilibili` | B 站接口连通性自检 |

命令行用法示例：

```bash
# 提交任务
curl -X POST http://127.0.0.1:8848/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.bilibili.com/video/BV1GJ411x7h7"}'

# 下载 Markdown
curl -OJ "http://127.0.0.1:8848/api/notes/n_xxxx/download?kind=md"
```

---

## 🛠 常见问题

**Q：报错 `B 站返回 412（风控）`？**
在浏览器登录 B 站 → F12 → Network → 任意请求 → 复制 `Cookie` 整串 → 粘贴到设置里的「B 站 Cookie」。

**Q：提示「拿不到字幕」？**
B 站的字幕接口（`x/player/wbi/v2`）**需要登录态**，匿名请求只能拿到空的字幕列表——所以想抓字幕**必须配 Cookie**。
两条路（任选其一，推荐都配）：

1. 在设置里粘贴浏览器 Cookie，即可抓人工 CC 字幕与 B 站 AI 字幕；
2. 在设置里把「语音转写」打开（`faster-whisper` 本地离线免费，或 `openai` 走云端），
   工具会自动下载音频并转写，匿名也能用。

想确认到底是哪种情况，跑一下 `python tools/probe_bili_subtitle.py <BV号>`。

**Q：大模型报 401 / 余额不足？**
设置面板里点「测试大模型连接」可快速定位是 Key、地址还是模型名的问题。

**Q：长视频生成很慢？**
字幕超过 3 万字会自动分块 map-reduce，多次调用模型。可以换更快的模型，或把风格调成「精简」。

**Q：导图节点太多太乱？**
导图默认限制 4 层、单节点最多 7 个子节点。想更细可以调大 `src/llm/notes.py` 里 `generate_mindmap(max_nodes=...)`。

**Q：端口被占用？**
`python run.py --port 9000`。

**Q：前端导图不显示？**
说明 `web/vendor/` 里的依赖没加载成功。执行 `python tools/fetch_vendor.py` 重新拉取（会自动回退 CDN）。

---

## ⚠️ 免责声明

本项目仅用于**个人学习与研究**，请遵守 B 站用户协议与相关法律法规：

- 不要高频请求、批量抓取，请保持默认的请求间隔；
- 生成的笔记仅供个人复习使用，**请勿二次传播原视频内容**；
- 视频版权归原作者所有，请在笔记中保留原视频链接与 UP 主署名（本项目已自动写入）。

---

## 🗺 Roadmap

- [ ] YouTube / 抖音平台适配器
- [ ] 本地视频文件上传（走同一套转写链路）
- [ ] 批量链接排队与并发控制
- [ ] 笔记全文检索（SQLite FTS5）
- [ ] 导出 Word / PDF / Anki 卡片
- [ ] 支持 B 站合集与 UP 主整页批量蒸馏
- [ ] Docker 一键部署

## 📄 License

MIT
