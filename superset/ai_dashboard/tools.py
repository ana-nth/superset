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
SQL guard, virtual dataset creation, chart spec validation, and dashboard layout tools
for the AI Auto Dashboard agent.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

import sqlglot
from pydantic import BaseModel, field_validator

from superset.ai_dashboard.builder import (
    build_layout,
    Builder,
    ChartPlan,
    make_filter,
    save_dashboard,
    Section,
)
from superset.ai_dashboard.semantics import load_semantics, semantics_path
from superset.utils import json

logger = logging.getLogger(__name__)

ALLOWED_VIZ_TYPES = {
    "big_number_total",
    "echarts_timeseries_line",
    "echarts_timeseries_bar",
    "bar",
    "pie",
    "table",
    "heatmap_v2",
    "box_plot",
    "histogram_v2",
}

# Legacy / unsupported names the model may emit, mapped to current plugins
VIZ_ALIASES = {"bar": "echarts_timeseries_bar", "box_plot": "histogram_v2"}

WIDE_VIZ = {"echarts_timeseries_line", "echarts_timeseries_bar", "table", "heatmap_v2"}
MEDIUM_VIZ = {"bar", "pie", "box_plot", "histogram_v2"}
# big_number_total → width 4, wide → 12, medium → 6


class ChartSpec(BaseModel):
    dataset: int  # dataset id
    viz_type: str
    metrics: list[str] = []
    groupby: list[str] = []
    time_column: Optional[str] = None
    time_grain: Optional[str] = "P1M"
    filters: list[dict[str, Any]] = []
    title: str
    insight_text: str = ""
    severity: str = "info"

    @field_validator("viz_type")
    @classmethod
    def validate_viz_type(cls, v: str) -> str:
        if v not in ALLOWED_VIZ_TYPES:
            raise ValueError(f"viz_type must be one of {ALLOWED_VIZ_TYPES}")
        return v


def _metric(expr: str) -> dict[str, Any]:
    """Turn 'SUM(line_amount)' (or a bare column name) into an adhoc SQL metric."""
    expr = expr.strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", expr) and expr.lower() != "count":
        expr = f"SUM({expr})"
    if expr.lower() == "count":
        expr = "COUNT(*)"
    return Builder.metric(expr, expr)


def _build_params(spec: ChartSpec) -> dict[str, Any]:
    """Convert ChartSpec fields into Superset chart params (form data)."""
    viz_type = VIZ_ALIASES.get(spec.viz_type, spec.viz_type)
    metrics = [_metric(m) for m in spec.metrics] or [_metric("count")]
    base: dict[str, Any] = {
        "viz_type": viz_type,
        "datasource": f"{spec.dataset}__table",
        "metrics": metrics,
        "groupby": spec.groupby or [],
        "adhoc_filters": spec.filters or [],
        "time_range": "No filter",
        "row_limit": 1000,
    }
    if spec.time_column:
        base["granularity_sqla"] = spec.time_column
        base["time_grain_sqla"] = spec.time_grain or "P1M"

    if viz_type == "big_number_total":
        base = {
            "viz_type": viz_type,
            "datasource": f"{spec.dataset}__table",
            "metric": metrics[0],
            "adhoc_filters": spec.filters or [],
            "time_range": "No filter",
            "header_font_size": 0.4,
            "subheader": spec.insight_text[:100],
            "y_axis_format": "SMART_NUMBER",
        }
    elif viz_type in ("echarts_timeseries_line", "echarts_timeseries_bar"):
        if spec.time_column:
            base["x_axis"] = spec.time_column
            base["groupby"] = spec.groupby or []
        else:
            # Categorical bar: first groupby becomes the x-axis
            base["x_axis"] = spec.groupby[0] if spec.groupby else ""
            base["groupby"] = spec.groupby[1:]
            base["x_axis_sort"] = metrics[0]["label"]
            base["x_axis_sort_asc"] = False
        base["rich_tooltip"] = True
        base["show_legend"] = True
    elif viz_type == "pie":
        base["metric"] = metrics[0]
        base["labels_outside"] = True
        base["show_labels"] = True
    elif viz_type == "heatmap_v2":
        base["x_axis"] = spec.groupby[0] if spec.groupby else ""
        base["groupby"] = spec.groupby[1] if len(spec.groupby) > 1 else ""
        base["metric"] = metrics[0]
        # Kept for backwards compatibility with older heatmap form data
        base["all_columns_x"] = base["x_axis"]
        base["all_columns_y"] = base["groupby"]
    elif viz_type == "histogram_v2":
        base["column"] = (spec.groupby or [""])[0]
        base["columns"] = spec.groupby or []
        base["groupby"] = []
        base["bins"] = 20
    elif viz_type == "table":
        base["query_mode"] = "aggregate"

    return base


def _query_for(params: dict[str, Any]) -> dict[str, Any]:
    """Build the saved query object that matches the chart form data."""
    viz_type = params["viz_type"]
    metric = params.get("metric")
    metrics = [metric] if metric else list(params.get("metrics") or [])
    groupby = params.get("groupby") or []
    groupby = [groupby] if isinstance(groupby, str) and groupby else list(groupby or [])
    columns: list[Any] = []
    if viz_type in ("echarts_timeseries_line", "echarts_timeseries_bar"):
        x_axis = params.get("x_axis")
        if params.get("time_grain_sqla") and x_axis == params.get("granularity_sqla"):
            x_axis = {
                "timeGrain": params["time_grain_sqla"],
                "columnType": "BASE_AXIS",
                "sqlExpression": x_axis,
                "label": x_axis,
                "expressionType": "SQL",
            }
        columns = [x_axis, *groupby] if x_axis else groupby
    elif viz_type == "heatmap_v2":
        columns = [c for c in (params.get("x_axis"), *groupby) if c]
    elif viz_type == "histogram_v2":
        columns = [params["column"]] if params.get("column") else []
        metrics = []
    elif viz_type == "big_number_total":
        columns = []
    else:
        columns = groupby
    return {
        "columns": columns,
        "metrics": metrics,
        "orderby": [[metrics[0], False]]
        if metrics and viz_type != "heatmap_v2"
        else [],
        "row_limit": params.get("row_limit", 1000),
        "filters": [],
    }


def get_semantics() -> str:
    """Return condensed semantics.yaml (tables, joins, business metrics) as JSON."""
    data = load_semantics()
    if not data:
        return json.dumps(
            {"error": f"semantics.yaml not found (looked at {semantics_path()})"}
        )
    condensed = {
        "schema": data.get("schema"),
        "tables": data.get("tables", {}),
        "joins": data.get("joins", []),
        "business_metrics": data.get("business_metrics", {}),
    }
    # Cap at ~1500 tokens ≈ 6000 chars
    return json.dumps(condensed, default=str)[:6000]


def _parse_and_validate_sql(sql: str, max_rows: int = 200) -> str:
    """
    Validates sql is a single SELECT/WITH statement.
    Injects LIMIT if missing or > max_rows.
    Returns cleaned SQL string or raises ValueError.
    """
    sql = sql.strip().rstrip(";")
    try:
        statements = [
            s for s in sqlglot.parse(sql, dialect="postgres") if s is not None
        ]
    except Exception as exc:
        raise ValueError(f"SQL parse error: {exc}") from exc

    if len(statements) != 1:
        raise ValueError("Only a single SQL statement is allowed")

    stmt = statements[0]
    stmt_type = type(stmt).__name__
    if stmt_type not in ("Select", "With", "Union"):
        raise ValueError(f"Only SELECT/WITH statements are allowed, got {stmt_type}")

    # Force LIMIT
    sql_out = stmt.sql(dialect="postgres")
    m = re.search(r"\bLIMIT\s+(\d+)\s*$", sql_out, re.IGNORECASE)
    if not m:
        sql_out = f"{sql_out} LIMIT {max_rows}"
    elif int(m.group(1)) > max_rows:
        sql_out = f"{sql_out[: m.start()]}LIMIT {max_rows}"

    return sql_out


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return None if value != value else round(value, 4)  # NaN → None
    try:
        return round(float(value), 4)  # Decimal, numpy numbers
    except (TypeError, ValueError):
        return str(value)  # dates, timestamps, UUIDs


def run_sql(sql: str, database_id: int) -> dict[str, Any]:
    """
    Execute a read-only SQL query against the warehouse database.
    Returns compact output: columns + up to 20 rows + numeric aggregate stats.
    """
    try:
        validated_sql = _parse_and_validate_sql(sql)
    except ValueError as exc:
        return {"error": str(exc)}

    try:
        from superset.daos.database import DatabaseDAO

        database = DatabaseDAO.find_by_id(database_id)
        if not database:
            return {"error": f"Database {database_id} not found"}
        frame = database.get_df(validated_sql)
        columns = [str(c) for c in frame.columns]
        rows = [
            [_jsonable(v) for v in row]
            for row in frame.head(20).itertuples(index=False)
        ]

        stats: dict[str, dict[str, Any]] = {}
        for i, col in enumerate(columns):
            vals = [
                r[i]
                for r in rows
                if isinstance(r[i], (int, float)) and not isinstance(r[i], bool)
            ]
            if vals and len(vals) == sum(1 for r in rows if r[i] is not None):
                stats[col] = {
                    "min": round(min(vals), 4),
                    "max": round(max(vals), 4),
                    "mean": round(sum(vals) / len(vals), 4),
                    "nulls": sum(1 for r in rows if r[i] is None),
                }

        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(frame),
            "stats": stats,
        }
    except Exception as exc:
        logger.warning("run_sql error: %s", exc)
        return {"error": str(exc)[:500]}


def create_dataset(
    name: str, sql: str, database_id: int, schema: str = "wh"
) -> dict[str, Any]:
    """Create (or refresh) a virtual dataset; returns {dataset_id} or {error}."""
    try:
        _parse_and_validate_sql(sql)  # guard
    except ValueError as exc:
        return {"error": str(exc)}

    try:
        builder = Builder(database_id, schema)
        safe_name = (
            re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_")[:200] or "ai_dataset"
        )
        return {
            "dataset_id": builder.ensure_dataset(safe_name, sql.strip().rstrip(";"))
        }
    except Exception as exc:
        logger.warning("create_dataset error: %s", exc)
        from superset.extensions import db

        db.session.rollback()
        return {"error": str(exc)[:300]}


def create_chart(
    spec_dict: dict[str, Any],
    dataset_id: int,
    dashboard_id: Optional[int] = None,
) -> dict[str, Any]:
    """
    Build and persist a chart from a spec dict. Returns {chart_id} or {error}.
    The chart query is executed once first, so a chart that cannot render is rejected
    with the database error (the model can then fix the spec and retry).
    """
    try:
        spec = ChartSpec(**{**spec_dict, "dataset": dataset_id})
    except Exception as exc:
        return {"error": f"Invalid chart spec: {str(exc)[:300]}"}

    params = _build_params(spec)
    plan = ChartPlan(
        title=spec.title,
        viz_type=params["viz_type"],
        view=None,
        form_data=params,
        query=_query_for(params),
        width=_chart_width(params["viz_type"]),
        height=30 if params["viz_type"] == "big_number_total" else 50,
        dataset_id=dataset_id,
    )
    if not Builder.validate_chart(plan):
        return {
            "error": "Chart query failed; check metric expressions and column names"
        }
    try:
        Builder.persist_chart(plan)
        if dashboard_id:
            logger.info(
                "Chart %s will be attached to dashboard %s", plan.chart_id, dashboard_id
            )
        return {"chart_id": plan.chart_id, "uuid": plan.uuid, "viz_type": plan.viz_type}
    except Exception as exc:
        logger.warning("create_chart failed: %s", exc)
        from superset.extensions import db

        db.session.rollback()
        return {"error": str(exc)[:300]}


def _chart_width(viz_type: str) -> int:
    if viz_type == "big_number_total":
        return 4
    if viz_type in WIDE_VIZ:
        return 12
    return 6


def build_dashboard(  # noqa: C901
    title: str,
    sections: list[dict[str, Any]],
    chart_ids_with_specs: list[dict[str, Any]],
    date_column: str = "",
    dim_filters: Optional[list[str]] = None,
    dataset_id: Optional[int] = None,
    summary: str = "",
) -> dict[str, Any]:
    """
    Build and persist a Superset dashboard.
    sections: [{"title": str, "chart_ids": [int], "insight": str}]
    chart_ids_with_specs: [{"chart_id": int, "viz_type": str, "title": str,
                              "uuid": str}]
    Returns {dashboard_id} or {error}.
    """
    specs = {int(c["chart_id"]): c for c in chart_ids_with_specs if c.get("chart_id")}

    def plan_for(chart_id: int) -> ChartPlan:
        spec = specs.get(chart_id, {})
        viz_type = spec.get("viz_type", "table")
        return ChartPlan(
            title=spec.get("title", ""),
            viz_type=viz_type,
            view=None,
            form_data={},
            query={},
            width=_chart_width(viz_type),
            height=30 if viz_type == "big_number_total" else 50,
            chart_id=chart_id,
            uuid=spec.get("uuid", ""),
        )

    kpis = [
        plan_for(cid)
        for cid, s in specs.items()
        if s.get("viz_type") == "big_number_total"
    ]
    kpi_ids = {p.chart_id for p in kpis}
    placed = set(kpi_ids)
    secs: list[Section] = []
    for raw in sections or []:
        if not isinstance(raw, dict):
            continue
        sec = Section(
            key=str(raw.get("title", "")), title=str(raw.get("title", "Section"))
        )
        if raw.get("insight"):
            sec.insights.append(
                {"title": "Insight", "text": str(raw["insight"]), "severity": "info"}
            )
        for cid in raw.get("chart_ids") or []:
            try:
                cid = int(cid)
            except (TypeError, ValueError):
                continue
            if cid in specs and cid not in placed:
                sec.charts.append(plan_for(cid))
                placed.add(cid)
        secs.append(sec)
    # Charts the model forgot to put in a section still belong on the dashboard
    leftovers = [plan_for(cid) for cid in specs if cid not in placed]
    if leftovers:
        secs.append(Section(key="more", title="More charts", charts=leftovers))

    layout = build_layout(title, summary, kpis, secs)

    filters: list[dict[str, Any]] = []
    if date_column or dataset_id:
        filters.append(make_filter("Date Range", "filter_time", [{}]))
    for dim in (dim_filters or [])[:2]:
        target: dict[str, Any] = {"column": {"name": dim}}
        if dataset_id:
            target["datasetId"] = dataset_id
        filters.append(
            make_filter(dim.replace("_", " ").title(), "filter_select", [target])
        )

    try:
        dashboard_id = save_dashboard(title, layout, filters, list(specs))
        return {"dashboard_id": dashboard_id}
    except Exception as exc:
        logger.warning("build_dashboard error: %s", exc)
        from superset.extensions import db

        db.session.rollback()
        return {"error": str(exc)[:500]}
