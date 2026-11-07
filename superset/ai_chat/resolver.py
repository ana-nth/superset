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
Validate a ``ChartIntent`` against the catalog and turn it into a ``ResolvedSpec``.

Every name the planner produced is checked here. Small mistakes are repaired and
reported as notes (wrong case, near-miss spelling, SUM over a text column); real
gaps (an unknown column, an ambiguous filter value) become ``problems``, which
the service either sends back to the LLM for one repair round or turns into a
clarifying question for the user.
"""

from __future__ import annotations

import dataclasses
import difflib
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional

from superset.ai_chat import derived as dv
from superset.ai_chat.catalog import (
    _norm,
    _tokens,
    Catalog,
    ColumnInfo,
    PLACE_CODES,
    SourceInfo,
)
from superset.ai_chat.derived import DerivedDim, DerivedError
from superset.ai_chat.intent import (
    ChartIntent,
    DerivedIntent,
    GRAINS,
    MetricIntent,
    parse_derived_phrase,
    parse_time_range,
)
from superset.ai_dashboard.builder import _humanize

logger = logging.getLogger(__name__)

AGG_SQL = {
    "sum": "SUM({c})",
    "avg": "AVG({c})",
    "min": "MIN({c})",
    "max": "MAX({c})",
    "count": "COUNT({c})",
    "count_distinct": "COUNT(DISTINCT {c})",
}
AGG_LABEL = {
    "sum": "Total {l}",
    "avg": "Avg {l}",
    "min": "Min {l}",
    "max": "Max {l}",
    "count": "Count of {l}",
    "count_distinct": "Unique {l}",
}
HIGH_CARD_DEFAULT_TOP = 25
RANKED_TOP = 10
# "best selling game", "which genre": the answer is a ranked list of things
RANKING_WORDS = re.compile(
    r"\b(best|worst|top|which|most popular|least popular|best[- ]selling)\b", re.I
)
NAME_COLUMN = re.compile(r"(^|_)(name|title)$")
SERIES_LIMIT = 10


@dataclass
class ResolvedMetric:
    expr: str
    label: str
    column: Optional[str] = None
    aggregate: str = "sum"
    business: Optional[str] = None
    unit: str = ""


@dataclass
class ResolvedFilter:
    column: str
    operator: str
    value: Any = None

    def describe(self, label: Optional[str] = None) -> str:
        column = label or self.column
        if self.operator in ("IS NULL", "IS NOT NULL"):
            return f"{column} {self.operator.lower()}"
        if isinstance(self.value, list):
            shown = ", ".join(str(v) for v in self.value[:5])
            more = "…" if len(self.value) > 5 else ""
            word = "not in" if self.operator == "NOT IN" else "in"
            return f"{column} {word} {shown}{more}"
        op = {"==": "=", "ILIKE": "contains"}.get(self.operator, self.operator)
        value = str(self.value).strip("%") if self.operator == "ILIKE" else self.value
        return f"{column} {op} {value}"


@dataclass
class ResolvedSpec:
    source: str
    metrics: list[ResolvedMetric] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    time_column: Optional[str] = None
    time_grain: Optional[str] = None  # ISO 8601 duration (P1M)
    time_range: str = "No filter"
    time_range_label: str = ""
    filters: list[ResolvedFilter] = field(default_factory=list)
    chart_type: str = "auto"
    top_n: Optional[int] = None
    sort_desc: bool = True
    title: str = ""
    raw_column: Optional[str] = None  # histogram / raw listing column
    # Derived breakdowns used by dimensions or filters, by name
    derived: dict[str, DerivedDim] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResolvedSpec":
        return cls(
            **{
                **data,
                "metrics": [ResolvedMetric(**m) for m in data.get("metrics", [])],
                "filters": [ResolvedFilter(**f) for f in data.get("filters", [])],
                "derived": {
                    name: DerivedDim(**d)
                    for name, d in (data.get("derived") or {}).items()
                },
            }
        )

    def to_intent_dict(self) -> dict[str, Any]:
        """The spec in planner vocabulary, given to the LLM for follow-ups."""
        grain = next((k for k, v in GRAINS.items() if v == self.time_grain), None)
        return {
            "source": self.source,
            "metrics": [
                {"business_metric": m.business}
                if m.business
                else {"column": m.column, "aggregate": m.aggregate}
                for m in self.metrics
            ],
            "dimensions": [
                self.derived[d].to_intent() if d in self.derived else d
                for d in self.dimensions
            ],
            "time_grain": grain,
            "time_range": self.time_range_label or None,
            "filters": [
                {
                    **(
                        self.derived[f.column].to_intent()
                        if f.column in self.derived
                        else {"column": f.column}
                    ),
                    "operator": f.operator,
                    "value": f.value,
                }
                for f in self.filters
            ],
            "chart_type": self.chart_type,
            "top_n": self.top_n,
            "sort": "desc" if self.sort_desc else "asc",
            "title": self.title,
        }


def dim_label(spec: ResolvedSpec, name: str) -> str:
    """Display name of a dimension of ``spec``."""
    return spec.derived[name].label if name in spec.derived else _humanize(name)


def describe_filter(spec: ResolvedSpec, flt: ResolvedFilter) -> str:
    """'region = West'; derived filters by their label ('Weekday / Weekend = …')."""
    dim = spec.derived.get(flt.column)
    return flt.describe(dim.label if dim else None)


def dim_key(spec: ResolvedSpec, name: str) -> str:
    """Key of a dimension in result rows (adhoc columns are keyed by label)."""
    return spec.derived[name].label if name in spec.derived else name


def derived_column(dim: DerivedDim) -> ColumnInfo:
    """A catalog column standing in for a derived dimension."""
    return ColumnInfo(
        name=dim.name,
        kind="dimension",
        numeric=dim.numeric,
        description=dim.label,
        cardinality=len(dim.values) or 10,
        values=list(dim.values),
        display=dim.label,
    )


def with_derived(src: SourceInfo, spec: ResolvedSpec) -> SourceInfo:
    """``src`` plus the derived dimensions of ``spec`` (the catalog is untouched)."""
    if not spec.derived:
        return src
    columns = dict(src.columns)
    for name, dim in spec.derived.items():
        columns.setdefault(name, derived_column(dim))
    return dataclasses.replace(src, columns=columns)


@dataclass
class Resolution:
    spec: Optional[ResolvedSpec] = None
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    # Clarification for the user when problems remain
    question: str = ""
    options: list[str] = field(default_factory=list)
    # Columns moved from no-op filters to breakdowns
    promoted: list[str] = field(default_factory=list)
    # Measures named by a place used as a filter value ("Japan" → jp_sales)
    place_measures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.spec is not None and not self.problems


# ──────────────────────────────────────────────────────────────────────────────
# Name matching
# ──────────────────────────────────────────────────────────────────────────────


def match_column(  # noqa: C901
    name: str, source: SourceInfo, kinds: Optional[tuple[str, ...]] = None
) -> tuple[Optional[ColumnInfo], list[str]]:
    """
    Find ``name`` in ``source``: exact, normalised, humanised, close spelling, then
    description keywords ("revenue" → line_amount "Net revenue USD").
    Returns (column or None, suggestions).
    """
    candidates = [
        c for c in source.columns.values() if kinds is None or c.kind in kinds
    ]
    if not candidates:
        return None, []
    if name in source.columns and (kinds is None or source.columns[name].kind in kinds):
        return source.columns[name], []
    key = _norm(name)
    for col in candidates:
        if key in (_norm(col.name), _norm(col.label)):
            return col, []
    names = {_norm(c.name): c for c in candidates}
    close = difflib.get_close_matches(key, list(names), n=3, cutoff=0.82)
    if close:
        return names[close[0]], []
    # Word overlap with name + description, e.g. "revenue", "sales amount"
    words = set(_tokens(name))
    if words:
        scored = []
        for col in candidates:
            name_words = set(_tokens(col.name))
            desc_words = set(_tokens(col.description))
            score = len(words & name_words) * 2 + len(words & desc_words)
            if score:
                scored.append((score, col))
        scored.sort(key=lambda x: -x[0])
        if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            return scored[0][1], []
        if scored:
            return None, [c.name for _, c in scored[:4]]
    loose = difflib.get_close_matches(key, list(names), n=4, cutoff=0.5)
    return None, [names[n].name for n in loose]


def match_value(value: Any, column: ColumnInfo) -> tuple[Any, bool]:
    """Map a filter value onto the column's known values (case/spelling)."""
    if not column.values:
        return value, True
    text = str(value).strip()
    for known in column.values:
        if known == text:
            return known, True
    lowered = {_norm(v): v for v in column.values}
    if _norm(text) in lowered:
        return lowered[_norm(text)], True
    close = difflib.get_close_matches(_norm(text), list(lowered), n=1, cutoff=0.8)
    if close:
        return lowered[close[0]], True
    initials = _initials(text)
    if len(initials) >= 2 and initials in lowered:
        return lowered[initials], True  # "PlayStation 4" → PS4
    return value, not column.values_complete


def _initials(text: str) -> str:
    """'PlayStation 4' → 'ps4', 'Game Boy Advance' → 'gba'."""
    words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", text)
    if len(words) < 2:
        return ""
    return "".join(w if w.isdigit() else w[0] for w in words).lower()


def _closest_values(value: Any, column: ColumnInfo, n: int = 6) -> list[str]:
    lowered = {_norm(v): v for v in column.values}
    key = _norm(str(value))
    close = difflib.get_close_matches(key, list(lowered), n=n, cutoff=0.3)
    close += [k for k in lowered if k not in close and (key in k or k in key)]
    picked = [lowered[k] for k in close][:n]
    return picked + [v for v in column.values if v not in picked][: n - len(picked)]


def _coerce_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[,$%\s]", "", str(value))
    multiplier = 1.0
    if text[-1:].lower() in ("k", "m", "b"):
        multiplier = {"k": 1e3, "m": 1e6, "b": 1e9}[text[-1].lower()]
        text = text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Resolution
# ──────────────────────────────────────────────────────────────────────────────


def _base_name(name: str, intent: ChartIntent) -> Optional[str]:
    """The column a (possibly derived) dimension reads; None for the main date."""
    derived = intent.derivations.get(name)
    if derived is not None:
        return derived.column
    split = dv.split_name(name)
    if split:
        return split[0]
    phrase = parse_derived_phrase(name)
    if phrase is not None:
        return phrase.column
    return name


def _referenced_names(intent: ChartIntent) -> list[str]:
    names = [m.column for m in intent.metrics if m.column]
    names += [m.business_metric for m in intent.metrics if m.business_metric]
    for name in [*intent.dimensions, *(f.column for f in intent.filters)]:
        base = _base_name(name, intent)
        if base:
            names.append(base)
    if intent.time_column:
        names.append(intent.time_column)
    return names


def _is_column(name: str, src: SourceInfo) -> bool:
    """True when ``name`` is a real column of ``src`` (not a derivation phrase)."""
    if name in src.columns:
        return True
    key = _norm(name)
    return any(key in (_norm(c.name), _norm(c.label)) for c in src.columns.values())


def _derivation_for(
    name: str,
    intent: ChartIntent,
    src: SourceInfo,
    previous: Optional[ResolvedSpec],
) -> Optional[DerivedIntent]:
    asked = intent.derivations.get(name)
    if previous is not None and name in previous.derived:
        # The planner copied the previous chart's name ("unit_price__bucket")
        # without its edges or groups: keep the previous definition
        if asked is None or not (asked.edges or asked.groups):
            prev = previous.derived[name]
            return DerivedIntent.model_validate(prev.to_intent())
    if asked is not None:
        return asked
    if _is_column(name, src):
        return None
    split = dv.split_name(name)
    if split:
        return DerivedIntent(derive=split[1], column=split[0])
    return parse_derived_phrase(name)


DERIVE_KINDS = {"bucket": ("measure", "dimension"), "group": ("dimension",)}


def _compile(  # noqa: C901
    derived: DerivedIntent,
    src: SourceInfo,
    quote: Callable[[str], str],
    dialect: str,
    res: Resolution,
    question: str = "",
) -> Optional[DerivedDim]:
    """Check ``derived`` against the catalog and compile it, or record a problem."""
    kind = derived.derive
    if kind in dv.DATE_PARTS:
        name = derived.column or src.time_col
        if not name:
            res.problems.append(
                f"{src.label} has no date column to take the "
                f"{dv.PART_LABELS[kind].lower()} from"
            )
            return None
        col, suggestions = match_column(name, src, ("time",))
        if col is None:
            dates = [c.name for c in src.of_kind("time")]
            res.problems.append(
                f"'{name}' is not a date column of {src.name}"
                + (f"; did you mean {', '.join(dates)}?" if dates else "")
            )
            return None
        qualify = col.name != src.time_col
        return dv.compile_date_part(
            kind,
            col.name,
            quote,
            dialect,
            column_label=col.label if qualify else "",
            time_min=src.time_min if not qualify else "",
            time_max=src.time_max if not qualify else "",
        )

    col, suggestions = match_column(derived.column or "", src, DERIVE_KINDS[kind])
    if col is None:
        res.problems.append(
            f"Unknown column '{derived.column}' in {src.name}"
            + (f"; did you mean {', '.join(suggestions)}?" if suggestions else "")
        )
        return None
    try:
        if kind == "bucket":
            if not col.numeric:
                res.problems.append(f"{col.label} is not numeric, so it has no bands")
                return None
            edges = derived.edges
            typed = dv.find_edges(question) if question else None
            if typed and typed[2] and set(edges) < set(typed[2]):
                # Small models drop edges; the ones the user typed win
                edges = typed[2]
                shown = ", ".join(dv.fmt_edge(e) for e in edges)
                res.notes.append(f"Used the band edges you gave: {shown}.")
            elif typed and typed[2] and not edges:
                edges = typed[2]
            return dv.compile_bucket(
                col.name,
                quote,
                edges,
                column_label=col.label,
                lo=col.min_value,
                hi=col.max_value,
            )
        groups: dict[str, list[str]] = {}
        for group, members in derived.groups.items():
            matched: list[str] = []
            for member in members:
                value, known = match_value(member, col)
                if not known or not col.values:
                    shown = ", ".join(_closest_values(member, col, 10))
                    res.problems.append(
                        f"'{member}' is not a value of {col.name} (values: {shown})"
                    )
                    continue
                matched.append(str(value))
            groups[group] = matched
        if res.problems:
            return None
        return dv.compile_group(
            col.name, quote, groups, column_label=col.label, other=derived.other
        )
    except DerivedError as exc:
        res.problems.append(f"Can't make {col.label} bands or groups: {exc}")
        return None


def _named_days_over_day_type(
    spec: ResolvedSpec,
    src: SourceInfo,
    quote: Callable[[str], str],
    dialect: str,
    derived: dict[str, DerivedDim],
    question: str,
    res: Resolution,
) -> None:
    """'Only Saturdays' read as Weekend also keeps Sundays; filter the named days."""
    asked = question.lower()
    days = [d for d in dv.WEEKDAY_NAMES if re.search(rf"\b{d}s?\b", asked)]
    if not days or re.search(r"\bweek ?ends?\b", asked):
        return
    for i, flt in enumerate(spec.filters):
        dim = derived.get(flt.column)
        if dim is None or dim.derive != "day_type":
            continue
        weekday = _compile(
            DerivedIntent(derive="weekday", column=dim.column), src, quote, dialect, res
        )
        if weekday is None:
            return
        derived[weekday.name] = weekday
        src.columns[weekday.name] = derived_column(weekday)
        values = [weekday.values[dv.WEEKDAY_NAMES.index(d)] for d in days]
        spec.filters[i] = (
            ResolvedFilter(weekday.name, "==", values[0])
            if len(values) == 1
            else ResolvedFilter(weekday.name, "IN", values)
        )
        res.notes.append(f"Filtered to {', '.join(d.title() for d in days)} only.")
        return


def _prepare_derived(  # noqa: C901
    intent: ChartIntent,
    src: SourceInfo,
    quote: Callable[[str], str],
    dialect: str,
    previous: Optional[ResolvedSpec],
    res: Resolution,
    question: str = "",
) -> tuple[dict[str, DerivedDim], dict[str, str], set[str]]:
    """
    Compile the derivations the intent's dimensions and filters use and add
    them to ``src.columns`` (a per-request copy). Returns (derived by name,
    intent name → derived name, intent names that failed and were reported).
    """
    derived: dict[str, DerivedDim] = {}
    renamed: dict[str, str] = {}
    failed: set[str] = set()
    for name in [*intent.dimensions, *(f.column for f in intent.filters)]:
        if name in renamed or name in failed:
            continue
        spec = _derivation_for(name, intent, src, previous)
        if spec is None:
            continue
        dim = _compile(spec, src, quote, dialect, res, question)
        if dim is None:
            failed.add(name)
            continue
        derived[dim.name] = dim
        renamed[name] = dim.name
        src.columns[dim.name] = derived_column(dim)
    return derived, renamed, failed


def _pick_source(
    intent: ChartIntent,
    catalog: Catalog,
    previous: Optional[ResolvedSpec],
    notes: list[str],
) -> Optional[SourceInfo]:
    """Use the named source when it has the columns; otherwise the best fit."""
    names = _referenced_names(intent)

    def coverage(src: SourceInfo) -> int:
        hits = 0
        for name in names:
            col, _ = match_column(name, src)
            if col is not None or _match_business(name, src) is not None:
                hits += 1
        return hits

    named = catalog.find_source(intent.source)
    if named is not None and (not names or coverage(named) == len(names)):
        return named
    if named is None and previous is not None and intent.action == "modify_chart":
        prev = catalog.find_source(previous.source)
        if prev is not None and (not names or coverage(prev) == len(names)):
            return prev
    if not names:
        return (
            named
            or (catalog.find_source(previous.source) if previous else None)
            or catalog.default_source()
        )

    ranked = sorted(
        catalog.sources.values(),
        key=lambda s: (
            coverage(s),
            previous is not None and s.name == previous.source,
            s.name == (named.name if named else ""),
            s.row_count,
        ),
        reverse=True,
    )
    best = ranked[0]
    if (
        named is not None
        and best.name != named.name
        and coverage(best) > coverage(named)
    ):
        notes.append(f"Used {best.label} because it has the requested fields.")
    return best


def _match_business(name: str, src: SourceInfo) -> Optional[str]:
    if not src.metrics or not name:
        return None
    key = _norm(name)
    for metric in src.metrics:
        if key in (_norm(metric), _norm(_humanize(metric))):
            return metric
    close = difflib.get_close_matches(
        key, [_norm(m) for m in src.metrics], n=1, cutoff=0.8
    )
    if close:
        return next(m for m in src.metrics if _norm(m) == close[0])
    return None


def _entity_label(col: ColumnInfo) -> str:
    """customer_id → Customers (what a distinct count of the column counts)."""
    label = re.sub(r"\s+(Id|Key|Code|No)$", "", col.label)
    return label if label.endswith("s") else f"{label}s"


def _entity_column(
    question: str, src: SourceInfo, exclude: list[str]
) -> Optional[ColumnInfo]:
    """The column a "how many <things>" question counts: customers → customer_id."""
    words = set(_tokens(question))
    matches = [
        col
        for col in src.of_kind("dimension")
        if col.name not in exclude
        and _tokens(col.name)[:1]
        and _tokens(col.name)[0] in words
    ]
    # Prefer identifiers, then the most granular column
    matches.sort(key=lambda c: (not c.name.endswith("_id"), -c.cardinality))
    return matches[0] if matches else None


def _resolve_metric(  # noqa: C901
    metric: MetricIntent,
    src: SourceInfo,
    quote: Callable[[str], str],
    res: Resolution,
    question: str = "",
    dimensions: Optional[list[str]] = None,
) -> Optional[ResolvedMetric]:
    # Business metric, named as such or as a "column" that is not a real column
    business = _match_business(metric.business_metric or "", src)
    if business is None and metric.column and metric.column not in src.columns:
        business = _match_business(metric.column, src)
    if business is not None:
        spec = src.metrics[business]
        unit = spec.get("unit", "")
        label = metric.label or _humanize(business) + (
            f" ({unit})" if unit == "%" else ""
        )
        return ResolvedMetric(
            expr=spec["sql"],
            label=label,
            aggregate="custom",
            business=business,
            unit=unit,
        )
    if metric.business_metric and not metric.column:
        res.problems.append(
            f"'{metric.business_metric}' is not a business metric of {src.name}; "
            f"available: {', '.join(src.metrics) or 'none'}"
        )
        return None

    agg = metric.aggregate
    if not metric.column:
        entity = (
            _entity_column(question, src, dimensions or [])
            if agg in ("count", "count_distinct")
            else None
        )
        if entity is not None and (
            agg == "count_distinct" or entity.name.endswith("_id")
        ):
            return ResolvedMetric(
                expr=AGG_SQL["count_distinct"].format(c=quote(entity.name)),
                label=metric.label or f"Unique {_entity_label(entity)}",
                column=entity.name,
                aggregate="count_distinct",
            )
        if agg != "count":
            res.notes.append(f"No column given for {agg}; counted rows instead.")
        return ResolvedMetric(
            expr="COUNT(*)", label=metric.label or "Count", aggregate="count"
        )

    col, suggestions = match_column(metric.column, src)
    if col is None:
        res.problems.append(
            f"Unknown column '{metric.column}' in {src.name}"
            + (f"; did you mean {', '.join(suggestions)}?" if suggestions else "")
        )
        return None
    if col.name != metric.column and _norm(col.name) != _norm(metric.column):
        res.notes.append(f"Interpreted '{metric.column}' as {col.label}.")

    if not col.numeric and agg in ("sum", "avg"):
        agg = "count_distinct"
        res.notes.append(f"{col.label} is not numeric, so I counted distinct values.")
    expr = AGG_SQL[agg].format(c=quote(col.name))
    if agg == "count_distinct":
        default_label = f"Unique {_entity_label(col)}"
    else:
        default_label = AGG_LABEL[agg].format(l=col.label)
    label = metric.label or default_label
    return ResolvedMetric(
        expr=expr, label=label, column=col.name, aggregate=agg, unit=col.unit
    )


def _auto_grain(src: SourceInfo, time_range: str) -> str:
    """Pick a grain that gives a readable number of points for the period."""
    start: Optional[date] = None
    end: Optional[date] = None
    try:
        start = date.fromisoformat(src.time_min) if src.time_min else None
        end = date.fromisoformat(src.time_max) if src.time_max else None
    except ValueError:
        pass
    if time_range != "No filter":
        try:
            from superset.utils.date_parser import get_since_until

            since, until = get_since_until(time_range)
            if since:
                start = max(start, since.date()) if start else since.date()
            if until:
                end = min(end, until.date()) if end else until.date()
        except Exception:  # pylint: disable=broad-except
            logger.debug("Could not bound the period %s", time_range, exc_info=True)
    if not start or not end:
        return GRAINS["month"]
    days = (end - start).days
    if days <= 62:
        return GRAINS["day"]
    if days <= 200:
        return GRAINS["week"]
    if days <= 365 * 6:
        return GRAINS["month"]
    return GRAINS["year"]


def _validate_range(time_range: str) -> bool:
    try:
        from superset.utils.date_parser import get_since_until

        get_since_until(time_range)
        return True
    except Exception:  # pylint: disable=broad-except
        return False


def _resolve_filters(  # noqa: C901
    intent: ChartIntent,
    src: SourceInfo,
    res: Resolution,
    derived: Optional[dict[str, DerivedDim]] = None,
    renamed: Optional[dict[str, str]] = None,
    failed: Optional[set[str]] = None,
) -> tuple[list[ResolvedFilter], Optional[str]]:
    """Returns filters and, when a filter targets a date column, a time phrase."""
    derived = derived or {}
    renamed = renamed or {}
    filters: list[ResolvedFilter] = []
    time_phrase: Optional[str] = None
    for flt in intent.filters:
        if flt.column in (failed or set()):
            continue
        col, suggestions = match_column(renamed.get(flt.column, flt.column), src)
        places = _measures_for_place(flt.value, src)
        if places and flt.operator in ("==", "IN") and not _is_value_of(flt.value, col):
            # "Japan" is the jp_sales measure, not a value of platform
            res.place_measures += [m.name for m in places]
            target = f"a {col.label} value" if col else "a column"
            res.notes.append(
                f"“{flt.value}” is not {target}; I used {places[0].label} for it."
            )
            continue
        if col is None:
            # A filter on a value without a column ("region": "North" → the column
            # may simply be misnamed); look the value up in every dimension
            owner = _column_owning_value(flt.value, src)
            if owner is None:
                res.problems.append(
                    f"Unknown filter column '{flt.column}' in {src.name}"
                    + (
                        f"; did you mean {', '.join(suggestions)}?"
                        if suggestions
                        else ""
                    )
                )
                continue
            col = owner
        op = flt.operator
        value = flt.value

        if col.kind == "time":
            if op in ("IS NULL", "IS NOT NULL"):
                filters.append(ResolvedFilter(col.name, op))
            elif isinstance(value, str) and parse_time_range(value):
                time_phrase = value
            elif op in (">", ">=") and value:
                time_phrase = f"since {str(value)[:10]}"
            elif op in ("<", "<=") and value:
                time_phrase = f"1900-01-01 to {str(value)[:10]}"
            else:
                res.notes.append(f"Ignored the date filter on {col.name} ({value}).")
            continue

        if op in ("IS NULL", "IS NOT NULL"):
            filters.append(ResolvedFilter(col.name, op))
            continue

        if isinstance(value, str) and op in ("IN", "NOT IN"):
            value = [v.strip() for v in value.split(",") if v.strip()]
        if isinstance(value, list) and op in ("==", "!="):
            op = "IN" if op == "==" else "NOT IN"
        if value is None or value == [] or value == "":
            res.notes.append(f"Ignored the filter on {col.name}: no value given.")
            continue

        dim = derived.get(col.name)
        if op == "ILIKE" and dim is not None:
            # Derived values are known: "contains" becomes the matching values
            text = str(value[0] if isinstance(value, list) else value).strip("%")
            value = [v for v in dim.values if text.lower() in v.lower()] or [text]
            op = "IN"
        if op == "ILIKE":
            text = str(value[0] if isinstance(value, list) else value).strip("%")
            filters.append(ResolvedFilter(col.name, "ILIKE", f"%{text}%"))
            continue

        if col.numeric and op in (">", ">=", "<", "<=", "==", "!="):
            number = _coerce_number(value)
            if number is None:
                res.problems.append(f"'{value}' is not a number for {col.name}")
                continue
            filters.append(
                ResolvedFilter(
                    col.name, op, int(number) if number.is_integer() else number
                )
            )
            continue

        values = value if isinstance(value, list) else [value]
        matched: list[Any] = []
        for item in values:
            if dim is not None:
                hit = dv.match_value(dim, item)
                mapped, known = (hit, True) if hit is not None else (item, False)
            else:
                mapped, known = match_value(item, col)
            if not known:
                shown = ", ".join(_closest_values(item, col, 10))
                res.problems.append(
                    f"'{item}' is not a value of {col.name} (values: {shown})"
                )
                res.options = res.options or [
                    f"{col.label}: {v}" for v in _closest_values(item, col)
                ]
                continue
            if mapped not in matched:
                matched.append(mapped)
        if not matched:
            continue
        if op == "IN" and col.values_complete and set(matched) >= set(col.values):
            # Keeps every value: the user meant "by <column>", not a filter
            res.notes.append(
                f"Split by {col.label} instead of filtering on all its values."
            )
            res.promoted.append(col.name)
            continue
        if op in ("IN", "NOT IN"):
            filters.append(ResolvedFilter(col.name, op, matched))
        elif len(matched) > 1:
            filters.append(
                ResolvedFilter(col.name, "IN" if op == "==" else "NOT IN", matched)
            )
        else:
            filters.append(ResolvedFilter(col.name, op, matched[0]))
    return filters, time_phrase


def _is_value_of(value: Any, col: Optional[ColumnInfo]) -> bool:
    if col is None:
        return False
    items = value if isinstance(value, list) else [value]
    known = {_norm(v) for v in col.values}
    return any(_norm(str(item)) in known for item in items)


def _measures_for_place(value: Any, src: SourceInfo) -> list[ColumnInfo]:
    """Measures limited to the place a filter value names ('Japan' → jp_sales)."""
    items = value if isinstance(value, list) else [value]
    if len(items) != 1 or isinstance(items[0], dict) or items[0] is None:
        return []
    key = _norm(str(items[0]))
    return [
        c
        for c in src.of_kind("measure")
        if c.place
        and key
        in {_norm(c.place)} | {code for code, p in PLACE_CODES.items() if p == c.place}
    ]


def _apply_place_measure(
    spec: ResolvedSpec,
    src: SourceInfo,
    quote: Callable[[str], str],
    res: Resolution,
) -> None:
    """Use the place's own measure in place of the closest metric asked for."""
    names = res.place_measures
    if any(m.column in names for m in spec.metrics):
        return
    target = spec.metrics[0] if spec.metrics else None
    base = set(_tokens(target.column)) if target and target.column else set()
    col = max(
        (src.columns[n] for n in names),
        key=lambda c: len(base & set(_tokens(c.name))),
    )
    if target is not None and target.aggregate not in ("sum", "avg", "min", "max"):
        # Counts and business metrics keep their meaning, limited to the place
        spec.filters.append(ResolvedFilter(col.name, ">", 0))
        return
    agg = target.aggregate if target is not None else col.agg
    metric = ResolvedMetric(
        expr=AGG_SQL[agg].format(c=quote(col.name)),
        label=AGG_LABEL[agg].format(l=col.label),
        column=col.name,
        aggregate=agg,
        unit=col.unit,
    )
    if target is not None:
        spec.metrics[0] = metric
    else:
        spec.metrics.append(metric)


def _ranking_dimension(question: str, src: SourceInfo) -> Optional[ColumnInfo]:
    """The thing being ranked: a dimension the question names, else the name column."""
    words = set(_tokens(question))
    dims = src.of_kind("dimension")
    named = [
        d for d in dims if words & (set(_tokens(d.name)) | set(_tokens(d.description)))
    ]
    if len(named) == 1:
        return named[0]
    names = [d for d in dims if NAME_COLUMN.search(d.name.lower())]
    return max(names, key=lambda d: d.cardinality) if names else None


def _rank_instead_of_extreme(
    spec: ResolvedSpec,
    src: SourceInfo,
    quote: Callable[[str], str],
    question: str,
    grain: Optional[str],
    res: Resolution,
) -> Optional[int]:
    """'Best selling game' answered with MAX(sales) hides the game; rank totals."""
    if grain or not spec.metrics or not RANKING_WORDS.search(question):
        return None
    metric = spec.metrics[0]
    col = src.columns.get(metric.column or "")
    if metric.aggregate not in ("max", "min") or col is None or col.agg != "sum":
        return None
    dim = None
    if not spec.dimensions:
        dim = _ranking_dimension(question, src)
        if dim is None:
            return None
        spec.dimensions = [dim.name]
    spec.metrics[0] = ResolvedMetric(
        expr=AGG_SQL["sum"].format(c=quote(col.name)),
        label=AGG_LABEL["sum"].format(l=col.label),
        column=col.name,
        aggregate="sum",
        unit=col.unit,
    )
    if dim is None:
        res.notes.append(f"Ranked by total {col.label}.")
        return None
    res.notes.append(f"Ranked {dim.label} by {col.label} to show which one leads.")
    return RANKED_TOP


def _drop_pinned_breakdowns(
    spec: ResolvedSpec, src: SourceInfo, res: Resolution
) -> None:
    """'By channel' with 'channel = Retail' splits by one value; drop the split."""
    for flt in spec.filters:
        single = flt.operator == "==" or (
            flt.operator == "IN" and isinstance(flt.value, list) and len(flt.value) == 1
        )
        if single and flt.column in spec.dimensions and len(spec.dimensions) > 1:
            spec.dimensions.remove(flt.column)
            label = src.columns[flt.column].label
            res.notes.append(f"Removed the {label} breakdown: it has one value.")


def _count_place_rows(spec: ResolvedSpec, src: SourceInfo) -> None:
    """'How many games sold in Japan' counts rows with Japan sales, not every row."""
    for i, metric in enumerate(spec.metrics):
        col = src.columns.get(metric.column or "")
        if metric.aggregate != "count" or col is None or not col.place:
            continue
        spec.metrics[i] = dataclasses.replace(
            metric, expr="COUNT(*)", label="Count", column=None
        )
        if not any(f.column == col.name for f in spec.filters):
            spec.filters.append(ResolvedFilter(col.name, ">", 0))


def _column_owning_value(value: Any, src: SourceInfo) -> Optional[ColumnInfo]:
    if value is None or isinstance(value, (list, dict)):
        return None
    key = _norm(str(value))
    owners = [
        c for c in src.of_kind("dimension") if any(_norm(v) == key for v in c.values)
    ]
    return owners[0] if len(owners) == 1 else None


def resolve(  # noqa: C901
    intent: ChartIntent,
    catalog: Catalog,
    quote: Callable[[str], str],
    previous: Optional[ResolvedSpec] = None,
    question: str = "",
    dialect: str = "postgresql",
) -> Resolution:
    """Validate and normalise ``intent`` into a ResolvedSpec (see module doc)."""
    res = Resolution()
    src = _pick_source(intent, catalog, previous, res.notes)
    if src is None:
        res.problems.append("No data source is available in this schema.")
        return res
    # Derived dimensions join the columns of a copy, never the cached catalog
    src = dataclasses.replace(src, columns=dict(src.columns))
    derived, renamed, failed = _prepare_derived(
        intent, src, quote, dialect, previous, res, question
    )

    spec = ResolvedSpec(source=src.name, chart_type=intent.chart_type)

    # ── dimensions / time axis ───────────────────────────────────────────────
    grain = intent.time_grain
    time_col: Optional[str] = None
    if intent.time_column:
        col, _ = match_column(intent.time_column, src, ("time",))
        if col is None:
            res.notes.append(
                f"'{intent.time_column}' is not a date column; "
                f"used {src.time_col or 'none'}."
            )
        else:
            time_col = col.name
    for name in intent.dimensions:
        if name in failed:
            continue
        if (
            name not in renamed
            and name.lower() in GRAINS
            and src.time_col
            and not _is_column(name, src)
        ):
            # "dimensions": ["month"] asks for a monthly trend
            time_col = time_col or src.time_col
            grain = grain or name.lower()
            continue
        name = renamed.get(name, name)
        col, suggestions = match_column(name, src)
        if col is None:
            res.problems.append(
                f"Unknown dimension '{name}' in {src.name}"
                + (f"; did you mean {', '.join(suggestions)}?" if suggestions else "")
            )
            continue
        if col.kind == "time":
            # "by order_date" means a time axis
            time_col = col.name
            grain = grain or "auto"
            continue
        if col.kind == "measure" and col.cardinality > 100:
            res.notes.append(
                f"{col.label} has {col.cardinality} distinct values; grouping by it "
                "may be hard to read."
            )
        if col.name not in spec.dimensions:
            spec.dimensions.append(col.name)
    if len(spec.dimensions) > 2:
        dropped = spec.dimensions[2:]
        spec.dimensions = spec.dimensions[:2]
        res.notes.append(
            f"Charts support two breakdowns; dropped {', '.join(dropped)}."
        )

    # ── metrics ──────────────────────────────────────────────────────────────
    for metric in intent.metrics:
        resolved = _resolve_metric(metric, src, quote, res, question, spec.dimensions)
        if resolved and all(m.expr != resolved.expr for m in spec.metrics):
            spec.metrics.append(resolved)

    ranked_top = _rank_instead_of_extreme(spec, src, quote, question, grain, res)

    if grain:
        time_col = time_col or src.time_col or None
        if time_col is None:
            res.notes.append(
                f"{src.label} has no date column, so no time axis is shown."
            )
            grain = None

    # ── filters & time range ─────────────────────────────────────────────────
    spec.filters, phrase_from_filter = _resolve_filters(
        intent, src, res, derived, renamed, failed
    )
    _named_days_over_day_type(spec, src, quote, dialect, derived, question, res)
    if res.place_measures:
        _apply_place_measure(spec, src, quote, res)
    _count_place_rows(spec, src)
    _drop_pinned_breakdowns(spec, src, res)
    for name in res.promoted:
        if name not in spec.dimensions and len(spec.dimensions) < 2:
            spec.dimensions.append(name)
    phrase = intent.time_range or phrase_from_filter
    if phrase:
        parsed = parse_time_range(phrase)
        range_col = time_col or src.time_col
        if parsed is None:
            res.notes.append(
                f"I couldn't understand the period '{phrase}', so I used all dates."
            )
        elif not range_col:
            res.notes.append(f"{src.label} has no date column; ignored '{phrase}'.")
        elif not _validate_range(parsed[0]):
            res.notes.append(f"The period '{phrase}' is invalid; used all dates.")
        else:
            spec.time_range, spec.time_range_label = parsed
            time_col = time_col or range_col
    spec.time_column = time_col
    asked = question.lower()
    if (
        grain in ("hour", "day", "week")
        and question
        and not re.search(rf"\b({grain}|{grain}s|{grain}ly|daily|per {grain})\b", asked)
    ):
        grain = "auto"  # the planner guessed a fine grain the user never asked for
    if grain:
        spec.time_grain = (
            _auto_grain(src, spec.time_range) if grain == "auto" else GRAINS[grain]
        )

    # ── histogram / raw column ───────────────────────────────────────────────
    if intent.chart_type == "histogram":
        raw = next((m.column for m in spec.metrics if m.column), None)
        if raw is None:
            raw = src.main_measure or None
        spec.raw_column = raw

    # ── defaults ─────────────────────────────────────────────────────────────
    if not spec.metrics and not res.problems and intent.chart_type != "histogram":
        main = src.columns.get(src.main_measure)
        if main is not None:
            spec.metrics.append(
                ResolvedMetric(
                    expr=AGG_SQL[main.agg].format(c=quote(main.name)),
                    label=AGG_LABEL[main.agg].format(l=main.label),
                    column=main.name,
                    aggregate=main.agg,
                    unit=main.unit,
                )
            )
            res.notes.append(f"No measure was named, so I used {main.label}.")
        else:
            spec.metrics.append(
                ResolvedMetric(expr="COUNT(*)", label="Count", aggregate="count")
            )

    spec.top_n = intent.top_n or ranked_top
    spec.sort_desc = intent.sort != "asc"
    if ranked_top and re.search(r"\b(worst|least)\b", question, re.I):
        spec.sort_desc = False
    if spec.dimensions and not spec.top_n and not spec.time_grain:
        first = src.columns[spec.dimensions[0]]
        if first.cardinality > 50:
            spec.top_n = HIGH_CARD_DEFAULT_TOP
            res.notes.append(
                f"{first.label} has {first.cardinality} values; showing the top "
                f"{HIGH_CARD_DEFAULT_TOP}."
            )

    used = {*spec.dimensions, *(f.column for f in spec.filters)}
    spec.derived = {name: d for name, d in derived.items() if name in used}
    spec.title = intent.title or default_title(spec, src)
    if res.problems:
        res.question = _question_for(res.problems, question)
        if not res.options:
            res.options = _options_for(res.problems)
        res.spec = spec
        return res
    res.spec = spec
    return res


def default_title(spec: ResolvedSpec, src: SourceInfo) -> str:
    """'Total Line Amount by Region (monthly, last year)'."""
    names = " & ".join(m.label for m in spec.metrics[:2]) or "Rows"
    if spec.raw_column:
        names = f"Distribution of {_humanize(spec.raw_column)}"
    parts = [names]
    if spec.dimensions:
        parts.append("by " + " & ".join(dim_label(spec, d) for d in spec.dimensions))
    if spec.top_n and spec.dimensions:
        parts.insert(0, f"{'Top' if spec.sort_desc else 'Bottom'} {spec.top_n}")
    grain_word = {
        "PT1H": "hourly",
        "P1D": "daily",
        "P1W": "weekly",
        "P1M": "monthly",
        "P3M": "quarterly",
        "P1Y": "yearly",
    }.get(spec.time_grain or "")
    qualifiers = [q for q in (grain_word, spec.time_range_label) if q]
    title = " ".join(parts)
    if qualifiers:
        title += f" ({', '.join(qualifiers)})"
    return title[:200]


def _question_for(problems: list[str], question: str) -> str:
    first = problems[0]
    if "not a value of" in first:
        return f"I couldn't match a filter value. {first}. Which one did you mean?"
    if first.startswith("Unknown"):
        return f"I couldn't find some of the fields you mentioned. {first}."
    return f"I need a bit more detail: {first}."


def _options_for(problems: list[str]) -> list[str]:
    options: list[str] = []
    for problem in problems:
        match = re.search(r"did you mean (.+)\?$", problem)
        if match:
            options += [o.strip() for o in match.group(1).split(",")]
    return [_humanize(o) for o in options][:6]
