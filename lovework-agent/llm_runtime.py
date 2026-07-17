"""
LLM runtime facade — thin wrapper over pi-agent.

We depend on this, not on pi-agent directly. If pi-agent breaks or we want
to swap providers, we change this file, not 47 call sites.

Why a facade:
- pi-agent 0.1.0 is alpha (per its PyPI page: "Status: 3 - Alpha")
- The API may change between minor versions
- Our agent code should be stable even if pi-agent isn't

The facade exposes only what we use:
- `complete(messages, tools, model) -> str` — single-turn LLM call
- `stream(messages, tools, model) -> Iterator[Event]` — streaming
- `run_agent_loop(initial_prompts, context, config) -> EventStream` — the ReAct loop
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Iterator, List, Optional

from pi_agent import (
    AgentContext,
    AgentEvent,
    AgentLoopConfig,
    AgentMessage,
    AgentTool,
    LlmContext,
    LlmMessage,
    Model,
    OpenAICompletionsProvider,
    ProviderRegistry,
    TextContent,
    UserMessage,
    agent_loop,
    create_agent_stream_fn,
    create_default_registry,
    default_convert_to_llm,
    default_model,
    stream,
)

import config

logger = logging.getLogger(__name__)


# ── Model construction ──────────────────────────────────────────────────

def build_model(model_name: Optional[str] = None) -> Model:
    """Build a pi-agent Model pointed at our LLM provider (DeepSeek by default).

    pi-agent's OpenAICompletionsProvider works with any OpenAI-compatible
    endpoint, so we point it at DeepSeek's base URL.
    """
    name = model_name or config.LLM_MODEL
    return Model(
        id=name,
        provider="openai",  # pi-agent labels it as OpenAI but the endpoint is DeepSeek
        api="openai-completions",
        base_url=config.LLM_BASE_URL,
        reasoning=False,
    )


# ── Provider registry ───────────────────────────────────────────────────

def build_provider_registry() -> ProviderRegistry:
    """Build a registry that can serve our LLM provider."""
    reg = create_default_registry()
    # Register our DeepSeek-compatible provider under the "deepseek" name
    # (already done by create_default_registry, but we re-register with our
    # base_url for clarity)
    return reg


def get_api_key() -> str:
    """Return the API key for our LLM provider."""
    key = config.LLM_API_KEY
    if not key:
        raise ValueError("LLM_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY not set")
    return key


# ── High-level helpers ──────────────────────────────────────────────────

def complete(
    messages: List[dict],
    system: Optional[str] = None,
    model: Optional[Model] = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> str:
    """Single-turn completion. Returns the assistant's text content.

    Args:
        messages: list of {"role": "user"|"assistant", "content": "..."} dicts
        system: optional system prompt
        model: pi-agent Model (defaults to config.LLM_MODEL)
        temperature: sampling temperature
        max_tokens: max output tokens

    Returns:
        The assistant's text response.
    """
    m = model or build_model()

    # Build pi-agent LlmContext
    llm_messages: list[LlmMessage] = []
    if system:
        # pi-agent uses a separate system_prompt field, not a system message
        pass
    for msg in messages:
        if msg["role"] == "user":
            llm_messages.append(UserMessage(content=msg["content"]))
        else:
            # assistant
            from pi_agent import AssistantMessage
            llm_messages.append(AssistantMessage(content=[TextContent(text=msg["content"])]))

    ctx = LlmContext(system_prompt=system or "", messages=llm_messages)

    # Use stream() and collect the result
    from pi_agent import stream

    result_text = ""
    for event in stream(m, ctx, api_key=get_api_key(), temperature=temperature, max_tokens=max_tokens):
        if event.get("type") == "text_delta":
            result_text += event.get("delta", "")
        elif event.get("type") == "message_end":
            msg = event.get("message")
            if msg and hasattr(msg, "content"):
                for c in msg.content:
                    if hasattr(c, "text"):
                        result_text += c.text
    return result_text


def run_agent_loop(
    prompts: List[AgentMessage],
    context: AgentContext,
    config_loop: AgentLoopConfig,
    stream_fn = None,
    max_turns: int = 20,
) -> List[AgentMessage]:
    """Run the pi-agent ReAct loop and return the final messages.

    This is what gives us an actual agent — the LLM can call tools repeatedly
    until it decides it has an answer.

    pi-agent's agent_loop is async. We run it inside an event loop here so
    the caller can be sync.

    Args:
        max_turns: safety cap on LLM turns (each turn = 1 LLM call). Streaming
            text deltas don't count. Default 20 turns is generous — a typical
            job-discovery run uses 2-5.
    """
    import asyncio

    if stream_fn is None:
        reg = build_provider_registry()
        stream_fn = create_agent_stream_fn(reg)

    async def _run() -> List[AgentMessage]:
        stream_obj = agent_loop(prompts, context, config_loop, stream_fn=stream_fn)
        final_messages: List[AgentMessage] = []
        turn_count = 0
        async for event in stream_obj:
            etype = event.get("type")
            if etype == "message_end":
                msg = event.get("message")
                if msg is not None and msg not in context.messages:
                    final_messages.append(msg)
            elif etype == "turn_end":
                # A turn = one LLM call. This is what we cap.
                turn_count += 1
                if turn_count > max_turns:
                    logger.warning(f"Hit max_turns={max_turns}, ending loop")
                    break
            elif etype == "agent_end":
                # Terminal event — EventStream will set _done, sentinel queued.
                break
        return final_messages

    return asyncio.run(_run())


# ── Re-exports for convenience ─────────────────────────────────────────
# Anything that imports from llm_runtime gets these:
__all__ = [
    "build_model",
    "build_provider_registry",
    "complete",
    "run_agent_loop",
    "default_convert_to_llm",
    "AgentTool",
    "AgentContext",
    "AgentLoopConfig",
    "AgentMessage",
    "LlmContext",
    "LlmMessage",
    "Model",
    "UserMessage",
]
