# tools/ 自检与调试脚本

这些脚本都是**独立可执行**的，方便在改代码后快速验证，或者在出问题时定位责任方。

## 快速开始

```bash
# 1. 离线自检（秒级，不需要网络、不需要 API Key）
python tools/selftest_offline.py

# 2. 联网自检（验证 B 站接口 / 字幕 / 音频下载是否可用）
python tools/selftest_network.py
python tools/selftest_network.py https://www.bilibili.com/video/BVxxxx   # 指定视频

# 3. 端到端自检（会起一个"假的大模型服务"，不需要真实 API Key）
python tools/selftest_e2e.py
```

## 脚本清单

| 脚本 | 作用 | 依赖 |
| --- | --- | --- |
| `selftest_offline.py` | 配置加载、SQLite 读写与迁移、Markdown→导图解析、XMind/OPML 生成、字幕切块、笔记模板渲染、B 站链接解析 | 仅标准库（httpx 缺失时自动跳过 B 站解析项） |
| `selftest_network.py` | 真实请求 B 站：链接解析 → 视频信息 → 分P → 字幕接口 → 音频流下载 | 网络 + httpx |
| `selftest_e2e.py` | 起假模型服务 + FastAPI TestClient，跑完建任务→转写→生成→导出→清理全流程 | 网络（抓真实视频）+ 全部依赖 |
| `fetch_vendor.py` | 把 markmap / d3 / marked 下载到 `web/vendor/`（离线可用） | 网络 |
| `make_sample_artifact.py` | 用假数据生成一份 md/xmind/opml 样例，人工检查排版 | 无 |
| `find_subtitle_video.py` | 搜索并检测哪些视频带字幕（排查"拿不到字幕"用） | 网络 |
| `probe_bili_api.py` | 探测各 B 站接口在匿名下的返回码与字段 | 网络 |
| `probe_bili_subtitle.py` | 深入对比 `player/v2` 与 `player/wbi/v2` 的字幕返回 | 网络 |
| `probe_video_page.py` | 检查视频页 HTML 里是否内嵌字幕信息 | 网络 |
| `gh_push.py` | 校验 GitHub Token、创建仓库、推送代码（token 走 `GH_TOKEN` 环境变量） | 网络 |
| `gh_check.py` | 对比本地已提交文件与远端仓库是否一致 | 网络 |

## 关于 `selftest_e2e.py`

它会临时把配置指向本地的假模型服务（`http://127.0.0.1:18765/v1`），跑完后请到网页设置里改回你自己的 API。

- `--mock-only`：只起假服务，方便手工 `curl` / 开网页联调。
  配合 `run.py` 使用：

  ```bash
  # 终端 1
  python tools/selftest_e2e.py --mock-only
  # 终端 2（把模型与转写都指向假服务）
  set LLM_BASE_URL=http://127.0.0.1:18765/v1
  set LLM_API_KEY=mock-key
  set ASR_PROVIDER=openai
  set ASR_BASE_URL=http://127.0.0.1:18765/v1
  python run.py --no-browser
  ```

## 测试产物位置

所有测试产物都写到 `.tmp/`（已 gitignore），不污染 `data/`：

```
.tmp/selftest/   离线自检的临时文件
.tmp/e2e/        端到端自检的数据库
.tmp/sample/     样例产物
```

## 排查"拿不到字幕"

B 站的字幕接口（`x/player/wbi/v2`）**需要登录态**，匿名请求会返回空的字幕列表。
判断方法：

```bash
python tools/probe_bili_subtitle.py BV1xx411c7mD
```

- 若 `subtitle.subtitles=0` 且你没配 Cookie → 属正常，请到网页设置里粘贴浏览器 Cookie，
  或开启「语音转写」用音频兜底（匿名也能下到音频流）。
- 若配了 Cookie 仍为 0 → Cookie 可能已过期，重新复制一次。
