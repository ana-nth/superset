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
The structured intent the planner (LLM or rules) produces for one chat message.

Models are deliberately lenient: small local models emit metrics as strings,
operators as words and time ranges as phrases, so the validators coerce those
shapes instead of rejecting the whole answer.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from superset.ai_chat.derived import (
    canonical_name,
    DATE_PARTS,
    normalize_derive,
    split_name,
)

ACTIONS = (
    "create_chart",
    "modify_chart",
    "save_chart",
    "show_sql",
    "describe_data",
    "clarify",
    "smalltalk",
    "unsupported",
)

AGGREGATES = ("sum", "avg", "count", "count_distinct", "min", "max")
AGG_ALIASES = {
    "average": "avg",
    "mean": "avg",
    "median": "avg",
    "total": "sum",
    "countd": "count_distinct",
    "count distinct": "count_distinct",
    "distinct": "count_distinct",
    "distinct_count": "count_distinct",
    "unique": "count_distinct",
    "nunique": "count_distinct",
    "maximum": "max",
    "highest": "max",
    "minimum": "min",
    "lowest": "min",
    "number": "count",
    "number of": "count",
    "cnt": "count",
}

CHART_TYPES = (
    "auto",
    "big_number",
    "line",
    "bar",
    "area",
    "pie",
    "table",
    "heatmap",
    "histogram",
    "treemap",
    "funnel",
    "scatter",
)
CHART_ALIASES = {
    "kpi": "big_number",
    "number": "big_number",
    "big number": "big_number",
    "metric": "big_number",
    "donut": "pie",
    "doughnut": "pie",
    "column": "bar",
    "bar_chart": "bar",
    "barchart": "bar",
    "line_chart": "line",
    "trend": "line",
    "timeseries": "line",
    "time_series": "line",
    "grid": "table",
    "list": "table",
    "pivot": "table",
    "heat_map": "heatmap",
    "distribution": "histogram",
    "bubble": "scatter",
    "correlation": "scatter",
}

OPERATORS = (
    "==",
    "!=",
    ">",
    ">=",
    "<",
    "<=",
    "IN",
    "NOT IN",
    "LIKE",
    "ILIKE",
    "IS NULL",
    "IS NOT NULL",
)
OPERATOR_ALIASES = {
    "=": "==",
    "eq": "==",
    "equals": "==",
    "is": "==",
    "<>": "!=",
    "ne": "!=",
    "not": "!=",
    "not equals": "!=",
    "is not": "!=",
    "gt": ">",
    "greater than": ">",
    "gte": ">=",
    "ge": ">=",
    "lt": "<",
    "less than": "<",
    "lte": "<=",
    "le": "<=",
    "in": "IN",
    "not_in": "NOT IN",
    "not in": "NOT IN",
    "nin": "NOT IN",
    "contains": "ILIKE",
    "like": "ILIKE",
    "ilike": "ILIKE",
    "is_null": "IS NULL",
    "null": "IS NULL",
    "is null": "IS NULL",
    "is_not_null": "IS NOT NULL",
    "not null": "IS NOT NULL",
    "is not null": "IS NOT NULL",
}

GRAINS = {
    "hour": "PT1H",
    "day": "P1D",
    "week": "P1W",
    "month": "P1M",
    "quarter": "P3M",
    "year": "P1Y",
}
GRAIN_ALIASES = {
    "hourly": "hour",
    "daily": "day",
    "days": "day",
    "weekly": "week",
    "weeks": "week",
    "monthly": "month",
    "months": "month",
    "quarterly": "quarter",
    "quarters": "quarter",
    "yearly": "year",
    "annual": "year",
    "annually": "year",
    "years": "year",
    "auto": "auto",
}

MAX_METRICS = 5
MAX_FILTERS = 10
MAX_DIMENSIONS = 3


def _clean_name(value: Any) -> Optional[str]:
    """Column names may arrive as dicts ({"column": "x"}) or with table prefixes."""
    if isinstance(value, dict):
        value = value.get("column") or value.get("name") or value.get("column_name")
    if value is None:
        return None
    text = str(value).strip().strip("`\"'")
    if not text or text.lower() in ("null", "none"):
        return None
    # "fact_sales.region" → "region"
    if "." in text and " " not in text:
        text = text.rsplit(".", 1)[1]
    return text


class MetricIntent(BaseModel):
    """An aggregate over a column, or a named business metric."""

    model_config = ConfigDict(extra="ignore")

    column: Optional[str] = None
    aggregate: str = "sum"
    business_metric: Optional[str] = None
    label: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def coerce(cls, value: Any) -> Any:
        if isinstance(value, str):
            # "SUM(line_amount)", "count(*)", "avg line_amount", "line_amount"
            text = value.strip()
            match = re.fullmatch(
                r"([A-Za-z_ ]+)\s*\(\s*(distinct\s+)?([^)]*)\)", text, re.IGNORECASE
            )
            if match:
                agg = match.group(1).strip().lower()
                if match.group(2):
                    agg = "count_distinct"
                col = match.group(3).strip()
                return {"aggregate": agg, "column": None if col == "*" else col}
            return {"column": text}
        if isinstance(value, dict):
            data = dict(value)
            data.setdefault("aggregate", data.pop("agg", None) or "sum")
            if "column" not in data and "name" in data:
                data["column"] = data.pop("name")
            return data
        return value

    @field_validator("column", "business_metric", mode="before")
    @classmethod
    def clean_column(cls, value: Any) -> Optional[str]:
        name = _clean_name(value)
        return None if name in ("*",) else name

    @field_validator("aggregate", mode="before")
    @classmethod
    def normalize_aggregate(cls, value: Any) -> str:
        text = str(value or "sum").strip().lower().replace("-", "_")
        text = AGG_ALIASES.get(text, text)
        return text if text in AGGREGATES else "sum"

    @field_validator("label", mode="before")
    @classmethod
    def clean_label(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        text = re.sub(r"\s+", " ", str(value)).strip()
        return text[:80] or None


class DerivedIntent(BaseModel):
    """
    A breakdown computed from a column: a date part (weekday, month of year…),
    numeric bands, or named groups of a dimension's values.
    """

    model_config = ConfigDict(extra="ignore")

    derive: str
    column: Optional[str] = None  # None: the source's main date (date parts)
    edges: list[float] = []
    groups: dict[str, list[str]] = {}
    other: str = "Other"

    @model_validator(mode="before")
    @classmethod
    def coerce(cls, value: Any) -> Any:
        if isinstance(value, str):
            parsed = parse_derived_call(value)
            return parsed.model_dump() if parsed else {"derive": value}
        if isinstance(value, dict):
            data = dict(value)
            for alias in ("derived", "part", "function", "fn", "type", "kind"):
                if "derive" not in data and alias in data:
                    data["derive"] = data.pop(alias)
            for alias in ("col", "field", "source_column", "of"):
                if "column" not in data and alias in data:
                    data["column"] = data.pop(alias)
            for alias in ("bands", "bins", "breaks", "thresholds", "ranges"):
                if "edges" not in data and alias in data:
                    data["edges"] = data.pop(alias)
            return data
        return value

    @field_validator("derive", mode="before")
    @classmethod
    def normalize(cls, value: Any) -> str:
        derive = normalize_derive(value)
        if derive is None:
            raise ValueError(f"unknown derivation {value!r}")
        return derive

    @field_validator("column", mode="before")
    @classmethod
    def clean_column(cls, value: Any) -> Optional[str]:
        return _clean_name(value)

    @field_validator("edges", mode="before")
    @classmethod
    def parse_edges(cls, value: Any) -> list[float]:
        """[100, 500], "0-50, 50-200", [[0, 50], [50, 200]] → sorted numbers."""
        if value is None:
            return []
        if isinstance(value, (int, float)):
            value = [value]
        if isinstance(value, str):
            # "1,000-5,000" → 1000, 5000; a "-" between numbers is a range
            value = re.sub(r"(?<=\d),(?=\d{3}\b)", "", value)
            value = re.findall(r"\d+(?:\.\d+)?", value)
        numbers: list[float] = []
        for item in value if isinstance(value, list) else []:
            items = item if isinstance(item, (list, tuple)) else [item]
            for part in items:
                try:
                    numbers.append(float(str(part).replace(",", "")))
                except ValueError:
                    continue
        return sorted(set(numbers))

    @field_validator("groups", mode="before")
    @classmethod
    def parse_groups(cls, value: Any) -> dict[str, list[str]]:
        """{"East": ["NE", "SE"]} or [{"name": "East", "values": [...]}]."""
        if not value:
            return {}
        if isinstance(value, list):
            value = {
                str(g.get("name") or g.get("label") or f"Group {i + 1}"): g.get(
                    "values"
                )
                or g.get("members")
                or []
                for i, g in enumerate(value)
                if isinstance(g, dict)
            }
        if not isinstance(value, dict):
            return {}
        groups: dict[str, list[str]] = {}
        for name, members in value.items():
            items = members if isinstance(members, list) else [members]
            if isinstance(members, str):
                items = [m for m in re.split(r"\s*,\s*", members) if m]
            groups[str(name)] = [str(m) for m in items if m is not None]
        return groups

    @property
    def name(self) -> str:
        """How dimensions and filters refer to this derivation."""
        return canonical_name(self.column, self.derive) if self.column else self.derive


_CALL = re.compile(r"\s*([A-Za-z_ ]+?)\s*\(\s*([^()]+?)\s*\)\s*")


def parse_derived_call(text: str) -> Optional[DerivedIntent]:
    """
    Unambiguous derivations written as text: 'weekday(order_date)' or the
    canonical 'order_date__weekday'. Phrases ('order date weekday') need the
    catalog to rule out a real column and are parsed by the resolver.
    """
    match = _CALL.fullmatch(text)
    if match and normalize_derive(match.group(1)):
        return DerivedIntent(derive=match.group(1), column=match.group(2))
    split = split_name(text.strip())
    if split:
        return DerivedIntent(derive=split[1], column=split[0])
    return None


def parse_derived_phrase(text: str) -> Optional[DerivedIntent]:
    """
    'day of week of order_date', 'order_date weekday', 'unit_price bands',
    'day of week' (main date). Bare grain words ('month', 'year') are trends,
    not derivations, so they are left alone.
    """
    clean = re.sub(r"\s+", " ", str(text).strip().lower())
    if not clean:
        return None
    if clean in ("day", "week", "month", "quarter", "year", "hour"):
        return None
    whole = normalize_derive(clean)
    if whole in DATE_PARTS:
        return DerivedIntent(derive=whole)
    # "<part> of <column>": try every " of " so "day of week of x" works
    for match in re.finditer(r" of (?:the )?", clean):
        part, column = clean[: match.start()], clean[match.end() :]
        if normalize_derive(part) and column:
            return DerivedIntent(derive=part, column=column.replace(" ", "_"))
    # "<column> <part>", with the part up to three words long
    words = clean.split(" ")
    for size in (3, 2, 1):
        if len(words) <= size:
            continue
        part, column = " ".join(words[-size:]), "_".join(words[:-size])
        if normalize_derive(part):
            return DerivedIntent(derive=part, column=column)
    return None


def _extract_derived(item: Any, derivations: dict[str, Any]) -> Any:
    """Replace a derivation in a dimension or filter by its name, recording it."""
    try:
        if isinstance(item, dict):
            derived = DerivedIntent.model_validate(item)
        elif isinstance(item, str):
            derived = parse_derived_call(item)  # type: ignore[assignment]
            if derived is None:
                return item
        else:
            return item
    except ValueError:
        return item
    if derived.derive in ("bucket", "group") and not derived.column:
        return item
    derivations[derived.name] = derived
    return derived.name


def _extract_filter(flt: Any, derivations: dict[str, Any]) -> Any:
    """A filter on a derived value names the derivation in ``column``."""
    if not isinstance(flt, dict):
        return flt
    flt = dict(flt)
    column = flt.get("column")
    if isinstance(column, dict):
        flt["column"] = _extract_derived(column, derivations)
    elif flt.get("derive"):
        spec = {
            k: flt.pop(k) for k in ("derive", "edges", "groups", "other") if k in flt
        }
        flt["column"] = _extract_derived({**spec, "column": column}, derivations)
    elif isinstance(column, str):
        flt["column"] = _extract_derived(column, derivations)
    return flt


class FilterIntent(BaseModel):
    """A WHERE condition on one column."""

    model_config = ConfigDict(extra="ignore")

    column: str
    operator: str = "=="
    value: Any = None

    @model_validator(mode="before")
    @classmethod
    def coerce(cls, value: Any) -> Any:
        if isinstance(value, dict):
            data = dict(value)
            for alias in ("col", "field", "subject", "name"):
                if "column" not in data and alias in data:
                    data["column"] = data.pop(alias)
            for alias in ("op", "operation", "comparison"):
                if "operator" not in data and alias in data:
                    data["operator"] = data.pop(alias)
            for alias in ("val", "values", "comparator"):
                if "value" not in data and alias in data:
                    data["value"] = data.pop(alias)
            return data
        return value

    @field_validator("column", mode="before")
    @classmethod
    def clean_column(cls, value: Any) -> str:
        name = _clean_name(value)
        if not name:
            raise ValueError("filter column is required")
        return name

    @field_validator("operator", mode="before")
    @classmethod
    def normalize_operator(cls, value: Any) -> str:
        text = str(value or "==").strip()
        upper = text.upper()
        if upper in OPERATORS:
            return upper
        return OPERATOR_ALIASES.get(text.lower(), "==")


class ChartIntent(BaseModel):
    """What the user asked for, expressed against catalog names."""

    model_config = ConfigDict(extra="ignore")

    action: str = "create_chart"
    source: Optional[str] = None
    metrics: list[MetricIntent] = []
    dimensions: list[str] = []
    time_column: Optional[str] = None
    time_grain: Optional[str] = None
    time_range: Optional[str] = None
    filters: list[FilterIntent] = []
    chart_type: str = "auto"
    top_n: Optional[int] = None
    sort: str = "desc"
    title: Optional[str] = None
    reply: str = ""
    question: Optional[str] = None
    options: list[str] = []
    dashboard: Optional[str] = None
    # Derived breakdowns by name; dimensions and filter columns refer to them
    derivations: dict[str, DerivedIntent] = {}

    @model_validator(mode="before")
    @classmethod
    def extract_derivations(cls, value: Any) -> Any:
        """
        Dimensions and filters may be derivations ({"derive": "weekday",
        "column": "order_date"}); keep their names in place and their
        definitions in ``derivations``.
        """
        if not isinstance(value, dict):
            return value
        data = dict(value)
        derivations: dict[str, Any] = {}
        for name, item in (data.get("derivations") or {}).items():
            try:
                derivations[name] = DerivedIntent.model_validate(item)
            except ValueError:
                continue
        dims = data.get("dimensions")
        filters = data.get("filters")
        if isinstance(dims, (list, dict)):
            items = dims if isinstance(dims, list) else [dims]
            # {"derive": "weekday", "value": "Saturday"} is a filter, not a breakdown
            moved = [d for d in items if isinstance(d, dict) and "value" in d]
            if moved and (filters is None or isinstance(filters, list)):
                items = [d for d in items if not any(d is m for m in moved)]
                filters = [*(filters if isinstance(filters, list) else []), *moved]
            data["dimensions"] = [_extract_derived(d, derivations) for d in items]
        if isinstance(filters, list):
            data["filters"] = [_extract_filter(f, derivations) for f in filters]
        data["derivations"] = derivations
        return data

    @field_validator("action", mode="before")
    @classmethod
    def normalize_action(cls, value: Any) -> str:
        text = str(value or "create_chart").strip().lower().replace(" ", "_")
        aliases = {
            "create": "create_chart",
            "chart": "create_chart",
            "new_chart": "create_chart",
            "modify": "modify_chart",
            "update_chart": "modify_chart",
            "edit_chart": "modify_chart",
            "refine": "modify_chart",
            "save": "save_chart",
            "add_to_dashboard": "save_chart",
            "sql": "show_sql",
            "explain": "show_sql",
            "describe": "describe_data",
            "help": "describe_data",
            "list_data": "describe_data",
            "clarification": "clarify",
            "ask": "clarify",
            "greeting": "smalltalk",
            "chat": "smalltalk",
            "refuse": "unsupported",
            "out_of_scope": "unsupported",
        }
        text = aliases.get(text, text)
        return text if text in ACTIONS else "create_chart"

    @field_validator("source", "time_column", "dashboard", mode="before")
    @classmethod
    def clean_optional_name(cls, value: Any) -> Optional[str]:
        return _clean_name(value)

    @field_validator("metrics", mode="before")
    @classmethod
    def listify_metrics(cls, value: Any) -> list[Any]:
        if value is None:
            return []
        items = value if isinstance(value, list) else [value]
        return items[:MAX_METRICS]

    @field_validator("filters", mode="before")
    @classmethod
    def listify_filters(cls, value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, dict) and "column" not in value:
            # {"region": "North", "channel": ["A", "B"]}
            value = [
                {
                    "column": k,
                    "operator": "IN" if isinstance(v, list) else "==",
                    "value": v,
                }
                for k, v in value.items()
            ]
        items = value if isinstance(value, list) else [value]
        return [f for f in items if isinstance(f, dict)][:MAX_FILTERS]

    @field_validator("dimensions", mode="before")
    @classmethod
    def clean_dimensions(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [v for v in re.split(r"\s*,\s*", value) if v]
        names = [
            _clean_name(v) for v in (value if isinstance(value, list) else [value])
        ]
        unique: list[str] = []
        for name in names:
            if name and name not in unique:
                unique.append(name)
        return unique[:MAX_DIMENSIONS]

    @field_validator("time_grain", mode="before")
    @classmethod
    def normalize_grain(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip().lower()
        if text in ("", "null", "none"):
            return None
        if text in GRAINS.values():  # already ISO (P1M)
            return next(k for k, v in GRAINS.items() if v == text.upper())
        text = GRAIN_ALIASES.get(text, text)
        return text if text in GRAINS or text == "auto" else "auto"

    @field_validator("time_range", mode="before")
    @classmethod
    def normalize_range(cls, value: Any) -> Optional[str]:
        if value is None or isinstance(value, (dict, list)):
            if isinstance(value, dict):
                start, end = value.get("start"), value.get("end")
                if start or end:
                    return f"{start or ''} to {end or ''}".strip()
            return None
        text = str(value).strip()
        return (
            None if text.lower() in ("", "null", "none", "no filter", "all") else text
        )

    @field_validator("chart_type", mode="before")
    @classmethod
    def normalize_chart_type(cls, value: Any) -> str:
        text = str(value or "auto").strip().lower()
        text = text.replace(" chart", "").replace("_chart", "").strip()
        text = CHART_ALIASES.get(text, text)
        return text if text in CHART_TYPES else "auto"

    @field_validator("top_n", mode="before")
    @classmethod
    def clamp_top_n(cls, value: Any) -> Optional[int]:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return max(1, min(number, 500)) if number > 0 else None

    @field_validator("sort", mode="before")
    @classmethod
    def normalize_sort(cls, value: Any) -> str:
        return (
            "asc"
            if str(value or "").strip().lower() in ("asc", "ascending")
            else "desc"
        )

    @field_validator("title", "question", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        text = re.sub(r"\s+", " ", str(value)).strip()
        return text[:200] or None

    @field_validator("reply", mode="before")
    @classmethod
    def clean_reply(cls, value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()[:600]

    @field_validator("options", mode="before")
    @classmethod
    def clean_options(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(v).strip()[:120] for v in value if str(v).strip()][:6]


# ──────────────────────────────────────────────────────────────────────────────
# Time ranges
# ──────────────────────────────────────────────────────────────────────────────

_TODAY = 'DATETIME("today")'
_TOMORROW = 'DATEADD(DATETIME("today"), 1, day)'
_UNITS = ("day", "week", "month", "quarter", "year")
_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        )
    )
}


def _date(text: str) -> Optional[date]:
    text = text.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text)
        if re.fullmatch(r"\d{4}-\d{2}", text):
            return date.fromisoformat(f"{text}-01")
        if re.fullmatch(r"\d{4}", text):
            return date(int(text), 1, 1)
    except ValueError:
        return None
    return None


def _next_period(start: date, text: str) -> date:
    """End (exclusive) of the period that ``text`` names, starting at ``start``."""
    if re.fullmatch(r"\d{4}", text):
        return date(start.year + 1, 1, 1)
    if re.fullmatch(r"\d{4}-\d{2}", text):
        return date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return start + timedelta(days=1)


def parse_time_range(phrase: Optional[str]) -> Optional[tuple[str, str]]:  # noqa: C901
    """
    Convert a phrase ("last 30 days", "this year", "2024", "Q3 2024",
    "2024-01-01 to 2024-06-30") into ``(superset_time_range, human_label)``.

    Returns None when the phrase is not understood; callers report that instead
    of guessing a range.
    """
    if not phrase:
        return None
    text = re.sub(r"\s+", " ", phrase.strip().lower())
    text = text.replace("past", "last").replace("previous", "last")

    if text in ("all", "all time", "no filter", "everything"):
        return None

    match = re.fullmatch(
        r"(?:in the )?last (\d+) (day|week|month|quarter|year)s?", text
    )
    if match:
        n, unit = int(match.group(1)), match.group(2)
        return (
            f"DATEADD({_TODAY}, -{n}, {unit}) : {_TOMORROW}",
            f"last {n} {unit}{'s' if n != 1 else ''}",
        )

    match = re.fullmatch(r"(?:this|current) (day|week|month|quarter|year)", text)
    if match or text in ("today", "ytd", "year to date", "mtd", "month to date", "qtd"):
        unit = (
            match.group(1)
            if match
            else {
                "today": "day",
                "mtd": "month",
                "month to date": "month",
                "qtd": "quarter",
            }.get(text, "year")
        )
        return f"DATETRUNC({_TODAY}, {unit}) : {_TOMORROW}", f"this {unit}"

    match = re.fullmatch(r"(?:last|yesterday)(?: (day|week|month|quarter|year))?", text)
    if match and (match.group(1) or text == "yesterday"):
        unit = match.group(1) or "day"
        return (
            f"DATEADD(DATETRUNC({_TODAY}, {unit}), -1, {unit}) : "
            f"DATETRUNC({_TODAY}, {unit})",
            f"last {unit}",
        )

    match = re.fullmatch(r"q([1-4])\s*(?:of\s*)?(\d{4})", text) or re.fullmatch(
        r"(\d{4})\s*q([1-4])", text
    )
    if match:
        groups = match.groups()
        quarter, year = (
            (int(groups[0]), int(groups[1]))
            if len(groups[0]) == 1
            else (int(groups[1]), int(groups[0]))
        )
        start = date(year, 3 * (quarter - 1) + 1, 1)
        end = date(year + (quarter == 4), (3 * quarter) % 12 + 1, 1)
        return f"{start.isoformat()} : {end.isoformat()}", f"Q{quarter} {year}"

    match = re.fullmatch(r"([a-z]+) (\d{4})", text)
    if match and match.group(1) in _MONTHS:
        month, year = _MONTHS[match.group(1)], int(match.group(2))
        start = date(year, month, 1)
        end = date(year + (month == 12), month % 12 + 1, 1)
        return (
            f"{start.isoformat()} : {end.isoformat()}",
            f"{match.group(1).title()} {year}",
        )

    match = re.fullmatch(
        r"(?:from |between |since )?(\d{4}(?:-\d{2}){0,2})"
        r"(?:\s*(?:to|until|through|and|-|:|–)\s*(\d{4}(?:-\d{2}){0,2}))?",
        text,
    )
    if match:
        start_text, end_text = match.group(1), match.group(2)
        first = _date(start_text)
        if first is None:
            return None
        start = first
        if text.startswith("since"):
            return f"{start.isoformat()} : {_TOMORROW}", f"since {start_text}"
        if end_text:
            end_start = _date(end_text)
            if end_start is None or end_start < start:
                return None
            end = _next_period(end_start, end_text)
            return (
                f"{start.isoformat()} : {end.isoformat()}",
                f"{start_text} to {end_text}",
            )
        end = _next_period(start, start_text)
        return f"{start.isoformat()} : {end.isoformat()}", start_text

    return None
