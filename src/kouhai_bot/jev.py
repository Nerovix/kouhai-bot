"""Jev (TypeSafe System One) decision-model client.

Used by the entertainment command ``/sbmjev``. Jev does not generate text: it
answers typed questions (choice / noul / score) over a shared ``state``. The
bot only needs a binary reviewer verdict, so the request uses one ``choice``
question and reads back ``answers.verdict.choice``.

Docs: https://docs.rightapi.ai/docs/rc_jev/
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

import aiohttp

from .config import get_config

logger = logging.getLogger("kouhai-bot.jev")

# A custom User-Agent is REQUIRED: rightapi.ai sits behind Cloudflare and
# rejects the default Python/urllib signature with "error code: 1010" (403).
_USER_AGENT = "kouhai-bot/1.0"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504, 529}


@dataclass
class JevResult:
    """Outcome of one Jev decision request."""

    choice: str | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None
    model: str = ""
    input_tokens: int = 0
    failure_kind: str = ""  # "" on success; timeout / service_unavailable / error / malformed / not_configured


async def _post_once(url: str, api_key: str, payload: dict, timeout_sec: float) -> tuple[int, str]:
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
        async with session.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": _USER_AGENT,
            },
        ) as resp:
            return resp.status, await resp.text()


def _parse_response(body: str) -> JevResult:
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return JevResult(failure_kind="malformed")
    if not isinstance(data, dict):
        return JevResult(failure_kind="malformed")

    answer = (data.get("answers") or {}).get("verdict")
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        return JevResult(failure_kind="malformed")
    choice = answer.get("choice")
    if choice not in {"correct", "incorrect"}:
        return JevResult(failure_kind="malformed")

    probabilities: dict[str, float] = {}
    raw_probabilities = answer.get("probabilities")
    if isinstance(raw_probabilities, dict):
        for key, value in raw_probabilities.items():
            try:
                probabilities[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
    confidence = answer.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    try:
        input_tokens = int(usage.get("input_tokens") or 0)
    except (TypeError, ValueError):
        input_tokens = 0
    return JevResult(
        choice=choice,
        probabilities=probabilities,
        confidence=confidence,
        model=str(data.get("model") or ""),
        input_tokens=input_tokens,
    )


async def run_jev(
    state: dict,
    questions: dict,
    *,
    timeout_sec: float | None = None,
) -> JevResult:
    """Answer ``questions`` over ``state`` with one Jev request."""
    cfg = get_config()
    if not cfg.jev_api_key:
        logger.warning("jev judge requested but jev.api_key is not configured")
        return JevResult(failure_kind="not_configured")

    url = f"{cfg.jev_base_url.rstrip('/')}/v1/systemone"
    payload = {
        "model": cfg.jev_model,
        "state": state,
        "questions": questions,
    }
    attempts = 1 + max(0, int(cfg.jev_max_retries))
    timeout = float(timeout_sec if timeout_sec is not None else cfg.jev_timeout_sec)
    failure_kind = "service_unavailable"

    for attempt in range(attempts):
        try:
            status, body = await _post_once(url, cfg.jev_api_key, payload, timeout)
        except asyncio.TimeoutError:
            failure_kind = "timeout"
        except aiohttp.ClientError as exc:
            failure_kind = "service_unavailable"
            logger.warning("jev request failed: %s", exc)
        else:
            if status == 200:
                return _parse_response(body)
            failure_kind = "service_unavailable" if status in _RETRYABLE_STATUS else "error"
            logger.warning(
                "jev API returned HTTP %s: %s", status, body[:300]
            )
            if status not in _RETRYABLE_STATUS:
                break

        if attempt + 1 < attempts:
            delay = min(4.0, 0.5 * (2**attempt))
            logger.info(
                "jev retrying in %.1fs (attempt %s/%s, failure=%s)",
                delay,
                attempt + 1,
                attempts,
                failure_kind,
            )
            await asyncio.sleep(delay)

    return JevResult(failure_kind=failure_kind)
