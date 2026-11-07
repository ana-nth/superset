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

Configuration (``AI_CHAT_*`` falls back to the Auto Dashboard ``AI_DASH_*``):
- ``AI_CHAT_PROVIDER``: ``openrouter`` (default), ``opensource`` (any other
  OpenAI-compatible server: Ollama, vLLM) or ``anthropic``
- ``AI_CHAT_MODEL``, ``AI_CHAT_BASE_URL``, ``AI_CHAT_API_KEY``
- ``OPENROUTER_API_KEY``: the key used by the ``openrouter`` provider
- ``AI_CHAT_LLM_TIMEOUT``: seconds per call (default 60)

The chat always asks for JSON mode (``response_format``), so with OpenRouter pick a
model that supports it; requests are only routed to providers that honour it.

After a failed call the LLM is skipped for ``AI_CHAT_LLM_COOLDOWN`` seconds (default 60)
so that, while it is down, messages are answered by the rules planner immediately
instead of each waiting for a timeout.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

from superset.utils import json

logger = logging.getLogger(__name__)

DOWN_KEY = "ai_chat:llm_down"
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


def _env(name: str, default: str = "") -> str:
    chat = os.environ.get(f"AI_CHAT_{name}")
    if chat:
        return chat
    return os.environ.get(f"AI_DASH_{name}") or default


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def provider() -> str:
    return _env("PROVIDER", "openrouter").lower()


def model_name() -> str:
    return _env("MODEL", DEFAULT_MODELS.get(provider(), DEFAULT_MODELS["opensource"]))


def _openai_settings() -> dict[str, Any]:
    """Connection settings for the OpenAI-compatible providers."""
    if provider() == "openrouter":
        api_key = os.environ.get("OPENROUTER_API_KEY") or _env("API_KEY")
        if not api_key:
            raise LLMUnavailableError("OPENROUTER_API_KEY is not set")
        return {
            "base_url": _env("BASE_URL", OPENROUTER_BASE_URL),
            "api_key": api_key,
            # Shown in the OpenRouter activity log
            "default_headers": {
                "X-Title": os.environ.get("OPENROUTER_APP_NAME", "Superset AI Chat")
            },
            # Only route to providers that support every parameter sent (JSON mode)
            "extra_body": {"provider": {"require_parameters": True}},
        }
    return {
        "base_url": _env("BASE_URL", "http://host.docker.internal:11434/v1"),
        "api_key": _env("API_KEY", "ollama"),
    }


def make_model() -> Any:
    """A LangChain chat model configured for short, deterministic JSON answers."""
    timeout = _int("LLM_TIMEOUT", 60)
    if provider() == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise LLMUnavailableError("ANTHROPIC_API_KEY is not set")
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model_name(),
            api_key=api_key,
            temperature=0,
            max_tokens=1024,
            timeout=timeout,
            max_retries=1,
        )

    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(
        model=model_name(),
        temperature=0,
        max_tokens=1024,
        timeout=timeout,
        max_retries=1,
        **_openai_settings(),
    )
    # JSON mode (OpenRouter, Ollama, vLLM, OpenAI) stops prose around the object
    return model.bind(response_format={"type": "json_object"})


def _message_text(msg: Any) -> str:
    content = getattr(msg, "content", msg) or ""
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    return str(content)


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
    from superset.extensions import cache_manager

    try:
        reason = cache_manager.cache.get(DOWN_KEY)
    except Exception:  # pylint: disable=broad-except
        return None
    return str(reason) if reason else None


def _mark_down(reason: str) -> None:
    from superset.extensions import cache_manager

    try:
        cache_manager.cache.set(
            DOWN_KEY, reason[:300], timeout=_int("LLM_COOLDOWN", 60)
        )
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
    try:
        model = make_model()
        reply = model.invoke(messages)
    except LLMUnavailableError:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        reason = _describe_error(exc)
        logger.warning("AI chat LLM call failed: %s", exc)
        _mark_down(reason)
        raise LLMUnavailableError(reason) from exc
    return extract_json(_message_text(reply))
