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
AI Auto Dashboard orchestration.

Modes (AI_DASH_MODE):
- ``hybrid`` (default): the deterministic builder runs the five checks and builds the
  dashboard; the LLM only writes the executive summary. If the LLM is unreachable
  a templated summary is used, so a dashboard is always produced.
- ``rules``: deterministic builder only, no LLM calls.
- ``agent``: a deepagents agent with one SubAgent per check (C1–C5) drives the tools.
  If it fails or does not produce a dashboard, the deterministic builder takes over.

Providers (AI_DASH_PROVIDER): ``openrouter`` (default, key in OPENROUTER_API_KEY),
``opensource`` (any other OpenAI-compatible server, e.g. Ollama) or ``anthropic``.
AI_DASH_MODEL is the main model, AI_DASH_SUBMODEL the one the C1–C5 subagents use.

Budgets: MAX_SQL_CALLS run_sql calls, MAX_CHARTS charts, MAX_PROBES follow-up probes.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
from typing import Any, Callable, Optional

from superset.ai_dashboard.prompts import (
    SUBAGENT_SPECS,
    SUMMARY_PROMPT,
    SYSTEM_PROMPT,
)
from superset.utils import json

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str], None]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default) or default


def _int_env(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


MAX_SQL_CALLS = _int_env("AI_DASH_MAX_SQL_CALLS", 25)
MAX_CHARTS = _int_env("AI_DASH_MAX_CHARTS", 14)
MAX_PROBES = _int_env("AI_DASH_MAX_PROBES", 8)


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _make_models() -> tuple[Any, Any]:  # noqa: C901
    """Return (main_model, sub_model) for the configured provider."""
    provider = _env("AI_DASH_PROVIDER", "openrouter").lower()
    timeout = _int_env("AI_DASH_LLM_TIMEOUT", 120)
    if provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY env var not set")
        from langchain_anthropic import ChatAnthropic

        def anthropic(model: str, max_tokens: int) -> Any:
            params: dict[str, Any] = {
                "model": model,
                "api_key": api_key,
                "temperature": 0,
                "max_tokens": max_tokens,
                "timeout": timeout,
                "max_retries": 1,
            }
            return ChatAnthropic(**params)

        return (
            anthropic(_env("AI_DASH_MODEL", "claude-sonnet-5-5"), 4096),
            anthropic(_env("AI_DASH_SUBMODEL", "claude-haiku-5-5"), 2048),
        )

    from langchain_openai import ChatOpenAI

    extra: dict[str, Any] = {}
    if provider == "openrouter":
        api_key = os.environ.get("OPENROUTER_API_KEY") or _env("AI_DASH_API_KEY", "")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY env var not set")
        base_url = _env("AI_DASH_BASE_URL", OPENROUTER_BASE_URL)
        default_main = "nvidia/nemotron-3-ultra-550b-a55b:free"
        default_sub = "google/gemma-4-26b-a4b-it:free"
        # Shown in the OpenRouter activity log
        extra["default_headers"] = {
            "X-Title": _env("OPENROUTER_APP_NAME", "Superset AI Dashboard")
        }
    else:
        base_url = _env(
            "AI_DASH_BASE_URL",
            _env("OLLAMA_BASE_URL", "http://host.docker.internal:11434/v1"),
        )
        api_key = _env("AI_DASH_API_KEY", _env("OPENAI_API_KEY", "ollama"))
        default_main, default_sub = "llama3.1:8b", "llama3.2:3b"

    def openai_compat(model: str, max_tokens: int) -> Any:
        params: dict[str, Any] = {
            "model": model,
            "base_url": base_url,
            "api_key": api_key,
            "temperature": 0,
            "max_tokens": max_tokens,
            "timeout": timeout,
            "max_retries": 1,
            **extra,
        }
        return ChatOpenAI(**params)

    return (
        openai_compat(_env("AI_DASH_MODEL", default_main), 4096),
        openai_compat(_env("AI_DASH_SUBMODEL", default_sub), 2048),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────


def run_agent(
    database_id: int,
    schema: str,
    progress_callback: Optional[ProgressFn] = None,
) -> dict[str, Any]:
    """
    Produce an analytics dashboard for ``schema``.
    Returns {dashboard_id, insights, summary, mode, ...} or {error}.
    """
    progress: ProgressFn = progress_callback or (lambda _msg: None)
    mode = _env("AI_DASH_MODE", "hybrid").lower()
    note = ""

    if mode == "agent":
        result = _run_deep_agent(database_id, schema, progress)
        if isinstance(result.get("dashboard_id"), int):
            result.setdefault("mode", "agent")
            return result
        note = str(
            result.get("error") or result.get("warning") or "no dashboard produced"
        )
        logger.warning("Deep agent did not build a dashboard (%s); using builder", note)
        progress(
            "AI agent did not finish — building the dashboard with the "
            "rule-based engine…"
        )

    try:
        result = _run_builder(database_id, schema, progress, use_llm=mode != "rules")
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("AI dashboard builder failed")
        from superset.extensions import db

        db.session.rollback()
        return {"error": f"Dashboard build failed: {str(exc)[:400]}"}
    if note:
        result["fallback_reason"] = note[:300]
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Hybrid / rules mode
# ──────────────────────────────────────────────────────────────────────────────


def _run_builder(
    database_id: int,
    schema: str,
    progress: ProgressFn,
    use_llm: bool,
) -> dict[str, Any]:
    from superset.ai_dashboard.builder import Builder

    builder = Builder(database_id, schema, progress=progress, max_charts=MAX_CHARTS)
    builder.run_checks()
    progress("Creating and validating charts…")
    builder.finalize_charts()
    insights = builder.all_insights()

    summary, llm_used = "", False
    if use_llm:
        progress("Asking the AI model for an executive summary…")
        summary = _llm_summary(insights)
        llm_used = bool(summary)
    if not summary:
        summary = _template_summary(insights)

    progress("Building dashboard layout…")
    title = f"Auto Dashboard · {schema} · {datetime.date.today():%Y-%m-%d}"
    dashboard_id = builder.build_dashboard(title, summary)
    chart_ids = [p.chart_id for p in builder.kpis] + [
        p.chart_id for s in builder.sections for p in s.charts
    ]
    return {
        "dashboard_id": dashboard_id,
        "insights": insights,
        "summary": summary,
        "mode": "hybrid" if use_llm else "rules",
        "llm_used": llm_used,
        "charts_created": len(chart_ids),
    }


_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _top_insights(
    insights: list[dict[str, Any]], limit: int = 5
) -> list[dict[str, Any]]:
    return sorted(
        insights, key=lambda i: _SEVERITY_ORDER.get(i.get("severity", "info"), 3)
    )[:limit]


def _template_summary(insights: list[dict[str, Any]]) -> str:
    """A deterministic summary used when the LLM is unavailable."""
    top = _top_insights(insights)
    if not top:
        return ""
    return "\n".join(f"- **{i['title']}:** {i['text']}" for i in top)


def _llm_summary(insights: list[dict[str, Any]]) -> str:
    """Ask the model for a short markdown summary; returns '' on any failure."""
    if not insights:
        return ""
    try:
        model, _ = _make_models()
        facts = json.dumps(
            [
                {k: i.get(k) for k in ("check", "title", "text", "severity")}
                for i in insights
            ],
        )
        reply = model.invoke(
            [
                {"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": f"Findings:\n{facts}"},
            ]
        )
        text = _message_text(reply).strip()
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("LLM summary unavailable: %s", exc)
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    bullets = [
        ln if ln.startswith(("-", "*")) else f"- {ln}"
        for ln in lines
        if not ln.startswith("#")
    ]
    # Guard against rambling or empty output from small local models
    return "\n".join(bullets[:6])[:1500]


def _message_text(msg: Any) -> str:
    content = getattr(msg, "content", msg) or ""
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    return str(content)


# ──────────────────────────────────────────────────────────────────────────────
# Agent mode (deepagents)
# ──────────────────────────────────────────────────────────────────────────────


def _make_lc_tools(  # noqa: C901
    database_id: int,
    schema: str,
    sql_calls: list[int],
    chart_registry: list[dict[str, Any]],
    progress_callback: Optional[ProgressFn] = None,
    include_dashboard: bool = True,
) -> list[Any]:
    """Build LangChain tool objects wrapping our tools.py functions."""
    from langchain_core.tools import tool as lc_tool

    from superset.ai_dashboard import tools

    @lc_tool
    def get_semantics() -> str:
        """Return condensed semantics.yaml (tables, columns, joins, metrics).

        Call once at startup.
        """
        return tools.get_semantics()

    @lc_tool
    def run_sql(sql: str) -> str:
        """Execute a read-only SELECT/WITH SQL on the warehouse.

        LIMIT is capped at 200 and the number of calls is budgeted.
        """
        if sql_calls[0] >= MAX_SQL_CALLS:
            return json.dumps(
                {"error": f"SQL budget exhausted ({MAX_SQL_CALLS} calls)"}
            )
        sql_calls[0] += 1
        result = tools.run_sql(sql, database_id)
        return json.dumps(result, default=str)[:1500]

    @lc_tool
    def create_dataset(name: str, sql: str) -> str:
        """Create a virtual Superset dataset (a named SQL view) and return its id.

        Typically a fact table joined to its dimension tables.
        """
        return json.dumps(tools.create_dataset(name, sql, database_id, schema))

    @lc_tool
    def create_chart(spec: dict) -> str:  # type: ignore[type-arg]
        """Create a Superset chart. spec keys: dataset (int dataset_id), viz_type
        (big_number_total|echarts_timeseries_line|echarts_timeseries_bar|pie|table|heatmap_v2|histogram_v2),
        title, metrics (SQL aggregates such as "SUM(line_amount)"),
        groupby (column names),
        time_column (for time series). Returns chart_id or an error to fix."""
        if len(chart_registry) >= MAX_CHARTS:
            return json.dumps(
                {"error": f"Chart budget exhausted ({MAX_CHARTS} charts)"}
            )
        dataset_id = spec.get("dataset")
        try:
            dataset_id = int(dataset_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return json.dumps(
                {"error": "spec.dataset (integer dataset_id) is required"}
            )
        result = tools.create_chart(spec, dataset_id)
        if "chart_id" in result:
            chart_registry.append(
                {
                    "chart_id": result["chart_id"],
                    "uuid": result.get("uuid", ""),
                    "viz_type": result.get("viz_type", spec.get("viz_type", "table")),
                    "title": spec.get("title", ""),
                    "dataset": dataset_id,
                }
            )
            if progress_callback:
                progress_callback(f"Chart created: {spec.get('title', '')}")
        return json.dumps(result)

    @lc_tool
    def build_dashboard(title: str, sections: list) -> str:  # type: ignore[type-arg]
        """Assemble the final Superset dashboard from all charts.

        Call exactly once at the end.
        sections: list of {title, chart_ids, insight} dicts."""
        dataset_id = next(
            (c["dataset"] for c in chart_registry if c.get("dataset")), None
        )
        result = tools.build_dashboard(
            title, sections, chart_registry, dataset_id=dataset_id
        )
        return json.dumps(result)

    if include_dashboard:
        return [get_semantics, run_sql, create_dataset, create_chart, build_dashboard]
    return [run_sql, create_chart]


def _make_subagent_tools(
    database_id: int,
    schema: str,
    sql_calls: list[int],
    chart_registry: list[dict[str, Any]],
    progress_callback: Optional[ProgressFn] = None,
) -> list[Any]:
    """Subagents get run_sql + create_chart but not build_dashboard."""
    return _make_lc_tools(
        database_id,
        schema,
        sql_calls,
        chart_registry,
        progress_callback,
        include_dashboard=False,
    )


def _run_deep_agent(
    database_id: int, schema: str, progress: ProgressFn
) -> dict[str, Any]:
    """Drive the deepagents agent.

    Returns {dashboard_id, insights} or {error}/{warning}.
    """
    try:
        from deepagents import create_deep_agent
    except ImportError as exc:
        return {"error": f"Missing dependency: {exc}. Run: pip install deepagents"}

    try:
        main_model, sub_model = _make_models()
    except Exception as exc:  # pylint: disable=broad-except
        return {"error": str(exc)}

    sql_calls = [0]
    chart_registry: list[dict[str, Any]] = []
    progress("Starting AI deep agent…")

    main_tools = _make_lc_tools(
        database_id, schema, sql_calls, chart_registry, progress
    )
    sub_tools = _make_subagent_tools(
        database_id, schema, sql_calls, chart_registry, progress
    )
    subagents = [
        {
            "name": name,
            "description": desc,
            "system_prompt": prompt,
            "model": sub_model,
            "tools": sub_tools,
        }
        for name, desc, prompt in SUBAGENT_SPECS
    ]

    agent = create_deep_agent(
        model=main_model,
        tools=main_tools,
        system_prompt=SYSTEM_PROMPT,
        subagents=subagents,  # type: ignore[arg-type]
    )

    user_message = (
        "Generate a full analytics dashboard for "
        f"database_id={database_id}, schema={schema}. "
        "Follow your system instructions exactly. "
        "1. Call get_semantics first. "
        "2. Use create_dataset to build datasets, then delegate each of "
        "C1–C5 to its subagent. "
        f"3. Run up to {MAX_PROBES} follow-up hypotheses with run_sql. "
        "4. Call build_dashboard once. "
        '5. Return ONLY valid JSON: {"dashboard_id": <int>, '
        '"insights": [{"title": str, "text": str, '
        '"severity": "info"|"warning"|"critical", "chart_id": <int>}]}'
    )

    progress("Calling deep agent…")
    try:
        result_state = agent.invoke(
            {"messages": [{"role": "user", "content": user_message}]},
            config={"recursion_limit": _int_env("AI_DASH_RECURSION_LIMIT", 120)},
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Deep agent error: %s", exc)
        from superset.extensions import db

        db.session.rollback()
        return _salvage(chart_registry, schema, f"Agent error: {exc}")

    messages = (
        result_state.get("messages", []) if isinstance(result_state, dict) else []
    )
    logger.info(
        "Agent finished. chart_registry=%d charts. Total messages=%d",
        len(chart_registry),
        len(messages),
    )
    for msg in reversed(messages):
        parsed = _parse_final_json(_message_text(msg))
        if parsed is not None:
            return parsed

    structured = (
        result_state.get("structured_response")
        if isinstance(result_state, dict)
        else None
    )
    if isinstance(structured, dict) and "dashboard_id" in structured:
        return structured
    if structured is not None and hasattr(structured, "model_dump"):
        return structured.model_dump()

    return _salvage(
        chart_registry, schema, "Agent completed but did not emit final JSON"
    )


def _parse_final_json(text: str) -> Optional[dict[str, Any]]:
    text = text.strip()
    if not text:
        return None
    candidates = [text]
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        candidates.append(match.group())
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict) and "dashboard_id" in parsed:
            try:
                parsed["dashboard_id"] = int(parsed["dashboard_id"])
            except (TypeError, ValueError):
                continue
            parsed["insights"] = [
                i for i in parsed.get("insights") or [] if isinstance(i, dict)
            ]
            return parsed
    return None


def _salvage(
    chart_registry: list[dict[str, Any]], schema: str, reason: str
) -> dict[str, Any]:
    """If the agent created enough charts but no dashboard, assemble one from them."""
    if len(chart_registry) < 5:
        return {"error": f"{reason} ({len(chart_registry)} charts created)"}
    from superset.ai_dashboard import tools

    dataset_id = next((c["dataset"] for c in chart_registry if c.get("dataset")), None)
    built = tools.build_dashboard(
        f"Auto Dashboard · {schema} · {datetime.date.today():%Y-%m-%d}",
        [],
        chart_registry,
        dataset_id=dataset_id,
    )
    if "dashboard_id" in built:
        return {
            "dashboard_id": built["dashboard_id"],
            "insights": [],
            "warning": reason,
        }
    return {"error": f"{reason}; {built.get('error', '')}"}
