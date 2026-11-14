"""Provider-aware OpenAI-compatible LLM client with reasoning support."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, TypeVar
from urllib.parse import urlparse

from openai import OpenAI
from pydantic import BaseModel

import config

logger = logging.getLogger(__name__)

# A hung completion must not stall a multi-hour crawl. The OpenAI SDK's own
# default is a 600 s read timeout with 2 retries — up to ~30 minutes invisible
# per advert — and the relay has been observed accepting a request and then
# sending nothing (2026-09-20: the first full LJ registry replay sat on one
# socket with ~1 s of CPU and zero verdicts until it was stopped). These are
# hard, env-tunable limits: connect/pool fail fast, a read is bounded at
# LOVEWORK_LLM_TIMEOUT (default 180 s — an ordinary match returns in 5-20 s),
# and the SDK retries LOVEWORK_LLM_MAX_RETRIES times. A caller that legitimately
# needs longer raises the env value rather than removing the bound.
_LLM_TIMEOUT_SECONDS = float(os.getenv("LOVEWORK_LLM_TIMEOUT", "180"))
LLM_MAX_RETRIES = int(os.getenv("LOVEWORK_LLM_MAX_RETRIES", "2"))


def _llm_timeout():
    """The httpx timeout object, or a plain float when httpx is unavailable."""
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx ships with the SDK
        return _LLM_TIMEOUT_SECONDS
    return httpx.Timeout(
        connect=10.0, read=_LLM_TIMEOUT_SECONDS, write=30.0, pool=10.0
    )


LLM_TIMEOUT = _llm_timeout()
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


def _as_int(value: Any) -> int | None:
    """An int from a usage field, or None when absent/unparseable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _as_float(value: Any) -> float | None:
    """A float from a cost/speed field, or None when absent/unparseable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    return None


def _extract_usage(response: Any) -> dict[str, Any]:
    """Pull accounting fields off a provider response, tolerating absence.

    The choke point is `chat()`: every LLM call (chat or Responses API,
    `chat()` or `structured()`) funnels through it, so one extractor here
    covers streaming vs non-streaming (the client never streams today —
    verified by grep: no `stream=True` anywhere — but a future streaming
    path must route its final response through this same function), retry
    paths (only the successful attempt records; failed attempts raise and
    never reach `_record`), and both API styles.

    OpenAI-shaped `usage` carries `prompt_tokens` / `completion_tokens` /
    `total_tokens`; some relays add `cost`, `cost_usd`, or speed fields.
    Anything missing stays absent (null in the ledger) — never zero-filled.
    """
    if response is None:
        return {}
    usage = getattr(response, "usage", None)
    out: dict[str, Any] = {}
    prompt = completion = total = None
    if usage is not None:
        # completion_tokens_details / prompt_tokens_details are per-bucket
        # breakdowns, not additional totals — only the three top-level
        # fields are summed.
        prompt = _as_int(getattr(usage, "prompt_tokens", None))
        if prompt is None and isinstance(usage, dict):
            prompt = _as_int(usage.get("prompt_tokens"))
        completion = _as_int(getattr(usage, "completion_tokens", None))
        if completion is None and isinstance(usage, dict):
            completion = _as_int(usage.get("completion_tokens"))
        total = _as_int(getattr(usage, "total_tokens", None))
        if total is None and isinstance(usage, dict):
            total = _as_int(usage.get("total_tokens"))
        if prompt is None and isinstance(usage, dict):
            prompt = _as_int(usage.get("input_tokens"))
        if completion is None and isinstance(usage, dict):
            completion = _as_int(usage.get("output_tokens"))
        if prompt is not None:
            out["prompt_tokens"] = prompt
        if completion is not None:
            out["completion_tokens"] = completion
        if total is not None:
            out["total_tokens"] = total
        # Cost: accept the common key variants; first hit wins.
        for key in ("cost_usd", "cost", "total_cost", "price_usd"):
            raw = getattr(usage, key, None)
            if raw is None and isinstance(usage, dict):
                raw = usage.get(key)
            cost = _as_float(raw)
            if cost is not None:
                out["cost_usd"] = cost
                break
        # Speed: provider-reported tok/s wins over the derived fallback.
        for key in ("tokens_per_second", "completion_tokens_per_second",
                    "tokens_per_sec", "speed_tps"):
            raw = getattr(usage, key, None)
            if raw is None and isinstance(usage, dict):
                raw = usage.get(key)
            tps = _as_float(raw)
            if tps is not None and tps > 0:
                out["tokens_per_second"] = tps
                out["tps_source"] = "reported"
                break
    else:
        # A dict-shaped response (some test doubles) carries usage at top level.
        if isinstance(response, dict):
            for key, field in (("prompt_tokens", "prompt_tokens"),
                               ("completion_tokens", "completion_tokens"),
                               ("total_tokens", "total_tokens"),
                               ("cost_usd", "cost_usd"), ("cost", "cost_usd")):
                val = _as_int(response.get(key)) if "tokens" in field else _as_float(response.get(key))
                if val is not None:
                    out[field] = val
    return out


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
        provider: str | None = None,
    ) -> None:
        # One resolution for everything the profile can choose (docs/49): provider,
        # endpoint, model, effort, temperature, cap and credential. The caller's
        # explicit argument still wins; the profile beats the global defaults.
        settings = config.llm_settings()
        self.api_key = api_key or settings.api_key
        self.base_url = base_url or settings.base_url
        self.model = model or settings.model
        self.temperature = temperature if temperature is not None else settings.temperature
        self.max_tokens = max_tokens if max_tokens is not None else settings.max_tokens
        self.request_profile = resolve_request_profile(self.base_url, self.model)
        # **An explicit provider wins over the derived one** (2026-10-18, per-principal
        # since 2026-10-21). Both OpenCode subscriptions share `opencode.ai`, so the
        # host cannot say which key is in use — the label is what the call log and the
        # dataset ledger record. Explicit argument, then env, then the profile; the
        # generic defaults (`openai`, `auto`, empty) keep the derived answer.
        # `settings.provider` already resolves env `LOVEWORK_LLM_PROVIDER` → the
        # profile → `.env`'s `LLM_PROVIDER`, so reading `os.getenv("LLM_PROVIDER")`
        # here would let the tracked `.env` beat a per-principal profile.
        explicit_provider = (provider
                             or getattr(settings, "provider", "")
                             or "").strip()
        if explicit_provider and explicit_provider.lower() not in ("", "auto", "openai"):
            self.request_profile = replace(self.request_profile,
                                           provider=explicit_provider)
        # Callers such as the rehearsal agents opt into reasoning explicitly;
        # otherwise the profile's effort applies (max since 2026-10-21).
        self.reasoning_effort = (
            reasoning_effort if reasoning_effort is not None
            else getattr(settings, "reasoning_effort", None)
        )

        if not self.api_key:
            raise ValueError("No API key found. Set LLM_API_KEY or OPENAI_API_KEY.")

        # OpenCode hosts require an x-opencode-session header (2026-09-09):
        # Console Go rejects chat/completions without it (HTTP 400
        # MissingSessionID). One stable value per crawl session is expected —
        # all requests to the OpenCode API carry the same key. Only sent when
        # the endpoint is an OpenCode host; other providers are unaffected.
        headers: dict[str, str] = {}
        if "opencode.ai" in urlparse(self.base_url).netloc.lower():
            headers["x-opencode-session"] = (
                os.environ.get("LOVEWORK_OPENCODE_SESSION_ID")
                or str(uuid.uuid4())
            )

        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            default_headers=headers,
            timeout=LLM_TIMEOUT,
            max_retries=LLM_MAX_RETRIES,
        )

    @property
    def _transcript_index(self) -> int:
        """Monotonic per-client call index, so a run's calls are ordered."""
        self.__dict__["_call_count"] = self.__dict__.get("_call_count", 0) + 1
        return self.__dict__["_call_count"]

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
                started = time.monotonic()
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
                latency_s = time.monotonic() - started

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
                # Record the exact request and its response: WP5's exact playback
                # is impossible without them, and the operation's run id is what
                # lets a seal find its own calls (docs/28 §WP5).
                self._record(messages, content, finish_reason, label,
                             temperature=temp, max_tokens=tok,
                             response_format=response_format,
                             reasoning_effort=effort,
                             latency_s=latency_s, response=response)
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

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_choice: str = "auto",
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        retries: int = 3,
        context: str = "",
    ) -> dict:
        """One tool-calling request: {"content": str, "tool_calls": [...]}.

        Chat-completions transports only (the RLM session runner's path —
        the opencode relay honours `tools`, measured 2026-10-24). Each call
        in `tool_calls` is {"id", "name", "arguments"} with arguments as the
        raw JSON *string* the API returns. Raises NotImplementedError on
        Responses-API transports so the caller can fall back to text; other
        failures retry exactly like `chat`.
        """
        if self.request_profile.uses_responses_api:
            raise NotImplementedError("tools need the chat-completions API")
        label = context or "tool call"
        kwargs = self._chat_kwargs(
            messages,
            temperature=temperature if temperature is not None else self.temperature,
            max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
            response_format=None,
            reasoning_effort=reasoning_effort if reasoning_effort is not None else self.reasoning_effort,
        )
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice
        for attempt in range(retries):
            try:
                started = time.monotonic()
                response = self.client.chat.completions.create(**kwargs)
                choice = response.choices[0]
                message = choice.message
                calls = []
                for call in (getattr(message, "tool_calls", None) or []):
                    function = getattr(call, "function", None)
                    calls.append({
                        "id": getattr(call, "id", "") or "",
                        "name": getattr(function, "name", "") or "",
                        "arguments": getattr(function, "arguments", "") or "",
                    })
                content = message.content or ""
                latency_s = time.monotonic() - started
                summary = content or f"{len(calls)} tool call(s)"
                self._record(messages, summary,
                             str(getattr(choice, "finish_reason", "unknown")),
                             label, latency_s=latency_s, response=response)
                return {"content": content, "tool_calls": calls,
                        "finish_reason": getattr(choice, "finish_reason", "")}
            except NotImplementedError:
                raise
            except Exception as exc:
                logger.warning("LLM tool call failed (attempt %d/%d): %s",
                               attempt + 1, retries, exc)
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise
        return {"content": "", "tool_calls": [], "finish_reason": "error"}

    def _record(self, messages: list[dict], content: str, finish_reason: str,
                label: str, *, temperature=None, max_tokens=None,
                response_format=None, reasoning_effort=None,
                latency_s: float | None = None, response: Any = None) -> None:
        """Best-effort transcript write; never affects the call's outcome."""
        try:
            import model_transcripts as mt

            run_id = os.environ.get("LOVEWORK_OUTER_RUN_ID", "") or "unattributed"
            usage = _extract_usage(response)
            mt.record_call(
                run_id=run_id, model=self.model,
                provider=self.provider, api_style=self.api_style,
                messages=messages,
                canonical=mt.canonical_request(
                    messages, temperature=temperature, max_tokens=max_tokens,
                    response_format=response_format,
                    reasoning_effort=reasoning_effort),
                content=content, finish_reason=str(finish_reason), label=label,
                call_index=self._transcript_index, latency_s=latency_s,
                **usage)
        except Exception as e:  # bookkeeping must not break a crawl
            logger.debug("Transcript recording skipped: %s", e)

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
            # Some providers (opencode-go / mimo-v2.6-flash observed 2026-09-27
            # onward) occasionally wrap the requested object in an OpenAI-style
            # JSON-schema envelope {"description": ..., "properties": {...}}.
            # The real fields live under "properties"; unwrap before declaring
            # the response unparsable so one envelope-shaped reply does not
            # cost a full extract fallback (a second LLM call) per page.
            unwrapped = _unwrap_properties_envelope(content)
            if unwrapped is not None:
                try:
                    return schema.model_validate_json(unwrapped)
                except Exception:
                    pass
            # **A truncated answer is a generation failure, not a parse one** (2026-10-22).
            # Four `complete_pack` calls died with "Invalid JSON: EOF while parsing a
            # string": with `reasoning_effort=max` the thinking can spend the completion
            # budget before the object closes, and a JSON-shaped task does not need it.
            # Retry **once with reasoning off** (the empty string is the explicit
            # "send no effort" value — `None` would fall back to the profile's max);
            # only then raise.
            effective = (reasoning_effort if reasoning_effort is not None
                         else self.reasoning_effort)
            if effective:
                logger.warning("Structured response unparsable (%s); retrying without "
                               "reasoning", str(exc)[:120])
                retry = self.chat(
                    msgs,
                    response_format=schema,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    reasoning_effort="",
                    context=context,
                )
                try:
                    return schema.model_validate_json(_strip_markdown(retry))
                except Exception:
                    logger.error("Structured response unparsable after the no-reasoning "
                                 "retry: %s\nContent: %s", exc, retry[:500])
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


def _unwrap_properties_envelope(content: str) -> str | None:
    """Return the JSON payload of an OpenAI-style envelope, if present.

    Language models asked for a JSON object sometimes return the request's
    JSON-schema *document* shape instead of a bare instance — a top-level
    object with ``description`` and ``properties`` keys where the actual
    schema fields live nested under ``properties``.  Pydantic then reports
    every schema field as missing.  This recognizes that shape and returns
    the serialized ``properties`` payload, or None when the content is not
    an envelope (so the caller preserves its original error).
    """
    try:
        data = json.loads(content)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    props = data.get("properties")
    if not isinstance(props, dict) or not props:
        return None
    if not any(
        key in props
        for key in ("found_jobs", "job_listings", "next_urls", "confidence",
                    "reasoning", "title", "jobs")
    ):
        return None
    return json.dumps(props, ensure_ascii=False)
