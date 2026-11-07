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
"""Derived dimensions: weekday, month of year, bands, value groups."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from ai_chat import derived as dv
from ai_chat.catalog import Catalog, ColumnInfo, SourceInfo
from ai_chat.charts import choose_viz, render_config
from ai_chat.executor import Query
from ai_chat.intent import ChartIntent, DerivedIntent, parse_derived_phrase
from ai_chat.narrate import narrate
from ai_chat.resolver import resolve, ResolvedSpec
from ai_chat.rules import plan_with_rules
from ai_chat.sqlgen import build_query


def quote(name: str) -> str:
    return f'"{name}"'


@pytest.fixture
def catalog() -> Catalog:
    sales = SourceInfo(
        name="fact_sales",
        label="Sales",
        description="One row per order line",
        sql="SELECT * FROM wh.fact_sales",
        dataset_name="ai_fact_sales",
        time_col="order_date",
        time_min="2024-10-19",
        time_max="2025-10-19",
        row_count=60000,
        main_measure="line_amount",
    )
    columns = [
        ColumnInfo("order_date", "time"),
        ColumnInfo("ship_date", "time"),
        ColumnInfo("line_amount", "measure", "sum", True, "Net revenue USD", "USD", 5000),  # noqa: E501
        ColumnInfo(
            "unit_price", "measure", "avg", True, "Unit price USD", "USD", 900,
            min_value=4.5, max_value=980.0,
        ),
        ColumnInfo("quantity", "measure", "sum", True, "Units ordered", "units", 140),
        ColumnInfo(
            "region", "dimension", cardinality=4,
            values=["Midwest", "Northeast", "Southeast", "West"],
        ),
        ColumnInfo(
            "sales_channel", "dimension", cardinality=4,
            values=["Direct", "Distributor", "Online", "Retail"],
        ),
    ]  # fmt: skip
    sales.columns = {c.name: c for c in columns}
    return Catalog(
        database_id="1",
        schema="wh",
        database_name="warehouse",
        sources={"fact_sales": sales},
    )


def _resolve(intent: dict[str, Any], catalog: Catalog, **kw: Any):
    return resolve(ChartIntent.model_validate(intent), catalog, quote, **kw)


# ──────────────────────────────────────────────────────────────────────────────
# SQL
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "dialect,expected",
    [
        ("postgresql", "EXTRACT(DOW FROM"),
        ("redshift", "MOD("),
        ("mysql", "WEEKDAY("),
        ("sqlite", "STRFTIME('%w'"),
        ("mssql", "@@DATEFIRST"),
        ("oracle", "TRUNC(\"d\", 'IW')"),
        ("bigquery", "EXTRACT(DAYOFWEEK FROM"),
    ],
)
def test_weekday_sql_per_dialect(dialect: str, expected: str) -> None:
    dim = dv.compile_date_part("weekday", "d", quote, dialect)
    assert expected in dim.sql
    assert dim.values[0] == "1 Mon"
    assert dim.values[-1] == "7 Sun"
    assert dim.name == "d__weekday"


def _run(sql: str, rows: list[Any], col_type: str = "TEXT") -> list[Any]:
    conn = sqlite3.connect(":memory:")
    conn.execute(f'CREATE TABLE t ("d" {col_type})')
    conn.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    query = f"SELECT {sql} FROM t ORDER BY rowid"  # noqa: S608
    return [r[0] for r in conn.execute(query)]


def test_date_parts_compute_correct_values_in_sqlite() -> None:
    # 2025-01-04 is a Saturday, 2025-01-06 a Monday, 2025-03-09 a Sunday
    dates = ["2025-01-04 09:30:00", "2025-01-06 15:00:00", "2025-03-09 23:59:00"]
    sql = lambda part: dv.compile_date_part(part, "d", quote, "sqlite").sql  # noqa: E731
    assert _run(sql("weekday"), dates) == ["6 Sat", "1 Mon", "7 Sun"]
    assert _run(sql("day_type"), dates) == ["Weekend", "Weekday", "Weekend"]
    assert _run(sql("month"), dates) == ["01 Jan", "01 Jan", "03 Mar"]
    assert _run(sql("quarter"), dates) == ["Q1", "Q1", "Q1"]
    assert _run(sql("hour"), dates) == ["09", "15", "23"]
    assert _run(sql("year"), dates) == [2025, 2025, 2025]


def test_year_values_come_from_the_date_coverage() -> None:
    dim = dv.compile_date_part(
        "year", "d", quote, "postgresql", time_min="2023-05-01", time_max="2025-01-31"
    )
    assert dim.values == ["2023", "2024", "2025"]
    assert dim.numeric


def test_bucket_labels_sort_in_order_and_compute_in_sqlite() -> None:
    dim = dv.compile_bucket("d", quote, [500, 100], column_label="Price")
    assert dim.values == ["1. under 100", "2. 100-500", "3. 500+"]
    assert dim.label == "Price Band"
    assert _run(dim.sql, [5, 100, 499.9, 750, None], "REAL") == [
        "1. under 100",
        "2. 100-500",
        "2. 100-500",
        "3. 500+",
        None,
    ]


def test_bucket_edges_default_to_nice_bands_and_drop_empty_ones() -> None:
    assert dv.nice_edges(0, 1000) == [200, 400, 600, 800]
    auto = dv.compile_bucket("d", quote, lo=4.5, hi=980.0)
    assert auto.params["edges"] == [200, 400, 600, 800]
    # An edge at or below the minimum would make an empty "under 0" band
    assert dv.compile_bucket("d", quote, [0, 50, 200], lo=4.5).params["edges"] == [
        50,
        200,
    ]


def test_bucket_errors() -> None:
    with pytest.raises(dv.DerivedError, match="range"):
        dv.compile_bucket("d", quote)
    with pytest.raises(dv.DerivedError, match="at most"):
        dv.compile_bucket("d", quote, list(range(1, 12)))
    with pytest.raises(dv.DerivedError, match="not a number"):
        dv.compile_bucket("d", quote, ["ten"])


def test_group_escapes_literals_and_adds_other() -> None:
    dim = dv.compile_group(
        "d", quote, {"East's <side>": ["O'Brien", "Northeast"], "West": ["West"]}
    )
    assert "'O''Brien'" in dim.sql
    assert dim.values == ["Easts side", "West", "Other"]
    assert _run(dim.sql, ["O'Brien", "West", "Midwest"]) == [
        "Easts side",
        "West",
        "Other",
    ]


def test_filter_sql() -> None:
    dim = dv.compile_date_part("day_type", "d", quote, "postgresql")
    assert dv.filter_sql(dim, "==", "Weekend").endswith(" = 'Weekend'")
    assert dv.filter_sql(dim, "NOT IN", ["Weekday"]).endswith(" NOT IN ('Weekday')")
    year = dv.compile_date_part("year", "d", quote, "postgresql")
    assert dv.filter_sql(year, ">=", "2024").endswith(" >= 2024")
    with pytest.raises(dv.DerivedError):
        dv.filter_sql(dim, "ILIKE", "%x%")


@pytest.mark.parametrize(
    "part,value,expected",
    [
        ("weekday", "Saturday", "6 Sat"),
        ("weekday", "mon", "1 Mon"),
        ("weekday", "tues", "2 Tue"),
        ("month", "March", "03 Mar"),
        ("month", "12", "12 Dec"),
        ("day_type", "weekends", "Weekend"),
        ("day_type", "Sunday", "Weekend"),
        ("quarter", "q3", "Q3"),
        ("hour", "3pm", "15"),
        ("hour", "9", "09"),
        ("weekday", "someday", None),
    ],
)
def test_match_value(part: str, value: str, expected: str | None) -> None:
    dim = dv.compile_date_part(part, "d", quote, "postgresql")
    assert dv.match_value(dim, value) == expected


# ──────────────────────────────────────────────────────────────────────────────
# Intent parsing
# ──────────────────────────────────────────────────────────────────────────────


def test_intent_extracts_derivations_from_dimensions_and_filters() -> None:
    intent = ChartIntent.model_validate(
        {
            "dimensions": [
                {"derive": "day of week", "column": "order_date"},
                "region",
                "month_of_year(order_date)",
                {"derive": "bands", "col": "unit_price", "edges": "0-50, 50-200"},
            ],
            "filters": [
                {"column": "order_date", "derive": "weekend", "value": "Weekend"},
                {"column": {"derive": "quarter"}, "value": "Q1"},
            ],
        }
    )
    assert intent.dimensions == [
        "order_date__weekday",
        "region",
        "order_date__month",
    ]  # three dimensions at most
    assert intent.derivations["unit_price__bucket"].edges == [0, 50, 200]
    assert [f.column for f in intent.filters] == ["order_date__day_type", "quarter"]
    assert intent.derivations["quarter"].column is None


def test_groups_accept_a_list_of_named_groups() -> None:
    derived = DerivedIntent.model_validate(
        {
            "derive": "group",
            "column": "region",
            "groups": [{"name": "East", "values": "Northeast, Southeast"}],
        }
    )
    assert derived.groups == {"East": ["Northeast", "Southeast"]}


def test_phrases() -> None:
    phrase = parse_derived_phrase("day of week of ship date")
    assert phrase
    assert (phrase.derive, phrase.column) == ("weekday", "ship_date")
    phrase = parse_derived_phrase("unit price bands")
    assert phrase
    assert (phrase.derive, phrase.column) == ("bucket", "unit_price")
    phrase = parse_derived_phrase("weekday")
    assert phrase
    assert (phrase.derive, phrase.column) == ("weekday", None)
    assert parse_derived_phrase("month") is None  # a monthly trend
    assert parse_derived_phrase("region") is None


# ──────────────────────────────────────────────────────────────────────────────
# Resolution
# ──────────────────────────────────────────────────────────────────────────────


def test_weekday_on_the_main_date(catalog: Catalog) -> None:
    res = _resolve(
        {"metrics": [{"column": "line_amount"}], "dimensions": ["day of week"]},
        catalog,
    )
    assert res.ok, res.problems
    spec = res.spec
    assert spec.dimensions == ["order_date__weekday"]
    assert spec.derived["order_date__weekday"].label == "Day of Week"
    assert spec.time_grain is None
    assert spec.title == "Total Line Amount by Day of Week"
    # The cached catalog is untouched
    assert "order_date__weekday" not in catalog.sources["fact_sales"].columns


def test_other_date_columns_are_named_in_the_label(catalog: Catalog) -> None:
    res = _resolve({"dimensions": [{"derive": "month", "column": "ship"}]}, catalog)
    assert res.ok, res.problems
    assert res.spec.derived["ship_date__month"].label == "Ship Date Month of Year"


def test_bare_grain_word_is_a_trend(catalog: Catalog) -> None:
    res = _resolve({"dimensions": ["month"]}, catalog)
    assert res.ok, res.problems
    assert res.spec.time_grain == "P1M"
    assert not res.spec.derived


def test_derivation_errors_become_problems(catalog: Catalog) -> None:
    res = _resolve({"dimensions": [{"derive": "weekday", "column": "region"}]}, catalog)
    assert res.problems
    assert "not a date column" in res.problems[0]
    assert len(res.problems) == 1  # not reported again as an unknown dimension

    res = _resolve({"dimensions": [{"derive": "bucket", "column": "region"}]}, catalog)
    assert res.problems

    res = _resolve(
        {
            "dimensions": [
                {"derive": "group", "column": "region", "groups": {"E": ["Narnia"]}}
            ]
        },
        catalog,
    )
    assert res.problems
    assert "Narnia" in res.problems[0]


def test_bucket_uses_profiled_range(catalog: Catalog) -> None:
    res = _resolve(
        {
            "metrics": [{"aggregate": "count"}],
            "dimensions": [{"derive": "bucket", "column": "unit_price"}],
        },
        catalog,
    )
    assert res.ok, res.problems
    dim = res.spec.derived["unit_price__bucket"]
    assert dim.values[0] == "1. under 200"


def test_group_values_are_matched(catalog: Catalog) -> None:
    res = _resolve(
        {
            "dimensions": [
                {
                    "derive": "group",
                    "column": "region",
                    "groups": {"East": ["north east", "southeast"]},
                }
            ]
        },
        catalog,
    )
    assert res.ok, res.problems
    dim = res.spec.derived["region__group"]
    assert dim.params["groups"] == {"East": ["Northeast", "Southeast"]}
    assert dim.values == ["East", "Other"]


def test_filter_on_a_derived_value(catalog: Catalog) -> None:
    res = _resolve(
        {
            "dimensions": ["region"],
            "filters": [
                {"column": "order_date", "derive": "day_type", "value": "weekends"}
            ],
        },
        catalog,
    )
    assert res.ok, res.problems
    flt = res.spec.filters[0]
    assert (flt.column, flt.operator, flt.value) == (
        "order_date__day_type",
        "==",
        "Weekend",
    )
    assert "order_date__day_type" in res.spec.derived

    res = _resolve(
        {"filters": [{"column": "order_date", "derive": "weekday", "value": "Funday"}]},
        catalog,
    )
    assert res.problems
    assert "not a value" in res.problems[0]


def test_follow_up_round_trip(catalog: Catalog) -> None:
    first = _resolve(
        {
            "dimensions": [
                {"derive": "bucket", "column": "unit_price", "edges": [100]}
            ],
            "filters": [
                {"column": "order_date", "derive": "day_type", "value": "Weekend"}
            ],
        },
        catalog,
    ).spec
    stored = ResolvedSpec.from_dict(first.to_dict())
    assert stored.derived == first.derived
    again = _resolve(stored.to_intent_dict(), catalog, previous=stored)
    assert again.ok, again.problems
    assert again.spec.derived == first.derived
    assert again.spec.filters == first.filters
    # The LLM may copy the canonical name back; the previous edges are kept
    by_name = _resolve({"dimensions": ["unit_price__bucket"]}, catalog, previous=stored)
    assert by_name.spec.derived["unit_price__bucket"].params["edges"] == [100]


# ──────────────────────────────────────────────────────────────────────────────
# Charts and narration
# ──────────────────────────────────────────────────────────────────────────────


def test_query_selects_the_expression_by_label(catalog: Catalog) -> None:
    res = _resolve(
        {
            "metrics": [{"column": "line_amount"}],
            "dimensions": [{"derive": "weekday", "column": "order_date"}],
            "filters": [
                {"column": "order_date", "derive": "day_type", "value": "Weekday"}
            ],
        },
        catalog,
        dialect="sqlite",
    )
    spec = res.spec
    src = catalog.sources["fact_sales"]
    viz, _ = choose_viz(spec, src)
    assert viz == "bar"
    assert render_config(spec, viz, src)["x"] == "Day of Week"
    query = build_query(spec, viz, src.sql, "sqlite", quote)
    assert 'END) AS "Day of Week"' in query.sql
    # Derived values are bound parameters, and the axis is ordered Mon→Sun
    assert "Weekday" in query.params.values()
    assert query.sql.rstrip().split("ORDER BY ")[1].startswith("(CASE")


def test_colons_in_group_values_are_escaped_for_text(catalog: Catalog) -> None:
    src = catalog.sources["fact_sales"]
    src.columns["region"].values = ["East:1", "West"]
    res = _resolve(
        {
            "dimensions": [
                {"derive": "group", "column": "region", "groups": {"A": ["East:1"]}}
            ]
        },
        catalog,
    )
    assert res.ok, res.problems
    query = build_query(res.spec, "bar", src.sql, "postgresql", quote)
    assert r"'East\:1'" in query.sql
    assert isinstance(query, Query)


def test_pie_of_a_derived_dimension(catalog: Catalog) -> None:
    res = _resolve({"dimensions": ["weekend vs weekday"], "chart_type": "pie"}, catalog)
    assert res.ok, res.problems
    viz, notes = choose_viz(res.spec, catalog.sources["fact_sales"])
    assert viz == "pie"
    assert not notes


def test_narration_reads_rows_by_label(catalog: Catalog) -> None:
    spec = _resolve(
        {"metrics": [{"column": "line_amount"}], "dimensions": ["day of week"]},
        catalog,
    ).spec
    rows = [
        {"Day of Week": "1 Mon", "Total Line Amount": 100},
        {"Day of Week": "6 Sat", "Total Line Amount": 300},
    ]
    text = narrate(spec, "bar", rows)
    assert text.startswith("Top day of week by Total Line Amount: 6 Sat")


# ──────────────────────────────────────────────────────────────────────────────
# Keyword planner (LLM down)
# ──────────────────────────────────────────────────────────────────────────────


def test_rules_day_of_week_is_not_a_trend(catalog: Catalog) -> None:
    intent = plan_with_rules("revenue by day of week", catalog, None)
    assert intent.dimensions == ["order_date__weekday"]
    assert intent.time_grain is None


def test_rules_month_of_year(catalog: Catalog) -> None:
    intent = plan_with_rules("revenue by month of year", catalog, None)
    assert intent.dimensions == ["order_date__month"]
    assert intent.time_grain is None


def test_rules_weekend_vs_weekday(catalog: Catalog) -> None:
    intent = plan_with_rules("weekend vs weekday revenue", catalog, None)
    assert intent.dimensions == ["order_date__day_type"]
    assert not intent.filters


def test_rules_bands(catalog: Catalog) -> None:
    intent = plan_with_rules("order lines by unit price bands 1000-5000", catalog, None)
    assert intent.dimensions == ["unit_price__bucket"]
    assert intent.derivations["unit_price__bucket"].edges == [1000, 5000]
    assert intent.time_range is None  # "1000-5000" is not a period
    assert [m.aggregate for m in intent.metrics] == ["count"]


def test_rules_only_weekends_follow_up(catalog: Catalog) -> None:
    previous = _resolve(
        {"metrics": [{"column": "line_amount"}], "dimensions": ["region"]}, catalog
    ).spec
    intent = plan_with_rules("only weekends", catalog, previous)
    assert intent.action == "modify_chart"
    assert intent.dimensions == ["region"]
    assert [f.column for f in intent.filters] == ["order_date__day_type"]
    res = resolve(intent, catalog, quote, previous)
    assert res.ok, res.problems
    assert res.spec is not None
    assert res.spec.filters[0].value == "Weekend"


# ──────────────────────────────────────────────────────────────────────────────
# Shapes seen from llama3.1:8b in end-to-end runs
# ──────────────────────────────────────────────────────────────────────────────


def test_dimension_with_a_value_is_a_filter(catalog: Catalog) -> None:
    # "only Saturdays" came back as a dimension carrying operator and value
    intent = ChartIntent.model_validate(
        {
            "metrics": [{"column": None, "aggregate": "count"}],
            "dimensions": [
                {
                    "derive": "weekday",
                    "column": "order_date",
                    "operator": "==",
                    "value": "Saturday",
                }
            ],
        }
    )
    assert intent.dimensions == []
    assert [f.column for f in intent.filters] == ["order_date__weekday"]
    res = resolve(intent, catalog, quote)
    assert res.ok, res.problems
    assert res.spec is not None
    assert (res.spec.filters[0].column, res.spec.filters[0].value) == (
        "order_date__weekday",
        "6 Sat",
    )


def test_typed_band_edges_win_over_dropped_ones(catalog: Catalog) -> None:
    # The LLM answered "bands 50, 200, 500" with edges [50, 200]
    res = _resolve(
        {
            "dimensions": [
                {"derive": "bucket", "column": "unit_price", "edges": [50, 200]}
            ]
        },
        catalog,
        question="number of order lines by unit price bands 50, 200, 500 for 2024",
    )
    assert res.ok, res.problems
    assert res.spec.derived["unit_price__bucket"].params["edges"] == [50, 200, 500]


def test_find_edges() -> None:
    text = "price bands 0-50, 50-200 and 1,000 in 2024"
    found = dv.find_edges(text)
    assert found is not None
    start, end, edges = found
    assert text[start:end] == "bands 0-50, 50-200 and 1,000"
    assert edges == [0, 50, 200, 1000]
    assert dv.find_edges("revenue by region") is None


def test_named_days_replace_a_weekend_filter(catalog: Catalog) -> None:
    # "only Saturdays" came back as day_type = Weekend, which keeps Sundays too
    res = _resolve(
        {
            "dimensions": [{"derive": "weekday", "column": "order_date"}],
            "filters": [
                {"column": "order_date", "derive": "day_type", "value": "Weekend"}
            ],
        },
        catalog,
        question="only Saturdays",
    )
    assert res.ok, res.problems
    flt = res.spec.filters[0]
    assert (flt.column, flt.operator, flt.value) == (
        "order_date__weekday",
        "==",
        "6 Sat",
    )
    assert "order_date__day_type" not in res.spec.derived
    assert "Filtered to Saturday only." in res.notes
    # "weekends" keeps the Weekend filter
    kept = _resolve(
        {
            "filters": [
                {"column": "order_date", "derive": "day_type", "value": "Weekend"}
            ]
        },
        catalog,
        question="saturday and sunday, i.e. weekends",
    )
    assert kept.spec.filters[0].column == "order_date__day_type"
