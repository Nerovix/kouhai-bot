"""Provider-aware LLM transport — iterates fallback list with per-provider retry."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import aiohttp

from .config import get_config

logger = logging.getLogger("kouhai-bot.llm")


@dataclass(frozen=True)
class ChatCompletionResult:
    text: str | None
    failure_kind: str | None = None
    model_tag: str = ""


@dataclass(frozen=True)
class _ChatCompletionAttempt:
    text: str | None
    retryable: bool
    retry_after_sec: float | None
    failure_kind: str | None = None


def _chat_completions_url(base_url: str) -> str:
    """Build the /chat/completions URL from a provider base URL.

    IMPORTANT: The base_url must already include any version prefix
    (e.g. ``https://api.openai.com/v1``, not ``https://api.openai.com``).
    This function only appends ``/chat/completions`` — it does NOT add /v1.
    """
    base = (base_url or "").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _message_content_text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text", "")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts).strip()
    if content is None:
        return ""
    return str(content).strip()


def _provider_uses_stream(provider_name: str, base_url: str) -> bool:
    name = (provider_name or "").strip().lower()
    if name in {"aliyun", "bailian", "dashscope"}:
        return True

    parsed = urlparse((base_url or "").strip())
    host = (parsed.hostname or "").lower()
    if not host and "://" not in (base_url or ""):
        host = (base_url or "").split("/", 1)[0].lower()
    return host == "dashscope.aliyuncs.com" or host.endswith(".dashscope.aliyuncs.com")


def _should_retry_status(status: int) -> bool:
    return status in {408, 409, 429} or status >= 500


def _parse_retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _retry_delay_seconds(
    retry_number: int,
    base_delay_sec: float,
    max_delay_sec: float,
    retry_after_sec: float | None = None,
) -> float:
    if retry_after_sec is not None:
        return max(0.0, min(retry_after_sec, max_delay_sec))
    delay = base_delay_sec * (2 ** max(retry_number - 1, 0))
    return max(0.0, min(delay, max_delay_sec))


def _stream_content_text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text", "")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def _choice_delta_text(choice: dict) -> str:
    delta = choice.get("delta", {})
    if isinstance(delta, dict):
        text = _stream_content_text(delta)
        if text:
            return text

    message = choice.get("message", {})
    if isinstance(message, dict):
        return _stream_content_text(message)
    return ""


def _stream_event_text(data: dict) -> tuple[str, bool]:
    choices = data.get("choices", [])
    if not choices:
        return "", False
    choice = choices[0]
    if not isinstance(choice, dict):
        return "", False
    return _choice_delta_text(choice), True


async def _read_streaming_chat_completion(
    resp: aiohttp.ClientResponse,
    *,
    provider_name: str,
) -> _ChatCompletionAttempt:
    parts: list[str] = []
    event_lines: list[str] = []

    def finish(text: str) -> _ChatCompletionAttempt:
        if not text:
            logger.warning("%s API stream returned no text", provider_name)
        return _ChatCompletionAttempt(
            text=text or None,
            retryable=not bool(text),
            retry_after_sec=None,
            failure_kind=None if text else "service_unavailable",
        )

    def stream_failure(reason: str) -> _ChatCompletionAttempt:
        logger.warning("%s API stream failed: %s", provider_name, reason)
        return _ChatCompletionAttempt(
            text=None,
            retryable=True,
            retry_after_sec=None,
            failure_kind="service_unavailable",
        )

    def handle_event(raw_data: str) -> tuple[bool, _ChatCompletionAttempt | None]:
        data_text = raw_data.strip()
        if not data_text:
            return False, None
        if data_text == "[DONE]":
            return True, None
        try:
            data = json.loads(data_text)
        except json.JSONDecodeError:
            return False, stream_failure("invalid json")

        text, had_choice = _stream_event_text(data)
        if text:
            parts.append(text)
        if not had_choice:
            return False, stream_failure("missing choices")
        return False, None

    try:
        async for raw_line in resp.content:
            line = raw_line.decode("utf-8").strip()
            if not line:
                if event_lines:
                    done, failure = handle_event("\n".join(event_lines))
                    event_lines.clear()
                    if failure is not None:
                        return failure
                    if done:
                        return finish("".join(parts).strip())
                continue
            if line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            event_lines.append(line[5:].lstrip())

        if event_lines:
            done, failure = handle_event("\n".join(event_lines))
            if failure is not None:
                return failure
            if done:
                return finish("".join(parts).strip())

        return stream_failure("missing done sentinel")
    except UnicodeDecodeError:
        return stream_failure("invalid utf-8")


async def _post_chat_completion_once(
    session: aiohttp.ClientSession,
    *,
    provider_name: str,
    base_url: str,
    headers: dict[str, str],
    payload: dict,
    timeout: int,
) -> _ChatCompletionAttempt:
    try:
        async with session.post(
            _chat_completions_url(base_url),
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                retryable = _should_retry_status(resp.status)
                log_fn = logger.warning if retryable else logger.error
                log_fn(
                    "%s API error %s: %s", provider_name, resp.status, text[:300]
                )
                return _ChatCompletionAttempt(
                    text=None,
                    retryable=retryable,
                    retry_after_sec=_parse_retry_after_seconds(
                        resp.headers.get("Retry-After")
                    ),
                    failure_kind="service_unavailable" if retryable else "error",
                )

            if payload.get("stream") is True:
                return await _read_streaming_chat_completion(
                    resp,
                    provider_name=provider_name,
                )

            data = await resp.json()
            choices = data.get("choices", [])
            if not choices:
                logger.warning("%s API returned no choices", provider_name)
                return _ChatCompletionAttempt(
                    text=None,
                    retryable=True,
                    retry_after_sec=None,
                    failure_kind="service_unavailable",
                )
            message = choices[0].get("message", {})
            return _ChatCompletionAttempt(
                text=_message_content_text(message),
                retryable=False,
                retry_after_sec=None,
                failure_kind=None,
            )
    except asyncio.TimeoutError:
        logger.warning("%s API timeout", provider_name)
        return _ChatCompletionAttempt(
            text=None,
            retryable=True,
            retry_after_sec=None,
            failure_kind="timeout",
        )
    except aiohttp.ClientError as e:
        logger.warning("%s API client exception: %s", provider_name, e)
        return _ChatCompletionAttempt(
            text=None,
            retryable=True,
            retry_after_sec=None,
            failure_kind="service_unavailable",
        )
    except Exception as e:
        logger.error("%s API exception: %s", provider_name, e)
        return _ChatCompletionAttempt(
            text=None,
            retryable=False,
            retry_after_sec=None,
            failure_kind="error",
        )


async def chat_completion(
    messages: list[dict],
    model: str = "",
    task: str = "",
    temperature: float = 0.7,
    timeout: int = 120,
    response_format: dict | None = None,
    thinking: dict | None = None,
) -> ChatCompletionResult:
    """Call providers in fallback order; first success wins.

    Each provider is retried up to ``llm_max_retries`` times internally.
    On exhaustion, the next provider in the fallback list is tried.
    """
    cfg = get_config()
    providers = cfg.llm_providers
    if not providers:
        return ChatCompletionResult(text=None, failure_kind="error")

    max_retries = max(0, int(getattr(cfg, "llm_max_retries", 2) or 0))
    retry_base = max(
        0.0, float(getattr(cfg, "llm_retry_base_delay_sec", 1.0) or 0.0)
    )
    retry_max = max(
        retry_base,
        float(getattr(cfg, "llm_retry_max_delay_sec", 8.0) or 0.0),
    )

    last_failure_kind: str | None = None
    last_failed_provider: str | None = None

    async with aiohttp.ClientSession() as session:
        for provider in providers:
            model_name = provider.model_for(task=task, explicit_model=model)
            headers = {
                "Authorization": f"Bearer {provider.api_key}",
                "Content-Type": "application/json",
            }
            payload: dict = {
                "model": model_name,
                "messages": messages,
                "temperature": temperature,
            }
            if response_format:
                payload["response_format"] = response_format
            if thinking:
                payload["thinking"] = thinking
            reasoning_effort = provider.reasoning_effort.strip().lower()
            if reasoning_effort:
                payload["reasoning_effort"] = reasoning_effort
            if _provider_uses_stream(provider.name, provider.base_url):
                payload["stream"] = True

            for attempt in range(max_retries + 1):
                result = await _post_chat_completion_once(
                    session,
                    provider_name=provider.name,
                    base_url=provider.base_url,
                    headers=headers,
                    payload=payload,
                    timeout=timeout,
                )
                if result.text is not None:
                    if last_failed_provider:
                        logger.info(
                            "Fallback LLM provider '%s' succeeded after '%s' failed",
                            provider.name,
                            last_failed_provider,
                        )
                    return ChatCompletionResult(
                        text=result.text, failure_kind=None,
                        model_tag=provider.model_tag,
                    )

                last_failure_kind = result.failure_kind
                if not result.retryable or attempt >= max_retries:
                    break

                retry_number = attempt + 1
                delay = _retry_delay_seconds(
                    retry_number, retry_base, retry_max, result.retry_after_sec
                )
                logger.warning(
                    "%s API transient failure; retrying %s/%s in %.1fs",
                    provider.name,
                    retry_number,
                    max_retries,
                    delay,
                )
                await asyncio.sleep(delay)

            last_failed_provider = provider.name
            logger.warning(
                "LLM provider '%s' exhausted (max_retries=%s), "
                "moving to next fallback",
                provider.name,
                max_retries,
            )

    return ChatCompletionResult(text=None, failure_kind=last_failure_kind)
