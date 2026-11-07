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
Keyword planner used when the LLM is unavailable.

It understands the common shapes of chart requests ("revenue by region",
"monthly orders trend for Online last year", "top 10 products by quantity",
"make it a pie") by matching the message against catalog names, descriptions
and known dimension values. Anything it cannot place is left to the resolver,
which asks the user.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from superset.ai_chat.catalog import _tokens, Catalog, ColumnInfo, SourceInfo
from superset.ai_chat.derived import find_edges
from superset.ai_chat.intent import ChartIntent, parse_time_range
from superset.ai_chat.resolver import ResolvedSpec

CHART_WORDS = [
    (r"\b(pie|donut|doughnut)\b", "pie"),
    (r"\b(heat ?map)\b", "heatmap"),
    (r"\b(histogram|distribution)\b", "histogram"),
    (r"\b(tree ?map)\b", "treemap"),
    (r"\b(funnel)\b", "funnel"),
    (r"\b(scatter|bubble|correlation)\b", "scatter"),
    (r"\b(area)\b", "area"),
    (r"\b(table|list|tabular)\b", "table"),
    (r"\b(line)\b", "line"),
    (r"\b(bar|bars|column chart)\b", "bar"),
    (r"\b(kpi|big number|single number)\b", "big_number"),
]
GRAIN_WORDS = [
    (r"\b(hourly|by hour|per hour)\b", "hour"),
    (r"\b(daily|by day|per day|each day)\b", "day"),
    (r"\b(weekly|by week|per week|each week)\b", "week"),
    (r"\b(monthly|by month|per month|each month|month over month|mom)\b", "month"),
    (r"\b(quarterly|by quarter|per quarter|each quarter)\b", "quarter"),
    (
        r"\b(yearly|annual|annually|by year|per year|each year|year over year|yoy)\b",
        "year",
    ),
    (r"\b(over time|trend|trends|trending|timeline|time series|history)\b", "auto"),
]
RANGE_PATTERNS = [
    r"(?:in |for |during |over )?(?:the )?(?:last|past|previous) \d+ "
    r"(?:day|week|month|quarter|year)s?",
    r"(?:this|current|last|previous) (?:day|week|month|quarter|year)",
    r"\b(?:ytd|year to date|mtd|month to date|qtd|yesterday|today)\b",
    r"\bq[1-4] ?\d{4}\b",
    r"\b(?:january|february|march|april|may|june|july|august|september|october"
    r"|november|december) \d{4}\b",
    r"(?:from |between |since )?\d{4}(?:-\d{2}){0,2}"
    r"(?: ?(?:to|until|through|and|-) ?\d{4}(?:-\d{2}){0,2})?",
]
# Derived breakdowns, most specific first
_WEEKEND = r"week ?ends?"
_WEEKDAY = r"week ?days?"
DERIVED_DIMS = [
    (
        rf"\b(?:{_WEEKEND}|{_WEEKDAY})\b.{{0,20}}?\b(?:vs\.?|versus|or|and|compared"
        rf"(?: to| with)?)\s+(?:{_WEEKEND}|{_WEEKDAY})\b",
        "day_type",
    ),
    (
        r"\b(?:days? of (?:the )?week|(?:by|per|each|every) weekday|day names?"
        r"|weekday names?)\b",
        "weekday",
    ),
    (
        r"\b(?:months? of (?:the )?year|seasonality|seasonal|calendar months?"
        r"|month names?)\b",
        "month",
    ),
    (r"\b(?:quarters? of (?:the )?year)\b", "quarter"),
    (r"\b(?:hours? of (?:the )?day|time of day)\b", "hour"),
]
DERIVED_FILTERS = [
    (rf"\b{_WEEKEND}\b", "Weekend"),
    (rf"\b{_WEEKDAY}\b", "Weekday"),
]
MODIFY_HINTS = re.compile(
    r"^(make it|change|switch|turn it|show (it|that|this) as|as an? |instead|now |only|"
    r"just |same |also |add |remove |exclude |without |filter |limit |sort |"
    r"by (day|week|month|quarter|year)|top \d+|bottom \d+|for |in )",
)


def _grams(text: str, size: int = 3) -> list[str]:
    words = re.findall(r"[a-z0-9_]+", text.lower())
    grams: list[str] = []
    for n in range(size, 0, -1):
        grams += [" ".join(words[i : i + n]) for i in range(len(words) - n + 1)]
    return grams


# Name parts too generic to identify a column on their own
GENERIC_TOKENS = {
    "name", "date", "type", "code", "amount", "value", "total", "qty", "pct",
    "status", "number", "count", "level", "days", "units", "line", "unit",
}  # fmt: skip


def _value_words(src: SourceInfo) -> set[str]:
    """Words that are data values (e.g. 'retail'); never column synonyms."""
    return {
        word
        for col in src.of_kind("dimension")
        for value in col.values
        for word in _tokens(value)
    }


def _find_columns(  # noqa: C901
    text: str,
    src: SourceInfo,
    kinds: tuple[str, ...],
    synonyms: bool = True,
) -> list[tuple[int, ColumnInfo]]:
    """
    Columns of ``kinds`` mentioned in ``text`` with their position, earliest first.

    Matches full names ("sales channel"), distinctive name parts ("segment" →
    customer_segment) and, with ``synonyms``, description words ("revenue" →
    line_amount "Net revenue USD").
    """
    lowered = text.lower()
    words = _tokens(text)
    values = _value_words(src)
    found: dict[str, tuple[int, int, ColumnInfo]] = {}  # name → (strength, pos, col)
    matched_part: dict[str, str] = {}  # name → the name part that matched

    def add(col: ColumnInfo, strength: int, pos: int) -> None:
        if col.name not in found or found[col.name][0] < strength:
            found[col.name] = (strength, pos, col)

    for col in src.columns.values():
        if col.kind not in kinds:
            continue
        for name in {
            col.name.lower(),
            col.label.lower(),
            col.name.lower().replace("_", ""),
        }:
            match = re.search(rf"\b{re.escape(name)}s?\b", lowered)
            if match:
                add(col, 3, match.start())
                break
        else:
            parts = [
                t for t in _tokens(col.name) if len(t) > 3 and t not in GENERIC_TOKENS
            ]
            for part in parts:
                match = re.search(rf"\b{re.escape(part)}s?\b", lowered)
                if match:
                    add(col, 2, match.start())
                    matched_part[col.name] = part
                    break
            else:
                if synonyms:
                    desc = set(_tokens(col.description)) - GENERIC_TOKENS
                    for word in words:
                        if len(word) > 3 and word in desc and word not in values:
                            pos = lowered.find(word)
                            add(col, 1, pos if pos >= 0 else 999)
                            break
    # A part already inside a full match ("customer" in "customer segment") is
    # not a second column
    full_words = {
        word
        for strength, _, col in found.values()
        if strength == 3
        for word in _tokens(col.name)
    }
    for name, part in matched_part.items():
        if part in full_words:
            found.pop(name, None)
    # A part ("segment") that names several columns only counts when unique
    by_part: dict[int, list[str]] = {}
    for name, (strength, pos, _) in found.items():
        if strength == 2:
            by_part.setdefault(pos, []).append(name)
    for names in by_part.values():
        if len(names) > 1:
            for name in names:
                found.pop(name, None)
    # Description synonyms only fill in when nothing is named directly
    if any(strength >= 2 for strength, _, _ in found.values()):
        found = {k: v for k, v in found.items() if v[0] >= 2}
    ranked = sorted(found.values(), key=lambda f: (f[1], -f[0]))
    return [(pos, col) for _, pos, col in ranked]


def _value_filters(
    text: str, src: SourceInfo, context: Optional[list[str]] = None
) -> list[dict[str, Any]]:
    """
    'for Online and Retail', 'excluding Retail' → filters on known values.
    ``context`` lists columns of the chart being modified, preferred when a value
    belongs to several columns.
    """
    grams = set(_grams(text))
    lowered = text.lower()
    owners: dict[str, list[str]] = {}  # value → columns holding it
    for col in src.of_kind("dimension"):
        for value in col.values:
            key = value.lower()
            if len(key) >= 2 and key in grams:
                owners.setdefault(value, []).append(col.name)
    mentioned = {
        c.name for _, c in _find_columns(text, src, ("dimension",), synonyms=False)
    }
    by_column: dict[str, list[str]] = {}
    for value, columns in owners.items():
        if len(columns) > 1:
            # "Online and Retail": Retail is a channel and a segment; pick the
            # column named in the text or the one holding the other values
            named = [c for c in columns if c in mentioned]
            shared = [
                c
                for c in columns
                if any(
                    c in cols
                    for v, cols in owners.items()
                    if v != value and len(cols) == 1
                )
            ]
            in_context = [c for c in columns if c in (context or [])]
            columns = named or shared or in_context or columns[:1]
        by_column.setdefault(columns[0], []).append(value)
    filters = []
    for column, values in by_column.items():
        negated = any(
            re.search(
                rf"\b(excluding|except|not|without|other than)\b[^.]*"
                rf"\b{re.escape(v.lower())}\b",
                lowered,
            )
            for v in values
        )
        if len(values) > 1:
            operator = "NOT IN" if negated else "IN"
        else:
            operator = "!=" if negated else "=="
        filters.append(
            {
                "column": column,
                "operator": operator,
                "value": values if len(values) > 1 else values[0],
            }
        )
    return filters


def _measures_in(
    lowered: str, src: SourceInfo
) -> tuple[list[str], list[tuple[int, ColumnInfo]]]:
    """Business metrics and measure columns named in the text."""
    business = [
        m for m in src.metrics if m in lowered or m.replace("_", " ") in lowered
    ]
    # Remove business metric names so their words ("gross" in gross_margin) are
    # not taken as synonyms of other columns
    rest = lowered
    for metric in business:
        rest = rest.replace(metric.replace("_", " "), " ").replace(metric, " ")
    return business, _find_columns(rest, src, ("measure",))


def _aggregate_for(text: str, col: Optional[ColumnInfo]) -> Optional[str]:
    lowered = text.lower()
    if re.search(r"\b(average|avg|mean)\b", lowered):
        return "avg"
    if re.search(r"\b(distinct|unique)\b", lowered):
        return "count_distinct"
    if re.search(r"\b(max|maximum|largest single)\b", lowered):
        return "max"
    if re.search(r"\b(min|minimum|smallest single)\b", lowered):
        return "min"
    if re.search(r"\b(total|sum)\b", lowered):
        return "sum"
    return col.agg if col is not None and col.kind == "measure" else None


def _detect_action(text: str) -> Optional[str]:
    lowered = text.lower().strip()
    if re.search(
        r"\b(save|add (it|this|that) to|add to (a |the )?dashboard|pin)\b", lowered
    ):
        return "save_chart"
    if re.search(
        r"\b(sql|query behind|show (the )?query|how (was|is) (it|this) calculated)\b",
        lowered,
    ):
        return "show_sql"
    if re.search(
        r"^(help|what (data|tables|fields|columns|can i ask)|"
        r"which (data|tables|fields)|"
        r"list (the )?(data|tables|fields|columns|sources))",
        lowered,
    ):
        return "describe_data"
    if re.fullmatch(
        r"(hi|hello|hey|thanks|thank you|ok|okay|cool|great)[!. ]*", lowered
    ):
        return "smalltalk"
    if re.search(r"\b(delete|drop|update|insert|truncate|alter)\b", lowered):
        return "unsupported"
    return None


def _derived_requests(  # noqa: C901
    lowered: str, src: SourceInfo
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Optional[str], str]:
    """
    Derived breakdowns and filters named in the text: (dimensions, filters,
    the measure being banded, the text with those phrases masked out).
    """
    dims: list[dict[str, Any]] = []
    filters: list[dict[str, Any]] = []
    banded: Optional[str] = None
    masked = lowered
    if src.time_col:
        for pattern, part in DERIVED_DIMS:
            match = re.search(pattern, masked)
            if match:
                dims.append({"derive": part, "column": src.time_col})
                masked = masked[: match.start()] + " " + masked[match.end() :]
        if not any(d["derive"] == "day_type" for d in dims):
            for pattern, value in DERIVED_FILTERS:
                match = re.search(pattern, masked)
                if match:
                    filters.append(
                        {
                            "column": src.time_col,
                            "derive": "day_type",
                            "operator": "==",
                            "value": value,
                        }
                    )
                    masked = masked[: match.start()] + " " + masked[match.end() :]
                    break
    found = find_edges(masked)
    if found:
        start, end, edges = found
        before = _find_columns(masked[:start], src, ("measure",))
        if before:
            banded = before[-1][1].name
            dims.append({"derive": "bucket", "column": banded, "edges": edges})
            # Drop the keyword and its edges ("bands 1000-5000" is not a period)
            masked = masked[:start] + " " + masked[end:]
    return dims, filters, banded, masked


def plan_with_rules(  # noqa: C901
    text: str, catalog: Catalog, previous: Optional[ResolvedSpec]
) -> ChartIntent:
    action = _detect_action(text)
    if action == "save_chart":
        match = re.search(
            r"(?:to|into|on) (?:the |my )?(?:dashboard )?"
            r"[\"']?(.+?)[\"']?(?: dashboard)?$",
            text,
            re.IGNORECASE,
        )
        name = match.group(1).strip() if match else None
        if name and name.lower() in ("dashboard", "a dashboard", "it", "this"):
            name = None
        title = re.search(r"\bas [\"']([^\"']+)[\"']", text)
        return ChartIntent(
            action="save_chart",
            dashboard=name,
            title=title.group(1) if title else None,
            reply="",
        )
    if action:
        return ChartIntent(action=action)

    lowered = text.lower().strip()
    # Rank without favouring the previous chart's source: a new question may be
    # about different data
    ranked = catalog.rank_sources(text)
    src = ranked[0] if ranked else None
    if src is None:
        return ChartIntent(action="describe_data")
    business, named_measures = _measures_in(lowered, src)
    counting = bool(re.search(r"\b(how many|number of|count of|count)\b", lowered))
    derived_dims, derived_filters, banded, masked = _derived_requests(lowered, src)

    # A message naming its own measure and breakdown is a new chart even when it
    # starts like a follow-up ("top 5 products by quantity")
    has_breakdown = bool(
        re.search(r"\b(by|per|over time|trend|monthly|weekly|daily)\b", lowered)
    ) or bool(derived_dims)
    self_contained = bool(named_measures or business or counting) and has_breakdown
    is_modify = (
        previous is not None
        and not self_contained
        and (bool(MODIFY_HINTS.search(lowered)) or len(lowered.split()) <= 5)
    )
    if is_modify and previous is not None:
        prev_src = catalog.find_source(previous.source)
        if prev_src is not None and prev_src.name != src.name:
            src = prev_src
            business, named_measures = _measures_in(lowered, src)
            derived_dims, derived_filters, banded, masked = _derived_requests(
                lowered, src
            )
    if banded:
        # The banded measure is the breakdown; count rows unless another is named
        named_measures = [(p, c) for p, c in named_measures if c.name != banded]
        counting = counting or not (named_measures or business)

    data: dict[str, Any] = (
        {**previous.to_intent_dict(), "action": "modify_chart", "title": None}
        if is_modify and previous is not None
        else {"action": "create_chart", "source": src.name}
    )
    changed = False

    # Chart type
    for pattern, chart in CHART_WORDS:
        if re.search(pattern, lowered):
            data["chart_type"] = chart
            changed = True
            break

    # Time grain and period ("by month of year" is a breakdown, not a grain)
    for pattern, grain in GRAIN_WORDS:
        if re.search(pattern, masked):
            data["time_grain"] = grain
            changed = True
            break
    for pattern in RANGE_PATTERNS:
        match = re.search(pattern, masked)
        if match and parse_time_range(match.group(0)):
            data["time_range"] = match.group(0).strip()
            changed = True
            break

    # Top / bottom N
    match = re.search(r"\b(top|bottom|best|worst|highest|lowest) (\d+)\b", lowered)
    if match:
        data["top_n"] = int(match.group(2))
        data["sort"] = (
            "asc" if match.group(1) in ("bottom", "worst", "lowest") else "desc"
        )
        changed = True

    metrics: list[dict[str, Any]] = [{"business_metric": b} for b in business]
    for _, col in named_measures:
        metrics.append({"column": col.name, "aggregate": _aggregate_for(text, col)})
    if not metrics and counting:
        # The resolver turns this into a distinct count when an entity is named
        metrics.append({"column": None, "aggregate": "count"})
    if metrics:
        data["metrics"] = metrics[:3]
        changed = True

    # Breakdowns and value filters
    context = (
        [*previous.dimensions, *(f.column for f in previous.filters)]
        if is_modify and previous is not None
        else []
    )
    filters = _value_filters(text, src, context) + derived_filters
    filtered_columns = {f["column"] for f in filters}
    parts = re.split(
        r"\b(?:by|per|for each|across|split by|broken down by)\b", lowered, maxsplit=1
    )
    measure_names = {c.name for _, c in named_measures}

    def breakdowns(segment: str) -> list[ColumnInfo]:
        return [
            col
            for _, col in _find_columns(
                segment, src, ("dimension", "time"), synonyms=False
            )
            if col.name not in filtered_columns and col.name not in measure_names
        ]

    # "revenue by region" names the breakdown after "by"; "top 5 products by
    # revenue" and "best selling genre" name it before
    ranking = re.search(
        r"\b(?:top|best|worst|bottom|highest|lowest|most|least|leading)\b", lowered
    )
    found = breakdowns(parts[1]) if len(parts) > 1 else []
    if not found and (len(parts) > 1 or data.get("top_n") or ranking):
        found = breakdowns(parts[0])
    dims: list[Any] = []
    for col in found:
        if col.kind == "time":
            if not derived_dims:
                data.setdefault("time_grain", "auto")
        elif col.name not in dims:
            dims.append(col.name)
    dims = [*derived_dims, *dims]
    if dims:
        data["dimensions"] = dims[:2]
        changed = True
    elif not is_modify:
        data["dimensions"] = []
    if filters:
        if is_modify:
            kept = [
                f
                for f in data.get("filters", [])
                if f["column"] not in filtered_columns
            ]
            data["filters"] = kept + filters
        else:
            data["filters"] = filters
        changed = True
    if is_modify and re.search(
        r"\b(remove|clear|without|drop) (the |all )?filters?\b", lowered
    ):
        data["filters"] = []
        changed = True
    if is_modify and re.search(
        r"\b(all time|show all|remove (the )?(date|time|period))\b", lowered
    ):
        data["time_range"] = None
        changed = True

    if not changed or (
        not is_modify and not metrics and not dims and not data.get("time_grain")
    ):
        return ChartIntent(
            action="clarify",
            question="I couldn't tell what to chart from that. Which measure would you "
            "like to see, and broken down by what?",
            options=catalog.suggestions(4),
        )
    return ChartIntent(**data)
