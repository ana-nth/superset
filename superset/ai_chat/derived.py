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
Derived dimensions: breakdowns computed from a column instead of read from one.

"Orders by day of week", "weekend vs weekday", "revenue by month of year" and
"order lines by price band" group by values that no column holds. The planner
names a derivation from a fixed menu (``DERIVES``) and this module compiles it
to SQL for the database's dialect. The LLM never writes the expression.

Every derivation produces a known, ordered list of values. Labels carry their
order ("1 Mon" … "7 Sun", "01 Jan" … "12 Dec", "1. under 100" …), so sorting
them alphabetically gives the natural order in every chart.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

DATE_PARTS = ("year", "quarter", "month", "weekday", "day_type", "hour")
DERIVES = (*DATE_PARTS, "bucket", "group")

# Words small models (and users) use for each derivation
DERIVE_ALIASES = {
    "dow": "weekday",
    "day_of_week": "weekday",
    "dayofweek": "weekday",
    "day_name": "weekday",
    "weekday_name": "weekday",
    "week_day": "weekday",
    "month_of_year": "month",
    "month_name": "month",
    "moy": "month",
    "calendar_month": "month",
    "quarter_of_year": "quarter",
    "qoy": "quarter",
    "yr": "year",
    "calendar_year": "year",
    "hour_of_day": "hour",
    "time_of_day": "hour",
    "weekend": "day_type",
    "weekends": "day_type",
    "is_weekend": "day_type",
    "weekend_weekday": "day_type",
    "weekday_weekend": "day_type",
    "weekend_vs_weekday": "day_type",
    "daytype": "day_type",
    "band": "bucket",
    "bands": "bucket",
    "bin": "bucket",
    "bins": "bucket",
    "buckets": "bucket",
    "range": "bucket",
    "ranges": "bucket",
    "bracket": "bucket",
    "brackets": "bucket",
    "tier": "bucket",
    "tiers": "bucket",
    "groups": "group",
    "grouping": "group",
    "custom_group": "group",
}

MAX_EDGES = 8  # at most 9 bands, so "1." … "9." sort correctly
BUCKET_WORDS = re.compile(
    r"\b(?:bands?|ranges?|buckets?|brackets?|tiers?|bins?)\b", re.IGNORECASE
)
# The numbers right after a band word: "bands 0-50, 50-200 and 1,000"
_EDGE_RUN = re.compile(
    r"(?:\s*(?:of|at|:)?\s*\d[\d.,]*(?:\s*(?:-|–|to|and|,)\s*)?)+", re.IGNORECASE
)
MAX_GROUPS = 8
DEFAULT_BANDS = 5

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
MONTH_NAMES = (
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
PART_LABELS = {
    "year": "Year",
    "quarter": "Quarter",
    "month": "Month of Year",
    "weekday": "Day of Week",
    "day_type": "Weekday / Weekend",
    "hour": "Hour of Day",
}


class DerivedError(ValueError):
    """The derivation cannot be applied (wrong column type, bad edges…)."""


@dataclass
class DerivedDim:
    """A compiled derivation: its SQL and the values it can produce."""

    name: str  # "order_date__weekday": how specs and filters refer to it
    derive: str
    column: str
    label: str  # shown in charts; Superset also keys result rows by it
    sql: str
    values: list[str] = field(default_factory=list)
    numeric: bool = False  # SQL yields numbers (year), not text labels
    params: dict[str, Any] = field(default_factory=dict)

    def to_intent(self) -> dict[str, Any]:
        """The derivation in planner vocabulary (for follow-up prompts)."""
        data: dict[str, Any] = {"derive": self.derive, "column": self.column}
        if self.derive == "bucket":
            data["edges"] = self.params.get("edges", [])
        elif self.derive == "group":
            data["groups"] = self.params.get("groups", {})
            data["other"] = self.params.get("other", "Other")
        return data


# ──────────────────────────────────────────────────────────────────────────────
# Names
# ──────────────────────────────────────────────────────────────────────────────


def normalize_derive(value: Any) -> Optional[str]:
    """'Day of week', 'DOW', 'weekend' → a member of DERIVES, else None."""
    if value is None:
        return None
    text = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
    if text in DERIVES:
        return text
    return DERIVE_ALIASES.get(text)


def canonical_name(column: str, derive: str) -> str:
    return f"{column}__{derive}"


def split_name(name: str) -> Optional[tuple[str, str]]:
    """'order_date__weekday' → ('order_date', 'weekday')."""
    if "__" not in name:
        return None
    column, _, suffix = name.rpartition("__")
    derive = normalize_derive(suffix)
    if not column or derive is None:
        return None
    return column, derive


# ──────────────────────────────────────────────────────────────────────────────
# SQL
# ──────────────────────────────────────────────────────────────────────────────


def family(dialect: str) -> str:
    """SQL family of a SQLAlchemy dialect name."""
    dialect = (dialect or "").lower()
    if dialect in ("mysql", "mariadb"):
        return "mysql"
    if dialect in ("sqlite", "mssql", "oracle", "bigquery"):
        return dialect
    return "postgresql"


def _mod(expr: str, n: int, fam: str) -> str:
    if fam in ("sqlite", "mssql"):
        return f"(({expr}) % {n})"
    return f"MOD({expr}, {n})"


def _date_number(part: str, col: str, fam: str) -> str:  # noqa: C901
    """
    SQL for the number of ``part`` in the (quoted) column ``col``: ISO weekday
    1 (Monday) to 7 (Sunday), month 1–12, quarter 1–4, year, hour 0–23.
    """
    if part in ("weekday", "day_type"):
        if fam == "mysql":
            return f"(WEEKDAY({col}) + 1)"
        if fam == "oracle":
            return f"(TRUNC({col}) - TRUNC({col}, 'IW') + 1)"
        # Shift each dialect's day number so that (n + shift) mod 7 + 1 is ISO
        if fam == "sqlite":
            raw = "CAST(STRFTIME('%w', " + col + ") AS INTEGER) + 6"  # 0=Sunday
        elif fam == "mssql":
            raw = f"DATEPART(weekday, {col}) + @@DATEFIRST - 2"
        elif fam == "bigquery":
            raw = f"EXTRACT(DAYOFWEEK FROM {col}) + 5"  # 1=Sunday
        else:
            # DOW is 0=Sunday in PostgreSQL, Redshift, DuckDB and Snowflake and
            # 1=Monday…7=Sunday in Trino; (dow + 6) mod 7 + 1 is ISO for both
            raw = f"CAST(EXTRACT(DOW FROM {col}) AS INTEGER) + 6"
        return f"({_mod(raw, 7, fam)} + 1)"
    unit = {"month": "MONTH", "quarter": "QUARTER", "year": "YEAR", "hour": "HOUR"}[
        part
    ]
    if fam == "mysql":
        return f"{unit}({col})"
    if fam == "sqlite":
        fmt = {"month": "%m", "year": "%Y", "hour": "%H"}
        if part == "quarter":
            return f"((CAST(STRFTIME('%m', {col}) AS INTEGER) + 2) / 3)"
        return f"CAST(STRFTIME('{fmt[part]}', {col}) AS INTEGER)"
    if fam == "mssql":
        return f"DATEPART({part}, {col})"
    if fam == "oracle":
        if part == "quarter":
            return f"TO_NUMBER(TO_CHAR({col}, 'Q'))"
        if part == "hour":
            return f"TO_NUMBER(TO_CHAR({col}, 'HH24'))"
        return f"EXTRACT({unit} FROM {col})"
    if fam == "bigquery":
        return f"EXTRACT({unit} FROM {col})"
    return f"CAST(EXTRACT({unit} FROM {col}) AS INTEGER)"


def sql_literal(value: Any, numeric: bool = False) -> str:
    """A SQL literal for a value the catalog or this module produced."""
    if numeric:
        number = float(value)
        if not math.isfinite(number):
            raise DerivedError(f"{value!r} is not a number")
        return str(int(number)) if number.is_integer() else repr(number)
    return "'" + str(value).replace("'", "''") + "'"


def _case(number: str, labels: dict[int, str]) -> str:
    whens = " ".join(f"WHEN {k} THEN {sql_literal(v)}" for k, v in labels.items())
    return f"CASE {number} {whens} END"


def _years(time_min: str, time_max: str) -> list[str]:
    try:
        first, last = int(time_min[:4]), int(time_max[:4])
    except (TypeError, ValueError):
        return []
    if last < first or last - first > 200:
        return []
    return [str(y) for y in range(first, last + 1)]


def compile_date_part(
    part: str,
    column: str,
    quote: Callable[[str], str],
    dialect: str,
    column_label: str = "",
    time_min: str = "",
    time_max: str = "",
) -> DerivedDim:
    """Weekday, month, quarter, year, hour or weekday/weekend of a date column."""
    if part not in DATE_PARTS:
        raise DerivedError(f"'{part}' is not a date part")
    fam = family(dialect)
    number = _date_number(part, quote(column), fam)
    label = PART_LABELS[part]
    if column_label:
        label = f"{column_label} {label}"
    numeric = False
    values: list[str]
    if part == "weekday":
        mapping = {i + 1: f"{i + 1} {d}" for i, d in enumerate(WEEKDAYS)}
        sql, values = _case(number, mapping), list(mapping.values())
    elif part == "day_type":
        sql = f"CASE WHEN {number} IN (6, 7) THEN 'Weekend' ELSE 'Weekday' END"
        values = ["Weekday", "Weekend"]
    elif part == "month":
        mapping = {i + 1: f"{i + 1:02d} {m}" for i, m in enumerate(MONTHS)}
        sql, values = _case(number, mapping), list(mapping.values())
    elif part == "quarter":
        mapping = {q: f"Q{q}" for q in range(1, 5)}
        sql, values = _case(number, mapping), list(mapping.values())
    elif part == "hour":
        # Zero-padded text so 09 sorts before 10 in every chart
        mapping = {h: f"{h:02d}" for h in range(24)}
        sql, values = _case(number, mapping), list(mapping.values())
    else:  # year
        sql, values, numeric = number, _years(time_min, time_max), True
    return DerivedDim(
        name=canonical_name(column, part),
        derive=part,
        column=column,
        label=label,
        sql=sql,
        values=values,
        numeric=numeric,
    )


def _nice_step(raw: float) -> float:
    exponent = math.floor(math.log10(raw))
    base = 10.0**exponent
    for factor in (1, 2, 2.5, 5, 10):
        if raw <= factor * base:
            return factor * base
    return 10 * base


def nice_edges(lo: float, hi: float, bands: int = DEFAULT_BANDS) -> list[float]:
    """Round band edges that split [lo, hi] into about ``bands`` parts."""
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        return []
    step = _nice_step((hi - lo) / bands)
    edge = math.floor(lo / step) * step + step
    edges: list[float] = []
    while edge < hi and len(edges) < MAX_EDGES:
        edges.append(round(edge, 10))
        edge += step
    return edges


def find_edges(text: str) -> Optional[tuple[int, int, list[float]]]:
    """
    Band edges written after a band word: (start of the word, end of the
    numbers, edges); None when the text has no band word.
    """
    word = BUCKET_WORDS.search(text)
    if word is None:
        return None
    run = _EDGE_RUN.match(text, word.end())
    end = run.end() if run else word.end()
    numbers = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text[word.end() : end])
    edges = sorted({float(n) for n in re.findall(r"\d+(?:\.\d+)?", numbers)})
    return word.start(), end, edges


def fmt_edge(value: float) -> str:
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _clean_edges(edges: list[Any]) -> list[float]:
    numbers: list[float] = []
    for edge in edges:
        try:
            number = float(edge)
        except (TypeError, ValueError) as exc:
            raise DerivedError(f"band edge {edge!r} is not a number") from exc
        if not math.isfinite(number):
            raise DerivedError(f"band edge {edge!r} is not a number")
        numbers.append(number)
    numbers = sorted(set(numbers))
    if len(numbers) > MAX_EDGES:
        raise DerivedError(f"use at most {MAX_EDGES} band edges")
    return numbers


def compile_bucket(
    column: str,
    quote: Callable[[str], str],
    edges: Optional[list[Any]] = None,
    column_label: str = "",
    lo: Optional[float] = None,
    hi: Optional[float] = None,
) -> DerivedDim:
    """Numeric bands: ``CASE WHEN c < e1 THEN '1. under e1' … END``."""
    clean = _clean_edges(edges or [])
    if lo is not None:
        # An edge at or below the minimum makes an empty first band ("under 0")
        clean = [e for e in clean if e > lo]
    if hi is not None:
        clean = [e for e in clean if e <= hi] or clean[:1]
    if not clean:
        if lo is None or hi is None:
            raise DerivedError(
                f"I don't know the range of {column_label or column}; "
                "give band edges such as 100, 500"
            )
        clean = nice_edges(lo, hi)
        if not clean:
            raise DerivedError(f"{column_label or column} has a single value")
    col = quote(column)
    labels = [f"1. under {fmt_edge(clean[0])}"]
    first = sql_literal(clean[0], True)
    whens = [f"WHEN {col} < {first} THEN {sql_literal(labels[0])}"]
    for i in range(1, len(clean)):
        label = f"{i + 1}. {fmt_edge(clean[i - 1])}-{fmt_edge(clean[i])}"
        labels.append(label)
        whens.append(
            f"WHEN {col} < {sql_literal(clean[i], True)} THEN {sql_literal(label)}"
        )
    last = f"{len(clean) + 1}. {fmt_edge(clean[-1])}+"
    labels.append(last)
    sql = f"CASE WHEN {col} IS NULL THEN NULL {' '.join(whens)} ELSE {sql_literal(last)} END"  # noqa: E501
    return DerivedDim(
        name=canonical_name(column, "bucket"),
        derive="bucket",
        column=column,
        label=f"{column_label or column} Band",
        sql=sql,
        values=labels,
        params={"edges": [int(e) if e.is_integer() else e for e in clean]},
    )


def clean_group_name(name: Any) -> str:
    text = re.sub(r"[^\w &/+.-]", "", str(name)).strip()
    return re.sub(r"\s+", " ", text)[:40]


def compile_group(
    column: str,
    quote: Callable[[str], str],
    groups: dict[str, list[str]],
    column_label: str = "",
    other: str = "Other",
) -> DerivedDim:
    """
    Named groups of a dimension's values. ``groups`` must hold values already
    validated against the catalog: they are inlined as literals.
    """
    if not groups:
        raise DerivedError("no groups were given")
    if len(groups) > MAX_GROUPS:
        raise DerivedError(f"use at most {MAX_GROUPS} groups")
    col = quote(column)
    whens: list[str] = []
    kept: dict[str, list[str]] = {}
    seen: set[str] = set()
    for raw_name, members in groups.items():
        name = clean_group_name(raw_name)
        # A value belongs to the first group listing it
        members = [m for m in members if m not in seen]
        if not name or not members or name in kept:
            continue
        seen.update(members)
        kept[name] = members
        literals = ", ".join(sql_literal(m) for m in members)
        whens.append(f"WHEN {col} IN ({literals}) THEN {sql_literal(name)}")
    if not whens:
        raise DerivedError("the groups have no values")
    other_name = clean_group_name(other) or "Other"
    if other_name in kept:
        other_name = f"{other_name} (rest)"
    sql = f"CASE {' '.join(whens)} ELSE {sql_literal(other_name)} END"
    return DerivedDim(
        name=canonical_name(column, "group"),
        derive="group",
        column=column,
        label=f"{column_label or column} Group",
        sql=sql,
        values=[*kept, other_name],
        params={"groups": kept, "other": other_name},
    )


def filter_sql(dim: DerivedDim, operator: str, value: Any) -> str:
    """WHERE condition on a derived dimension (values come from ``dim.values``)."""
    expr = f"({dim.sql})"
    if operator in ("IS NULL", "IS NOT NULL"):
        return f"{expr} {operator}"
    if operator in ("IN", "NOT IN"):
        items = value if isinstance(value, list) else [value]
        literals = ", ".join(sql_literal(v, dim.numeric) for v in items)
        return f"{expr} {operator} ({literals})"
    op = {"==": "=", "!=": "<>"}.get(operator, operator)
    if op not in ("=", "<>", ">", ">=", "<", "<="):
        raise DerivedError(f"operator {operator} is not supported on {dim.label}")
    return f"{expr} {op} {sql_literal(value, dim.numeric)}"


# ──────────────────────────────────────────────────────────────────────────────
# Values
# ──────────────────────────────────────────────────────────────────────────────


def _ordinal(text: str, names: tuple[str, ...], short: tuple[str, ...]) -> int:
    """'Monday', 'mon', '1', '1 Mon' → 1-based index, or 0."""
    match = re.fullmatch(r"0?(\d{1,2})(?:\s+\w+)?", text)
    if match:
        number = int(match.group(1))
        return number if 1 <= number <= len(names) else 0
    for i, (full, abbr) in enumerate(zip(names, short, strict=False)):
        if text in (full, abbr.lower()) or (len(text) >= 3 and full.startswith(text)):
            return i + 1
    return 0


def match_value(dim: DerivedDim, value: Any) -> Optional[str]:  # noqa: C901
    """The value of ``dim`` a user wrote ('Saturday' → '6 Sat'), else None."""
    text = re.sub(r"\s+", " ", str(value).strip().lower())
    text = text.rstrip("s") if dim.derive == "day_type" else text
    for known in dim.values:
        if known.lower() == text:
            return known
    if dim.derive == "weekday":
        index = _ordinal(text, WEEKDAY_NAMES, WEEKDAYS)
        return dim.values[index - 1] if index else None
    if dim.derive == "month":
        index = _ordinal(text, MONTH_NAMES, MONTHS)
        return dim.values[index - 1] if index else None
    if dim.derive == "quarter":
        match = re.fullmatch(r"q?([1-4])", text)
        return f"Q{match.group(1)}" if match else None
    if dim.derive == "day_type":
        if text in ("weekend", "sat", "sun", "saturday", "sunday", "sat/sun"):
            return "Weekend"
        if text in ("weekday", "week day", "work day", "workday", "business day"):
            return "Weekday"
        return None
    if dim.derive == "hour":
        match = re.fullmatch(r"(\d{1,2})(?::00)?\s*(am|pm)?", text)
        if not match:
            return None
        hour = int(match.group(1))
        if match.group(2) == "pm" and hour < 12:
            hour += 12
        if match.group(2) == "am" and hour == 12:
            hour = 0
        return f"{hour:02d}" if 0 <= hour <= 23 else None
    if dim.derive == "year":
        match = re.fullmatch(r"(\d{4})", text)
        return match.group(1) if match else None
    for known in dim.values:  # bucket "2", group names in any case
        if re.sub(r"[^a-z0-9]", "", known.lower()) == re.sub(r"[^a-z0-9]", "", text):
            return known
        if dim.derive == "bucket" and known.split(".", 1)[0] == text:
            return known
    return None
