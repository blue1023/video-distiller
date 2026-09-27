"""大模型适配器：任何 OpenAI 兼容的 /v1/chat/completions 服务都能直接用。

覆盖：DeepSeek / 通义千问(compatible-mode) / Kimi / 硅基流动 / OpenAI / 本地 Ollama…

设计要点：
- 只依赖 httpx，不绑定任何厂商 SDK；
- 长文本走 map-reduce（分块摘要 -> 合并），避免超上下文；
- 结构化输出优先用 response_format=json_object，再靠健壮的 JSON 抽取兜底；
- 每次调用记录耗时/token，便于在网页上看到成本。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from ..core.config import Settings, get_settings
from ..core.utils import get_logger

log = get_logger("llm")


class LLMError(RuntimeError):
    """大模型调用失败（配置错误 / 限流 / 超时 / 返回不合法）。"""


@dataclass
class LLMResult:
    data: Dict[str, Any]
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed: float = 0.0
    calls: int = 1
    extra: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- 底层调用


def _endpoint(base_url: str) -> str:
    """把用户填的 base_url 规范化成 chat/completions 地址。

    兼容这几种写法：
        https://api.deepseek.com/v1
        https://api.deepseek.com/v1/
        https://api.deepseek.com/v1/chat/completions
        https://api.deepseek.com                （自动补 /v1）
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise LLMError("未配置大模型接口地址（llm_base_url）")
    if base.endswith("/chat/completions"):
        return base
    # 用户可能把别的端点也粘进来了，先剥掉再加正确的路径
    for suffix in ("/audio/transcriptions", "/completions", "/embeddings"):
        if base.endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
            break
    # 有些服务（如硅基流动）路径里没有 /v1
    if not re.search(r"/v\d+$", base) and "/compatible-mode" not in base:
        base = f"{base}/v1"
    return f"{base}/chat/completions"


def _extract_json(text: str) -> Any:
    """从模型输出里抠出 JSON：容忍 ```json 围栏、前后解释文字、尾随逗号。"""
    if not text:
        raise LLMError("模型返回内容为空")
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except ValueError:
        pass

    # 括号配对扫描，找出第一个完整的 JSON 对象/数组
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        if start < 0:
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(cleaned)):
            char = cleaned[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start : index + 1]
                    candidate = re.sub(r",(\s*[}\]])", r"\1", candidate)  # 去尾逗号
                    try:
                        return json.loads(candidate)
                    except ValueError:
                        break
    raise LLMError(f"无法从模型输出中解析 JSON，原文前 300 字：{text[:300]}")


async def chat(
    messages: List[Dict[str, str]],
    settings: Optional[Settings] = None,
    *,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    json_mode: bool = True,
    max_tokens: Optional[int] = None,
    retries: int = 2,
) -> Dict[str, Any]:
    """一次 chat 调用，返回 {content, model, usage, elapsed}。"""
    settings = settings or get_settings()
    url = _endpoint(settings.llm_base_url)
    target_model = model or settings.llm_model
    if not target_model:
        raise LLMError("未配置模型名（llm_model）")

    headers = {"Content-Type": "application/json"}
    if settings.llm_api_key.strip():
        headers["Authorization"] = f"Bearer {settings.llm_api_key.strip()}"

    payload: Dict[str, Any] = {
        "model": target_model,
        "messages": messages,
        "temperature": settings.llm_temperature if temperature is None else temperature,
        "stream": False,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        started = time.time()
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(float(settings.llm_timeout or 300), connect=30.0)
            ) as client:
                response = await client.post(url, headers=headers, json=payload)
            elapsed = time.time() - started

            if response.status_code >= 400:
                body = response.text[:500]
                # 有些服务不支持 json_object，去掉后重试一次
                if json_mode and response.status_code in (400, 422) and "response_format" in body:
                    log.warning("服务不支持 response_format=json_object，降级重试")
                    json_mode = False
                    payload.pop("response_format", None)
                    continue
                if response.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                    wait = 2 ** attempt
                    log.warning("LLM 返回 %s，%ss 后重试", response.status_code, wait)
                    await asyncio.sleep(wait)
                    last_error = LLMError(f"HTTP {response.status_code}: {body}")
                    continue
                raise LLMError(f"大模型接口返回 {response.status_code}：{body}")

            data = response.json()
            choices = data.get("choices") or []
            if not choices:
                raise LLMError(f"大模型返回结构异常：{str(data)[:300]}")
            message = choices[0].get("message") or {}
            content = message.get("content") or ""
            if not content and message.get("reasoning_content"):
                content = message["reasoning_content"]
            usage = data.get("usage") or {}
            return {
                "content": content,
                "model": data.get("model") or target_model,
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "elapsed": elapsed,
            }
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                wait = 2 ** attempt
                log.warning("LLM 调用异常（%s），%ss 后重试", exc, wait)
                await asyncio.sleep(wait)
                continue
            raise LLMError(f"大模型调用失败：{exc}") from exc

    raise LLMError(f"大模型调用失败：{last_error}")


async def chat_json(
    messages: List[Dict[str, str]],
    settings: Optional[Settings] = None,
    **kwargs: Any,
) -> LLMResult:
    result = await chat(messages, settings, **kwargs)
    try:
        parsed = _extract_json(result["content"])
    except LLMError:
        # 让模型自我修复一次
        repair = messages + [
            {"role": "assistant", "content": result["content"][:4000]},
            {
                "role": "user",
                "content": "上面的输出不是合法 JSON。请只输出合法 JSON，不要任何解释、不要 markdown 代码块。",
            },
        ]
        fixed = await chat(repair, settings, **kwargs)
        parsed = _extract_json(fixed["content"])
        result = fixed
    if not isinstance(parsed, dict):
        raise LLMError("模型返回的 JSON 顶层不是对象")
    return LLMResult(
        data=parsed,
        model=result["model"],
        prompt_tokens=result["prompt_tokens"],
        completion_tokens=result["completion_tokens"],
        elapsed=result["elapsed"],
    )
