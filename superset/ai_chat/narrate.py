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
Describe a chart's result in one or two sentences with exact numbers.

Computed from the rows the chart query returned, never by the LLM, so the
numbers in the chat always match the chart.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Optional

from superset.ai_chat.resolver import dim_key, dim_label, ResolvedMetric, ResolvedSpec
from superset.ai_dashboard.builder import _fmt, _humanize

ADDITIVE = ("sum", "count")


def fmt_value(value: Any, metric: Optional[ResolvedMetric] = None) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isnan(number):
        return "n/a"
    unit = (metric.unit if metric else "") or ""
    text = _fmt(number)
    if unit.upper() == "USD":
        return f"-${text[1:]}" if text.startswith("-") else f"${text}"
    if unit == "%":
        return f"{number:.1f}%"
    if unit and unit not in ("date",):
        return f"{text} {unit}"
    return text


def _to_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()
    if isinstance(value, (int, float)) and not math.isnan(value) and value > 1e11:
        return datetime.utcfromtimestamp(value / 1000)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "")[:19])
        except ValueError:
            return None
    return None


def fmt_period(value: Any, grain: Optional[str]) -> str:
    moment = _to_datetime(value)
    if moment is None:
        return str(value)
    if grain == "P1Y":
        return f"{moment.year}"
    if grain == "P3M":
        return f"Q{(moment.month - 1) // 3 + 1} {moment.year}"
    if grain == "P1M":
        return moment.strftime("%b %Y")
    if grain == "P1W":
        return "week of " + moment.strftime("%d %b %Y")
    return moment.strftime("%d %b %Y")


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _pct(new: float, old: float) -> Optional[str]:
    if not old:
        return None
    change = (new - old) / abs(old) * 100
    direction = "up" if change >= 0 else "down"
    return f"{direction} {abs(change):.1f}%"


def narrate(spec: ResolvedSpec, viz: str, rows: list[dict[str, Any]]) -> str:  # noqa: C901
    if not rows:
        return ""
    metric = spec.metrics[0] if spec.metrics else None
    label = metric.label if metric else "Value"
    key = label
    scope = f" for {spec.time_range_label}" if spec.time_range_label else ""

    if viz == "histogram_v2" and spec.raw_column:
        numbers = sorted(
            v for v in (_num(r.get(spec.raw_column)) for r in rows) if v is not None
        )
        if not numbers:
            return ""
        median = numbers[len(numbers) // 2]
        return (
            f"{_humanize(spec.raw_column)} ranges from {_fmt(numbers[0])} to "
            f"{_fmt(numbers[-1])} with a median of {_fmt(median)} "
            f"across {len(numbers):,} rows{scope}."
        )

    if viz in ("big_number_total",) or (not spec.dimensions and not spec.time_grain):
        parts = []
        for m in spec.metrics:
            value = rows[0].get(m.label)
            parts.append(f"{m.label} is {fmt_value(value, metric=m)}")
        return "; ".join(parts) + f"{scope}."

    if spec.time_grain and spec.time_column:
        time_key = spec.time_column
        if spec.dimensions:
            dim = spec.dimensions[0]
            dkey = dim_key(spec, dim)
            totals: dict[str, float] = defaultdict(float)
            for row in rows:
                value = _num(row.get(key))
                if value is not None:
                    totals[str(row.get(dkey))] += value
            if not totals:
                return ""
            ranked = sorted(totals.items(), key=lambda kv: -kv[1])
            leader, lead_value = ranked[0]
            text = f"{leader} leads {dim_label(spec, dim)} on {label}"
            if metric and metric.aggregate in ADDITIVE:
                grand = sum(totals.values())
                share = lead_value / grand * 100 if grand else 0
                text += (
                    f" with {fmt_value(lead_value, metric)} ({share:.0f}% of the total)"
                )
            if len(ranked) > 1:
                text += f"; {ranked[-1][0]} is lowest"
            return text + f" across {len(ranked)} series{scope}."

        points: list[tuple[datetime, float]] = []
        for row in rows:
            value = _num(row.get(key))
            if value is not None:
                points.append((_to_datetime(row.get(time_key)) or datetime.min, value))
        points.sort(key=lambda p: p[0])
        if not points:
            return ""
        peak = max(points, key=lambda p: p[1])
        low = min(points, key=lambda p: p[1])
        g = spec.time_grain
        text = (
            f"{label} peaked at {fmt_value(peak[1], metric)} "
            f"in {fmt_period(peak[0], g)} "
            f"and was lowest at {fmt_value(low[1], metric)} in {fmt_period(low[0], g)}"
        )
        if len(points) > 1:
            last, prev = points[-1], points[-2]
            change = _pct(last[1], prev[1])
            text += (
                f". The latest period ({fmt_period(last[0], g)}) is "
                f"{fmt_value(last[1], metric)}"
            )
            if change:
                text += f", {change} on the previous period"
        return text + f"{scope}."

    if viz == "bubble_v2" and len(spec.metrics) > 1:
        dim = dim_key(spec, spec.dimensions[0])
        x_m, y_m = spec.metrics[0], spec.metrics[1]
        top_x = max(rows, key=lambda r: _num(r.get(x_m.label)) or float("-inf"))
        top_y = max(rows, key=lambda r: _num(r.get(y_m.label)) or float("-inf"))
        return (
            f"{top_x.get(dim)} has the highest {x_m.label} "
            f"({fmt_value(top_x.get(x_m.label), x_m)}) and {top_y.get(dim)} "
            "the highest "
            f"{y_m.label} ({fmt_value(top_y.get(y_m.label), y_m)}) "
            f"among {len(rows)} {dim_label(spec, spec.dimensions[0]).lower()} "
            f"values{scope}."
        )

    if len(spec.dimensions) >= 2:
        best = max(rows, key=lambda r: _num(r.get(key)) or float("-inf"))
        a, b = (dim_key(spec, d) for d in spec.dimensions[:2])
        return (
            f"The highest {label} is {fmt_value(best.get(key), metric)} for "
            f"{best.get(a)} × {best.get(b)}, across {len(rows)} combinations{scope}."
        )

    dim = dim_key(spec, spec.dimensions[0])
    values: list[tuple[str, float]] = []
    for row in rows:
        number = _num(row.get(key))
        if number is not None:
            values.append((str(row.get(dim)), number))
    if not values:
        return ""
    values.sort(key=lambda kv: -kv[1] if spec.sort_desc else kv[1])
    top = values[:3]
    if metric and metric.aggregate in ADDITIVE:
        grand = sum(v for _, v in values)
        listed = ", ".join(
            f"{k} {fmt_value(v, metric)} ({v / grand * 100:.0f}%)" if grand else k
            for k, v in top
        )
    else:
        listed = ", ".join(f"{k} {fmt_value(v, metric)}" for k, v in top)
    noun = dim_label(spec, spec.dimensions[0]).lower()
    first_word, last_word, n_word = (
        ("Top", "Lowest", "top") if spec.sort_desc else ("Bottom", "Highest", "bottom")
    )
    text = f"{first_word} {noun} by {label}: {listed}"
    if len(values) > 3:
        text += (
            f". {last_word} is {values[-1][0]} at {fmt_value(values[-1][1], metric)}"
        )
    shown = (
        f"{n_word} {len(values)}"
        if spec.top_n and len(values) >= spec.top_n
        else str(len(values))
    )
    return text + f" ({shown} {noun} values{scope})."
