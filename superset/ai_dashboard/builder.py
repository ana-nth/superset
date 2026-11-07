# ruff: noqa: S608  (SQL is built from quoted identifiers and escaped literals)
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
Deterministic dashboard builder for the AI Auto Dashboard feature.

Works without any LLM: it profiles the selected schema (using semantics.yaml when
it describes that schema, otherwise database introspection), runs the five
mandatory checks (C1–C5) with plain SQL, and persists datasets, charts and a
native Superset dashboard. Every chart is executed once before it is placed on
the dashboard, so a broken chart is dropped instead of breaking the dashboard.
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import pandas as pd

from superset.ai_dashboard.semantics import load_semantics
from superset.extensions import db
from superset.utils import json
from superset.utils.core import get_user_id

logger = logging.getLogger(__name__)

ProgressFn = Optional[Callable[[str], None]]

MAIN_MEASURE_HINTS = (
    "total",
    "global",
    "amount",
    "revenue",
    "sales",
    "value",
    "price",
    "qty",
)
FACT_NAME_HINTS = ("sales", "order", "revenue", "transaction")
NUMERIC_TYPES = ("INT", "NUMERIC", "DECIMAL", "FLOAT", "DOUBLE", "REAL", "MONEY")
TIME_TYPES = ("DATE", "TIME")
MAX_KPIS = 8
HISTOGRAM_ROWS = 50000  # Superset's default ROW_LIMIT
LOW_CARD_MAX = 12
# Numeric columns that identify or locate things rather than measure them
NON_MEASURE_RE = re.compile(
    r"(^|_)(lat|lon|lng|latitude|longitude|zip|zipcode|postcode|postal|code|year|yr"
    r"|month|day|week|quarter|qtr|number|num|no|phone|rank|x|y|index|idx|key)($|_)"
)
# Numeric measures that should be averaged, not summed
AVG_MEASURE_RE = re.compile(
    r"price|rate|pct|percent|ratio|avg|mean|score|rating|(^|_)age($|_)|temperature|margin|_per_"
)
MID_CARD_MAX = 30
MIN_CELL_ROWS = 30


# ──────────────────────────────────────────────────────────────────────────────
# Metadata
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class TableMeta:
    """Roles of the columns of one physical table."""

    name: str
    columns: list[str] = field(default_factory=list)
    nullable: list[str] = field(default_factory=list)
    time_cols: list[str] = field(default_factory=list)
    measures: dict[str, str] = field(default_factory=dict)  # column -> agg
    dimensions: list[str] = field(default_factory=list)
    fks: list[tuple[str, str, str]] = field(default_factory=list)  # col, table, col
    row_count: int = 0

    @property
    def is_fact(self) -> bool:
        return bool(self.measures) and bool(self.time_cols) and self.row_count > 0


@dataclass
class FactView:
    """A denormalised virtual dataset: one fact table joined to its dimensions."""

    fact: TableMeta
    sql: str
    dataset_name: str
    time_col: str
    measures: dict[str, str]
    dimensions: list[str]
    cardinality: dict[str, int] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)  # dimension -> table
    dataset_id: int = 0

    @property
    def main_measure(self) -> str:
        sums = [c for c, a in self.measures.items() if a == "sum"]
        candidates = sums or list(self.measures)
        for hint in MAIN_MEASURE_HINTS:
            for col in candidates:
                if hint in col.lower():
                    return col
        return candidates[0]

    def dims_by_card(self, low: int = 2, high: int = LOW_CARD_MAX) -> list[str]:
        return [d for d in self.dimensions if low <= self.cardinality.get(d, 0) <= high]


@dataclass
class ChartPlan:
    """A chart about to be persisted."""

    title: str
    viz_type: str
    view: FactView | None
    form_data: dict[str, Any]
    query: dict[str, Any]
    width: int = 6
    height: int = 50
    dataset_id: int = 0
    chart_id: int = 0
    uuid: str = ""


@dataclass
class Section:
    """A dashboard section (one per check) with its charts and insights."""

    key: str
    title: str
    insights: list[dict[str, Any]] = field(default_factory=list)
    charts: list[ChartPlan] = field(default_factory=list)


def _humanize(name: str) -> str:
    return name.replace("_", " ").strip().title()


def _fmt(value: float) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    abs_v = abs(value)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs_v >= div:
            return f"{value / div:.1f}{suffix}"
    if float(value).is_integer():
        return f"{int(value)}"
    return f"{value:.2f}"


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


class Builder:
    """Profiles a schema and builds a dashboard from it."""

    def __init__(
        self,
        database_id: int,
        schema: str,
        progress: ProgressFn = None,
        max_charts: int = 14,
    ) -> None:
        from superset.daos.database import DatabaseDAO

        database = DatabaseDAO.find_by_id(database_id)
        if database is None:
            raise ValueError(f"Database {database_id} not found")
        self.database = database
        self.database_id = database_id
        self.schema = schema
        self.progress = progress or (lambda _msg: None)
        self.max_charts = max_charts
        self.tables: dict[str, TableMeta] = {}
        self.views: list[FactView] = []
        self.business_metrics: dict[str, dict[str, Any]] = {}
        self.sections: list[Section] = []
        self.kpis: list[ChartPlan] = []
        self.dq_datasets: dict[str, int] = {}
        self._quote = self.database.get_dialect().identifier_preparer.quote

    # ── SQL helpers ──────────────────────────────────────────────────────────

    def q(self, ident: str) -> str:
        return self._quote(ident)

    def tbl(self, table: str) -> str:
        return f"{self.q(self.schema)}.{self.q(table)}"

    def df(self, sql: str) -> pd.DataFrame:
        frame = self.database.get_df(sql, schema=self.schema)
        for col in frame.columns:
            if frame[col].dtype == object:
                converted = pd.to_numeric(frame[col], errors="ignore")
                frame[col] = converted
        return frame

    def safe_df(self, sql: str) -> pd.DataFrame | None:
        try:
            return self.df(sql)
        except Exception as exc:  # pylint: disable=broad-except
            logger.info("AI dashboard query skipped (%s): %s", exc, sql[:200])
            return None

    # ── Profiling ────────────────────────────────────────────────────────────

    def profile(self) -> None:  # noqa: C901
        """Collect table/column roles from semantics.yaml and the database."""
        from superset.sql.parse import Table

        semantics = load_semantics()
        sem_tables: dict[str, Any] = {}
        if semantics and semantics.get("schema") == self.schema:
            sem_tables = semantics.get("tables") or {}
            self.business_metrics = semantics.get("business_metrics") or {}

        # Entries are (table, schema, catalog) tuples
        table_names = sorted(
            entry[0] if isinstance(entry, tuple) else str(entry)
            for entry in self.database.get_all_table_names_in_schema(
                catalog=None, schema=self.schema
            )
        )
        if not table_names:
            raise ValueError(f"Schema '{self.schema}' has no tables")

        for name in table_names[:20]:
            try:
                cols = self.database.get_columns(Table(name, self.schema))
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("Cannot read columns of %s: %s", name, exc)
                continue
            meta = TableMeta(name=name)
            sem_cols = (sem_tables.get(name) or {}).get("columns") or {}
            pk_like = {f"{name}_id", "id"}
            for col in cols:
                cname = col["column_name"]
                ctype = str(col.get("type", "")).upper()
                meta.columns.append(cname)
                if col.get("nullable", True):
                    meta.nullable.append(cname)
                sem = sem_cols.get(cname) or {}
                role = sem.get("role") or self._infer_role(cname, ctype, pk_like)
                if role == "time":
                    meta.time_cols.append(cname)
                elif role == "measure" and any(t in ctype for t in NUMERIC_TYPES):
                    agg = sem.get("agg") or self._infer_agg(cname)
                    meta.measures[cname] = agg if agg in ("sum", "avg") else "sum"
                elif role == "dimension":
                    meta.dimensions.append(cname)
            try:
                for fk in self.database.get_foreign_keys(Table(name, self.schema)):
                    if fk.get("constrained_columns") and fk.get("referred_columns"):
                        meta.fks.append(
                            (
                                fk["constrained_columns"][0],
                                fk["referred_table"],
                                fk["referred_columns"][0],
                            )
                        )
            except Exception:  # pylint: disable=broad-except
                logger.debug("No foreign keys for %s", name, exc_info=True)
            # Prefer DATE columns over timestamps for the main time axis
            meta.time_cols.sort(key=lambda c: 0 if "date" in c.lower() else 1)
            self.tables[name] = meta

        # Semantic joins complement database FKs
        for join in (semantics or {}).get("joins", []) if sem_tables else []:
            try:
                lt, lc = join["left"].split(".")
                rt, rc = join["right"].split(".")
            except (KeyError, ValueError, AttributeError):
                continue
            if lt in self.tables and (lc, rt, rc) not in self.tables[lt].fks:
                self.tables[lt].fks.append((lc, rt, rc))

        counts = " UNION ALL ".join(
            f"SELECT '{t}' AS t, COUNT(*) AS n FROM {self.tbl(t)}" for t in self.tables
        )
        frame = self.safe_df(counts)
        if frame is not None:
            for _, row in frame.iterrows():
                self.tables[str(row["t"])].row_count = int(row["n"])

    @staticmethod
    def _infer_role(name: str, ctype: str, pk_like: set[str]) -> str:
        lname = name.lower()
        if any(t in ctype for t in TIME_TYPES) and "INTERVAL" not in ctype:
            return "time"
        if lname in pk_like or lname.endswith("_id") or lname == "id":
            return "id"
        if any(t in ctype for t in NUMERIC_TYPES):
            return "id" if NON_MEASURE_RE.search(lname) else "measure"
        if "BOOL" in ctype:
            return "flag"
        return "dimension"

    @staticmethod
    def _infer_agg(name: str) -> str:
        return "avg" if AVG_MEASURE_RE.search(name.lower()) else "sum"

    def build_views(self) -> None:  # noqa: C901
        """Create one denormalised view per fact table (largest first)."""
        # Tables that other tables reference are dimensions (dim_product…)
        referenced = {ref for t in self.tables.values() for _, ref, _ in t.fks}
        facts = [
            t for t in self.tables.values() if t.is_fact and t.name not in referenced
        ] or [t for t in self.tables.values() if t.is_fact]
        if not facts:
            # No fact-shaped tables: use any table that has measures
            facts = [t for t in self.tables.values() if t.measures and t.row_count]
        if not facts:
            raise ValueError(
                f"No table in schema '{self.schema}' has numeric measures to analyse"
            )

        def score(t: TableMeta) -> tuple[int, int, int]:
            # Business-sounding names first, then tables that can be sliced
            # (dimensions + measures), then size
            bonus = int(any(h in t.name.lower() for h in FACT_NAME_HINTS))
            richness = min(len(t.dimensions) + len(t.fks), 5) + min(len(t.measures), 5)
            return bonus, richness, t.row_count

        facts.sort(key=score, reverse=True)

        for fact in facts[:3]:
            select = ["f.*"]
            joins: list[str] = []
            used = set(fact.columns)
            dims = list(fact.dimensions)
            sources = {d: fact.name for d in dims}
            joined: set[str] = set()
            # Join each dimension once, preferring the FK named like its key
            # (warehouse_id → warehouse_id over counterpart_warehouse_id)
            fks = sorted(fact.fks, key=lambda fk: fk[0] != fk[2])
            for idx, (col, ref_table, ref_col) in enumerate(fks):
                dim = self.tables.get(ref_table)
                if not dim or ref_table in joined or ref_table == fact.name:
                    continue
                joined.add(ref_table)
                alias = f"d{idx}"
                joins.append(
                    f"LEFT JOIN {self.tbl(ref_table)} {alias} "
                    f"ON f.{self.q(col)} = {alias}.{self.q(ref_col)}"
                )
                for dcol in dim.dimensions:
                    out = dcol
                    if out in used:
                        out = f"{re.sub(r'^dim_', '', ref_table)}_{dcol}"
                    if out in used:
                        continue
                    used.add(out)
                    dims.append(out)
                    sources[out] = ref_table
                    select.append(f"{alias}.{self.q(dcol)} AS {self.q(out)}")
            sql = (
                f"SELECT {', '.join(select)} FROM {self.tbl(fact.name)} f "
                + " ".join(joins)
            ).strip()
            time_col = fact.time_cols[0] if fact.time_cols else ""
            view = FactView(
                fact=fact,
                sql=sql,
                dataset_name=f"ai_{fact.name}",
                time_col=time_col,
                measures=dict(fact.measures),
                dimensions=dims,
                sources=sources,
            )
            cols = [*dims, *view.measures]
            card_sql = (
                "SELECT "
                + ", ".join(f"COUNT(DISTINCT {self.q(c)}) AS {self.q(c)}" for c in cols)
                + f" FROM ({sql}) v"
            )
            frame = self.safe_df(card_sql)
            if frame is not None and not frame.empty:
                view.cardinality = {
                    c: int(frame.iloc[0][c]) for c in cols if c in frame.columns
                }
            # Columns that are empty or hold a single value add nothing
            view.dimensions = [d for d in dims if view.cardinality.get(d, 0) >= 2]
            view.measures = {
                m: a
                for m, a in view.measures.items()
                if view.cardinality.get(m, 0) >= 2
            }
            if view.measures:
                self.views.append(view)
        if not self.views:
            raise ValueError(
                f"No table in schema '{self.schema}' has non-empty numeric measures"
            )

    # ── Persistence helpers ──────────────────────────────────────────────────

    def ensure_dataset(self, name: str, sql: str, time_col: str = "") -> int:
        """Create or refresh a virtual dataset; returns its id."""
        from superset.connectors.sqla.models import SqlaTable
        from superset.extensions import security_manager

        user_id = get_user_id()
        owners = (
            [security_manager.get_user_by_id(user_id)] if user_id is not None else []
        )
        dataset = (
            db.session.query(SqlaTable)
            .filter_by(
                database_id=self.database_id, schema=self.schema, table_name=name
            )
            .one_or_none()
        )
        if dataset is None:
            dataset = SqlaTable(
                table_name=name,
                schema=self.schema,
                database_id=self.database_id,
                database=self.database,
                sql=sql,
                owners=[o for o in owners if o],
            )
            db.session.add(dataset)
        else:
            dataset.sql = sql
        dataset.description = "Generated by AI Auto Dashboard"
        db.session.flush()
        dataset.fetch_metadata()
        if time_col:
            dataset.main_dttm_col = time_col
            for col in dataset.columns:
                if col.column_name == time_col:
                    col.is_dttm = True
        db.session.commit()
        return dataset.id

    # ── Chart factories ──────────────────────────────────────────────────────

    @staticmethod
    def metric(expr: str, label: str) -> dict[str, Any]:
        return {
            "expressionType": "SQL",
            "sqlExpression": expr,
            "label": label,
            "hasCustomLabel": True,
            "optionName": f"metric_{uuid.uuid4().hex[:10]}",
        }

    def measure_metric(
        self, view: FactView, col: str, agg: str | None = None
    ) -> dict[str, Any]:
        agg = (agg or view.measures.get(col, "sum")).upper()
        word = "Avg" if agg == "AVG" else "Total"
        label = _humanize(col)
        if not label.lower().startswith(("total", "avg", "average")):
            label = f"{word} {label}"
        return self.metric(f"{agg}({self.q(col)})", label)

    def base_form(self, viz_type: str, dataset_id: int) -> dict[str, Any]:
        return {
            "viz_type": viz_type,
            "datasource": f"{dataset_id}__table",
            "adhoc_filters": [],
            "time_range": "No filter",
            "extra_form_data": {},
            "dashboards": [],
        }

    def big_number(
        self, view: FactView, metric: dict[str, Any], title: str
    ) -> ChartPlan:
        form = {
            **self.base_form("big_number_total", view.dataset_id),
            "metric": metric,
            "header_font_size": 0.4,
            "subheader_font_size": 0.15,
            "y_axis_format": "SMART_NUMBER",
        }
        return ChartPlan(
            title, "big_number_total", view, form, {"metrics": [metric]}, 3, 30
        )

    def time_series(
        self,
        view: FactView,
        metric: dict[str, Any],
        title: str,
        groupby: list[str] | None = None,
        viz_type: str = "echarts_timeseries_line",
    ) -> ChartPlan:
        form = {
            **self.base_form(viz_type, view.dataset_id),
            "x_axis": view.time_col,
            "time_grain_sqla": "P1M",
            "metrics": [metric],
            "groupby": groupby or [],
            "row_limit": 10000,
            "rich_tooltip": True,
            "show_legend": True,
            "markerEnabled": True,
            "truncate_metric": True,
        }
        axis = {
            "timeGrain": "P1M",
            "columnType": "BASE_AXIS",
            "sqlExpression": view.time_col,
            "label": view.time_col,
            "expressionType": "SQL",
        }
        query = {
            "columns": [axis, *(groupby or [])],
            "metrics": [metric],
            "orderby": [[metric, False]],
            "row_limit": 10000,
            "series_columns": groupby or [],
        }
        return ChartPlan(title, viz_type, view, form, query, 12 if not groupby else 6)

    def category_bar(
        self,
        view: FactView,
        metric: dict[str, Any],
        dim: str,
        title: str,
        limit: int = 10,
    ) -> ChartPlan:
        form = {
            **self.base_form("echarts_timeseries_bar", view.dataset_id),
            "x_axis": dim,
            "metrics": [metric],
            "groupby": [],
            "row_limit": limit,
            "x_axis_sort": metric["label"],
            "x_axis_sort_asc": False,
            "orientation": "vertical",
            "show_legend": False,
            "show_value": True,
        }
        query = {
            "columns": [dim],
            "metrics": [metric],
            "orderby": [[metric, False]],
            "row_limit": limit,
        }
        return ChartPlan(title, "echarts_timeseries_bar", view, form, query)

    def pie(
        self, view: FactView, metric: dict[str, Any], dim: str, title: str
    ) -> ChartPlan:
        form = {
            **self.base_form("pie", view.dataset_id),
            "groupby": [dim],
            "metric": metric,
            "row_limit": 25,
            "show_labels": True,
            "label_type": "key_percent",
            "labels_outside": True,
            "donut": True,
            "show_legend": True,
        }
        query = {
            "columns": [dim],
            "metrics": [metric],
            "orderby": [[metric, False]],
            "row_limit": 25,
        }
        return ChartPlan(title, "pie", view, form, query)

    def heatmap(
        self, view: FactView, metric: dict[str, Any], x: str, y: str, title: str
    ) -> ChartPlan:
        form = {
            **self.base_form("heatmap_v2", view.dataset_id),
            "x_axis": x,
            "groupby": y,
            "metric": metric,
            "row_limit": 1000,
            "normalize_across": "heatmap",
            "legend_type": "continuous",
            "show_legend": True,
            "show_values": True,
            "sort_x_axis": "alpha_asc",
            "sort_y_axis": "alpha_asc",
        }
        query = {
            "columns": [x, y],
            "metrics": [metric],
            "orderby": [],
            "row_limit": 1000,
        }
        return ChartPlan(title, "heatmap_v2", view, form, query, 12)

    def histogram(self, view: FactView, col: str, title: str) -> ChartPlan:
        form = {
            **self.base_form("histogram_v2", view.dataset_id),
            "column": col,
            "groupby": [],
            "bins": 20,
            "row_limit": HISTOGRAM_ROWS,
            "normalize": False,
            "cumulative": False,
        }
        query = {
            "columns": [col],
            "metrics": [],
            "orderby": [],
            "row_limit": HISTOGRAM_ROWS,
        }
        return ChartPlan(title, "histogram_v2", view, form, query)

    def table(
        self, dataset_id: int, columns: list[str], title: str, order: str = ""
    ) -> ChartPlan:
        form = {
            **self.base_form("table", dataset_id),
            "query_mode": "raw",
            "all_columns": columns,
            "order_by_cols": [json.dumps([order, False])] if order else [],
            "row_limit": 100,
            "server_page_length": 10,
            "table_timestamp_format": "%Y-%m-%d",
        }
        query = {
            "columns": columns,
            "metrics": [],
            "orderby": [[order, False]] if order else [],
            "row_limit": 100,
        }
        plan = ChartPlan(title, "table", None, form, query, 6)
        plan.dataset_id = dataset_id
        return plan

    # ── Checks ───────────────────────────────────────────────────────────────

    def view_sql(self, view: FactView) -> str:
        return f"({view.sql}) v"

    def check_data_quality(self) -> Section:  # noqa: C901
        """C1: row counts, freshness, null % and duplicate business keys."""
        sec = Section("C1", "C1 · Data Quality & Freshness")
        parts = []
        for t in self.tables.values():
            tcol = (
                f"CAST(MAX({self.q(t.time_cols[0])}) AS DATE)"
                if t.time_cols
                else "CAST(NULL AS DATE)"
            )
            parts.append(
                f"SELECT '{t.name}' AS table_name, COUNT(*) AS row_count, "
                f"{tcol} AS latest_record FROM {self.tbl(t.name)}"
            )
        freshness_sql = " UNION ALL ".join(parts)
        null_parts = []
        for t in self.tables.values():
            for col in t.nullable[:25]:
                null_parts.append(
                    f"SELECT '{t.name}' AS table_name, '{col}' AS column_name, "
                    f"COUNT(*) - COUNT({self.q(col)}) AS null_count, "
                    f"ROUND(100.0 * (COUNT(*) - COUNT({self.q(col)})) / "
                    f"NULLIF(COUNT(*), 0), 2) AS null_pct FROM {self.tbl(t.name)}"
                )

        fresh = self.safe_df(freshness_sql)
        if fresh is not None and not fresh.empty:
            ds = self.ensure_dataset("ai_dq_freshness", freshness_sql)
            sec.charts.append(
                self.table(
                    ds,
                    ["table_name", "row_count", "latest_record"],
                    "Table Row Counts & Freshness",
                    "row_count",
                )
            )
            total = int(fresh["row_count"].sum())
            latest = pd.to_datetime(fresh["latest_record"], errors="coerce").max()
            text = f"{len(fresh)} tables hold {_fmt(total)} rows in total."
            if pd.notna(latest):
                lag = (
                    pd.Timestamp.now().normalize()
                    - latest.tz_localize(None).normalize()
                ).days
                text += (
                    f" The most recent record is from {latest.date()} ({lag} days ago)."
                )
                sev = "warning" if lag > 7 else "info"
            else:
                sev = "info"
            sec.insights.append({"title": "Freshness", "text": text, "severity": sev})
            empty = fresh[fresh["row_count"] == 0]["table_name"].tolist()
            if empty:
                sec.insights.append(
                    {
                        "title": "Empty tables",
                        "text": f"{len(empty)} table(s) are empty: "
                        f"{', '.join(map(str, empty))}.",
                        "severity": "warning",
                    }
                )

        if null_parts:
            null_sql = (
                "SELECT * FROM ("
                + " UNION ALL ".join(null_parts)
                + ") n WHERE null_count > 0"
            )
            nulls = self.safe_df(null_sql)
            if nulls is not None:
                ds = self.ensure_dataset("ai_dq_nulls", null_sql)
                sec.charts.append(
                    self.table(
                        ds,
                        ["table_name", "column_name", "null_count", "null_pct"],
                        "Columns With Missing Values",
                        "null_pct",
                    )
                )
                if nulls.empty:
                    sec.insights.append(
                        {
                            "title": "Completeness",
                            "text": "No NULL values found in any nullable column.",
                            "severity": "info",
                        }
                    )
                else:
                    worst = nulls.sort_values("null_pct", ascending=False).iloc[0]
                    pct = _num(worst["null_pct"])
                    sec.insights.append(
                        {
                            "title": "Missing values",
                            "text": f"{len(nulls)} column(s) contain NULLs; worst is "
                            f"{worst['table_name']}.{worst['column_name']} "
                            f"at {pct:.2f}% "
                            f"({_fmt(_num(worst['null_count']))} rows).",
                            "severity": "warning" if pct >= 1 else "info",
                        }
                    )

        # Duplicate business keys: near-unique document/reference style columns
        for t in self.tables.values():
            for col in t.dimensions:
                if not re.search(
                    r"_doc$|_ref$|document|invoice|reference_(doc|no|id)", col.lower()
                ):
                    continue
                frame = self.safe_df(
                    f"SELECT COUNT(DISTINCT {self.q(col)}) AS uniq, "
                    f"COUNT({self.q(col)}) AS n, "
                    f"(SELECT COUNT(*) FROM (SELECT {self.q(col)} "
                    f"FROM {self.tbl(t.name)} "
                    f"WHERE {self.q(col)} IS NOT NULL "
                    "GROUP BY 1 HAVING COUNT(*) > 1) d) AS dup_keys "
                    f"FROM {self.tbl(t.name)}"
                )
                if frame is None or frame.empty:
                    continue
                row = frame.iloc[0]
                is_key = int(row["n"]) and int(row["uniq"]) / int(row["n"]) > 0.9
                if is_key and int(row["dup_keys"]) > 0:
                    n = int(row["dup_keys"])
                    sec.insights.append(
                        {
                            "title": "Duplicate keys",
                            "text": f"{n} duplicate value(s) found in {t.name}.{col}.",
                            "severity": "warning",
                        }
                    )
        return sec

    def check_trends(self) -> Section:
        """C2: monthly trend, MoM growth, peaks/troughs and Q4 seasonality."""
        sec = Section("C2", "C2 · Trend & Seasonality")
        for view in self.views[:2]:
            if not view.time_col:
                continue
            m = view.main_measure
            agg = view.measures[m].upper()
            frame = self.safe_df(
                f"SELECT {self.q(view.time_col)} AS t, {agg}({self.q(m)}) AS v "
                f"FROM {self.view_sql(view)} "
                f"WHERE {self.q(view.time_col)} IS NOT NULL GROUP BY 1"
            )
            if frame is None or frame.empty:
                continue
            frame["t"] = pd.to_datetime(
                frame["t"], errors="coerce", utc=True
            ).dt.tz_localize(None)
            frame = frame.dropna(subset=["t"])
            monthly = frame.set_index("t")["v"].astype(float).resample("MS")
            series = monthly.sum() if agg == "SUM" else monthly.mean()
            series = series[series.notna()]
            # Partial first/last months (< 20 days of data) distort peaks and growth
            days = (
                frame.set_index("t")
                .resample("MS")["v"]
                .apply(
                    lambda s: (s.index.max() - s.index.min()).days + 1 if len(s) else 0
                )
            )
            if agg == "SUM" and len(series) > 3:
                edges = [series.index[0], series.index[-1]]
                partial = [m for m in edges if days.get(m, 31) < 20]
                series = series.drop(partial)
            title = f"Monthly {_humanize(m)} — {_humanize(view.fact.name)}"
            sec.charts.append(
                self.time_series(view, self.measure_metric(view, m), title)
            )
            if len(series) >= 2:
                peak, trough = series.idxmax(), series.idxmin()
                growth = (
                    series.pct_change()
                    .replace([math.inf, -math.inf], float("nan"))
                    .dropna()
                )
                text = (
                    f"{_humanize(m)} peaks in {peak:%b %Y} at {_fmt(series.max())} "
                    "and bottoms in "
                    f"{trough:%b %Y} at {_fmt(series.min())} "
                    f"({series.max() / series.min():.1f}× spread)"
                    if series.min() > 0
                    else f"{_humanize(m)} peaks in {peak:%b %Y} at {_fmt(series.max())}"
                )
                if not growth.empty:
                    text += (
                        "; average month-over-month change is "
                        f"{growth.mean() * 100:+.1f}%."
                    )
                sec.insights.append(
                    {
                        "title": f"{_humanize(view.fact.name)} trend",
                        "text": text,
                        "severity": "info",
                    }
                )
                q4 = series[series.index.month >= 10]
                rest = series[series.index.month < 10]
                if len(q4) and len(rest) and rest.mean() > 0:
                    ratio = q4.mean() / rest.mean()
                    if abs(ratio - 1) >= 0.15:
                        sec.insights.append(
                            {
                                "title": "Seasonality",
                                "text": f"Q4 months average {ratio:.1f}× the "
                                "rest of the "
                                f"year for {_humanize(m)}.",
                                "severity": "warning"
                                if ratio > 1.5 or ratio < 0.67
                                else "info",
                            }
                        )
        return sec

    def check_segmentation(self) -> Section:
        """C3: top-N slices, channel share, Pareto concentration."""
        sec = Section("C3", "C3 · Segmentation & Pareto")
        if not self.views:
            return sec
        view = self.views[0]
        m = view.main_measure
        metric = self.measure_metric(view, m, "sum")
        high = sorted(
            (d for d in view.dimensions if view.cardinality.get(d, 0) > LOW_CARD_MAX),
            key=lambda d: view.cardinality.get(d, 0),
            reverse=True,
        )
        # Prefer name-like columns for the Pareto (product_name over sku)
        high.sort(key=lambda d: 0 if "name" in d.lower() else 1)
        low = view.dims_by_card()
        if high:
            dim = high[0]
            sec.charts.append(
                self.category_bar(
                    view, metric, dim, f"Top 10 {_humanize(dim)} by {_humanize(m)}"
                )
            )
            frame = self.safe_df(
                f"SELECT {self.q(dim)} AS k, SUM({self.q(m)}) AS v "
                f"FROM {self.view_sql(view)} GROUP BY 1"
            )
            if frame is not None and len(frame) >= 5:
                values = frame["v"].astype(float).sort_values(ascending=False)
                top_n = max(1, int(round(len(values) * 0.2)))
                share = (
                    values.iloc[:top_n].sum() / values.sum() * 100
                    if values.sum()
                    else 0
                )
                sec.insights.append(
                    {
                        "title": "Pareto concentration",
                        "text": f"The top 20% of {_humanize(dim)} values "
                        f"({top_n} of {len(values)}) generate {share:.1f}% "
                        f"of {_humanize(m)}.",
                        "severity": "warning" if share >= 70 else "info",
                    }
                )
        for dim in low[:2]:
            sec.charts.append(
                self.pie(view, metric, dim, f"{_humanize(m)} Share by {_humanize(dim)}")
            )
            frame = self.safe_df(
                f"SELECT {self.q(dim)} AS k, SUM({self.q(m)}) AS v "
                f"FROM {self.view_sql(view)} GROUP BY 1 ORDER BY 2 DESC"
            )
            if frame is not None and not frame.empty and frame["v"].astype(float).sum():
                top = frame.iloc[0]
                share = float(top["v"]) / frame["v"].astype(float).sum() * 100
                sec.insights.append(
                    {
                        "title": f"Leading {_humanize(dim)}",
                        "text": f"'{top['k']}' leads {_humanize(dim)} with "
                        f"{share:.1f}% of {_humanize(m)} ({_fmt(float(top['v']))}).",
                        "severity": "info",
                    }
                )
        return sec

    def check_distribution(self) -> Section:  # noqa: C901
        """C4: distribution shape, outliers and per-segment spikes."""
        sec = Section("C4", "C4 · Distribution & Anomalies")
        if not self.views:
            return sec
        view = self.views[0]
        m = view.main_measure
        frame = self.safe_df(
            f"SELECT AVG({self.q(m)}) AS mean, STDDEV({self.q(m)}) AS sd, "
            f"COUNT(*) AS n, MIN({self.q(m)}) AS lo, MAX({self.q(m)}) AS hi "
            f"FROM {self.view_sql(view)}"
        )
        if frame is not None and not frame.empty:
            mean, sd = _num(frame.iloc[0]["mean"]), _num(frame.iloc[0]["sd"])
            sec.charts.append(
                self.histogram(view, m, f"Distribution of {_humanize(m)}")
            )
            if sd and not math.isnan(sd):
                out = self.safe_df(
                    f"SELECT COUNT(*) AS n FROM {self.view_sql(view)} "
                    f"WHERE ABS({self.q(m)} - {mean}) > 3 * {sd}"
                )
                n_out = int(out.iloc[0]["n"]) if out is not None else 0
                total = int(frame.iloc[0]["n"]) or 1
                sec.insights.append(
                    {
                        "title": "Outliers",
                        "text": f"{n_out} rows ({n_out / total * 100:.2f}%) of "
                        f"{_humanize(m)} lie beyond 3σ "
                        f"(mean {_fmt(mean)}, σ {_fmt(sd)}).",
                        "severity": "warning" if n_out / total > 0.01 else "info",
                    }
                )

        # Spike detection: avg-type measure per (month × low-cardinality dimension)
        avg_measures = [c for c, a in view.measures.items() if a == "avg"]
        avg_measures.sort(
            key=lambda c: 0 if re.search(r"disc|pct|rate|price", c) else 1
        )
        low = view.dims_by_card()
        best: tuple[float, str, str, Any, Any, float, float] | None = None
        if view.time_col:
            for col in avg_measures[:2]:
                for dim in low[:3]:
                    cells = self.safe_df(
                        f"SELECT {self.q(dim)} AS k, "
                        f"EXTRACT(MONTH FROM {self.q(view.time_col)}) AS mo, "
                        f"AVG({self.q(col)}) AS v, COUNT(*) AS n "
                        f"FROM {self.view_sql(view)} GROUP BY 1, 2"
                    )
                    if cells is not None:
                        # Ignore NULL groups and cells too small to be meaningful
                        cells = cells[
                            cells["k"].notna() & (cells["n"] >= MIN_CELL_ROWS)
                        ]
                        cells = cells.reset_index(drop=True)
                    if cells is None or len(cells) < 6:
                        continue
                    vals = cells["v"].astype(float)
                    if not vals.std():
                        continue
                    z = (vals - vals.mean()) / vals.std()
                    idx = z.abs().idxmax()
                    if best is None or abs(z[idx]) > best[0]:
                        row = cells.loc[idx]
                        best = (
                            abs(z[idx]),
                            col,
                            dim,
                            row["k"],
                            int(row["mo"]),
                            float(row["v"]),
                            float(vals.mean()),
                        )
        if best and best[0] >= 2:
            z, col, dim, key, month, val, avg = best
            sec.charts.append(
                self.time_series(
                    view,
                    self.measure_metric(view, col, "avg"),
                    f"Avg {_humanize(col)} by {_humanize(dim)}",
                    [dim],
                )
            )
            month_name = pd.Timestamp(2000, month, 1).strftime("%B")
            sec.insights.append(
                {
                    "title": f"{_humanize(col)} spike",
                    "text": f"{_humanize(dim)} '{key}' in {month_name} averages "
                    f"{_fmt(val)} {_humanize(col)} vs {_fmt(avg)} overall "
                    f"({z:.1f}σ).",
                    "severity": "critical" if z >= 3 else "warning",
                }
            )

        # Segment outliers in the secondary fact tables (e.g. adjustments per site)
        # Within each value of the fact's own type-like dimension (e.g.
        # movement_type = ADJUSTMENT) compare the negative-row share across the
        # joined dimensions (e.g. warehouses) and report the strongest outlier.
        for other in self.views[1:3]:
            om = other.main_measure
            own = [d for d in other.dims_by_card() if d in other.fact.dimensions][:3]
            joined = [
                d
                for d in other.dims_by_card(3, MID_CARD_MAX)
                if d not in other.fact.dimensions
            ]
            joined.sort(key=lambda d: 0 if "name" in d else 1)
            found: tuple[float, str, str, Any, Any, float, float] | None = None
            for kind in own:
                for dim in joined[:3]:
                    frame = self.safe_df(
                        f"SELECT {self.q(kind)} AS a, {self.q(dim)} AS k, "
                        f"COUNT(CASE WHEN {self.q(om)} < 0 THEN 1 END) AS neg, "
                        "COUNT(*) AS n "
                        f"FROM {self.view_sql(other)} GROUP BY 1, 2"
                    )
                    if frame is None or frame.empty:
                        continue
                    frame["share"] = frame["neg"].astype(float) / frame["n"].astype(
                        float
                    )
                    for a_val, grp in frame.groupby("a"):
                        median = grp["share"].median()
                        if len(grp) < 3 or not median or grp["share"].max() >= 0.999:
                            continue
                        top = grp.sort_values("share", ascending=False).iloc[0]
                        ratio = float(top["share"]) / median
                        if found is None or ratio > found[0]:
                            found = (
                                ratio,
                                kind,
                                dim,
                                a_val,
                                top["k"],
                                float(top["share"]),
                                float(median),
                            )
            if found and found[0] >= 1.5:
                ratio, kind, dim, a_val, key, share, median = found
                literal = str(a_val).replace("'", "''")
                neg_metric = self.metric(
                    f"COUNT(CASE WHEN {self.q(om)} < 0 "
                    f"AND {self.q(kind)} = '{literal}' THEN 1 END)",
                    f"Negative {a_val} rows",
                )
                sec.charts.append(
                    self.category_bar(
                        other, neg_metric, dim, f"Negative {a_val} by {_humanize(dim)}"
                    )
                )
                sec.insights.append(
                    {
                        "title": f"{_humanize(kind)} anomaly",
                        "text": f"{_humanize(dim)} '{key}': {share * 100:.1f}% of its "
                        f"{a_val} rows are negative, {ratio:.1f}× the median "
                        f"of {median * 100:.1f}%.",
                        "severity": "critical" if ratio >= 3 else "warning",
                    }
                )
        return sec

    def check_kpis(self) -> Section:
        """C5: business KPIs (big numbers) and a two-dimension heatmap."""
        sec = Section("C5", "C5 · KPIs & Relationships")
        for view in self.views[:2]:
            m = view.main_measure
            agg = view.measures[m].upper()
            metric = self.measure_metric(view, m)
            frame = self.safe_df(
                f"SELECT {agg}({self.q(m)}) AS v FROM {self.view_sql(view)}"
            )
            if frame is not None and not frame.empty:
                self.kpis.append(self.big_number(view, metric, metric["label"]))

        if not self.business_metrics and self.views:
            self.generic_kpis(self.views[0])

        for key, spec in list(self.business_metrics.items()):
            sql = (spec or {}).get("sql")
            if not sql:
                continue
            for view in self.views:
                frame = self.safe_df(f"SELECT {sql} AS v FROM {self.view_sql(view)}")
                if frame is None or frame.empty or pd.isna(frame.iloc[0]["v"]):
                    continue
                value = float(frame.iloc[0]["v"])
                unit = (spec or {}).get("unit", "")
                suffix = "%" if unit == "%" else f" {unit}" if unit else ""
                label = _humanize(key) + (" (%)" if unit == "%" else "")
                self.kpis.append(self.big_number(view, self.metric(sql, label), label))
                sec.insights.append(
                    {
                        "title": _humanize(key),
                        "text": f"{_humanize(key)} is {_fmt(value)}{suffix}. "
                        f"{spec.get('meaning', '')}".strip(),
                        "severity": "info",
                    }
                )
                break

        if self.views:
            view = self.views[0]
            dims = view.dims_by_card(2, MID_CARD_MAX)
            # Two dimensions from different joined tables (e.g. warehouse × category)
            dims.sort(
                key=lambda d: (
                    view.sources.get(d) == view.fact.name,
                    not re.search(r"name|category|region|segment|channel", d),
                )
            )
            if len(dims) >= 2:
                x = dims[0]
                y = next(
                    (d for d in dims[1:] if view.sources.get(d) != view.sources.get(x)),
                    dims[1],
                )
                m = view.main_measure
                sec.charts.append(
                    self.heatmap(
                        view,
                        self.measure_metric(view, m, "sum"),
                        x,
                        y,
                        f"{_humanize(m)}: {_humanize(x)} × {_humanize(y)}",
                    )
                )
        return sec

    def generic_kpis(self, view: FactView) -> None:
        """KPIs for schemas without semantics.yaml: volume, reach and averages."""
        self.kpis.append(
            self.big_number(view, self.metric("COUNT(*)", "Records"), "Records")
        )
        entities = sorted(
            (d for d in view.dimensions if view.cardinality.get(d, 0) > LOW_CARD_MAX),
            key=lambda d: (
                0 if "name" in d.lower() else 1,
                -view.cardinality.get(d, 0),
            ),
        )
        if entities:
            dim = entities[0]
            label = f"Distinct {_humanize(dim)}"
            metric = self.metric(f"COUNT(DISTINCT {self.q(dim)})", label)
            self.kpis.append(self.big_number(view, metric, label))
        for col in [c for c, a in view.measures.items() if a == "avg"][:2]:
            metric = self.measure_metric(view, col, "avg")
            self.kpis.append(self.big_number(view, metric, metric["label"]))

    # ── Assembly ─────────────────────────────────────────────────────────────

    @staticmethod
    def validate_chart(plan: ChartPlan) -> bool:
        """Run the chart query once; drop the chart if it fails."""
        from superset.charts.schemas import ChartDataQueryContextSchema
        from superset.commands.chart.data.get_data_command import ChartDataCommand

        try:
            ctx = ChartDataQueryContextSchema().load(Builder.query_context(plan))
            command = ChartDataCommand(ctx)
            command.validate()
            command.run()
            return True
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("AI dashboard dropped chart '%s': %s", plan.title, exc)
            db.session.rollback()
            return False

    @staticmethod
    def query_context(plan: ChartPlan) -> dict[str, Any]:
        query = {
            "filters": [],
            "extras": {"having": "", "where": ""},
            "applied_time_extras": {},
            "time_range": "No filter",
            "post_processing": [],
            **plan.query,
        }
        return {
            "datasource": {"id": plan.dataset_id, "type": "table"},
            "force": False,
            "queries": [query],
            "form_data": plan.form_data,
            "result_format": "json",
            "result_type": "full",
        }

    @staticmethod
    def persist_chart(plan: ChartPlan) -> None:
        from superset.extensions import security_manager
        from superset.models.slice import Slice

        user_id = get_user_id()
        owner = (
            security_manager.get_user_by_id(user_id) if user_id is not None else None
        )
        chart = Slice(
            slice_name=plan.title[:250],
            viz_type=plan.viz_type,
            datasource_id=plan.dataset_id,
            datasource_type="table",
            params=json.dumps(plan.form_data),
            query_context=json.dumps(Builder.query_context(plan)),
            owners=[owner] if owner else [],
            description="Generated by AI Auto Dashboard",
        )
        db.session.add(chart)
        db.session.commit()
        plan.chart_id = chart.id
        plan.uuid = str(chart.uuid)
        plan.form_data["slice_id"] = chart.id

    def finalize_charts(self) -> None:
        """Attach dataset ids, validate, enforce the chart budget, persist.

        KPI tiles (at most MAX_KPIS) do not count against the chart budget.
        """
        budget = self.max_charts
        for plan in [*self.kpis, *(c for s in self.sections for c in s.charts)]:
            if plan.view is not None:
                plan.dataset_id = plan.view.dataset_id
                plan.form_data["datasource"] = f"{plan.dataset_id}__table"
        kept_kpis: list[ChartPlan] = []
        for plan in self.kpis[:MAX_KPIS]:
            if self.validate_chart(plan):
                self.persist_chart(plan)
                kept_kpis.append(plan)
        self.kpis = kept_kpis
        # Round-robin across sections so every check gets charts within the budget
        queues = [list(s.charts) for s in self.sections]
        kept: dict[int, list[ChartPlan]] = {i: [] for i in range(len(self.sections))}
        while budget > 0 and any(queues):
            for i, queue in enumerate(queues):
                if not queue or budget <= 0:
                    continue
                plan = queue.pop(0)
                if self.validate_chart(plan):
                    self.persist_chart(plan)
                    kept[i].append(plan)
                    budget -= 1
                    self.progress(f"Chart created: {plan.title}")
        for i, sec in enumerate(self.sections):
            sec.charts = kept[i]

    def build_dashboard(self, title: str, summary: str = "") -> int:
        """Persist the dashboard with KPI row, one section per check and filters."""
        layout = build_layout(title, summary, self.kpis, self.sections)
        chart_ids = [
            p.chart_id
            for p in [*self.kpis, *(c for s in self.sections for c in s.charts)]
        ]
        return save_dashboard(title, layout, self.native_filters(), chart_ids)

    def native_filters(self) -> list[dict[str, Any]]:
        """Date range filter plus up to two dimension filters on the main dataset."""
        if not self.views:
            return []
        view = self.views[0]
        filters: list[dict[str, Any]] = []
        if view.time_col:
            filters.append(make_filter("Date Range", "filter_time", [{}]))
        dims = view.dims_by_card(2, MID_CARD_MAX)
        # Prefer dimensions coming from joined dimension tables (warehouse, category…)
        dims.sort(
            key=lambda d: (
                d in view.fact.dimensions,
                not re.search(r"name|category", d),
            )
        )
        chosen: list[str] = []
        for dim in dims:
            # One filter per source table (warehouse + category, not category +
            # subcategory)
            if all(view.sources.get(dim) != view.sources.get(c) for c in chosen):
                chosen.append(dim)
        for dim in chosen[:2]:
            filters.append(
                make_filter(
                    _humanize(dim),
                    "filter_select",
                    [{"datasetId": view.dataset_id, "column": {"name": dim}}],
                )
            )
        return filters

    def run_checks(self) -> list[Section]:
        """Profile the schema, create datasets and compute C1–C5."""
        self.progress("Profiling schema…")
        self.profile()
        self.build_views()
        for view in self.views:
            self.progress(f"Creating dataset {view.dataset_name}…")
            view.dataset_id = self.ensure_dataset(
                view.dataset_name, view.sql, view.time_col
            )
        steps = [
            ("C1", "Running C1 data quality checks…", self.check_data_quality),
            ("C2", "Running C2 trend analysis…", self.check_trends),
            ("C3", "Running C3 segmentation…", self.check_segmentation),
            (
                "C4",
                "Running C4 distribution & anomaly detection…",
                self.check_distribution,
            ),
            ("C5", "Running C5 KPI computation…", self.check_kpis),
        ]
        for key, message, step in steps:
            self.progress(message)
            try:
                self.sections.append(step())
            except Exception as exc:  # pylint: disable=broad-except
                logger.exception("AI dashboard check %s failed", key)
                db.session.rollback()
                failed = {
                    "title": "Check failed",
                    "text": str(exc)[:200],
                    "severity": "warning",
                }
                self.sections.append(Section(key, f"{key} · Check", [failed]))
        return self.sections

    def all_insights(self) -> list[dict[str, Any]]:
        return [{**i, "check": s.key} for s in self.sections for i in s.insights]


# ──────────────────────────────────────────────────────────────────────────────
# Dashboard assembly (shared with the LLM agent tools)
# ──────────────────────────────────────────────────────────────────────────────

SEVERITY_ICONS = {"critical": "🔴", "warning": "🟠", "info": "🟢"}


def build_layout(  # noqa: C901
    title: str,
    summary: str,
    kpis: list[ChartPlan],
    sections: list[Section],
) -> dict[str, Any]:
    """Return a v2 position_json: summary, KPI row, then one block per section."""
    layout: dict[str, Any] = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "GRID_ID": {
            "type": "GRID",
            "id": "GRID_ID",
            "children": [],
            "parents": ["ROOT_ID"],
        },
        "HEADER_ID": {"id": "HEADER_ID", "type": "HEADER", "meta": {"text": title}},
    }
    grid = layout["GRID_ID"]["children"]

    def new_row(items: list[str]) -> None:
        row_id = f"ROW-{uuid.uuid4().hex[:10]}"
        layout[row_id] = {
            "type": "ROW",
            "id": row_id,
            "children": items,
            "parents": ["ROOT_ID", "GRID_ID"],
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
        }
        for item in items:
            layout[item]["parents"] = ["ROOT_ID", "GRID_ID", row_id]
        grid.append(row_id)

    def chart_cell(plan: ChartPlan) -> str:
        cell = f"CHART-{uuid.uuid4().hex[:10]}"
        layout[cell] = {
            "type": "CHART",
            "id": cell,
            "children": [],
            "meta": {
                "chartId": plan.chart_id,
                "width": plan.width,
                "height": plan.height,
                "sliceName": plan.title,
                "uuid": plan.uuid,
            },
        }
        return cell

    def markdown(code: str, height: int) -> str:
        cell = f"MARKDOWN-{uuid.uuid4().hex[:10]}"
        layout[cell] = {
            "type": "MARKDOWN",
            "id": cell,
            "children": [],
            "meta": {"width": 12, "height": height, "code": code},
        }
        return cell

    def rows_of(plans: list[ChartPlan]) -> None:
        current: list[str] = []
        used = 0
        for plan in plans:
            if used + plan.width > 12 and current:
                new_row(current)
                current, used = [], 0
            current.append(chart_cell(plan))
            used += plan.width
        if current:
            new_row(current)

    if summary:
        height = 14 + 5 * summary.count("\n")
        new_row([markdown(f"## Executive summary\n\n{summary}", height)])
    rows_of(kpis)
    for sec in sections:
        if not sec.charts and not sec.insights:
            continue
        bullets = (
            "\n".join(
                f"- {SEVERITY_ICONS.get(i.get('severity', 'info'), '🟢')} "
                f"**{i.get('title', '')}:** {i.get('text', '')}"
                for i in sec.insights
            )
            or "_No significant findings._"
        )
        height = 10 + 5 * max(1, len(sec.insights))
        new_row([markdown(f"### {sec.title}\n\n{bullets}", height)])
        rows_of(sec.charts)
    return layout


def make_filter(
    name: str,
    filter_type: str,
    targets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return one native filter definition scoped to the whole dashboard."""
    control: dict[str, Any] = {}
    if filter_type == "filter_select":
        control = {
            "enableEmptyFilter": False,
            "defaultToFirstItem": False,
            "multiSelect": True,
            "searchAllOptions": False,
            "inverseSelection": False,
        }
    return {
        "id": f"NATIVE_FILTER-{uuid.uuid4().hex[:10]}",
        "name": name,
        "filterType": filter_type,
        "targets": targets,
        "controlValues": control,
        "defaultDataMask": {"extraFormData": {}, "filterState": {}, "ownState": {}},
        "cascadeParentIds": [],
        "scope": {"rootPath": ["ROOT_ID"], "excluded": []},
        "type": "NATIVE_FILTER",
        "description": "",
        "chartsInScope": [],
        "tabsInScope": [],
    }


def dashboard_metadata(filters: list[dict[str, Any]]) -> dict[str, Any]:
    """Return json_metadata for a generated dashboard."""
    return {
        "native_filter_configuration": filters,
        "color_scheme": "supersetColors",
        "refresh_frequency": 0,
        "timed_refresh_immune_slices": [],
        "expanded_slices": {},
        "cross_filters_enabled": True,
        "chart_configuration": {},
        "global_chart_configuration": {
            "scope": {"rootPath": ["ROOT_ID"], "excluded": []},
            "chartsInScope": [],
        },
    }


def save_dashboard(
    title: str,
    layout: dict[str, Any],
    filters: list[dict[str, Any]],
    chart_ids: list[int],
) -> int:
    """Persist a published dashboard owned by the current user; returns its id."""
    from superset.extensions import security_manager
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    slices = (
        db.session.query(Slice).filter(Slice.id.in_(chart_ids)).all()
        if chart_ids
        else []
    )
    user_id = get_user_id()
    owner = security_manager.get_user_by_id(user_id) if user_id is not None else None
    dashboard = Dashboard(
        dashboard_title=title,
        position_json=json.dumps(layout),
        json_metadata=json.dumps(dashboard_metadata(filters)),
        published=True,
        owners=[owner] if owner else [],
        slices=slices,
    )
    db.session.add(dashboard)
    db.session.commit()
    return dashboard.id
