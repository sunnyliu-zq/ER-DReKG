"""DeepSeek Flash client (OpenAI-compatible Chat Completions).

The API key is read only from backend settings / environment. It is never
logged, returned to the frontend, or written into cache files.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from app.core.config import settings


class ProviderConfigError(RuntimeError):
    """The provider is not configured or the key is missing."""


class ProviderRequestError(RuntimeError):
    """The provider could not be reached or returned a non-usable response."""


class ProviderAuthError(ProviderRequestError):
    """The provider rejected the configured credentials or model permission."""


class ProviderResponseError(ProviderRequestError):
    """The provider responded, but the explanation payload is unusable."""


def _api_key() -> str:
    return settings.deepseek_api_key or os.getenv("DEEPSEEK_API_KEY", "")


def _endpoint() -> str:
    base = (settings.deepseek_base_url or "https://api.deepseek.com").rstrip("/")
    return f"{base}/chat/completions"


def _post_once(payload: dict[str, Any], key: str) -> tuple[dict[str, Any], float]:
    request = urllib.request.Request(
        _endpoint(),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=settings.deepseek_timeout_seconds) as response:
            raw = response.read()
            if not raw.strip():
                raise ProviderRequestError(
                    f"DeepSeek 返回空响应（HTTP {response.status}，content-type={response.headers.get('Content-Type')}）"
                )
            try:
                result = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                preview = raw[:160].decode("utf-8", errors="replace").replace("\r", " ").replace("\n", " ")
                raise ProviderRequestError(
                    f"DeepSeek 返回非 JSON 响应（HTTP {response.status}，bytes={len(raw)}，preview={preview!r}）"
                ) from None
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise ProviderAuthError(f"DeepSeek 拒绝认证（HTTP {error.code}）") from None
        detail = error.read().decode("utf-8", errors="replace")[:600]
        raise ProviderRequestError(f"DeepSeek HTTP {error.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise ProviderRequestError(f"DeepSeek 连接失败：{type(error).__name__}") from None
    return result, time.perf_counter() - start


def parse_content(content: Any) -> dict[str, Any]:
    """Parse model content into a JSON object; tolerate a fenced code block."""
    if isinstance(content, list):
        content = "".join(block.get("text", "") for block in content if isinstance(block, dict))
    if not isinstance(content, str) or not content.strip():
        raise ProviderResponseError("DeepSeek 返回空解释内容")
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ProviderResponseError(f"DeepSeek 解释内容不是合法 JSON：{error.msg}") from None
    if not isinstance(parsed, dict):
        raise ProviderResponseError("DeepSeek 解释内容不是 JSON 对象")
    return parsed


def chat_json(messages: list[dict[str, str]], *, max_tokens: int | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call DeepSeek Flash and return (parsed_json, metadata)."""
    key = _api_key()
    if not key:
        raise ProviderConfigError("DeepSeek API key 未配置（DEEPSEEK_API_KEY）")
    payload = {
        "model": settings.deepseek_model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens or settings.deepseek_max_tokens,
        "response_format": {"type": "json_object"},
    }
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            result, latency = _post_once(payload, key)
            break
        except ProviderRequestError as error:
            last_error = error
            # Retry only transient server/rate-limit failures.
            if attempt == 1 or not any(code in str(error) for code in ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504")):
                raise
            time.sleep(2)
    else:  # pragma: no cover - defensive
        raise last_error or ProviderRequestError("DeepSeek 请求失败")

    choice = (result.get("choices") or [{}])[0] or {}
    message = choice.get("message") or {}
    parsed = parse_content(message.get("content"))
    if not parsed:
        raise ProviderResponseError(
            f"DeepSeek 返回空解释内容（finish_reason={choice.get('finish_reason')}，usage={result.get('usage')}）"
        )
    metadata = {
        "model": result.get("model") or settings.deepseek_model,
        "latency_seconds": round(latency, 3),
        "usage": result.get("usage") or {},
        "finish_reason": choice.get("finish_reason"),
    }
    return parsed, metadata
