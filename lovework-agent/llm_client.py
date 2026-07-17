"""
LLM client wrapper for DeepSeek (OpenAI-compatible) with structured output support.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional, Type, TypeVar

from openai import OpenAI
from pydantic import BaseModel

import config

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLMClient:
    """OpenAI-compatible LLM client with retry logic and structured output."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ):
        self.api_key = api_key or config.LLM_API_KEY
        self.base_url = base_url or config.LLM_BASE_URL
        self.model = model or config.LLM_MODEL
        self.temperature = temperature if temperature is not None else config.LLM_TEMPERATURE
        self.max_tokens = max_tokens if max_tokens is not None else config.LLM_MAX_TOKENS

        if not self.api_key:
            raise ValueError("No API key found. Set LLM_API_KEY or DEEPSEEK_API_KEY.")

        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def chat(
        self,
        messages: List[Dict[str, str]],
        response_format: Optional[Type[T]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        retries: int = 3,
        context: str = "",
    ) -> str:
        """Send a chat request. Returns raw content string."""
        temp = temperature if temperature is not None else self.temperature
        tok = max_tokens if max_tokens is not None else self.max_tokens

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temp,
            "max_tokens": tok,
        }

        if response_format is not None:
            kwargs["response_format"] = {"type": "json_object"}

        # Snapshot the first user message summary (up to 80 chars) for logging
        first_user = next(
            (m["content"][:80] for m in messages if m.get("role") == "user"),
            "",
        )
        label = context or first_user
        logger.info(f"LLM call: {label}")

        for attempt in range(retries):
            try:
                resp = self.client.chat.completions.create(**kwargs)
                content = resp.choices[0].message.content or ""
                logger.info(f"LLM ok: {label} — {len(content)} chars")
                return content
            except Exception as e:
                logger.warning(f"LLM call failed (attempt {attempt + 1}/{retries}): {e}")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise

        return ""

    def structured(
        self,
        messages: List[Dict[str, str]],
        schema: Type[T],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        context: str = "",
    ) -> T:
        """Send a chat request and parse response into a Pydantic model."""
        # Inject schema hint into system or user message
        schema_json = schema.model_json_schema()
        schema_hint = f"\n\nRespond with a single JSON object matching this schema:\n{json.dumps(schema_json, indent=2)}"

        # Append to last user message
        msgs = [m.copy() for m in messages]
        if msgs and msgs[-1]["role"] == "user":
            msgs[-1]["content"] = msgs[-1]["content"] + schema_hint
        else:
            msgs.append({"role": "user", "content": schema_hint})

        content = self.chat(msgs, response_format=schema, temperature=temperature, max_tokens=max_tokens, context=context)
        content = _strip_markdown(content)

        try:
            return schema.model_validate_json(content)
        except Exception as e:
            logger.error(f"Failed to parse structured response: {e}\nContent: {content[:500]}")
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
