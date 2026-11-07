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
# ruff: noqa: E501  (prompt text reads better unwrapped)
"""
Prompts for the chat planner.

Few-shot examples are generated from the user's own catalog: small models copy
the names they see in examples, so examples built on real names stay valid.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from superset.ai_chat.catalog import Catalog, SourceInfo
from superset.utils import json

SYSTEM_PROMPT = """You turn a business user's chat message into a JSON chart request for Apache Superset.
Today is {today}. Answer with ONE JSON object and nothing else.

Fields:
- action: create_chart (a new chart) | modify_chart (change the PREVIOUS CHART) | save_chart (save the previous chart or add it to a dashboard) | show_sql (show the query behind the previous chart) | describe_data (what data, fields or questions are available) | clarify (impossible to tell what to chart) | smalltalk (greetings, thanks) | unsupported (unrelated to this data, or asks to change/delete data)
- source: one SOURCE name from the catalog
- metrics: list of {{"column": <measure or null>, "aggregate": "sum|avg|count|count_distinct|min|max"}} or {{"business_metric": <name>}}
- dimensions: 0-2 columns to group by. A dimension may also be DERIVED from a column: {{"derive": "weekday|day_type|month|quarter|year|hour", "column": <date column>}} (day of week, weekday vs weekend, month of year, quarter of year, year, hour of day), {{"derive": "bucket", "column": <measure>, "edges": [100, 500]}} (bands/ranges of a number), or {{"derive": "group", "column": <dimension>, "groups": {{"<group name>": [<values>]}}}} (custom groups of values; the rest become Other)
- time_grain: day|week|month|quarter|year for trends / "over time" / "monthly"; otherwise null
- time_range: a period phrase such as "last 30 days", "this year", "last year", "2024", "Q1 2025", "2024-01-01 to 2024-03-31"; null for all time
- filters: list of {{"column", "operator": "==|!=|>|>=|<|<=|IN|NOT IN|contains", "value"}}; add "derive" to filter on a derived value, e.g. {{"column": <date column>, "derive": "day_type", "operator": "==", "value": "Weekend"}}
- chart_type: auto|big_number|line|bar|area|pie|table|heatmap|histogram|treemap|funnel|scatter
- top_n: number for "top N" / "bottom N"; sort: "desc" (top, highest) or "asc" (bottom, lowest)
- title: a short chart title
- reply: one friendly sentence on what you made, including any assumption
- question and options: only when action is clarify; options are 2-5 short alternative requests the user can click, e.g. "Revenue for Retail"
- dashboard: dashboard name when the user wants the chart added to one

Rules:
1. Use only SOURCE, column, business_metric names and filter values that appear in the CATALOG, spelled exactly. Filter values come from the CATALOG lists or the user's own words ("PlayStation 4 games" → filter platform on "PlayStation 4"); never make one up. When a place is part of a measure (jp_sales means Japan Sales), "in Japan" means that measure, not a filter.
2. Use a measure's listed aggregate unless the user asks otherwise. "How many <things>" is count, or count_distinct of that dimension (e.g. customers).
3. "by X", "per X", "for each X", "across X", and "best/top/which X" make X a dimension (rank it; never a max/min single number). "trend", "over time", "monthly", "by month" set time_grain. "Day of week", "weekday", "weekends", "month of year", "seasonality", "hour of day" and "bands/ranges/buckets" are DERIVED dimensions, not time_grain; never invent a column for them.
4. Keep chart_type "auto" unless the user names a chart or asks for a share/proportion/breakdown of a whole (then "pie") or a single number/KPI (then "big_number").
5. Prefer a sensible assumption over a question: if "sales" could mean revenue or quantity, pick the revenue-like measure and say so in reply. Use clarify only when no measure or source fits at all.
6. Follow-ups ("make it a pie", "only for Online", "by quarter instead", "add region") change the PREVIOUS CHART: copy it, change only what was asked, return the complete request with action modify_chart.
7. Never write SQL.

CATALOG
{catalog}
"""

PREVIOUS_CHART = "PREVIOUS CHART (the user's follow-ups refer to this):\n{spec}"

REPAIR_PROMPT = """Your JSON had these problems:
{problems}
Fix them using only names and values from the CATALOG and answer with the corrected JSON object only."""


def _example_source(catalog: Catalog, message: str) -> Optional[SourceInfo]:
    for src in catalog.rank_sources(message):
        if src.main_measure and any(
            2 <= d.cardinality <= 25 for d in src.of_kind("dimension")
        ):
            return src
    return None


def build_examples(catalog: Catalog, message: str) -> list[dict[str, str]]:
    """Two to four user/assistant pairs that use the catalog's own names."""
    src = _example_source(catalog, message)
    if src is None:
        return []
    measure = src.columns[src.main_measure]
    dims = sorted(
        (d for d in src.of_kind("dimension") if 2 <= d.cardinality <= 25),
        key=lambda d: d.cardinality,
    )
    dim = dims[0]
    other = dims[1] if len(dims) > 1 else None
    value = dim.values[0] if dim.values else None
    m = {"column": measure.name, "aggregate": measure.agg}
    examples: list[tuple[str, dict[str, Any]]] = []

    base = {
        "action": "create_chart",
        "source": src.name,
        "metrics": [m],
        "dimensions": [dim.name],
        "chart_type": "auto",
        "title": f"{measure.label} by {dim.label}",
        "reply": f"Here is {measure.label.lower()} by {dim.label.lower()}.",
    }
    examples.append((f"{measure.label.lower()} by {dim.label.lower()}", base))
    # A follow-up right after the chart it modifies
    examples.append(
        (
            "show that as a pie chart",
            {
                **base,
                "action": "modify_chart",
                "chart_type": "pie",
                "reply": "Switched it to a pie chart.",
            },
        )
    )

    if src.time_col and value is not None:
        examples.append(
            (
                f"monthly {measure.label.lower()} trend for {value} this year",
                {
                    "action": "create_chart",
                    "source": src.name,
                    "metrics": [m],
                    "dimensions": [],
                    "time_grain": "month",
                    "time_range": "this year",
                    "filters": [{"column": dim.name, "operator": "==", "value": value}],
                    "chart_type": "auto",
                    "title": f"Monthly {measure.label} – {value}",
                    "reply": f"Monthly {measure.label.lower()} for {value} this year.",
                },
            )
        )

    if src.time_col:
        examples.append(
            (
                f"{measure.label.lower()} by day of week",
                {
                    "action": "create_chart",
                    "source": src.name,
                    "metrics": [m],
                    "dimensions": [{"derive": "weekday", "column": src.time_col}],
                    "chart_type": "auto",
                    "title": f"{measure.label} by Day of Week",
                    "reply": f"Here is {measure.label.lower()} by day of week.",
                },
            )
        )

    if other is not None:
        examples.append(
            (
                f"how many rows per {other.label.lower()}? bottom 3 only",
                {
                    "action": "create_chart",
                    "source": src.name,
                    "metrics": [{"column": None, "aggregate": "count"}],
                    "dimensions": [other.name],
                    "top_n": 3,
                    "sort": "asc",
                    "chart_type": "auto",
                    "title": f"Bottom 3 {other.label} by Count",
                    "reply": f"The 3 {other.label.lower()} values with the fewest rows.",
                },
            )
        )

    messages: list[dict[str, str]] = []
    for question, answer in examples:
        messages.append({"role": "user", "content": question})
        messages.append({"role": "assistant", "content": json.dumps(answer)})
    return messages


def system_prompt(catalog: Catalog, message: str, prefer: Optional[str]) -> str:
    return SYSTEM_PROMPT.format(
        today=date.today().isoformat(),
        catalog=catalog.to_prompt(message, prefer),
    )
