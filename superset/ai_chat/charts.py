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
Choose the visualization for a ResolvedSpec and build Superset form data.

The requested chart type is honoured when the data shape supports it; otherwise
the best fitting type is used and the reason is returned as a note, e.g. a pie
over 200 products becomes a bar chart of the top 25.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from superset.ai_chat.catalog import SourceInfo
from superset.ai_chat.derived import filter_sql
from superset.ai_chat.resolver import ResolvedSpec, SERIES_LIMIT, with_derived
from superset.ai_dashboard.builder import Builder
from superset.utils import json

PIE_MAX_SLICES = 12
HEATMAP_MAX_CARD = 40
STACK_MAX_SERIES = 12
DEFAULT_ROW_LIMIT = 10000

VIZ = {
    "big_number_total": "Big Number",
    "big_number": "Big Number with Trendline",
    "echarts_timeseries_line": "Line Chart",
    "echarts_timeseries_bar": "Bar Chart",
    "echarts_area": "Area Chart",
    "pie": "Pie Chart",
    "table": "Table",
    "heatmap_v2": "Heatmap",
    "histogram_v2": "Histogram",
    "treemap_v2": "Treemap",
    "funnel": "Funnel Chart",
    "bubble_v2": "Bubble Chart",
}


def _card(src: SourceInfo, spec: ResolvedSpec, index: int = 0) -> int:
    """Categories shown for dimension ``index`` (after any top-N limit)."""
    card = src.columns[spec.dimensions[index]].cardinality
    if index == 0 and spec.top_n:
        return min(card, spec.top_n)
    return card


def _incompatibility(viz: str, spec: ResolvedSpec, src: SourceInfo) -> Optional[str]:  # noqa: C901
    """Why ``viz`` cannot show ``spec``, or None when it can."""
    dims, time = spec.dimensions, bool(spec.time_grain)
    if viz in ("pie", "funnel", "treemap_v2"):
        if not dims:
            return "needs a category to split by"
        if viz != "treemap_v2" and len(dims) > 1:
            return "can only show one category"
        if viz == "pie" and _card(src, spec) > PIE_MAX_SLICES:
            return f"becomes unreadable with more than {PIE_MAX_SLICES} slices"
    if viz in ("pie", "funnel", "treemap_v2", "bubble_v2") and time:
        return "cannot show a time axis"
    if viz == "big_number_total" and dims:
        return "shows a single number, not a breakdown"
    if viz == "big_number":
        if dims:
            return "shows a single number, not a breakdown"
        if not spec.time_column:
            return "needs a date column for its trendline"
    if viz == "heatmap_v2":
        axes = len(dims) + int(time)
        if axes != 2:
            return "needs exactly two breakdowns"
        if any(_card(src, spec, i) > HEATMAP_MAX_CARD for i in range(len(dims))):
            return (
                f"becomes unreadable with more than {HEATMAP_MAX_CARD} rows or columns"
            )
    if viz == "histogram_v2":
        col = src.columns.get(spec.raw_column or "")
        if col is None or not col.numeric:
            return "needs a numeric column to bin"
    if viz == "bubble_v2":
        if len(dims) != 1:
            return "needs one category for its bubbles"
        if len(spec.metrics) < 2:
            return "needs two measures for its axes"
    if viz in ("echarts_timeseries_line", "echarts_timeseries_bar", "echarts_area"):
        if not dims and not time:
            return "needs a time axis or a category"
        if time and len(dims) > 1:
            return "can only split a time axis by one category"
    return None


REQUESTED = {
    "big_number": "big_number_total",
    "line": "echarts_timeseries_line",
    "bar": "echarts_timeseries_bar",
    "area": "echarts_area",
    "pie": "pie",
    "table": "table",
    "heatmap": "heatmap_v2",
    "histogram": "histogram_v2",
    "treemap": "treemap_v2",
    "funnel": "funnel",
    "scatter": "bubble_v2",
}


def _auto_viz(spec: ResolvedSpec, src: SourceInfo) -> str:
    dims, time = spec.dimensions, bool(spec.time_grain)
    if spec.raw_column:
        return "histogram_v2"
    if time:
        return "echarts_timeseries_line" if len(dims) <= 1 else "table"
    if not dims:
        return "big_number_total" if len(spec.metrics) == 1 else "table"
    if len(dims) == 1:
        return "echarts_timeseries_bar"
    if len(spec.metrics) == 1:
        if all(_card(src, spec, i) <= HEATMAP_MAX_CARD for i in range(2)):
            return "heatmap_v2"
        if _card(src, spec, 1) <= STACK_MAX_SERIES:
            return "echarts_timeseries_bar"
    return "table"


def choose_viz(spec: ResolvedSpec, src: SourceInfo) -> tuple[str, list[str]]:
    """Return (viz_type, notes)."""
    src = with_derived(src, spec)
    notes: list[str] = []
    requested = REQUESTED.get(spec.chart_type)
    if requested == "big_number_total" and spec.time_grain:
        requested = "big_number"
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
# Form data
# ──────────────────────────────────────────────────────────────────────────────


def _metric(expr: str, label: str) -> dict[str, Any]:
    return Builder.metric(expr, label)


def _dim(spec: ResolvedSpec, name: str) -> Any:
    """A dimension as form data: a column name, or an adhoc SQL column."""
    dim = spec.derived.get(name)
    if dim is None:
        return name
    return {"label": dim.label, "sqlExpression": dim.sql, "expressionType": "SQL"}


def _ordered_axis(spec: ResolvedSpec) -> bool:
    """A single derived breakdown (weekday, month…) reads best in its own order."""
    return (
        len(spec.dimensions) == 1
        and spec.dimensions[0] in spec.derived
        and not spec.top_n
    )


def _adhoc_filters(spec: ResolvedSpec) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = []
    for flt in spec.filters:
        dim = spec.derived.get(flt.column)
        if dim is not None:
            filters.append(
                {
                    "expressionType": "SQL",
                    "sqlExpression": filter_sql(dim, flt.operator, flt.value),
                    "clause": "WHERE",
                    "subject": None,
                    "operator": None,
                    "comparator": None,
                    "isExtra": False,
                    "filterOptionName": f"filter_{uuid.uuid4().hex[:12]}",
                }
            )
            continue
        item: dict[str, Any] = {
            "expressionType": "SIMPLE",
            "subject": flt.column,
            "operator": flt.operator,
            "clause": "WHERE",
            "isExtra": False,
            "filterOptionName": f"filter_{uuid.uuid4().hex[:12]}",
        }
        if flt.operator not in ("IS NULL", "IS NOT NULL"):
            item["comparator"] = flt.value
        filters.append(item)
    if spec.time_column:
        filters.append(
            {
                "expressionType": "SIMPLE",
                "subject": spec.time_column,
                "operator": "TEMPORAL_RANGE",
                "comparator": spec.time_range or "No filter",
                "clause": "WHERE",
                "isExtra": False,
                "filterOptionName": f"filter_{uuid.uuid4().hex[:12]}",
            }
        )
    return filters


def build_form_data(  # noqa: C901
    spec: ResolvedSpec, viz: str, dataset_id: int, src: SourceInfo
) -> dict[str, Any]:
    """Explore-compatible form data for ``viz``."""
    src = with_derived(src, spec)
    metrics = [_metric(m.expr, m.label) for m in spec.metrics]
    first = metrics[0] if metrics else _metric("COUNT(*)", "Count")
    dims = [_dim(spec, d) for d in spec.dimensions]
    form: dict[str, Any] = {
        "viz_type": viz,
        "datasource": f"{dataset_id}__table",
        "adhoc_filters": _adhoc_filters(spec),
        # Superset copies time_range into TEMPORAL_RANGE filters, so they must agree
        "time_range": spec.time_range or "No filter",
        "extra_form_data": {},
        "dashboards": [],
        "slice_name": spec.title,
    }
    limit = spec.top_n

    if viz == "big_number_total":
        form |= {
            "metric": first,
            "header_font_size": 0.4,
            "subheader_font_size": 0.15,
            "y_axis_format": "SMART_NUMBER",
            "subheader": spec.time_range_label or "",
        }
    elif viz == "big_number":
        form |= {
            "metric": first,
            "x_axis": spec.time_column,
            "time_grain_sqla": spec.time_grain or "P1M",
            "show_trend_line": True,
            "start_y_axis_at_zero": True,
            "compare_lag": 1,
            "compare_suffix": "vs previous period",
            "header_font_size": 0.4,
            "subheader_font_size": 0.15,
            "y_axis_format": "SMART_NUMBER",
            "rolling_type": "None",
        }
    elif viz in ("echarts_timeseries_line", "echarts_timeseries_bar", "echarts_area"):
        form |= {
            "metrics": metrics,
            "rich_tooltip": True,
            "show_legend": len(metrics) > 1
            or bool(dims and spec.time_grain)
            or len(dims) > 1,
            "truncate_metric": True,
            "y_axis_format": "SMART_NUMBER",
            "row_limit": DEFAULT_ROW_LIMIT,
        }
        if spec.time_grain:
            form |= {
                "x_axis": spec.time_column,
                "time_grain_sqla": spec.time_grain,
                "groupby": dims[:1],
                "markerEnabled": True,
                "x_axis_sort_asc": True,
            }
            if dims:
                # Keep the legend readable: only the top series by the first metric
                form |= {
                    "limit": limit or SERIES_LIMIT,
                    "timeseries_limit_metric": first,
                    "order_desc": spec.sort_desc,
                }
        else:
            n_bars = _card(src, spec)
            form |= {
                "x_axis": dims[0],
                "groupby": dims[1:2],
                "x_axis_sort": first["label"],
                "x_axis_sort_asc": not spec.sort_desc,
                "row_limit": limit or DEFAULT_ROW_LIMIT,
                "show_value": n_bars <= 15 and len(metrics) == 1 and len(dims) == 1,
                "xAxisLabelRotation": 45 if n_bars > 8 else 0,
            }
            if len(dims) > 1:
                form["x_axis_sort_series_type"] = "sum"
            if _ordered_axis(spec):
                # Mon→Sun, Jan→Dec, low→high bands: the labels sort in order
                form["x_axis_sort"] = dims[0]["label"]
                form["x_axis_sort_asc"] = True
        if (
            viz in ("echarts_timeseries_bar", "echarts_area")
            and len(dims) + int(bool(spec.time_grain)) > 1
        ):
            form["stack"] = "Stack"
        if viz == "echarts_timeseries_bar":
            form["orientation"] = "vertical"
    elif viz in ("pie", "funnel", "treemap_v2"):
        form |= {
            "groupby": dims,
            "metric": first,
            "row_limit": limit or (PIE_MAX_SLICES if viz == "pie" else 100),
            "sort_by_metric": True,
            "show_labels": True,
            "show_legend": viz != "treemap_v2",
        }
        if viz == "pie":
            form |= {"label_type": "key_percent", "labels_outside": True, "donut": True}
        elif viz == "funnel":
            form |= {"label_type": "key_value_percent", "sort_by_metric": True}
        else:
            form |= {"label_type": "key_value"}
    elif viz == "heatmap_v2":
        x_axis = spec.time_column if spec.time_grain else dims[0]
        y_axis = dims[0] if spec.time_grain else dims[1]
        form |= {
            "x_axis": x_axis,
            "groupby": y_axis,
            "metric": first,
            "row_limit": DEFAULT_ROW_LIMIT,
            "normalize_across": "heatmap",
            "legend_type": "continuous",
            "show_legend": True,
            "show_values": True,
            "sort_x_axis": "alpha_asc",
            "sort_y_axis": "alpha_asc",
            "y_axis_format": "SMART_NUMBER",
            # Control defaults Explore would fill in; the chat renders this form
            # data as is and the plugin reads border_color.r unguarded
            "border_color": {"r": 0, "g": 0, "b": 0, "a": 1},
            "border_width": 0,
            "xscale_interval": -1,
            "yscale_interval": -1,
            "left_margin": "auto",
            "bottom_margin": "auto",
            "value_bounds": [None, None],
        }
        if spec.time_grain:
            form["time_grain_sqla"] = spec.time_grain
    elif viz == "histogram_v2":
        form |= {
            "column": spec.raw_column,
            "groupby": dims[:1],
            "bins": 20,
            "row_limit": 50000,
            "normalize": False,
            "cumulative": False,
        }
    elif viz == "bubble_v2":
        size = metrics[2] if len(metrics) > 2 else _metric("COUNT(*)", "Count")
        form |= {
            "entity": dims[0],
            "x": metrics[0],
            "y": metrics[1],
            "size": size,
            "row_limit": limit or 100,
            "max_bubble_size": "25",
            "x_axis_label": metrics[0]["label"],
            "y_axis_label": metrics[1]["label"],
            "opacity": 0.6,
            "show_legend": False,
        }
    else:  # table
        groupby: list[Any] = list(dims)
        if spec.time_grain and spec.time_column:
            groupby.insert(0, spec.time_column)
            form["time_grain_sqla"] = spec.time_grain
            form["temporal_columns_lookup"] = {spec.time_column: True}
        form |= {
            "query_mode": "aggregate",
            "groupby": groupby,
            "metrics": metrics,
            "all_columns": [],
            "percent_metrics": [],
            "row_limit": limit or 1000,
            "order_desc": spec.sort_desc,
            "timeseries_limit_metric": first if groupby else None,
            "server_pagination": False,
            "server_page_length": 10,
            "include_search": True,
            "show_cell_bars": True,
            "table_timestamp_format": "smart_date",
        }
    return form


def build_query(spec: ResolvedSpec, viz: str) -> dict[str, Any]:
    """
    A query equivalent to the chart's for validation and narration: the same
    columns, metrics and filters, without the plugin's post-processing.
    """
    metrics = [_metric(m.expr, m.label) for m in spec.metrics]
    columns: list[Any] = []
    if spec.time_grain and spec.time_column:
        columns.append(
            {
                "timeGrain": spec.time_grain,
                "columnType": "BASE_AXIS",
                "sqlExpression": spec.time_column,
                "label": spec.time_column,
                "expressionType": "SQL",
            }
        )
    dims = [_dim(spec, d) for d in spec.dimensions]
    columns += dims
    if viz == "histogram_v2":
        columns, metrics = [spec.raw_column, *dims[:1]], []
    if viz == "bubble_v2" and len(metrics) < 3:
        metrics.append(_metric("COUNT(*)", "Count"))
    filters: list[dict[str, Any]] = []
    where: list[str] = []
    for flt in spec.filters:
        dim = spec.derived.get(flt.column)
        if dim is not None:
            where.append(filter_sql(dim, flt.operator, flt.value))
            continue
        clause: dict[str, Any] = {"col": flt.column, "op": flt.operator}
        if flt.operator not in ("IS NULL", "IS NOT NULL"):
            clause["val"] = flt.value
        filters.append(clause)
    if spec.time_column and spec.time_range != "No filter":
        filters.append(
            {"col": spec.time_column, "op": "TEMPORAL_RANGE", "val": spec.time_range}
        )
    # Time series are sorted by narrate(); ordering by a truncated time column is
    # dialect-sensitive, so only categorical queries are ordered here
    orderby = (
        [[metrics[0], not spec.sort_desc]] if metrics and not spec.time_grain else []
    )
    row_limit = DEFAULT_ROW_LIMIT
    if spec.top_n and not spec.time_grain:
        row_limit = spec.top_n
    return {
        "columns": columns,
        "metrics": metrics,
        "orderby": orderby,
        "row_limit": 50000 if viz == "histogram_v2" else row_limit,
        "filters": filters,
        "extras": {"having": "", "where": " AND ".join(f"({w})" for w in where)},
        "applied_time_extras": {},
        "time_range": spec.time_range or "No filter",
        "post_processing": [],
    }


def query_context(
    dataset_id: int,
    form_data: dict[str, Any],
    query: dict[str, Any],
    result_type: str = "full",
) -> dict[str, Any]:
    return {
        "datasource": {"id": dataset_id, "type": "table"},
        "force": False,
        "queries": [query],
        "form_data": form_data,
        "result_format": "json",
        "result_type": result_type,
    }


def explore_url(form_data: dict[str, Any]) -> str:
    """Explore link carrying the (unsaved) form data."""
    from urllib.parse import quote

    return "/explore/?form_data=" + quote(json.dumps(form_data))
