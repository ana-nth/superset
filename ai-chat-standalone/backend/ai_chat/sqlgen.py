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
# ruff: noqa: S608  (SQL is built from quoted identifiers and bound values)
"""
Build the SQL for a chart from its ResolvedSpec.

Identifiers come from the catalog and are quoted by the database's dialect;
filter values and dates are bound parameters, so nothing the user or the LLM
typed is ever pasted into the statement. The shape follows the chart:

- time series: one row per (period, series); series limited to the top N
- categories: grouped and ordered by the first metric, top N applied
- histogram: the raw numeric column (binned in ``charts.histogram``)
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import date
from typing import Optional

from ai_chat import dialects, timerange
from ai_chat.charts import PIE_MAX_SLICES, SIZE_LABEL
from ai_chat.executor import Query
from ai_chat.resolver import ResolvedSpec, SERIES_LIMIT

DEFAULT_ROW_LIMIT = 10000
HISTOGRAM_ROWS = 50000
TABLE_ROW_LIMIT = 1000
SMALL_CHART_LIMIT = 100
COMPARISONS = {"==": "=", "!=": "<>", ">": ">", ">=": ">=", "<": "<", "<=": "<="}

Quote = Callable[[str], str]


def _dim_expr(spec: ResolvedSpec, name: str, q: Quote) -> str:
    """A dimension in SQL: its quoted column or its derived expression."""
    dim = spec.derived.get(name)
    if dim is None:
        return q(name)
    # text() reads ":word" as a parameter even inside quotes; derived SQL has none
    return f"({dim.sql})".replace(":", "\\:")


def _dim_alias(spec: ResolvedSpec, name: str, q: Quote) -> str:
    """Result column of a dimension: derived ones are keyed by their label."""
    dim = spec.derived.get(name)
    return q(dim.label if dim is not None else name)


def _ordered_axis(spec: ResolvedSpec) -> bool:
    """A single derived breakdown (weekday, month…) reads best in its own order."""
    return (
        len(spec.dimensions) == 1
        and spec.dimensions[0] in spec.derived
        and not spec.top_n
    )


def _date_param(value: date, dialect: str) -> object:
    # SQLite stores dates as ISO text; compare text with text
    return value.isoformat() if dialect == "sqlite" else value


def where_clauses(
    spec: ResolvedSpec, query: Query, dialect: str, q: Quote
) -> list[str]:
    """Conditions for the spec's filters and period."""
    conds: list[str] = []
    for flt in spec.filters:
        col = _dim_expr(spec, flt.column, q)
        op = flt.operator
        dim = spec.derived.get(flt.column)
        if dim is not None and dim.numeric and op not in ("IS NULL", "IS NOT NULL"):
            items = flt.value if isinstance(flt.value, list) else [flt.value]
            numbers = [int(float(v)) for v in items]
            flt = dataclasses.replace(
                flt, value=numbers if isinstance(flt.value, list) else numbers[0]
            )
        if op in ("IS NULL", "IS NOT NULL"):
            conds.append(f"{col} {op}")
        elif op in ("IN", "NOT IN"):
            values = flt.value if isinstance(flt.value, list) else [flt.value]
            conds.append(f"{col} {op} {query.bind(values)}")
        elif op == "ILIKE":
            conds.append(dialects.ilike(col, query.bind(str(flt.value)), dialect))
        elif op == "LIKE":
            conds.append(f"{col} LIKE {query.bind(str(flt.value))}")
        elif op in COMPARISONS:
            conds.append(f"{col} {COMPARISONS[op]} {query.bind(flt.value)}")
    if spec.time_column:
        try:
            today = date.fromisoformat(spec.anchor_date) if spec.anchor_date else None
            start, end = timerange.evaluate(spec.time_range, today=today)
        except ValueError:
            start, end = None, None
        col = q(spec.time_column)
        if start:
            conds.append(f"{col} >= {query.bind(_date_param(start, dialect))}")
        if end:
            conds.append(f"{col} < {query.bind(_date_param(end, dialect))}")
    return conds


def _top_values(
    dim: str,
    metric: str,
    n: int,
    desc: bool,
    source: str,
    where: list[str],
    dialect: str,
    q: Quote,
) -> str:
    """Subquery listing the ``n`` values of ``dim`` with the largest ``metric``."""
    inner = f"SELECT {dim} AS {q('k')}, {metric} AS {q('m')} FROM {source}"
    if where:
        inner += " WHERE " + " AND ".join(where)
    inner += f" GROUP BY {dim} ORDER BY {metric} {'DESC' if desc else 'ASC'}"
    inner = dialects.limit(inner, n, dialect, ordered=True)
    return f"SELECT {q('k')} FROM ({inner}) t"


def _row_limit(spec: ResolvedSpec, viz: str, time: bool) -> Optional[int]:
    if time:
        return DEFAULT_ROW_LIMIT
    if not spec.dimensions:
        return None  # a single aggregated row
    if viz == "pie":
        return spec.top_n or PIE_MAX_SLICES
    if viz in ("funnel", "treemap", "bubble"):
        return spec.top_n or SMALL_CHART_LIMIT
    if viz == "table":
        return spec.top_n or TABLE_ROW_LIMIT
    if len(spec.dimensions) > 1:
        return DEFAULT_ROW_LIMIT  # top N is applied to the first dimension
    return spec.top_n or DEFAULT_ROW_LIMIT


def build_query(  # noqa: C901
    spec: ResolvedSpec, viz: str, source_sql: str, dialect: str, q: Quote
) -> Query:
    """The SQL (and parameters) that returns the rows ``viz`` draws."""
    query = Query("")
    source = f"({source_sql}) s"
    dims = list(spec.dimensions)
    time = bool(spec.time_grain and spec.time_column)
    where = where_clauses(spec, query, dialect, q)

    if viz == "histogram" and spec.raw_column:
        raw = q(spec.raw_column)
        cols = [
            raw,
            *(f"{_dim_expr(spec, d, q)} AS {_dim_alias(spec, d, q)}" for d in dims[:1]),
        ]
        conds = [*where, f"{raw} IS NOT NULL"]
        sql = f"SELECT {', '.join(cols)} FROM {source} WHERE {' AND '.join(conds)}"
        query.sql = dialects.limit(sql, HISTOGRAM_ROWS, dialect, ordered=False)
        return query

    select: list[str] = []
    group: list[str] = []
    bucket = ""
    if time:
        assert spec.time_column and spec.time_grain
        bucket = dialects.time_bucket(q(spec.time_column), spec.time_grain, dialect)
        select.append(f"{bucket} AS {q(spec.time_column)}")
        group.append(bucket)
    for dim in dims:
        expr = _dim_expr(spec, dim, q)
        alias = _dim_alias(spec, dim, q)
        select.append(expr if expr == alias else f"{expr} AS {alias}")
        group.append(expr)
    metrics = [(m.expr, m.label) for m in spec.metrics] or [("COUNT(*)", "Count")]
    if viz == "bubble" and len(metrics) < 3:
        metrics.append(("COUNT(*)", SIZE_LABEL))
    select += [f"{expr} AS {q(label)}" for expr, label in metrics]
    first = metrics[0][0]

    # Keep legends readable: only the top values of the first breakdown
    top = None
    if dims and viz != "table":
        if time:
            top = spec.top_n or SERIES_LIMIT
        elif len(dims) > 1 and spec.top_n:
            top = spec.top_n
    conds = list(where)
    if top:
        first_dim = _dim_expr(spec, dims[0], q)
        subquery = _top_values(
            first_dim, first, top, spec.sort_desc, source, where, dialect, q
        )
        conds.append(f"{first_dim} IN ({subquery})")

    sql = f"SELECT {', '.join(select)} FROM {source}"
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    if group:
        sql += " GROUP BY " + ", ".join(group)
    order: list[str] = []
    if time:
        order.append(f"{bucket} ASC")
        if dims:
            order.append(f"{first} {'DESC' if spec.sort_desc else 'ASC'}")
    elif group and _ordered_axis(spec) and viz != "table":
        # Mon→Sun, Jan→Dec, low→high bands: the labels sort in order
        order.append(f"{group[0]} ASC")
    elif group:
        order.append(f"{first} {'DESC' if spec.sort_desc else 'ASC'}")
    if order:
        sql += " ORDER BY " + ", ".join(order)
    limit = _row_limit(spec, viz, time)
    if limit:
        sql = dialects.limit(sql, limit, dialect, ordered=bool(order))
    query.sql = sql
    return query
