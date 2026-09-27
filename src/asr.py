"""语音转写适配器：无字幕视频的兜底方案。

两种实现：
- openai        : 调用任何 OpenAI 兼容的 /v1/audio/transcriptions（如 OpenAI、硅基流动）
- faster-whisper: 本地离线转写（可选依赖，装不上就明确报错而不是静默失败）

音频统一先用 ffmpeg 转成 16k 单声道 wav（有 ffmpeg 时），否则直接把原始
m4s/mp4 交给后端 —— OpenAI 接口能直接吃 mp4/m4a，faster-whisper 也能解码。
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Optional

import httpx

from .core.config import MEDIA_DIR, Settings, get_settings
from .core.utils import format_duration, get_logger
from .platforms.base import PlatformError, Transcript

log = get_logger("asr")


class ASRError(RuntimeError):
    pass


async def transcribe_audio(
    audio_path: Path, settings: Optional[Settings] = None
) -> Transcript:
    settings = settings or get_settings()
    provider = (settings.asr_provider or "none").strip().lower()

    if provider in ("none", "", "off", "disabled"):
        raise PlatformError(
            "该视频没有字幕，且语音转写未开启。"
            "可在右上角设置里把「语音转写」设为 openai 或 faster-whisper 后重试。"
        )
    if not audio_path.exists() or audio_path.stat().st_size == 0:
        raise ASRError("音频文件为空，下载可能失败")

    prepared = await asyncio.to_thread(_prepare_audio, audio_path)

    if provider == "openai":
        text = await _transcribe_openai(prepared, settings)
    elif provider in ("faster-whisper", "faster_whisper", "local"):
        text = await asyncio.to_thread(_transcribe_local, prepared, settings)
    else:
        raise ASRError(f"未知的语音转写后端：{provider}")

    if not (text or "").strip():
        raise ASRError("转写结果为空，可能是纯音乐/无人声视频")
    log.info("转写完成，共 %d 字", len(text))
    return Transcript(text=text.strip(), source=f"asr_{provider}", language="")


def _prepare_audio(audio_path: Path) -> Path:
    """有 ffmpeg 就转 16k 单声道 wav（体积小、兼容性最好）。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        log.warning("未检测到 ffmpeg，直接使用原始音频文件交给转写后端")
        return audio_path
    target = audio_path.with_suffix(".wav")
    command = [
        ffmpeg, "-y", "-loglevel", "error",
        "-i", str(audio_path),
        "-vn", "-ac", "1", "-ar", "16000", "-f", "wav",
        str(target),
    ]
    try:
        import subprocess

        subprocess.run(command, check=True, capture_output=True, timeout=1800)
        return target
    except Exception as exc:  # noqa: BLE001 - 转码失败就退回原文件
        log.warning("ffmpeg 转码失败（%s），改用原始音频", exc)
        return audio_path


async def _transcribe_openai(audio_path: Path, settings: Settings) -> str:
    base_url = (settings.asr_base_url or settings.llm_base_url or "").strip().rstrip("/")
    api_key = settings.asr_api_key or settings.llm_api_key
    if not base_url:
        raise ASRError("未配置语音转写的接口地址（asr_base_url）")
    if not api_key and "127.0.0.1" not in base_url and "localhost" not in base_url:
        raise ASRError("未配置语音转写的 API Key（asr_api_key）")

    # 规范化：允许用户直接粘贴完整端点，或只填到 /v1
    for suffix in ("/audio/transcriptions", "/chat/completions", "/completions"):
        if base_url.endswith(suffix):
            base_url = base_url[: -len(suffix)].rstrip("/")
            break
    if not re.search(r"/v\d+$", base_url) and "/compatible-mode" not in base_url:
        base_url = f"{base_url}/v1"

    url = f"{base_url}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    size_mb = audio_path.stat().st_size / 1024 / 1024
    log.info("调用 %s 转写 %s（%.1f MB），可能较慢…", url, audio_path.name, size_mb)

    # 分片上传由服务端处理；这里直接整文件流式发送
    with audio_path.open("rb") as handle:
        files = {"file": (audio_path.name, handle, "application/octet-stream")}
        data = {"model": settings.asr_model or "whisper-1", "response_format": "text"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(3600.0, connect=30.0)) as client:
            response = await client.post(url, headers=headers, files=files, data=data)
    if response.status_code >= 400:
        raise ASRError(
            f"转写接口返回 {response.status_code}：{response.text[:300]}"
        )
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = response.json()
        return str(payload.get("text") or "")
    return response.text


def _transcribe_local(audio_path: Path, settings: Settings) -> str:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as exc:
        raise ASRError(
            "未安装 faster-whisper。请执行：pip install faster-whisper；"
            "或把语音转写改成 openai。"
        ) from exc

    model_size = settings.asr_local_model or "small"
    device = (settings.asr_local_device or "auto").strip()
    if device == "auto":
        device = "cuda"
        try:  # 没 GPU 就退 CPU
            import ctranslate2  # type: ignore

            if ctranslate2.get_cuda_device_count() <= 0:
                device = "cpu"
        except Exception:  # noqa: BLE001
            device = "cpu"
    compute_type = "float16" if device == "cuda" else "int8"

    log.info("本地加载 faster-whisper 模型 %s（device=%s）…", model_size, device)
    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    segments, info = model.transcribe(str(audio_path), vad_filter=True, beam_size=5)
    lines = []
    for segment in segments:
        lines.append(f"[{format_duration(int(segment.start))}] {segment.text.strip()}")
    log.info("本地转写完成，语言=%s，片段=%d", getattr(info, "language", "?"), len(lines))
    return "\n".join(lines)
