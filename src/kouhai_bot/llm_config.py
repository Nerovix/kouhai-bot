"""LLM provider fallback configuration — dataclass + YAML loader."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _parse_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


@dataclass(frozen=True)
class LlmProviderConfig:
    """A single LLM provider entry in one fallback queue.

    Fallback order is defined by list position in the YAML config.
    """

    name: str
    api_key: str
    base_url: str
    model: str
    reasoning_effort: str = ""
    model_tag: str = ""
    stream: bool = False

    def model_for(self, explicit_model: str = "") -> str:
        """Resolve the model name for this provider.

        Priority: explicit_model > provider model.
        """
        return explicit_model or self.model


def _build_provider_list(raw: Any, *, section_name: str) -> list[LlmProviderConfig]:
    if not isinstance(raw, list):
        raise RuntimeError(
            f"config.yaml: llm.{section_name} must be a list of provider mappings"
        )
    if not raw:
        raise RuntimeError(
            f"No LLM providers configured in config.yaml: llm.{section_name} is empty"
        )

    providers: list[LlmProviderConfig] = []
    seen: set[str] = set()
    for p in raw:
        if not isinstance(p, dict):
            raise RuntimeError(
                f"config.yaml: each entry in llm.{section_name} must be a mapping, "
                f"got {type(p).__name__}: {p!r}"
            )
        name = str(p.get("name", "")).strip()
        if not name:
            raise RuntimeError(
                f"Each LLM provider in llm.{section_name} must have a 'name' field"
            )
        if name in seen:
            raise RuntimeError(
                f"Duplicate LLM provider name in llm.{section_name}: {name}"
            )
        seen.add(name)

        api_key = str(p.get("api_key", "")).strip()
        if not api_key:
            raise RuntimeError(
                f"LLM provider '{name}' in llm.{section_name} requires 'api_key'"
            )

        model = str(p.get("model", "")).strip()
        if not model:
            raise RuntimeError(
                f"LLM provider '{name}' in llm.{section_name} requires 'model'"
            )

        base_url = str(p.get("base_url", "https://api.openai.com/v1")).strip()
        if not base_url:
            raise RuntimeError(
                f"LLM provider '{name}' in llm.{section_name} has empty base_url; "
                f"set base_url (e.g. https://api.deepseek.com) or omit for OpenAI default"
            )

        providers.append(
            LlmProviderConfig(
                name=name,
                api_key=api_key,
                base_url=base_url,
                model=model,
                reasoning_effort=str(p.get("reasoning_effort", "")).strip(),
                model_tag=str(p.get("model_tag", "")).strip(),
                stream=_parse_bool(p.get("stream", False)),
            )
        )
    return providers


def build_provider_queues_from_yaml(
    llm_section: dict[str, Any],
) -> tuple[list[LlmProviderConfig], list[LlmProviderConfig]]:
    """Build smart/general provider fallback queues from the YAML ``llm`` section.

    Raises RuntimeError if either queue is empty or required fields are missing.
    """
    return (
        _build_provider_list(
            llm_section.get("smart_model", []),
            section_name="smart_model",
        ),
        _build_provider_list(
            llm_section.get("general_model", []),
            section_name="general_model",
        ),
    )
