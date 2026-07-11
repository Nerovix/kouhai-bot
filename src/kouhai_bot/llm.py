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
    provider_name: str = ""
    model: str = ""


@dataclass(frozen=True)
class _ChatCompletionAttempt:
    text: str | None
    retryable: bool
    retry_after_sec: float | None
    failure_kind: str | None = None
    finish_reason: str | None = None
    reasoning_chars: int = 0
    usage: dict | None = None


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


def _provider_uses_stream(
    provider_name: str,
    base_url: str,
    *,
    explicit_stream: bool = False,
) -> bool:
    if explicit_stream:
        return True

    return _provider_is_dashscope(provider_name, base_url)


def _provider_is_dashscope(provider_name: str, base_url: str) -> bool:
    name = (provider_name or "").strip().lower()
    if name in {"aliyun", "bailian", "dashscope"} or "dashscope" in name:
        return True

    parsed = urlparse((base_url or "").strip())
    host = (parsed.hostname or "").lower()
    if not host and "://" not in (base_url or ""):
        host = (base_url or "").split("/", 1)[0].lower()
    return (
        host in {
            "dashscope.aliyuncs.com",
            "dashscope-intl.aliyuncs.com",
            "dashscope-us.aliyuncs.com",
        }
        or host.endswith(".dashscope.aliyuncs.com")
        or host.endswith(".maas.aliyuncs.com")
    )



def _chat_completion_timeout(
    *,
    payload: dict,
    total_timeout_sec: int,
    stream_idle_timeout_sec: int | float | None,
) -> aiohttp.ClientTimeout:
    if payload.get("stream") is True:
        idle_timeout = (
            float(stream_idle_timeout_sec)
            if stream_idle_timeout_sec and stream_idle_timeout_sec > 0
            else None
        )
        return aiohttp.ClientTimeout(total=total_timeout_sec, sock_read=idle_timeout)
    return aiohttp.ClientTimeout(total=total_timeout_sec)


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


def _choice_reasoning_text(choice: dict) -> str:
    for key in ("delta", "message"):
        value = choice.get(key, {})
        if not isinstance(value, dict):
            continue
        reasoning_content = value.get("reasoning_content", "")
        if isinstance(reasoning_content, str):
            return reasoning_content
    return ""


def _stream_event_reasoning_text(data: dict) -> str:
    choices = data.get("choices", [])
    if choices:
        choice = choices[0]
        if isinstance(choice, dict):
            return _choice_reasoning_text(choice)

    output = data.get("output")
    if isinstance(output, dict):
        choices = output.get("choices", [])
        if choices:
            choice = choices[0]
            if isinstance(choice, dict):
                message = choice.get("message", {})
                if isinstance(message, dict):
                    reasoning_content = message.get("reasoning_content", "")
                    if isinstance(reasoning_content, str):
                        return reasoning_content
    return ""


def _stream_event_text(data: dict) -> tuple[str, bool, str | None]:
    error = data.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or error.get("type") or "error")
        return "", True, message

    event_type = str(data.get("type") or "").strip()
    if event_type in {"response.failed", "response.incomplete"}:
        error_obj = data.get("error") or data.get("response", {}).get("error")
        if isinstance(error_obj, dict):
            message = str(error_obj.get("message") or error_obj.get("type") or event_type)
        else:
            message = event_type
        return "", True, message

    choices = data.get("choices", [])
    if choices:
        choice = choices[0]
        if isinstance(choice, dict):
            return _choice_delta_text(choice), True, None
        return "", True, None

    if event_type == "response.output_text.delta":
        delta = data.get("delta", "")
        return delta if isinstance(delta, str) else "", True, None

    if event_type in {"response.output_text.done", "response.completed"}:
        text = _responses_event_text(data)
        return text, True, None

    # Streaming APIs often emit lifecycle, usage, or reasoning events with no
    # chat-completions choice payload. They are valid SSE frames, just not answer text.
    return "", True, None


def _responses_event_text(data: dict) -> str:
    text = data.get("text", "")
    if isinstance(text, str):
        return text

    response = data.get("response")
    if not isinstance(response, dict):
        return ""

    parts: list[str] = []
    for item in response.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") in {"output_text", "text"}:
                value = content.get("text", "")
                if isinstance(value, str):
                    parts.append(value)
    return "".join(parts)


async def _read_streaming_chat_completion(
    resp: aiohttp.ClientResponse,
    *,
    provider_name: str,
) -> _ChatCompletionAttempt:
    parts: list[str] = []
    event_lines: list[str] = []
    reasoning_chars = 0
    finish_reason: str | None = None
    usage: dict | None = None

    def finish(text: str) -> _ChatCompletionAttempt:
        if not text:
            logger.warning(
                "%s API stream returned no text finish_reason=%s usage=%s",
                provider_name,
                finish_reason,
                usage,
            )
        if reasoning_chars:
            logger.info(
                "%s API stream returned reasoning_content chars=%s",
                provider_name,
                reasoning_chars,
            )
        if finish_reason or usage:
            logger.info(
                "%s API stream finished finish_reason=%s usage=%s",
                provider_name,
                finish_reason,
                usage,
            )
        empty_after_reasoning = not text and reasoning_chars > 0
        length_stopped = not text and finish_reason == "length"
        return _ChatCompletionAttempt(
            text=text or None,
            retryable=(
                not bool(text)
                and not empty_after_reasoning
                and not length_stopped
            ),
            retry_after_sec=None,
            failure_kind=(
                None
                if text
                else "length"
                if length_stopped
                else "empty_content_after_reasoning"
                if empty_after_reasoning
                else "service_unavailable"
            ),
            finish_reason=finish_reason,
            reasoning_chars=reasoning_chars,
            usage=usage,
        )

    def stream_failure(reason: str) -> _ChatCompletionAttempt:
        logger.warning("%s API stream failed: %s", provider_name, reason)
        return _ChatCompletionAttempt(
            text=None,
            retryable=True,
            retry_after_sec=None,
            failure_kind="service_unavailable",
            finish_reason=finish_reason,
            reasoning_chars=reasoning_chars,
            usage=usage,
        )

    def handle_event(raw_data: str) -> tuple[bool, _ChatCompletionAttempt | None]:
        nonlocal finish_reason, reasoning_chars, usage
        data_text = raw_data.strip()
        if not data_text:
            return False, None
        if data_text == "[DONE]":
            return True, None
        try:
            data = json.loads(data_text)
        except json.JSONDecodeError:
            return False, stream_failure("invalid json")

        reasoning_chars += len(_stream_event_reasoning_text(data))
        event_usage = data.get("usage")
        if isinstance(event_usage, dict):
            usage = event_usage
        for choice in data.get("choices", []) or []:
            if isinstance(choice, dict) and choice.get("finish_reason") is not None:
                finish_reason = str(choice.get("finish_reason"))
        text, valid_event, error_message = _stream_event_text(data)
        if error_message:
            return False, stream_failure(error_message)
        event_type = str(data.get("type") or "").strip()
        if text and not (parts and event_type in {"response.output_text.done", "response.completed"}):
            parts.append(text)
        if not valid_event:
            return False, stream_failure("invalid event")
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
    stream_idle_timeout_sec: int | float | None = None,
) -> _ChatCompletionAttempt:
    try:
        async with session.post(
            _chat_completions_url(base_url),
            json=payload,
            headers=headers,
            timeout=_chat_completion_timeout(
                payload=payload,
                total_timeout_sec=timeout,
                stream_idle_timeout_sec=stream_idle_timeout_sec,
            ),
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
    provider_name: str = "",
    send_reasoning_effort: bool = True,
) -> ChatCompletionResult:
    """Call providers in fallback order; first success wins.

    Each provider is retried up to ``llm_max_retries`` times internally.
    On exhaustion, the next provider in the fallback list is tried.
    """
    cfg = get_config()
    task_name = (task or "").strip().lower()
    if task_name in {"judge", "review"}:
        providers = cfg.llm_smart_providers
    else:
        providers = cfg.llm_general_providers
    if provider_name:
        providers = [p for p in providers if p.name == provider_name]
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
    stream_idle_timeout_sec = int(
        getattr(cfg, "llm_stream_idle_timeout_sec", 120) or 0
    )

    last_failure_kind: str | None = None
    last_failed_provider: str | None = None

    async with aiohttp.ClientSession() as session:
        for provider in providers:
            model_name = provider.model_for(explicit_model=model)
            headers = {
                "Authorization": f"Bearer {provider.api_key}",
                "Content-Type": "application/json",
            }
            payload: dict = {
                "model": model_name,
                "messages": messages,
                "temperature": (
                    provider.temperature
                    if provider.temperature is not None
                    else temperature
                ),
            }
            if response_format:
                payload["response_format"] = response_format
            if thinking and provider.send_thinking:
                payload["thinking"] = thinking
                if _provider_is_dashscope(provider.name, provider.base_url):
                    payload["enable_thinking"] = True
                    payload.setdefault("thinking_budget", 100000)
            reasoning_effort = provider.reasoning_effort.strip().lower()
            if reasoning_effort and send_reasoning_effort:
                payload["reasoning_effort"] = reasoning_effort
            if provider.extra_body:
                payload.update(provider.extra_body)
            uses_stream = _provider_uses_stream(
                provider.name,
                provider.base_url,
                explicit_stream=provider.stream,
            )
            if uses_stream:
                payload["stream"] = True
                if _provider_is_dashscope(provider.name, provider.base_url):
                    payload.setdefault("stream_options", {"include_usage": True})

            for attempt in range(max_retries + 1):
                result = await _post_chat_completion_once(
                    session,
                    provider_name=provider.name,
                    base_url=provider.base_url,
                    headers=headers,
                    payload=payload,
                    timeout=timeout,
                    stream_idle_timeout_sec=stream_idle_timeout_sec,
                )
                if result.text is not None:
                    if last_failed_provider:
                        logger.info(
                            "Fallback LLM provider '%s' succeeded after '%s' failed",
                            provider.name,
                            last_failed_provider,
                        )
                    return ChatCompletionResult(
                        text=result.text,
                        failure_kind=None,
                        model_tag=provider.model_tag,
                        provider_name=provider.name,
                        model=model_name,
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
            if provider_name:
                logger.warning(
                    "Pinned LLM provider '%s' exhausted (max_retries=%s)",
                    provider.name,
                    max_retries,
                )
            else:
                logger.warning(
                    "LLM provider '%s' exhausted (max_retries=%s), "
                    "moving to next fallback",
                    provider.name,
                    max_retries,
                )

    return ChatCompletionResult(text=None, failure_kind=last_failure_kind)
