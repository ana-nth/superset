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
Orchestration of one chat turn: plan → resolve → chart → validate → narrate.

Failure handling, from most to least recoverable:
- LLM unreachable / slow / bad JSON  → rules planner, the user is told
- unknown names or values            → one LLM repair round, then a question
- incompatible chart type            → best fitting type, the user is told why
- query returns no rows              → explanation with the data's date coverage
- query / connection errors          → a specific message, nothing is saved
- anything else                      → a generic message with a reference id
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import ValidationError
from sqlalchemy import Engine, func, select

from ai_chat import charts, config, datasources, executor, llm, store
from ai_chat.catalog import Catalog, CatalogError, get_catalog, SourceInfo
from ai_chat.charts import choose_viz, render_config, VIZ
from ai_chat.formatting import humanize as _humanize
from ai_chat.intent import ChartIntent
from ai_chat.metadb import Dashboard, DashboardChart, SavedChart, session_scope
from ai_chat.narrate import narrate
from ai_chat.prompts import (
    answer_prompt,
    build_examples,
    explain_prompt,
    PREVIOUS_CHART,
    REPAIR_PROMPT,
    system_prompt,
)
from ai_chat.resolver import (
    describe_filter,
    dim_label,
    Resolution,
    resolve,
    ResolvedSpec,
    with_derived,
)
from ai_chat.rules import is_schema_question, plan_with_rules
from ai_chat.sqlgen import build_query

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str], None]
MAX_MESSAGE_CHARS = 2000
HISTORY_TURNS = 3
GRAIN_WORDS = {
    "PT1H": "Hourly",
    "P1D": "Daily",
    "P1W": "Weekly",
    "P1M": "Monthly",
    "P3M": "Quarterly",
    "P1Y": "Yearly",
}


class ChatError(Exception):
    """An error with a message that is safe and useful to show to the user."""

    def __init__(self, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Plan:
    intent: ChartIntent
    engine: str  # "llm" | "rules"
    notes: list[str] = field(default_factory=list)
    messages: list[dict[str, str]] = field(default_factory=list)
    raw: Optional[dict[str, Any]] = None


# ──────────────────────────────────────────────────────────────────────────────
# Planning
# ──────────────────────────────────────────────────────────────────────────────


def _history(conv: dict[str, Any]) -> list[dict[str, str]]:
    """The last few exchanges, assistant turns as the JSON the planner produced."""
    turns: list[dict[str, str]] = []
    messages = [m for m in conv["messages"] if m.get("role") in ("user", "assistant")]
    for msg in messages[-(HISTORY_TURNS * 2 + 1) : -1]:
        if msg["role"] == "user":
            turns.append({"role": "user", "content": msg.get("text", "")[:500]})
        elif msg.get("_intent"):
            turns.append({"role": "assistant", "content": json.dumps(msg["_intent"])})
    # Keep strict user/assistant alternation that some chat templates require
    cleaned: list[dict[str, str]] = []
    for turn in turns:
        if cleaned and cleaned[-1]["role"] == turn["role"]:
            cleaned[-1] = turn
        else:
            cleaned.append(turn)
    if cleaned and cleaned[-1]["role"] == "user":
        cleaned.pop()
    return cleaned


def _rules_plan(
    text: str, catalog: Catalog, previous: Optional[ResolvedSpec], reason: str
) -> Plan:
    note = (
        f"The AI model is unavailable ({reason}), so I used keyword matching. "
        "Results may be less precise."
    )
    return Plan(plan_with_rules(text, catalog, previous), "rules", [note])


def make_plan(
    text: str,
    catalog: Catalog,
    conv: dict[str, Any],
    previous: Optional[ResolvedSpec],
) -> Plan:
    down = llm.recently_down()
    if down:
        return _rules_plan(text, catalog, previous, down)

    system = system_prompt(catalog, text, previous.source if previous else None)
    if previous is not None:
        system += "\n\n" + PREVIOUS_CHART.format(
            spec=json.dumps(previous.to_intent_dict())
        )
    messages = [
        {"role": "system", "content": system},
        *build_examples(catalog, text),
        *_history(conv),
        {"role": "user", "content": text},
    ]
    for attempt in range(2):
        try:
            data = llm.complete_json(messages)
            intent = ChartIntent.model_validate(data)
            return Plan(intent, "llm", messages=messages, raw=data)
        except llm.LLMUnavailableError as exc:
            return _rules_plan(text, catalog, previous, str(exc))
        except (llm.LLMBadOutputError, ValidationError) as exc:
            logger.info(
                "AI chat: unusable LLM answer (attempt %d): %s", attempt + 1, exc
            )
            messages = [
                *messages,
                {
                    "role": "user",
                    "content": "That was not a valid JSON object for the schema. "
                    "Answer with the JSON object only.",
                },
            ]
    return _rules_plan(text, catalog, previous, "it did not return valid JSON")


def _repair(plan: Plan, problems: list[str]) -> Optional[ChartIntent]:
    """Give the LLM one chance to fix names the resolver rejected."""
    if plan.engine != "llm" or plan.raw is None:
        return None
    messages = [
        *plan.messages,
        {"role": "assistant", "content": json.dumps(plan.raw)},
        {
            "role": "user",
            "content": REPAIR_PROMPT.format(
                problems="\n".join(f"- {p}" for p in problems)
            ),
        },
    ]
    try:
        data = llm.complete_json(messages)
        return ChartIntent.model_validate(data)
    except (llm.LLMUnavailableError, llm.LLMBadOutputError, ValidationError) as exc:
        logger.info("AI chat repair failed: %s", exc)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Query execution
# ──────────────────────────────────────────────────────────────────────────────


def _short_error(exc: Exception) -> str:
    text = str(getattr(exc, "orig", None) or exc)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\[SQL:.*", "", text)  # never echo the full statement
    return text[:300] or type(exc).__name__


def engine_for(database_id: str) -> Engine:
    try:
        return datasources.get_engine(datasources.get_connection(database_id))
    except datasources.DataSourceError as exc:
        raise ChatError(str(exc), "datasource") from exc


def query_for(
    catalog: Catalog, src: SourceInfo, spec: ResolvedSpec, viz: str
) -> executor.Query:
    engine = engine_for(catalog.database_id)
    return build_query(
        spec,
        viz,
        src.sql,
        engine.dialect.name,
        executor.quoter(engine.dialect),
    )


def run_chart_query(
    catalog: Catalog, src: SourceInfo, spec: ResolvedSpec, viz: str
) -> list[dict[str, Any]]:
    """Run the chart's query (read-only, cached for a few minutes)."""
    query = query_for(catalog, src, spec, viz)
    try:
        return executor.cached_run(
            catalog.database_id, engine_for(catalog.database_id), query
        )
    except ChatError:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("AI chat chart query failed: %s", exc)
        message = _short_error(exc)
        if "permission denied" in message.lower():
            raise ChatError(
                "The database user may not read this data. Ask an admin to grant "
                "SELECT on the schema.",
                "forbidden",
            ) from exc
        raise ChatError(f"The chart query failed: {message}", "query_failed") from exc


def _source_of(catalog: Catalog, spec: ResolvedSpec) -> SourceInfo:
    src = catalog.find_source(spec.source)
    if src is None:
        raise ChatError(
            f"The data behind this chart ({spec.source}) no longer exists in "
            f"{catalog.schema}.",
            "not_found",
        )
    return src


def chart_rows(
    database_id: str, schema: str, viz: str, spec_data: dict[str, Any]
) -> list[dict[str, Any]]:
    """Fresh rows for a chat or saved chart."""
    try:
        catalog = get_catalog(database_id, schema)
    except CatalogError as exc:
        raise ChatError(str(exc), "catalog") from exc
    spec = ResolvedSpec.from_dict(spec_data)
    return run_chart_query(catalog, _source_of(catalog, spec), spec, viz)


def chart_payload(
    database_id: str,
    schema: str,
    viz: str,
    spec_data: dict[str, Any],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """What the frontend draws: rows, or bins for a histogram."""
    rows = chart_rows(database_id, schema, viz, spec_data)
    if viz == "histogram":
        return {"histogram": charts.histogram(rows, cfg), "row_count": len(rows)}
    return {"rows": rows, "row_count": len(rows)}


# ──────────────────────────────────────────────────────────────────────────────
# Message builders
# ──────────────────────────────────────────────────────────────────────────────


def _message(kind: str, text: str, **extra: Any) -> dict[str, Any]:
    msg = {
        "id": store.new_id(),
        "role": "assistant",
        "type": kind,
        "text": text,
        "created_at": store.now_iso(),
    }
    msg.update({k: v for k, v in extra.items() if v is not None})
    return msg


def _interpretation(spec: ResolvedSpec, src: SourceInfo, viz: str) -> list[str]:
    chips = [f"Data: {src.label}", f"Chart: {VIZ.get(viz, viz)}"]
    if spec.raw_column and viz == "histogram":
        chips.append(f"Column: {_humanize(spec.raw_column)}")
    else:
        chips += [f"Measure: {m.label}" for m in spec.metrics]
    chips += [f"By: {dim_label(spec, d)}" for d in spec.dimensions]
    if spec.time_grain:
        chips.append(
            f"{GRAIN_WORDS.get(spec.time_grain, 'Over time')} ({spec.time_column})"
        )
    if spec.time_range_label:
        anchor = f" (to {spec.anchor_date})" if spec.anchor_date else ""
        chips.append(f"Period: {spec.time_range_label}{anchor}")
    chips += [f"Filter: {describe_filter(spec, f)}" for f in spec.filters]
    if spec.top_n and spec.dimensions:
        chips.append(f"{'Top' if spec.sort_desc else 'Bottom'} {spec.top_n}")
    return chips


def _no_data_message(
    spec: ResolvedSpec, src: SourceInfo, notes: list[str]
) -> dict[str, Any]:
    reasons = []
    if spec.filters:
        reasons.append(
            "filters: " + "; ".join(describe_filter(spec, f) for f in spec.filters)
        )
    if spec.time_range_label:
        reasons.append(f"period: {spec.time_range_label}")
    text = "No rows match this request"
    if reasons:
        text += " (" + ", ".join(reasons) + ")"
    text += "."
    if src.time_min and src.time_max:
        text += f" {src.label} has data from {src.time_min} to {src.time_max}."
    options = []
    if spec.time_range_label:
        options.append("Show all time instead")
    if spec.filters:
        options.append("Remove the filters")
    return _message("no_data", text, notes=notes, options=options)


def _overview_text(catalog: Catalog) -> str:
    """A factual description of the schema, used when the LLM is unavailable."""
    sources = catalog.overview()
    lines = [
        f"{catalog.database_name}.{catalog.schema} has {len(sources)} data source"
        f"{'s' if len(sources) != 1 else ''} you can ask about:"
    ]
    for src in sources:
        period = f", {src['time_min']} to {src['time_max']}" if src["time_min"] else ""
        line = f"• {src['label']} ({src['name']}, {src['rows']:,} rows{period})"
        if src["description"]:
            line += f": {src['description']}"
        if len(src["tables"]) > 1:
            line += f" It joins {src['tables'][0]} to {', '.join(src['tables'][1:])}."
        measures = ", ".join(m["label"] for m in src["measures"][:6]) or "row counts"
        line += f" Measures: {measures}."
        dims = ", ".join(d["label"] for d in src["dimensions"][:8])
        if dims:
            line += f" Breakdowns: {dims}."
        if src["metrics"]:
            line += (
                " Business metrics: "
                + "; ".join(
                    f"{m['label']}" + (f" ({m['meaning']})" if m["meaning"] else "")
                    for m in src["metrics"]
                )
                + "."
            )
        lines.append(line)
    lines.append("Open a source below for its columns, or try one of these:")
    return "\n".join(lines)


def _tidy(text: str) -> str:
    """Plain text with one bullet per line, whatever the model's formatting."""
    text = re.sub(r"\*\*|__|^#+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*(?:•|^[-*](?= ))\s*", "\n• ", text, flags=re.MULTILINE)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _explain(catalog: Catalog, text: str) -> Optional[str]:
    """The LLM's answer to a question about the data, grounded in the catalog."""
    messages = [
        {"role": "system", "content": explain_prompt(catalog, text)},
        {"role": "user", "content": text},
    ]
    try:
        answer = str(llm.complete_json(messages).get("answer") or "").strip()
    except (llm.LLMUnavailableError, llm.LLMBadOutputError) as exc:
        logger.info("AI chat explanation failed: %s", exc)
        return None
    return _tidy(answer) or None


def _describe_data(catalog: Catalog, text: str, use_llm: bool) -> dict[str, Any]:
    notes: list[str] = []
    answer = _explain(catalog, text) if use_llm and not llm.recently_down() else None
    if answer is None and use_llm:
        notes.append("The AI model is unavailable, so this is a factual summary.")
    return _message(
        "catalog",
        answer or _overview_text(catalog),
        schema=catalog.overview(),
        options=catalog.suggestions(),
        notes=notes or None,
    )


def _answer_question(
    question: str,
    spec: ResolvedSpec,
    chips: list[str],
    notes: list[str],
    rows: list[dict[str, Any]],
    facts: str,
    grain: str = "",
) -> Optional[str]:
    """A direct answer to ``question`` using only the numbers in the chart's rows."""
    queried = "; ".join(chips)
    if grain:
        queried += f"; each underlying row is {grain}"
    units = {m.label: m.unit for m in spec.metrics if m.unit}
    if units:
        queried += "; units: " + ", ".join(f"{k} in {v}" for k, v in units.items())
    if notes:
        queried += "; assumptions: " + " ".join(notes)
    messages = [
        {"role": "system", "content": answer_prompt(question, queried, rows, facts)},
        {"role": "user", "content": question},
    ]
    try:
        answer = str(llm.complete_json(messages).get("answer") or "").strip()
    except (llm.LLMUnavailableError, llm.LLMBadOutputError) as exc:
        logger.info("AI chat answer failed: %s", exc)
        return None
    return _tidy(answer) or None


# ──────────────────────────────────────────────────────────────────────────────
# Chart creation
# ──────────────────────────────────────────────────────────────────────────────


def _resolve_with_repair(
    plan: Plan,
    catalog: Catalog,
    quote: Callable[[str], str],
    previous: Optional[ResolvedSpec],
    text: str,
    progress: ProgressFn,
    dialect: str = "postgresql",
) -> tuple[Resolution, Plan]:
    resolution = resolve(plan.intent, catalog, quote, previous, text, dialect)
    if resolution.problems and plan.engine == "llm":
        progress("Double-checking field names")
        repaired = _repair(plan, resolution.problems)
        if repaired is not None:
            second = resolve(repaired, catalog, quote, previous, text, dialect)
            if len(second.problems) < len(resolution.problems):
                plan = Plan(repaired, plan.engine, plan.notes, plan.messages, plan.raw)
                plan.raw = repaired.model_dump()
                resolution = second
    return resolution, plan


def create_chart(
    plan: Plan,
    catalog: Catalog,
    quote: Callable[[str], str],
    previous: Optional[ResolvedSpec],
    text: str,
    progress: ProgressFn,
    dialect: str = "postgresql",
) -> tuple[dict[str, Any], Optional[ResolvedSpec]]:
    """Returns (assistant message, spec to remember for follow-ups)."""
    resolution, plan = _resolve_with_repair(
        plan, catalog, quote, previous, text, progress, dialect
    )
    notes = [*plan.notes, *resolution.notes]
    if resolution.problems or resolution.spec is None:
        return (
            _message(
                "clarification",
                resolution.question or "Could you rephrase that?",
                options=resolution.options or catalog.suggestions(4),
                notes=notes,
                details=resolution.problems,
            ),
            None,
        )

    spec = resolution.spec
    src = with_derived(catalog.sources[spec.source], spec)
    viz, viz_notes = choose_viz(spec, src)
    notes += viz_notes

    progress("Running the query")
    rows = run_chart_query(catalog, src, spec, viz)
    if not rows:
        return _no_data_message(spec, src, notes), spec

    insight = ""
    try:
        insight = narrate(spec, viz, rows)
    except Exception:  # pylint: disable=broad-except
        logger.warning("AI chat narration failed", exc_info=True)

    chips = _interpretation(spec, src, viz)
    answer = None
    if (
        plan.engine == "llm"
        and viz != "histogram"
        and config.env_bool("ANSWER_WITH_LLM", True)
        and not llm.recently_down()
    ):
        progress("Writing the answer")
        answer = _answer_question(text, spec, chips, notes, rows, insight, src.grain)
    if answer:
        reply = answer
    elif plan.engine == "llm" and plan.intent.reply and not viz_notes:
        # The LLM's sentence describes what it asked for; when the chart type was
        # overridden it would be wrong, so use a factual reply instead
        reply = plan.intent.reply
    else:
        reply = f"Here is “{spec.title}”."
    chart = {
        "title": spec.title,
        "viz_type": viz,
        "viz_label": VIZ.get(viz, viz),
        "database_id": catalog.database_id,
        "schema": catalog.schema,
        "config": render_config(spec, viz, src),
        "row_count": len(rows),
        "interpretation": chips,
        "spec": spec.to_dict(),
    }
    return (
        _message("chart", reply, insight=insight, notes=notes, chart=chart),
        spec,
    )


def chart_sql(chart: dict[str, Any]) -> str:
    """The SQL behind a chat or saved chart, with values inlined for reading."""
    try:
        catalog = get_catalog(chart["database_id"], chart["schema"])
    except CatalogError as exc:
        raise ChatError(str(exc), "catalog") from exc
    spec = ResolvedSpec.from_dict(chart["spec"])
    query = query_for(catalog, _source_of(catalog, spec), spec, chart["viz_type"])
    return executor.render_sql(query)


def show_sql(previous_msg: Optional[dict[str, Any]]) -> dict[str, Any]:
    chart = (previous_msg or {}).get("chart")
    if not chart:
        return _message(
            "text", "There is no chart yet. Ask for one first, then I can show its SQL."
        )
    return _message(
        "sql", f"This is the query behind “{chart['title']}”:", sql=chart_sql(chart)
    )


# ──────────────────────────────────────────────────────────────────────────────
# Saving
# ──────────────────────────────────────────────────────────────────────────────


def _find_dashboards(user_id: str, name: str) -> list[dict[str, str]]:
    """The user's dashboards whose title matches ``name`` (exact matches first)."""
    with session_scope() as session:
        rows = session.scalars(
            select(Dashboard)
            .where(Dashboard.owner == user_id)
            .where(func.lower(Dashboard.title).contains(name.strip().lower()))
            .limit(50)
        ).all()
        found = [{"id": d.id, "title": d.title} for d in rows]
    exact = [d for d in found if d["title"].lower() == name.strip().lower()]
    return exact or found


def save_chart(  # noqa: C901
    user_id: str,
    message: dict[str, Any],
    title: Optional[str] = None,
    dashboard_id: Optional[str] = None,
    new_dashboard_title: Optional[str] = None,
) -> dict[str, Any]:
    """Persist the chart of ``message``; optionally add it to a dashboard."""
    chart = message.get("chart")
    if not chart:
        raise ChatError("That message has no chart to save.", "no_chart")

    with session_scope() as session:
        dashboard: Optional[Dashboard] = None
        created_dashboard = False
        if dashboard_id:
            dashboard = session.get(Dashboard, dashboard_id)
            if dashboard is None:
                raise ChatError(f"Dashboard {dashboard_id} was not found.", "not_found")
            if dashboard.owner != user_id:
                raise ChatError(
                    "You can only add charts to dashboards you own.", "forbidden"
                )
        elif new_dashboard_title and new_dashboard_title.strip():
            dashboard = Dashboard(
                title=new_dashboard_title.strip()[:500], owner=user_id
            )
            session.add(dashboard)
            created_dashboard = True

        saved: Optional[SavedChart] = None
        if chart.get("saved_chart_id"):
            saved = session.get(SavedChart, chart["saved_chart_id"])
            if saved is not None and saved.owner != user_id:
                saved = None
        if saved is None:
            saved = SavedChart(
                title=(title or chart["title"]).strip()[:250],
                owner=user_id,
                database_id=chart["database_id"],
                schema=chart["schema"],
                viz_type=chart["viz_type"],
                spec=chart["spec"],
                config=chart["config"],
            )
            session.add(saved)
        session.flush()

        if dashboard is not None and not any(
            p.chart_id == saved.id for p in dashboard.placements
        ):
            session.add(
                DashboardChart(
                    dashboard_id=dashboard.id,
                    chart_id=saved.id,
                    position=len(dashboard.placements),
                )
            )
        result: dict[str, Any] = {
            "chart_id": saved.id,
            "chart_title": saved.title,
            "chart_url": f"/charts/{saved.id}",
        }
        if dashboard is not None:
            result |= {
                "dashboard_id": dashboard.id,
                "dashboard_title": dashboard.title,
                "dashboard_url": f"/dashboards/{dashboard.id}",
                "dashboard_created": created_dashboard,
            }

    chart["saved_chart_id"] = result["chart_id"]
    chart["saved_title"] = result["chart_title"]
    if "dashboard_id" in result:
        chart.setdefault("dashboards", [])
        if result["dashboard_id"] not in [d["id"] for d in chart["dashboards"]]:
            chart["dashboards"].append(
                {"id": result["dashboard_id"], "title": result["dashboard_title"]}
            )
    return result


def _save_from_chat(
    plan: Plan, user_id: str, previous_msg: Optional[dict[str, Any]]
) -> dict[str, Any]:
    if not previous_msg or not previous_msg.get("chart"):
        return _message("text", "There is no chart to save yet. Ask for a chart first.")
    intent = plan.intent
    dashboard_id = None
    new_title = None
    note = ""
    if intent.dashboard:
        matches = _find_dashboards(user_id, intent.dashboard)
        if len(matches) > 1:
            return _message(
                "clarification",
                f"Several of your dashboards match “{intent.dashboard}”. Which one?",
                options=[f"Add it to {d['title']}" for d in matches[:6]],
            )
        if matches:
            dashboard_id = matches[0]["id"]
        else:
            new_title = intent.dashboard
            note = (
                f"No dashboard of yours is called “{intent.dashboard}”, "
                "so I created it."
            )
    result = save_chart(
        user_id,
        previous_msg,
        title=intent.title,
        dashboard_id=dashboard_id,
        new_dashboard_title=new_title,
    )
    text = f"Saved “{result['chart_title']}”."
    if result.get("dashboard_title"):
        text = (
            f"Saved “{result['chart_title']}” and added it to "
            f"“{result['dashboard_title']}”."
        )
    return _message("saved", text, saved=result, notes=[note] if note else None)


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────


def _previous(
    conv: dict[str, Any], reference_message_id: Optional[str]
) -> tuple[Optional[dict[str, Any]], Optional[ResolvedSpec]]:
    """The chart message follow-ups refer to: the referenced one, else the latest."""
    msg = None
    if reference_message_id:
        msg = store.find_message(conv, reference_message_id)
    if msg is None:
        msg = next(
            (m for m in reversed(conv["messages"]) if m.get("chart") or m.get("_spec")),
            None,
        )
    if msg is None:
        return None, None
    data = (msg.get("chart") or {}).get("spec") or msg.get("_spec")
    try:
        return msg, ResolvedSpec.from_dict(data) if data else None
    except (TypeError, KeyError):
        return msg, None


def handle_message(  # noqa: C901
    conv: dict[str, Any],
    text: str,
    reference_message_id: Optional[str] = None,
    progress: Optional[ProgressFn] = None,
) -> dict[str, Any]:
    """Produce the assistant reply for ``text`` (conversation is not saved here)."""
    progress = progress or (lambda _msg: None)
    progress("Reading the data catalog")
    try:
        catalog = get_catalog(conv["database_id"], conv["schema"])
    except CatalogError as exc:
        raise ChatError(str(exc), "catalog") from exc

    sa_dialect = engine_for(conv["database_id"]).dialect
    quote = executor.quoter(sa_dialect)
    previous_msg, previous = _previous(conv, reference_message_id)

    progress("Understanding your question")
    plan = make_plan(text, catalog, conv, previous)
    intent = plan.intent
    action = intent.action
    if action == "modify_chart" and previous is None:
        action = "create_chart"
    if action in ("create_chart", "clarify", "unsupported") and is_schema_question(
        text
    ):
        # "Help me understand the database" is a question, not a chart request
        action = "describe_data"

    if action in ("create_chart", "modify_chart"):
        msg, spec = create_chart(
            plan, catalog, quote, previous, text, progress, sa_dialect.name
        )
        if spec is not None and "chart" not in msg:
            # Remember specs that returned no rows so "show all time" can modify them
            msg["_spec"] = spec.to_dict()
    elif action == "save_chart":
        progress("Saving the chart")
        msg = _save_from_chat(plan, conv["user_id"], previous_msg)
    elif action == "show_sql":
        progress("Fetching the SQL")
        msg = show_sql(previous_msg)
    elif action == "describe_data":
        progress("Explaining the data")
        msg = _describe_data(catalog, text, use_llm=plan.engine == "llm")
    elif action == "clarify":
        msg = _message(
            "clarification",
            intent.question
            or intent.reply
            or "Could you tell me a bit more about what to chart?",
            options=intent.options or catalog.suggestions(4),
        )
    elif action == "smalltalk":
        msg = _message(
            "text",
            intent.reply or "Hi! Ask me for a chart in plain English, for example:",
            options=catalog.suggestions(4),
        )
    else:
        lead = f"{intent.reply} " if plan.engine == "llm" and intent.reply else ""
        msg = _message(
            "text",
            f"{lead}I can only create charts from the data in {catalog.schema}; "
            "I can't "
            "change data or answer unrelated questions. Here are some things I can do:",
            options=catalog.suggestions(4),
        )

    if plan.notes and msg.get("type") not in ("chart", "no_data", "clarification"):
        msg["notes"] = [*plan.notes, *(msg.get("notes") or [])]
    msg["engine"] = plan.engine
    msg["_intent"] = (
        plan.raw if plan.raw is not None else intent.model_dump(exclude_defaults=True)
    )
    return msg


def public_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Strip internal fields before a message leaves the server."""
    return {k: v for k, v in msg.items() if not k.startswith("_")}


def public_conversation(conv: dict[str, Any]) -> dict[str, Any]:
    return {
        **{k: v for k, v in conv.items() if k not in ("messages", "user_id")},
        "messages": [public_message(m) for m in conv["messages"]],
    }
