"""LLM provider fallback configuration — dataclass + YAML loader."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LlmProviderConfig:
    """A single LLM provider with its own credentials and per-task model mapping.

    Fallback order is defined by list position in the YAML config.
    """

    name: str
    api_key: str
    base_url: str
    model: str
    judge_model: str = ""
    clarify_model: str = ""
    review_model: str = ""
    summary_model: str = ""
    reasoning_effort: str = ""
    model_tag: str = ""

    def model_for(self, task: str = "", explicit_model: str = "") -> str:
        """Resolve the model name for a given task.

        Priority: explicit_model > per-task override > default model.
        """
        if explicit_model:
            return explicit_model
        task_name = (task or "").strip().lower()
        if task_name == "judge":
            return self.judge_model or self.model
        if task_name == "clarify":
            return self.clarify_model or self.model
        if task_name == "review":
            return self.review_model or self.model
        if task_name == "summary":
            return self.summary_model or self.model
        return self.model


def build_providers_from_yaml(llm_section: dict[str, Any]) -> list[LlmProviderConfig]:
    """Build ordered provider list from the YAML ``llm.providers`` list.

    Raises RuntimeError if the list is empty or required fields are missing.
    """
    providers_raw = llm_section.get("providers", [])
    if not isinstance(providers_raw, list):
        raise RuntimeError(
            "config.yaml: llm.providers must be a list of provider mappings"
        )
    if not providers_raw:
        raise RuntimeError(
            "No LLM providers configured in config.yaml: llm.providers is empty"
        )

    providers: list[LlmProviderConfig] = []
    seen: set[str] = set()
    for p in providers_raw:
        if not isinstance(p, dict):
            raise RuntimeError(
                f"config.yaml: each entry in llm.providers must be a mapping, "
                f"got {type(p).__name__}: {p!r}"
            )
        name = str(p.get("name", "")).strip()
        if not name:
            raise RuntimeError("Each LLM provider must have a 'name' field")
        if name in seen:
            raise RuntimeError(f"Duplicate LLM provider name: {name}")
        seen.add(name)

        api_key = str(p.get("api_key", "")).strip()
        if not api_key:
            raise RuntimeError(f"LLM provider '{name}' requires 'api_key'")

        model = str(p.get("model", "")).strip()
        if not model:
            raise RuntimeError(f"LLM provider '{name}' requires 'model'")

        base_url = str(p.get("base_url", "https://api.openai.com/v1")).strip()
        if not base_url:
            raise RuntimeError(
                f"LLM provider '{name}' has empty base_url; "
                f"set base_url (e.g. https://api.deepseek.com) or omit for OpenAI default"
            )

        providers.append(
            LlmProviderConfig(
                name=name,
                api_key=api_key,
                base_url=base_url,
                model=model,
                judge_model=str(p.get("judge_model", "")).strip(),
                clarify_model=str(p.get("clarify_model", "")).strip(),
                review_model=str(p.get("review_model", "")).strip(),
                summary_model=str(p.get("summary_model", "")).strip(),
                reasoning_effort=str(p.get("reasoning_effort", "")).strip(),
                model_tag=str(p.get("model_tag", "")).strip(),
            )
        )
    return providers
