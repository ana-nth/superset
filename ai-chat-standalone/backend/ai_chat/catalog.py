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
# ruff: noqa: S608  (SQL is built from quoted identifiers and catalog SQL)
"""
Data catalog for the chat: what can be charted in one database schema.

A *source* is a queryable unit: a fact table joined to its dimensions (see
``profiler.py``) or a standalone table. Each column
carries a role, a default aggregate, semantics from ``semantics.yaml`` when
available, its cardinality and — for low-cardinality dimensions — its values,
which lets "orders in the north region" become ``region = 'North'``.

The catalog is cached per (database, schema) because profiling issues a few
dozen small queries.
"""

from __future__ import annotations

import dataclasses
import difflib
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import sqlglot
from sqlglot import exp

from ai_chat import config, datasources
from ai_chat.formatting import humanize as _humanize
from ai_chat.kv import kv
from ai_chat.profiler import FACT_NAME_HINTS, FactView, Profiler, TableMeta
from ai_chat.semantics import load_semantics

logger = logging.getLogger(__name__)

CATALOG_VERSION = 4
MAX_SOURCES = 12
MAX_DIMS_WITH_VALUES = 15
MAX_VALUES = 25  # values listed in the LLM prompt
MAX_KNOWN_VALUES = 300  # values kept to validate filters

# Short place codes used in column names ("jp_sales"), so "Japan" is read as
# that measure instead of being invented as a filter value
PLACE_CODES = {
    "na": "North America",
    "eu": "Europe",
    "jp": "Japan",
    "jpn": "Japan",
    "us": "United States",
    "usa": "United States",
    "uk": "United Kingdom",
    "apac": "Asia Pacific",
    "emea": "EMEA",
    "latam": "Latin America",
    "cn": "China",
    "kr": "Korea",
    "au": "Australia",
}
PROMPT_SOURCES = 3  # sources described in full in the LLM prompt
EXPLAIN_SOURCES = 8  # sources described in full when explaining the schema
OVERVIEW_VALUES = 8  # sample values per dimension in the schema view


def _place_codes(name: str) -> list[str]:
    return [p for p in re.split(r"[^a-z0-9]+", name.lower()) if p in PLACE_CODES]


class CatalogError(Exception):
    """The schema cannot be charted (no access, no tables, database down)."""


@dataclass
class ColumnInfo:
    name: str
    kind: str  # "measure" | "dimension" | "time"
    agg: str = "sum"  # default aggregate for measures
    numeric: bool = False
    description: str = ""
    unit: str = ""
    cardinality: int = 0
    values: list[str] = field(default_factory=list)
    display: str = ""  # label override (derived dimensions)
    min_value: Optional[float] = None  # numeric range, for automatic bands
    max_value: Optional[float] = None

    @property
    def label(self) -> str:
        if self.display:
            return self.display
        label = _humanize(self.name)
        for code in _place_codes(self.name):
            label = re.sub(rf"\b{code}\b", PLACE_CODES[code], label, flags=re.I)
        return label

    @property
    def place(self) -> str:
        """The place this column is limited to ('jp_sales' → 'Japan'), if any."""
        places = {PLACE_CODES[code] for code in _place_codes(self.name)}
        return places.pop() if len(places) == 1 else ""

    @property
    def values_complete(self) -> bool:
        """True when ``values`` lists every value of the column."""
        return bool(self.values) and len(self.values) >= self.cardinality


@dataclass
class SourceInfo:
    name: str
    label: str
    description: str
    sql: str
    dataset_name: str
    columns: dict[str, ColumnInfo] = field(default_factory=dict)
    time_col: str = ""
    time_min: str = ""
    time_max: str = ""
    row_count: int = 0
    main_measure: str = ""
    metrics: dict[str, dict[str, str]] = field(default_factory=dict)
    # Physical tables combined in this source (the fact first) and its grain
    tables: list[str] = field(default_factory=list)
    grain: str = ""

    def of_kind(self, kind: str) -> list[ColumnInfo]:
        return [c for c in self.columns.values() if c.kind == kind]


@dataclass
class Catalog:
    database_id: str
    schema: str
    database_name: str = ""
    dialect: str = ""
    sources: dict[str, SourceInfo] = field(default_factory=dict)
    built_at: float = 0.0

    # ── serialisation (the cache stores plain dicts) ─────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {"version": CATALOG_VERSION, **dataclasses.asdict(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Catalog:
        sources = {}
        for name, src in (data.get("sources") or {}).items():
            columns = {
                cname: ColumnInfo(**col)
                for cname, col in src.get("columns", {}).items()
            }
            sources[name] = SourceInfo(**{**src, "columns": columns})
        return cls(
            database_id=data["database_id"],
            schema=data["schema"],
            database_name=data.get("database_name", ""),
            dialect=data.get("dialect", ""),
            sources=sources,
            built_at=data.get("built_at", 0.0),
        )

    # ── lookups ──────────────────────────────────────────────────────────────

    def find_source(self, name: Optional[str]) -> Optional[SourceInfo]:
        if not name:
            return None
        if name in self.sources:
            return self.sources[name]
        key = _norm(name)
        for src in self.sources.values():
            if key in (_norm(src.name), _norm(src.label), _norm(src.dataset_name)):
                return src
        close = difflib.get_close_matches(
            key, [_norm(s) for s in self.sources], n=1, cutoff=0.8
        )
        if close:
            return next(s for s in self.sources.values() if _norm(s.name) == close[0])
        return None

    def by_importance(self) -> list[SourceInfo]:
        """Business facts (sales, orders…) first, then dated sources, then size."""

        def rank(src: SourceInfo) -> tuple[bool, bool, bool, int]:
            return (
                any(h in src.name.lower() for h in FACT_NAME_HINTS),
                bool(src.time_col),
                bool(src.of_kind("measure")),
                src.row_count,
            )

        return sorted(self.sources.values(), key=rank, reverse=True)

    def default_source(self) -> Optional[SourceInfo]:
        """The source most questions are about."""
        ranked = self.by_importance()
        return ranked[0] if ranked else None

    # ── prompt rendering ─────────────────────────────────────────────────────

    def rank_sources(self, text: str, prefer: Optional[str] = None) -> list[SourceInfo]:
        """Order sources by how many words of ``text`` they mention (schema linking)."""
        words = set(_tokens(text))

        def score(src: SourceInfo) -> tuple[float, int]:
            vocab: set[str] = set(_tokens(src.name)) | set(_tokens(src.label))
            hits = 0.0
            for col in src.columns.values():
                col_words = set(_tokens(col.name)) | set(_tokens(col.description))
                hits += len(words & set(_tokens(col.name))) * 2
                hits += len(words & col_words) * 0.5
                hits += sum(1 for v in col.values if _norm(v) and _norm(v) in words)
            for metric in src.metrics:
                hits += len(words & set(_tokens(metric))) * 2
            hits += len(words & vocab)
            if prefer and src.name == prefer:
                hits += 3
            return hits, src.row_count

        return sorted(self.sources.values(), key=score, reverse=True)

    def to_prompt(
        self, text: str, prefer: Optional[str] = None, full_count: int = PROMPT_SOURCES
    ) -> str:
        """Compact description: the most relevant sources in full, others by name."""
        ranked = self.rank_sources(text, prefer)
        full, brief = ranked[:full_count], ranked[full_count:]
        parts = [_describe_source(src) for src in full]
        if brief:
            parts.append(
                "OTHER SOURCES (ask about them by name): "
                + "; ".join(f"{s.name} ({s.label})" for s in brief)
            )
        return "\n\n".join(parts)

    def summary(self) -> list[dict[str, Any]]:
        """Short, user-facing description used by the UI and 'what data do you have'."""
        return [
            {
                "name": s.name,
                "label": s.label,
                "description": s.description,
                "rows": s.row_count,
                "time_column": s.time_col,
                "time_min": s.time_min,
                "time_max": s.time_max,
                "measures": [c.name for c in s.of_kind("measure")],
                "dimensions": [c.name for c in s.of_kind("dimension")],
                "metrics": list(s.metrics),
            }
            for s in self.by_importance()
        ]

    def overview(self) -> list[dict[str, Any]]:
        """Everything a user needs to understand the schema, for the schema view."""
        return [
            {
                "name": s.name,
                "label": s.label,
                "description": s.description,
                "grain": s.grain,
                "rows": s.row_count,
                "tables": s.tables or [s.name],
                "time_column": s.time_col,
                "time_min": s.time_min,
                "time_max": s.time_max,
                "time_columns": [c.name for c in s.of_kind("time")],
                "measures": [
                    {
                        "name": c.name,
                        "label": c.label,
                        "unit": c.unit,
                        "agg": c.agg,
                        "description": c.description,
                    }
                    for c in s.of_kind("measure")
                ],
                "dimensions": [
                    {
                        "name": c.name,
                        "label": c.label,
                        "description": c.description,
                        "cardinality": c.cardinality,
                        "values": c.values[:OVERVIEW_VALUES],
                    }
                    for c in sorted(s.of_kind("dimension"), key=lambda c: c.cardinality)
                ],
                "metrics": [
                    {
                        "name": name,
                        "label": _humanize(name),
                        "meaning": m.get("meaning", ""),
                        "unit": m.get("unit", ""),
                    }
                    for name, m in s.metrics.items()
                ],
            }
            for s in self.by_importance()
        ]

    def suggestions(self, limit: int = 6) -> list[str]:  # noqa: C901
        """Starter questions that are guaranteed to be answerable."""
        out: list[str] = []
        for src in self.by_importance():
            measure = src.columns.get(src.main_measure)
            dims = sorted(
                (d for d in src.of_kind("dimension") if 2 <= d.cardinality <= 12),
                key=lambda d: d.cardinality,
            )
            if measure is None:
                continue
            m_label = measure.label.lower()
            if src.time_col:
                out.append(f"Monthly {m_label} trend")
            if dims:
                out.append(f"Total {m_label} by {dims[0].label.lower()}")
            if len(dims) > 1:
                out.append(
                    f"Share of {m_label} by {dims[1].label.lower()} as a pie chart"
                )
            high = [d for d in src.of_kind("dimension") if 12 < d.cardinality <= 500]
            if high:
                out.append(f"Top 10 {high[0].label.lower()} by {m_label}")
            for metric in list(src.metrics)[:1]:
                if dims:
                    out.append(f"{_humanize(metric)} by {dims[0].label.lower()}")
            if len(out) >= limit:
                break
        unique: list[str] = []
        for suggestion in out:
            if suggestion.lower() not in {u.lower() for u in unique}:
                unique.append(suggestion)
        return unique[:limit]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text).lower())


_STOP = {
    "the", "a", "an", "of", "by", "for", "in", "on", "and", "or", "to", "show", "me",
    "what", "is", "are", "how", "per", "with", "chart", "graph", "plot", "fk", "id",
    "pk", "usd", "rows", "row", "one", "each", "all", "as", "at", "from", "this",
}  # fmt: skip


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", str(text).lower())
    out = []
    for word in words:
        if word in _STOP or len(word) < 2:
            continue
        out.append(word)
        if len(word) > 3 and word.endswith("s"):
            out.append(word[:-1])  # "orders" also matches "order"
    return out


def _describe_source(src: SourceInfo) -> str:  # noqa: C901
    lines = [f"SOURCE {src.name} — {src.label} ({src.row_count} rows)"]
    if src.description:
        lines.append(f"  about: {src.description[:200]}")
    if src.grain:
        lines.append(f"  grain: {src.grain[:120]}")
    if len(src.tables) > 1:
        lines.append(
            f"  combines: {src.tables[0]} joined to {', '.join(src.tables[1:])}"
        )
    if src.time_col:
        others = [c.name for c in src.of_kind("time") if c.name != src.time_col]
        extra = f"; other dates: {', '.join(others)}" if others else ""
        lines.append(
            f"  main date: {src.time_col} "
            f"(data from {src.time_min} to {src.time_max}){extra}"
        )
    measures = []
    for col in src.of_kind("measure"):
        info = col.agg
        if col.unit:
            info += f", {col.unit}"
        if col.description:
            info += f": {col.description[:50]}"
        measures.append(f"{col.name} ({info})")
    if measures:
        lines.append("  measures: " + "; ".join(measures))
    dims = []
    for col in sorted(src.of_kind("dimension"), key=lambda c: c.cardinality):
        if col.values and col.values_complete and len(col.values) <= MAX_VALUES:
            shown = ", ".join(col.values[:12])
            more = "…" if len(col.values) > 12 else ""
            dims.append(f"{col.name} [{shown}{more}]")
        else:
            dims.append(f"{col.name} ({col.cardinality} values)")
    if dims:
        lines.append("  dimensions: " + "; ".join(dims))
    if src.metrics:
        lines.append(
            "  business_metrics: "
            + "; ".join(
                f"{name} ({m.get('unit') or ''}"
                f"{': ' + m['meaning'][:60] if m.get('meaning') else ''})"
                for name, m in src.metrics.items()
            )
        )
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Building
# ──────────────────────────────────────────────────────────────────────────────


def _cache_key(database_id: str, schema: str) -> str:
    return f"ai_chat:catalog:v{CATALOG_VERSION}:{database_id}:{schema}"


def get_catalog(database_id: str, schema: str, force: bool = False) -> Catalog:
    """Return the cached catalog for ``schema``, building it when needed."""
    try:
        info = datasources.get_connection(database_id)
        datasources.check_schema(info, schema)
    except datasources.DataSourceError as exc:
        raise CatalogError(str(exc)) from exc

    key = _cache_key(database_id, schema)
    if not force:
        try:
            cached = kv().get(key)
            if cached and cached.get("version") == CATALOG_VERSION:
                return Catalog.from_dict(cached)
        except Exception:  # pylint: disable=broad-except
            logger.warning("AI chat catalog cache unreadable", exc_info=True)

    catalog = build_catalog(info, schema)
    try:
        kv().set(key, catalog.to_dict(), config.catalog_ttl())
    except Exception:  # pylint: disable=broad-except
        logger.warning("AI chat catalog could not be cached", exc_info=True)
    return catalog


def build_catalog(info: datasources.ConnectionInfo, schema: str) -> Catalog:  # noqa: C901
    started = time.monotonic()
    semantics = load_semantics(schema, info.name)
    try:
        engine = datasources.get_engine(info)
        profiler = Profiler(engine, schema, semantics)
        profiler.profile()
    except (ValueError, datasources.DataSourceError) as exc:
        raise CatalogError(str(exc)) from exc
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("AI chat could not profile %s", schema)
        raise CatalogError(
            f"Could not read schema '{schema}': {str(exc)[:200]}"
        ) from exc
    try:
        profiler.build_views()
    except ValueError:
        profiler.views = []  # no fact tables: standalone tables only

    sem_tables: dict[str, Any] = semantics.get("tables") or {}
    business: dict[str, Any] = semantics.get("business_metrics") or {}

    catalog = Catalog(
        database_id=info.id,
        schema=schema,
        database_name=info.name,
        dialect=engine.dialect.name,
    )
    covered: set[str] = set()
    for view in profiler.views:
        src = _source_from_view(view, sem_tables)
        catalog.sources[src.name] = src
        covered.add(view.fact.name)
    for table in sorted(profiler.tables.values(), key=lambda t: -t.row_count):
        if len(catalog.sources) >= MAX_SOURCES:
            break
        if table.name in covered or table.row_count == 0:
            continue
        src = _source_from_table(profiler, table, sem_tables)
        if src.columns:
            catalog.sources[src.name] = src

    if not catalog.sources:
        raise CatalogError(f"Schema '{schema}' has no non-empty tables to chart.")

    for src in catalog.sources.values():
        _attach_business_metrics(src, business)
        _profile_values(profiler, src)
        _profile_time(profiler, src)
        _profile_ranges(profiler, src)
        for col in src.columns.values():
            if col.place and not col.description:
                col.description = col.label

    catalog.built_at = time.time()
    logger.info(
        "AI chat catalog for %s.%s built in %.1fs (%d sources)",
        info.name,
        schema,
        time.monotonic() - started,
        len(catalog.sources),
    )
    return catalog


def _sem_column(sem_tables: dict[str, Any], table: str, column: str) -> dict[str, Any]:
    cols = (sem_tables.get(table) or {}).get("columns") or {}
    if column in cols:
        return cols[column] or {}
    # Joined dimension columns may be prefixed ("warehouse_city" → city)
    prefix = re.sub(r"^dim_", "", table) + "_"
    if column.startswith(prefix):
        return cols.get(column[len(prefix) :]) or {}
    return {}


def _table_description(sem_tables: dict[str, Any], table: str) -> str:
    text = str((sem_tables.get(table) or {}).get("description") or "")
    return re.sub(r"\s+", " ", text).strip()


def _table_grain(sem_tables: dict[str, Any], table: str) -> str:
    text = str((sem_tables.get(table) or {}).get("grain") or "")
    return re.sub(r"\s+", " ", text).strip()


def _source_from_view(view: FactView, sem_tables: dict[str, Any]) -> SourceInfo:
    fact = view.fact
    src = SourceInfo(
        name=fact.name,
        label=_humanize(re.sub(r"^(fact|fct|f)_", "", fact.name)),
        description=_table_description(sem_tables, fact.name),
        sql=view.sql,
        dataset_name=view.dataset_name,
        time_col=view.time_col,
        row_count=fact.row_count,
        tables=[
            fact.name,
            *sorted({t for t in view.sources.values() if t != fact.name}),
        ],
        grain=_table_grain(sem_tables, fact.name),
    )
    for col in fact.time_cols:
        src.columns[col] = ColumnInfo(name=col, kind="time")
    for col, agg in view.measures.items():
        sem = _sem_column(sem_tables, fact.name, col)
        src.columns[col] = ColumnInfo(
            name=col,
            kind="measure",
            agg=agg,
            numeric=True,
            description=str(sem.get("description") or ""),
            unit=str(sem.get("unit") or ""),
            cardinality=view.cardinality.get(col, 0),
        )
    for dim in view.dimensions:
        sem = _sem_column(sem_tables, view.sources.get(dim, fact.name), dim)
        src.columns[dim] = ColumnInfo(
            name=dim,
            kind="dimension",
            description=str(sem.get("description") or ""),
            cardinality=view.cardinality.get(dim, 0),
        )
    src.main_measure = view.main_measure
    return src


def _source_from_table(
    profiler: Profiler, table: TableMeta, sem_tables: dict[str, Any]
) -> SourceInfo:
    sql = f"SELECT * FROM {profiler.tbl(table.name)}"
    src = SourceInfo(
        name=table.name,
        label=_humanize(re.sub(r"^(dim|d)_", "", table.name)),
        description=_table_description(sem_tables, table.name),
        sql=sql,
        dataset_name=f"ai_{table.name}",
        time_col=table.time_cols[0] if table.time_cols else "",
        row_count=table.row_count,
        tables=[table.name],
        grain=_table_grain(sem_tables, table.name),
    )
    cardinality = profiler.cardinality(sql, [*table.dimensions, *table.measures])
    for col in table.time_cols:
        src.columns[col] = ColumnInfo(name=col, kind="time")
    for col, agg in table.measures.items():
        if cardinality.get(col, 2) < 2:
            continue
        sem = _sem_column(sem_tables, table.name, col)
        src.columns[col] = ColumnInfo(
            name=col,
            kind="measure",
            agg=agg,
            numeric=True,
            description=str(sem.get("description") or ""),
            unit=str(sem.get("unit") or ""),
            cardinality=cardinality.get(col, 0),
        )
    for col in table.dimensions:
        if cardinality.get(col, 2) < 2:
            continue
        sem = _sem_column(sem_tables, table.name, col)
        src.columns[col] = ColumnInfo(
            name=col,
            kind="dimension",
            description=str(sem.get("description") or ""),
            cardinality=cardinality.get(col, 0),
        )
    measures = src.of_kind("measure")
    sums = [m for m in measures if m.agg == "sum"]
    src.main_measure = (sums or measures)[0].name if measures else ""
    return src


def _attach_business_metrics(src: SourceInfo, business: dict[str, Any]) -> None:
    """Attach semantics.yaml metrics whose every column exists in ``src``."""
    for name, spec in business.items():
        sql = str((spec or {}).get("sql") or "")
        if not sql:
            continue
        try:
            columns = {c.name for c in sqlglot.parse_one(sql).find_all(exp.Column)}
        except Exception:  # pylint: disable=broad-except
            logger.warning("Skipping business metric %s: unparseable SQL", name)
            continue
        if columns and columns <= set(src.columns):
            src.metrics[name] = {
                "sql": sql,
                "meaning": str(spec.get("meaning") or ""),
                "unit": str(spec.get("unit") or ""),
            }


def _profile_values(profiler: Profiler, src: SourceInfo) -> None:
    """Record the values of low-cardinality dimensions."""
    dims = sorted(
        (d for d in src.of_kind("dimension") if 0 < d.cardinality <= MAX_KNOWN_VALUES),
        key=lambda d: d.cardinality,
    )[:MAX_DIMS_WITH_VALUES]
    for dim in dims:
        col = profiler.q(dim.name)
        rows = profiler.safe_rows(
            profiler.limit(
                f"SELECT DISTINCT {col} AS {profiler.q('v')} FROM ({src.sql}) s "
                f"WHERE {col} IS NOT NULL ORDER BY {col}",
                MAX_KNOWN_VALUES + 1,
            )
        )
        if rows:
            dim.values = [str(r["v"]) for r in rows][:MAX_KNOWN_VALUES]
            dim.cardinality = max(dim.cardinality, len(dim.values))


def _profile_ranges(profiler: Profiler, src: SourceInfo) -> None:
    """Record MIN/MAX of numeric measures (one query per source)."""
    measures = [c for c in src.of_kind("measure") if c.numeric]
    if not measures:
        return
    parts = []
    for i, col in enumerate(measures):
        quoted = profiler.q(col.name)
        parts += [
            f"MIN({quoted}) AS {profiler.q(f'lo{i}')}",
            f"MAX({quoted}) AS {profiler.q(f'hi{i}')}",
        ]
    rows = profiler.safe_rows(f"SELECT {', '.join(parts)} FROM ({src.sql}) s")
    if not rows:
        return
    for i, col in enumerate(measures):
        try:
            lo, hi = float(rows[0][f"lo{i}"]), float(rows[0][f"hi{i}"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(lo) and math.isfinite(hi):
            col.min_value, col.max_value = lo, hi


def _profile_time(profiler: Profiler, src: SourceInfo) -> None:
    if not src.time_col:
        return
    col = profiler.q(src.time_col)
    rows = profiler.safe_rows(
        f"SELECT MIN({col}) AS {profiler.q('lo')}, MAX({col}) AS {profiler.q('hi')} "
        f"FROM ({src.sql}) s"
    )
    if rows:
        lo, hi = rows[0]["lo"], rows[0]["hi"]
        src.time_min = str(lo)[:10] if lo is not None else ""
        src.time_max = str(hi)[:10] if hi is not None else ""
