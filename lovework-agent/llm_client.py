"""Provider-aware OpenAI-compatible LLM client with reasoning support."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, TypeVar
from urllib.parse import urlparse

from openai import OpenAI
from pydantic import BaseModel

import config

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class RequestProfile:
    """Resolved transport capabilities for one provider/model pair."""

    provider: str
    api_style: str
    api_path: str
    uses_responses_api: bool
    supports_reasoning_effort: bool


def _is_openai_reasoning_model(base_url: str, model: str) -> bool:
    host = urlparse(base_url).netloc.lower()
    return host == "api.openai.com" and model.startswith(("gpt-5", "o1", "o3", "o4"))


def _is_deepseek_reasoning_model(base_url: str, model: str) -> bool:
    host = urlparse(base_url).netloc.lower()
    return (
        ("opencode.ai" in host or "api.deepseek.com" in host)
        and (model.startswith("deepseek-v4") or model == "deepseek-reasoner")
    )


def resolve_request_profile(base_url: str, model: str) -> RequestProfile:
    """Resolve API style and reasoning capabilities without provider guessing."""
    if _is_openai_reasoning_model(base_url, model):
        return RequestProfile(
            provider="openai",
            api_style="OpenAI Responses API",
            api_path="/responses",
            uses_responses_api=True,
            supports_reasoning_effort=True,
        )

    host = urlparse(base_url).netloc.lower()
    provider = (
        "opencode-go" if "opencode.ai" in host
        else "deepseek" if "api.deepseek.com" in host
        else "openai-compatible"
    )
    return RequestProfile(
        provider=provider,
        api_style="OpenAI-compatible Chat Completions",
        api_path="/chat/completions",
        uses_responses_api=False,
        supports_reasoning_effort=_is_deepseek_reasoning_model(base_url, model),
    )




def _map_reasoning_effort(profile: RequestProfile, effort: str) -> str:
    """Map generic effort names to provider-specific values."""
    if profile.provider == "opencode-go" and effort == "xhigh":
        return "max"
    return effort


class LLMClient:
    """Provider-aware LLM client with Chat and Responses API support."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.api_key = api_key or config.LLM_API_KEY
        self.base_url = base_url or config.LLM_BASE_URL
        self.model = model or config.LLM_MODEL
        self.temperature = temperature if temperature is not None else config.LLM_TEMPERATURE
        self.max_tokens = max_tokens if max_tokens is not None else config.LLM_MAX_TOKENS
        self.request_profile = resolve_request_profile(self.base_url, self.model)
        # Callers such as the rehearsal agents opt into reasoning explicitly;
        # ordinary crawl/matcher clients retain the provider default.
        self.reasoning_effort = (
            reasoning_effort or getattr(config, "LLM_REASONING_EFFORT", None)
        )

        if not self.api_key:
            raise ValueError("No API key found. Set LLM_API_KEY or OPENAI_API_KEY.")

        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    @property
    def provider(self) -> str:
        return self.request_profile.provider

    @property
    def api_style(self) -> str:
        return self.request_profile.api_style

    @property
    def api_path(self) -> str:
        return self.request_profile.api_path

    @property
    def max_output_parameter(self) -> str:
        return "max_output_tokens" if self.request_profile.uses_responses_api else "max_tokens"

    def _chat_kwargs(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        response_format: Any | None,
        reasoning_effort: str | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            kwargs["response_format"] = {"type": "json_object"}
        if self.request_profile.supports_reasoning_effort and reasoning_effort:
            kwargs["reasoning_effort"] = _map_reasoning_effort(
                self.request_profile, reasoning_effort
            )
        return kwargs

    def _responses_kwargs(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        response_format: Any | None,
        reasoning_effort: str | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": messages,
            "max_output_tokens": max_tokens,
        }
        if reasoning_effort:
            kwargs["reasoning"] = {"effort": reasoning_effort}
        if response_format is not None:
            kwargs["text"] = {"format": {"type": "json_object"}}
        return kwargs

    @staticmethod
    def _responses_text(response: Any) -> str:
        output_text = getattr(response, "output_text", None)
        if output_text:
            return output_text
        return ""

    def chat(
        self,
        messages: list[dict[str, str]],
        response_format: Any | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        retries: int = 3,
        context: str = "",
    ) -> str:
        """Send one request and return only visible model content."""
        temp = temperature if temperature is not None else self.temperature
        tok = max_tokens if max_tokens is not None else self.max_tokens
        effort = reasoning_effort if reasoning_effort is not None else self.reasoning_effort

        first_user = next(
            (m["content"][:80] for m in messages if m.get("role") == "user"),
            "",
        )
        label = context or first_user
        logger.info(
            "LLM call: provider=%s model=%s api=%s effort=%s label=%s",
            self.provider,
            self.model,
            self.api_style,
            effort,
            label,
        )

        for attempt in range(retries):
            try:
                if self.request_profile.uses_responses_api:
                    request_kwargs = self._responses_kwargs(
                        messages,
                        max_tokens=tok,
                        response_format=response_format,
                        reasoning_effort=effort,
                    )
                    response = self.client.responses.create(**request_kwargs)
                    content = self._responses_text(response)
                    finish_reason = getattr(response, "status", "unknown")
                    reasoning_content = ""
                else:
                    request_kwargs = self._chat_kwargs(
                        messages,
                        temperature=temp,
                        max_tokens=tok,
                        response_format=response_format,
                        reasoning_effort=effort,
                    )
                    response = self.client.chat.completions.create(**request_kwargs)
                    choice = response.choices[0]
                    message = choice.message
                    content = message.content or ""
                    finish_reason = getattr(choice, "finish_reason", "unknown")
                    reasoning_content = getattr(message, "reasoning_content", "") or ""

                if not content and reasoning_content:
                    logger.warning(
                        "LLM returned reasoning without visible content "
                        "(finish_reason=%s); suppressing reasoning text",
                        finish_reason,
                    )
                logger.info(
                    "LLM ok: %s — %d chars (finish_reason=%s)",
                    label,
                    len(content),
                    finish_reason,
                )
                return content
            except Exception as exc:
                logger.warning(
                    "LLM call failed (attempt %d/%d): %s",
                    attempt + 1,
                    retries,
                    exc,
                )
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise

        return ""

    def structured(
        self,
        messages: list[dict[str, str]],
        schema: type[T],
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        context: str = "",
    ) -> T:
        """Send a JSON-object request and validate it with the Pydantic schema."""
        schema_json = schema.model_json_schema()
        schema_hint = (
            "\n\nRespond with a single JSON object matching this schema:\n"
            f"{json.dumps(schema_json, indent=2)}"
        )
        msgs = [message.copy() for message in messages]
        if msgs and msgs[-1]["role"] == "user":
            msgs[-1]["content"] += schema_hint
        else:
            msgs.append({"role": "user", "content": schema_hint})

        content = self.chat(
            msgs,
            response_format=schema,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            context=context,
        )
        content = _strip_markdown(content)
        try:
            return schema.model_validate_json(content)
        except Exception as exc:
            logger.error("Failed to parse structured response: %s\nContent: %s", exc, content[:500])
            raise


def _strip_markdown(text: str) -> str:
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()
