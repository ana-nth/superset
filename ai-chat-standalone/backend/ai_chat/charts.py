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
Choose the visualization for a ResolvedSpec and describe how to draw it.

The requested chart type is honoured when the data shape supports it; otherwise
the best fitting type is used and the reason is returned as a note, e.g. a pie
over 200 products becomes a bar chart of the top 25.

``render_config`` tells the frontend which result columns play which role (x
axis, series, metrics); the rows come from ``sqlgen``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Optional

from ai_chat.catalog import SourceInfo
from ai_chat.formatting import humanize
from ai_chat.resolver import dim_key, ResolvedSpec, with_derived

PIE_MAX_SLICES = 12
HEATMAP_MAX_CARD = 40
STACK_MAX_SERIES = 12
HISTOGRAM_BINS = 20
SIZE_LABEL = "Rows"  # bubble size when no third measure is asked for

VIZ = {
    "big_number": "Big Number",
    "big_number_trend": "Big Number with Trendline",
    "line": "Line Chart",
    "bar": "Bar Chart",
    "area": "Area Chart",
    "pie": "Pie Chart",
    "table": "Table",
    "heatmap": "Heatmap",
    "histogram": "Histogram",
    "treemap": "Treemap",
    "funnel": "Funnel Chart",
    "bubble": "Bubble Chart",
}
XY_CHARTS = ("line", "bar", "area")


def _card(src: SourceInfo, spec: ResolvedSpec, index: int = 0) -> int:
    """Categories shown for dimension ``index`` (after any top-N limit)."""
    card = src.columns[spec.dimensions[index]].cardinality
    if index == 0 and spec.top_n:
        return min(card, spec.top_n)
    return card


def _incompatibility(viz: str, spec: ResolvedSpec, src: SourceInfo) -> Optional[str]:  # noqa: C901
    """Why ``viz`` cannot show ``spec``, or None when it can."""
    dims, time = spec.dimensions, bool(spec.time_grain)
    if viz in ("pie", "funnel", "treemap"):
        if not dims:
            return "needs a category to split by"
        if viz != "treemap" and len(dims) > 1:
            return "can only show one category"
        if viz == "pie" and _card(src, spec) > PIE_MAX_SLICES:
            return f"becomes unreadable with more than {PIE_MAX_SLICES} slices"
    if viz in ("pie", "funnel", "treemap", "bubble") and time:
        return "cannot show a time axis"
    if viz == "big_number" and dims:
        return "shows a single number, not a breakdown"
    if viz == "big_number_trend":
        if dims:
            return "shows a single number, not a breakdown"
        if not spec.time_column:
            return "needs a date column for its trendline"
    if viz == "heatmap":
        axes = len(dims) + int(time)
        if axes != 2:
            return "needs exactly two breakdowns"
        if any(_card(src, spec, i) > HEATMAP_MAX_CARD for i in range(len(dims))):
            return (
                f"becomes unreadable with more than {HEATMAP_MAX_CARD} rows or columns"
            )
    if viz == "histogram":
        col = src.columns.get(spec.raw_column or "")
        if col is None or not col.numeric:
            return "needs a numeric column to bin"
    if viz == "bubble":
        if len(dims) != 1:
            return "needs one category for its bubbles"
        if len(spec.metrics) < 2:
            return "needs two measures for its axes"
    if viz in XY_CHARTS:
        if not dims and not time:
            return "needs a time axis or a category"
        if time and len(dims) > 1:
            return "can only split a time axis by one category"
    return None


REQUESTED = {
    "big_number": "big_number",
    "line": "line",
    "bar": "bar",
    "area": "area",
    "pie": "pie",
    "table": "table",
    "heatmap": "heatmap",
    "histogram": "histogram",
    "treemap": "treemap",
    "funnel": "funnel",
    "scatter": "bubble",
}


def _auto_viz(spec: ResolvedSpec, src: SourceInfo) -> str:
    dims, time = spec.dimensions, bool(spec.time_grain)
    if spec.raw_column:
        return "histogram"
    if time:
        return "line" if len(dims) <= 1 else "table"
    if not dims:
        return "big_number" if len(spec.metrics) == 1 else "table"
    if len(dims) == 1:
        return "bar"
    if len(spec.metrics) == 1:
        if all(_card(src, spec, i) <= HEATMAP_MAX_CARD for i in range(2)):
            return "heatmap"
        if _card(src, spec, 1) <= STACK_MAX_SERIES:
            return "bar"
    return "table"


def choose_viz(spec: ResolvedSpec, src: SourceInfo) -> tuple[str, list[str]]:
    """Return (viz_type, notes)."""
    src = with_derived(src, spec)
    notes: list[str] = []
    requested = REQUESTED.get(spec.chart_type)
    if requested == "big_number" and spec.time_grain:
        requested = "big_number_trend"
    if requested:
        reason = _incompatibility(requested, spec, src)
        if reason is None:
            return requested, notes
        fallback = _auto_viz(spec, src)
        if _incompatibility(fallback, spec, src) is not None:
            fallback = "table"
        notes.append(
            f"A {VIZ[requested].lower()} {reason}, so I used a {VIZ[fallback].lower()}."
        )
        return fallback, notes
    viz = _auto_viz(spec, src)
    if _incompatibility(viz, spec, src) is not None:
        viz = "table"
    return viz, notes


# ──────────────────────────────────────────────────────────────────────────────
# Render config
# ──────────────────────────────────────────────────────────────────────────────


def _metric(label: str, unit: str = "") -> dict[str, str]:
    return {"key": label, "label": label, "unit": unit}


def render_config(  # noqa: C901
    spec: ResolvedSpec, viz: str, src: SourceInfo
) -> dict[str, Any]:
    """Which result columns play which role in ``viz``; keys are result columns."""
    src = with_derived(src, spec)
    metrics = [_metric(m.label, m.unit) for m in spec.metrics]
    # Derived dimensions are keyed by their label in the result
    dims = [dim_key(spec, d) for d in spec.dimensions]
    time = bool(spec.time_grain and spec.time_column)
    cfg: dict[str, Any] = {
        "viz_type": viz,
        "metrics": metrics,
        "dimensions": dims,
        "time_column": spec.time_column if time else None,
        "time_grain": spec.time_grain if time else None,
        "subheader": spec.time_range_label or "",
        "sort_desc": spec.sort_desc,
    }
    if viz in XY_CHARTS:
        n_bars = _card(src, spec) if dims and not time else 0
        # Over time the breakdown splits the lines; otherwise the second one does
        series_index = 0 if time else 1
        cfg |= {
            "x": spec.time_column if time else dims[0],
            "x_is_time": time,
            "series": dims[series_index] if len(dims) > series_index else None,
            "stack": viz in ("bar", "area") and len(dims) + int(time) > 1,
            "show_values": not time
            and n_bars <= 15
            and len(metrics) == 1
            and len(dims) == 1,
            "label_rotate": 45 if n_bars > 8 else 0,
        }
    elif viz == "big_number_trend":
        cfg |= {"x": spec.time_column, "x_is_time": True}
    elif viz in ("pie", "funnel", "treemap"):
        cfg |= {"categories": dims}
    elif viz == "heatmap":
        cfg |= {
            "x": spec.time_column if time else dims[0],
            "y": dims[0] if time else dims[1],
            "x_is_time": time,
        }
    elif viz == "histogram":
        cfg |= {
            "column": spec.raw_column,
            "column_label": humanize(spec.raw_column or ""),
            "series": dims[0] if dims else None,
            "bins": HISTOGRAM_BINS,
        }
    elif viz == "bubble":
        size = metrics[2] if len(metrics) > 2 else _metric(SIZE_LABEL)
        cfg |= {
            "entity": dims[0],
            "x_metric": metrics[0],
            "y_metric": metrics[1],
            "size_metric": size,
        }
    elif viz == "table":
        columns = ([spec.time_column] if time else []) + dims
        cfg |= {"columns": [*columns, *(m["key"] for m in metrics)]}
    return cfg


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def histogram(rows: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    """Bin the raw values of a histogram (per series when there is a breakdown)."""
    column, series = str(cfg.get("column") or ""), cfg.get("series")
    n_bins = int(cfg.get("bins") or HISTOGRAM_BINS)
    points: list[tuple[str, float]] = []
    for row in rows:
        number = _number(row.get(column))
        if number is not None:
            name = str(row.get(series)) if series else "Count"
            points.append((name, number))
    if not points:
        return {"bins": [], "series": []}
    low = min(v for _, v in points)
    high = max(v for _, v in points)
    if high == low:
        high = low + 1
    width = (high - low) / n_bins
    totals: dict[str, int] = defaultdict(int)
    for name, _ in points:
        totals[name] += 1
    # The most frequent series only, so the legend stays readable
    kept = sorted(totals, key=lambda name: -totals[name])[:STACK_MAX_SERIES]
    counts = {name: [0] * n_bins for name in kept}
    for name, value in points:
        if name in counts:
            counts[name][min(int((value - low) / width), n_bins - 1)] += 1
    return {
        "bins": [
            {"start": low + i * width, "end": low + (i + 1) * width}
            for i in range(n_bins)
        ],
        "series": [{"name": name, "counts": counts[name]} for name in kept],
    }
