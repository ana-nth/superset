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
Schema profiling: column roles (time / measure / dimension), foreign keys, row
counts and one denormalised view per fact table (the fact joined to its
dimensions). Roles come from ``semantics.yaml`` when it describes the schema,
otherwise from column names and types.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import Engine, inspect

from ai_chat import config, dialects, executor
from ai_chat.executor import Query

logger = logging.getLogger(__name__)

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
NUMERIC_TYPES = (
    "INT",
    "NUMERIC",
    "NUMBER",
    "DECIMAL",
    "FLOAT",
    "DOUBLE",
    "REAL",
    "MONEY",
)
TIME_TYPES = ("DATE", "TIME")
LOW_CARD_MAX = 12
MAX_FACT_VIEWS = 3
# Numeric columns that identify or locate things rather than measure them
NON_MEASURE_RE = re.compile(
    r"(^|_)(lat|lon|lng|latitude|longitude|zip|zipcode|postcode|postal|code|year|yr"
    r"|month|day|week|quarter|qtr|number|num|no|phone|rank|x|y|index|idx|key)($|_)"
)
# Numeric measures that should be averaged, not summed
AVG_MEASURE_RE = re.compile(
    r"price|rate|pct|percent|ratio|avg|mean|score|rating|(^|_)age($|_)|temperature"
    r"|margin|_per_"
)


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
    """A denormalised view: one fact table joined to its dimensions."""

    fact: TableMeta
    sql: str
    dataset_name: str
    time_col: str
    measures: dict[str, str]
    dimensions: list[str]
    cardinality: dict[str, int] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)  # dimension -> table

    @property
    def main_measure(self) -> str:
        sums = [c for c, a in self.measures.items() if a == "sum"]
        candidates = sums or list(self.measures)
        for hint in MAIN_MEASURE_HINTS:
            for col in candidates:
                if hint in col.lower():
                    return col
        return candidates[0]


def _type_name(col_type: Any, dialect: Any) -> str:
    try:
        return str(col_type.compile(dialect=dialect)).upper()
    except Exception:  # pylint: disable=broad-except
        return type(col_type).__name__.upper()


class Profiler:
    """Profiles one schema of one database."""

    def __init__(self, engine: Engine, schema: str, semantics: dict[str, Any]) -> None:
        self.engine = engine
        self.schema = schema
        self.dialect = engine.dialect.name
        self.semantics = semantics or {}
        self.tables: dict[str, TableMeta] = {}
        self.views: list[FactView] = []
        self._quote = executor.quoter(engine.dialect)

    # ── SQL helpers ──────────────────────────────────────────────────────────

    def q(self, ident: str) -> str:
        return self._quote(ident)

    def tbl(self, table: str) -> str:
        return f"{self.q(self.schema)}.{self.q(table)}"

    def limit(self, sql: str, n: int, ordered: bool = True) -> str:
        return dialects.limit(sql, n, self.dialect, ordered)

    def rows(self, sql: str) -> list[dict[str, Any]]:
        return executor.run(self.engine, Query(sql))

    def safe_rows(self, sql: str) -> Optional[list[dict[str, Any]]]:
        try:
            return self.rows(sql)
        except Exception as exc:  # pylint: disable=broad-except
            logger.info("AI chat profiling query skipped (%s): %s", exc, sql[:200])
            return None

    # ── Profiling ────────────────────────────────────────────────────────────

    def _table_names(self, insp: Any) -> list[str]:
        names = sorted(insp.get_table_names(schema=self.schema))
        try:
            views = sorted(insp.get_view_names(schema=self.schema))
        except Exception:  # pylint: disable=broad-except
            views = []
        return [*names, *[v for v in views if v not in names]][: config.max_tables()]

    def profile(self) -> None:  # noqa: C901
        """Collect table/column roles from semantics.yaml and the database."""
        sem_tables: dict[str, Any] = self.semantics.get("tables") or {}
        with self.engine.connect() as conn:
            insp = inspect(conn)
            table_names = self._table_names(insp)
            if not table_names:
                raise ValueError(f"Schema '{self.schema}' has no tables")

            for name in table_names:
                try:
                    cols = insp.get_columns(name, schema=self.schema)
                except Exception as exc:  # pylint: disable=broad-except
                    logger.warning("Cannot read columns of %s: %s", name, exc)
                    continue
                meta = TableMeta(name=name)
                sem_cols = (sem_tables.get(name) or {}).get("columns") or {}
                pk_like = {f"{name}_id", "id"}
                for col in cols:
                    cname = col["name"]
                    ctype = _type_name(col.get("type"), self.engine.dialect)
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
                    for fk in insp.get_foreign_keys(name, schema=self.schema):
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

        if not self.tables:
            raise ValueError(f"No table in schema '{self.schema}' could be read")

        # Semantic joins complement database FKs
        for join in self.semantics.get("joins", []) if sem_tables else []:
            try:
                lt, lc = join["left"].split(".")
                rt, rc = join["right"].split(".")
            except (KeyError, ValueError, AttributeError, TypeError):
                continue
            if lt in self.tables and (lc, rt, rc) not in self.tables[lt].fks:
                self.tables[lt].fks.append((lc, rt, rc))

        alias_t, alias_n = self.q("t"), self.q("n")
        counts = " UNION ALL ".join(
            f"SELECT '{t.replace(chr(39), chr(39) * 2)}' AS {alias_t}, "
            f"COUNT(*) AS {alias_n} FROM {self.tbl(t)}"
            for t in self.tables
        )
        rows = self.safe_rows(counts)
        if rows is None:
            # One unreadable table must not hide the others
            rows = []
            for t in self.tables:
                one = self.safe_rows(f"SELECT COUNT(*) AS {alias_n} FROM {self.tbl(t)}")
                if one:
                    rows.append({"t": t, "n": one[0]["n"]})
        for row in rows:
            name = str(row["t"])
            if name in self.tables:
                self.tables[name].row_count = int(row["n"] or 0)

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

    def cardinality(self, sql: str, cols: list[str]) -> dict[str, int]:
        if not cols:
            return {}
        card_sql = (
            "SELECT "
            + ", ".join(f"COUNT(DISTINCT {self.q(c)}) AS {self.q(c)}" for c in cols)
            + f" FROM ({sql}) v"
        )
        rows = self.safe_rows(card_sql)
        if not rows:
            return {}
        return {c: int(rows[0][c] or 0) for c in cols if c in rows[0]}

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

        for fact in facts[:MAX_FACT_VIEWS]:
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
            view.cardinality = self.cardinality(sql, [*dims, *view.measures])
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
