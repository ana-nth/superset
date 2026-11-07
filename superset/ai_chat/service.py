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
- query / permission / DB errors     → a specific message, nothing is saved
- anything else                      → a generic message with a reference id
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from pydantic import ValidationError

from superset.ai_chat import llm, store
from superset.ai_chat.catalog import (
    Catalog,
    CatalogError,
    ensure_source_dataset,
    get_catalog,
    SourceInfo,
)
from superset.ai_chat.charts import (
    build_form_data,
    build_query,
    choose_viz,
    query_context,
    VIZ,
)
from superset.ai_chat.intent import ChartIntent
from superset.ai_chat.narrate import narrate
from superset.ai_chat.prompts import (
    build_examples,
    PREVIOUS_CHART,
    REPAIR_PROMPT,
    system_prompt,
)
from superset.ai_chat.resolver import (
    describe_filter,
    dim_label,
    Resolution,
    resolve,
    ResolvedSpec,
    with_derived,
)
from superset.ai_chat.rules import plan_with_rules
from superset.ai_dashboard.builder import _humanize
from superset.extensions import db
from superset.utils import json

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


def run_chart_query(
    dataset_id: int,
    form_data: dict[str, Any],
    query: dict[str, Any],
    result_type: str = "full",
) -> dict[str, Any]:
    """Run the chart query through Superset (permissions, RLS, cache apply)."""
    from superset.charts.schemas import ChartDataQueryContextSchema
    from superset.commands.chart.data.get_data_command import ChartDataCommand
    from superset.exceptions import SupersetSecurityException

    try:
        context = ChartDataQueryContextSchema().load(
            query_context(dataset_id, form_data, query, result_type)
        )
        command = ChartDataCommand(context)
        command.validate()
        result = command.run()
    except SupersetSecurityException as exc:
        db.session.rollback()
        raise ChatError(
            "You don't have permission to query this data. Ask an admin for access "
            "to the dataset or schema.",
            "forbidden",
        ) from exc
    except Exception as exc:  # pylint: disable=broad-except
        db.session.rollback()
        logger.warning("AI chat chart query failed: %s", exc)
        raise ChatError(
            f"The chart query failed: {_short_error(exc)}", "query_failed"
        ) from exc
    queries = result.get("queries") or [{}]
    first = queries[0]
    if first.get("error"):
        raise ChatError(
            f"The chart query failed: {str(first['error'])[:300]}", "query_failed"
        )
    return first


def _short_error(exc: Exception) -> str:
    text = str(getattr(exc, "message", "") or exc)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\[SQL:.*", "", text)  # never echo the full statement
    return text[:300] or type(exc).__name__


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
    if spec.raw_column and viz == "histogram_v2":
        chips.append(f"Column: {_humanize(spec.raw_column)}")
    else:
        chips += [f"Measure: {m.label}" for m in spec.metrics]
    chips += [f"By: {dim_label(spec, d)}" for d in spec.dimensions]
    if spec.time_grain:
        chips.append(
            f"{GRAIN_WORDS.get(spec.time_grain, 'Over time')} ({spec.time_column})"
        )
    if spec.time_range_label:
        chips.append(f"Period: {spec.time_range_label}")
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


def _describe_data(catalog: Catalog) -> dict[str, Any]:
    lines = [f"Here is what I can chart in {catalog.database_name}.{catalog.schema}:"]
    for src in catalog.summary():
        period = f", {src['time_min']} to {src['time_max']}" if src["time_min"] else ""
        measures = ", ".join(_humanize(m) for m in src["measures"][:6]) or "row counts"
        dims = ", ".join(_humanize(d) for d in src["dimensions"][:8])
        line = (
            f"• {_humanize(src['name'])} ({src['rows']:,} rows{period}) "
            f"— measures: {measures}"
        )
        if dims:
            line += f"; breakdowns: {dims}"
        if src["metrics"]:
            line += f"; KPIs: {', '.join(_humanize(m) for m in src['metrics'])}"
        lines.append(line)
    lines.append("Try one of these:")
    return _message("catalog", "\n".join(lines), options=catalog.suggestions())


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

    progress("Preparing the dataset")
    try:
        dataset_id = ensure_source_dataset(catalog, src)
    except Exception as exc:  # pylint: disable=broad-except
        db.session.rollback()
        logger.exception("AI chat could not create the dataset for %s", src.name)
        raise ChatError(
            f"I couldn't prepare the dataset for {src.label}: {_short_error(exc)}",
            "dataset_failed",
        ) from exc

    form_data = build_form_data(spec, viz, dataset_id, src)
    query = build_query(spec, viz)
    progress("Running the query")
    result = run_chart_query(dataset_id, form_data, query)
    rows = result.get("data") or []
    if not rows:
        return _no_data_message(spec, src, notes), spec

    insight = ""
    try:
        insight = narrate(spec, viz, rows)
    except Exception:  # pylint: disable=broad-except
        logger.warning("AI chat narration failed", exc_info=True)

    if plan.engine == "llm" and plan.intent.reply and not viz_notes:
        # The LLM's sentence describes what it asked for; when the chart type was
        # overridden it would be wrong, so use a factual reply instead
        reply = plan.intent.reply
    else:
        reply = f"Here is “{spec.title}”."
    chart = {
        "title": spec.title,
        "viz_type": viz,
        "viz_label": VIZ.get(viz, viz),
        "dataset_id": dataset_id,
        "form_data": form_data,
        "row_count": result.get("rowcount", len(rows)),
        "interpretation": _interpretation(spec, src, viz),
        "spec": spec.to_dict(),
    }
    return (
        _message("chart", reply, insight=insight, notes=notes, chart=chart),
        spec,
    )


def show_sql(
    previous_msg: Optional[dict[str, Any]], spec: Optional[ResolvedSpec]
) -> dict[str, Any]:
    chart = (previous_msg or {}).get("chart")
    if not chart or spec is None:
        return _message(
            "text", "There is no chart yet. Ask for one first, then I can show its SQL."
        )
    result = run_chart_query(
        chart["dataset_id"],
        chart["form_data"],
        build_query(spec, chart["viz_type"]),
        result_type="query",
    )
    sql = str(result.get("query") or "").strip()
    if not sql:
        return _message("text", "Superset did not return SQL for this chart.")
    return _message("sql", f"This is the query behind “{chart['title']}”:", sql=sql)


# ──────────────────────────────────────────────────────────────────────────────
# Saving
# ──────────────────────────────────────────────────────────────────────────────


def _find_dashboards(name: str) -> list[Any]:
    """Dashboards the user can edit whose title matches ``name``."""
    from superset.extensions import security_manager
    from superset.models.dashboard import Dashboard

    candidates = (
        db.session.query(Dashboard)
        .filter(Dashboard.dashboard_title.ilike(f"%{name.strip()}%"))
        .limit(50)
        .all()
    )
    editable = [d for d in candidates if security_manager.is_owner(d)]
    exact = [
        d for d in editable if (d.dashboard_title or "").lower() == name.strip().lower()
    ]
    return exact or editable


def save_chart(  # noqa: C901
    conv: dict[str, Any],
    message: dict[str, Any],
    title: Optional[str] = None,
    dashboard_id: Optional[int] = None,
    new_dashboard_title: Optional[str] = None,
    client_query_context: Optional[str] = None,
) -> dict[str, Any]:
    """Persist the chart of ``message``; optionally add it to a dashboard."""
    from superset.commands.chart.create import CreateChartCommand
    from superset.commands.chart.exceptions import (
        ChartCreateFailedError,
        ChartInvalidError,
        DashboardsForbiddenError,
    )
    from superset.commands.dashboard.create import CreateDashboardCommand
    from superset.commands.dashboard.exceptions import (
        DashboardCreateFailedError,
        DashboardInvalidError,
    )
    from superset.daos.dashboard import DashboardDAO
    from superset.extensions import security_manager
    from superset.models.slice import Slice

    chart = message.get("chart")
    if not chart:
        raise ChatError("That message has no chart to save.", "no_chart")

    dashboard = None
    created_dashboard = False
    try:
        if dashboard_id:
            dashboard = DashboardDAO.find_by_id(dashboard_id)
            if dashboard is None:
                raise ChatError(f"Dashboard {dashboard_id} was not found.", "not_found")
            if not security_manager.is_owner(dashboard):
                raise ChatError(
                    "You can only add charts to dashboards you own.", "forbidden"
                )
        elif new_dashboard_title:
            dashboard = CreateDashboardCommand(
                {"dashboard_title": new_dashboard_title.strip()[:500]}
            ).run()
            created_dashboard = True
    except (DashboardCreateFailedError, DashboardInvalidError) as exc:
        db.session.rollback()
        raise ChatError(
            f"Could not create the dashboard: {_short_error(exc)}", "save_failed"
        ) from exc

    existing = None
    if chart.get("saved_chart_id"):
        existing = db.session.query(Slice).get(chart["saved_chart_id"])

    if existing is not None:
        if dashboard is not None and dashboard not in existing.dashboards:
            existing.dashboards.append(dashboard)
            db.session.commit()
        slc = existing
    else:
        form_data = dict(chart["form_data"])
        slice_name = (title or chart["title"]).strip()[:250]
        form_data["slice_name"] = slice_name
        qc = _query_context_for_save(chart, client_query_context)
        try:
            slc = CreateChartCommand(
                {
                    "slice_name": slice_name,
                    "viz_type": chart["viz_type"],
                    "datasource_id": chart["dataset_id"],
                    "datasource_type": "table",
                    "params": json.dumps(form_data),
                    "query_context": qc,
                    "dashboards": [dashboard.id] if dashboard is not None else [],
                    "description": "Created with AI Chart Chat",
                }
            ).run()
        except DashboardsForbiddenError as exc:
            raise ChatError(
                "You can only add charts to dashboards you own.", "forbidden"
            ) from exc
        except (ChartCreateFailedError, ChartInvalidError) as exc:
            db.session.rollback()
            raise ChatError(
                f"Could not save the chart: {_short_error(exc)}", "save_failed"
            ) from exc
        form_data["slice_id"] = slc.id
        slc.params = json.dumps(form_data)
        db.session.commit()

    chart["saved_chart_id"] = slc.id
    chart["saved_title"] = slc.slice_name
    result = {
        "chart_id": slc.id,
        "chart_title": slc.slice_name,
        "chart_url": f"/explore/?slice_id={slc.id}",
    }
    if dashboard is not None:
        chart.setdefault("dashboards", [])
        if dashboard.id not in [d["id"] for d in chart["dashboards"]]:
            chart["dashboards"].append(
                {"id": dashboard.id, "title": dashboard.dashboard_title}
            )
        result |= {
            "dashboard_id": dashboard.id,
            "dashboard_title": dashboard.dashboard_title,
            "dashboard_url": f"/superset/dashboard/{dashboard.id}/",
            "dashboard_created": created_dashboard,
        }
    return result


def _query_context_for_save(chart: dict[str, Any], client: Optional[str]) -> str:
    """Prefer the query context the browser built (exactly what Explore would save)."""
    if client:
        try:
            parsed = json.loads(client)
            if (
                isinstance(parsed, dict)
                and int((parsed.get("datasource") or {}).get("id", -1))
                == chart["dataset_id"]
                and isinstance(parsed.get("queries"), list)
            ):
                return json.dumps(parsed)
        except (ValueError, TypeError):
            pass
    spec = ResolvedSpec.from_dict(chart["spec"])
    return json.dumps(
        query_context(
            chart["dataset_id"],
            chart["form_data"],
            build_query(spec, chart["viz_type"]),
        )
    )


def _save_from_chat(
    plan: Plan, conv: dict[str, Any], previous_msg: Optional[dict[str, Any]]
) -> dict[str, Any]:
    if not previous_msg or not previous_msg.get("chart"):
        return _message("text", "There is no chart to save yet. Ask for a chart first.")
    intent = plan.intent
    dashboard_id = None
    new_title = None
    note = ""
    if intent.dashboard:
        matches = _find_dashboards(intent.dashboard)
        if len(matches) > 1:
            return _message(
                "clarification",
                f"Several of your dashboards match “{intent.dashboard}”. Which one?",
                options=[f"Add it to {d.dashboard_title}" for d in matches[:6]],
            )
        if matches:
            dashboard_id = matches[0].id
        else:
            new_title = intent.dashboard
            note = (
                f"No dashboard of yours is called “{intent.dashboard}”, "
                "so I created it."
            )
    result = save_chart(
        conv,
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

    from superset.daos.database import DatabaseDAO

    database = DatabaseDAO.find_by_id(conv["database_id"])
    sa_dialect = database.get_dialect()  # type: ignore[union-attr]
    quote = sa_dialect.identifier_preparer.quote
    previous_msg, previous = _previous(conv, reference_message_id)

    progress("Understanding your question")
    plan = make_plan(text, catalog, conv, previous)
    intent = plan.intent
    action = intent.action
    if action == "modify_chart" and previous is None:
        action = "create_chart"

    if action in ("create_chart", "modify_chart"):
        msg, spec = create_chart(
            plan, catalog, quote, previous, text, progress, sa_dialect.name
        )
        if spec is not None and "chart" not in msg:
            # Remember specs that returned no rows so "show all time" can modify them
            msg["_spec"] = spec.to_dict()
    elif action == "save_chart":
        progress("Saving the chart")
        msg = _save_from_chat(plan, conv, previous_msg)
    elif action == "show_sql":
        progress("Fetching the SQL")
        msg = show_sql(previous_msg, previous)
    elif action == "describe_data":
        msg = _describe_data(catalog)
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
