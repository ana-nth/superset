# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""
LLM access for the chat planner.

Configuration:
- ``AI_CHAT_PROVIDER``: ``openrouter`` (default), ``opensource`` (any other
  OpenAI-compatible server: Ollama, vLLM, LM Studio, OpenAI itself) or ``anthropic``
- ``AI_CHAT_MODEL``, ``AI_CHAT_BASE_URL``, ``AI_CHAT_API_KEY`` (``OPENROUTER_API_KEY``
  for OpenRouter, ``ANTHROPIC_API_KEY`` for Anthropic)
- ``AI_CHAT_LLM_TIMEOUT``: seconds per call (default 60)

After a failed call the LLM is skipped for ``AI_CHAT_LLM_COOLDOWN`` seconds (default 60)
so that, while it is down, messages are answered by the rules planner immediately
instead of each waiting for a timeout.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

from ai_chat.config import env_int, env_str
from ai_chat.kv import kv

logger = logging.getLogger(__name__)

DOWN_KEY = "ai_chat:llm_down"
MAX_TOKENS = 1024
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODELS = {
    "openrouter": "google/gemma-4-31b-it:free",
    "anthropic": "claude-sonnet-5-5",
    "opensource": "llama3.1:8b",
}


class LLMUnavailableError(Exception):
    """The model could not be reached or failed to answer."""


class LLMBadOutputError(Exception):
    """The model answered, but not with a JSON object."""


def provider() -> str:
    return env_str("PROVIDER", "openrouter").lower()


def model_name() -> str:
    return env_str(
        "MODEL", DEFAULT_MODELS.get(provider(), DEFAULT_MODELS["opensource"])
    )


def _merge_turns(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Join consecutive same-role turns (Anthropic expects alternation)."""
    merged: list[dict[str, str]] = []
    for msg in messages:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1] = {
                "role": msg["role"],
                "content": merged[-1]["content"] + "\n\n" + msg["content"],
            }
        else:
            merged.append(dict(msg))
    return merged


def _call_anthropic(messages: list[dict[str, str]], timeout: int) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY") or env_str("API_KEY")
    if not api_key:
        raise LLMUnavailableError("ANTHROPIC_API_KEY is not set")
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=1)
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = _merge_turns([m for m in messages if m["role"] != "system"])
    reply = client.messages.create(
        model=model_name(),
        max_tokens=MAX_TOKENS,
        system=system,
        messages=turns,  # type: ignore[arg-type]
    )
    return "".join(
        getattr(block, "text", "")
        for block in reply.content
        if getattr(block, "type", "") == "text"
    )


def _call_openai_compatible(messages: list[dict[str, str]], timeout: int) -> str:
    from openai import OpenAI

    extra: dict[str, Any] = {}
    if provider() == "openrouter":
        api_key = os.environ.get("OPENROUTER_API_KEY") or env_str("API_KEY")
        if not api_key:
            raise LLMUnavailableError("OPENROUTER_API_KEY is not set")
        client = OpenAI(
            base_url=env_str("BASE_URL", OPENROUTER_BASE_URL),
            api_key=api_key,
            timeout=timeout,
            max_retries=1,
            # Shown in the OpenRouter activity log
            default_headers={
                "X-Title": os.environ.get("OPENROUTER_APP_NAME", "AI Chart Chat")
            },
        )
        # Only route to providers that support every parameter sent (JSON mode)
        extra["extra_body"] = {"provider": {"require_parameters": True}}
    else:
        client = OpenAI(
            base_url=env_str("BASE_URL", "http://localhost:11434/v1"),
            api_key=env_str("API_KEY", "ollama"),
            timeout=timeout,
            max_retries=1,
        )
    reply = client.chat.completions.create(  # type: ignore[call-overload]
        model=model_name(),
        messages=messages,
        temperature=0,
        max_tokens=MAX_TOKENS,
        # JSON mode (OpenRouter, Ollama, vLLM, OpenAI) stops prose around the object
        response_format={"type": "json_object"},
        **extra,
    )
    return reply.choices[0].message.content or ""


def extract_json(text: str) -> dict[str, Any]:  # noqa: C901
    """Parse the first JSON object in ``text`` (tolerates code fences and prose)."""
    cleaned = re.sub(r"```(?:json)?", "", text or "").strip()
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
    except (ValueError, TypeError):
        pass
    start = cleaned.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for idx in range(start, len(cleaned)):
            char = cleaned[idx]
            if in_str:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_str = False
                continue
            if char == '"':
                in_str = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(cleaned[start : idx + 1])
                        if isinstance(value, dict):
                            return value
                    except (ValueError, TypeError):
                        break
                    break
        start = cleaned.find("{", start + 1)
    raise LLMBadOutputError(f"No JSON object in model output: {cleaned[:200]!r}")


def recently_down() -> Optional[str]:
    """The reason the LLM failed within the cool-down window, if it did."""
    try:
        reason = kv().get(DOWN_KEY)
    except Exception:  # pylint: disable=broad-except
        return None
    return str(reason) if reason else None


def _mark_down(reason: str) -> None:
    try:
        kv().set(DOWN_KEY, reason[:300], env_int("LLM_COOLDOWN", 60))
    except Exception:  # pylint: disable=broad-except
        logger.debug("Could not record LLM outage", exc_info=True)


def _describe_error(exc: Exception) -> str:
    name = type(exc).__name__
    text = str(exc)
    if "Connection" in name or "connect" in text.lower():
        return f"the AI model server is unreachable ({name})"
    if "Timeout" in name or "timed out" in text.lower():
        return "the AI model took too long to answer"
    if "NotFound" in name or "not found" in text.lower():
        return f"the AI model '{model_name()}' is not available on the server"
    if "Authentication" in name or "401" in text:
        return "the AI model rejected the API key"
    if "RateLimit" in name or "429" in text:
        return "the AI model is rate limited"
    if "402" in text:
        return "the AI model account is out of credits"
    return f"the AI model failed ({name})"


def complete_json(messages: list[dict[str, str]]) -> dict[str, Any]:
    """Send ``messages`` and return the JSON object the model answers with."""
    timeout = env_int("LLM_TIMEOUT", 60)
    try:
        if provider() == "anthropic":
            text = _call_anthropic(messages, timeout)
        else:
            text = _call_openai_compatible(messages, timeout)
    except LLMUnavailableError:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        reason = _describe_error(exc)
        logger.warning("AI chat LLM call failed: %s", exc)
        _mark_down(reason)
        raise LLMUnavailableError(reason) from exc
    return extract_json(text)
