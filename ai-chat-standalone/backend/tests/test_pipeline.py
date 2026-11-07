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
Unit tests for the planner, resolver, rules, chart choice and narration. They
use a synthetic catalog, so no database or LLM is needed:

  pytest tests/test_pipeline.py -q
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import patch

import pytest

from ai_chat import timerange
from ai_chat.catalog import Catalog, ColumnInfo, SourceInfo
from ai_chat.charts import choose_viz, render_config
from ai_chat.executor import render_sql
from ai_chat.intent import ChartIntent, MetricIntent, parse_time_range
from ai_chat.llm import extract_json, LLMBadOutputError
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
        metrics={
            "gross_margin": {
                "sql": "100.0*(SUM(line_amount)-SUM(cost_amount))"
                "/NULLIF(SUM(line_amount),0)",
                "meaning": "Gross margin %",
                "unit": "%",
            }
        },
    )
    columns = [
        ColumnInfo("order_date", "time"),
        ColumnInfo("line_amount", "measure", "sum", True, "Net revenue USD", "USD", 5000),  # noqa: E501
        ColumnInfo("cost_amount", "measure", "sum", True, "COGS USD", "USD", 5000),
        ColumnInfo("quantity", "measure", "sum", True, "Units ordered", "units", 140),
        ColumnInfo(
            "discount_pct", "measure", "avg", True, "Discount % (Retail)", "%", 300
        ),
        ColumnInfo(
            "sales_channel", "dimension", cardinality=4,
            values=["Direct", "Distributor", "Online", "Retail"],
        ),
        ColumnInfo(
            "customer_segment", "dimension", cardinality=4,
            values=["Enterprise", "Government", "Retail", "SMB"],
        ),
        ColumnInfo(
            "region", "dimension", cardinality=4,
            values=["Midwest", "Northeast", "Southeast", "West"],
        ),
        ColumnInfo("product_name", "dimension", cardinality=200),
        ColumnInfo("customer_id", "dimension", cardinality=5000),
    ]  # fmt: skip
    sales.columns = {c.name: c for c in columns}
    stock = SourceInfo(
        name="fact_stock",
        label="Stock",
        description="Weekly stock snapshot",
        sql="SELECT * FROM wh.fact_stock",
        dataset_name="ai_fact_stock",
        time_col="snapshot_date",
        time_min="2024-10-27",
        time_max="2025-10-19",
        row_count=80000,
        main_measure="stock_value",
    )
    stock.columns = {
        "snapshot_date": ColumnInfo("snapshot_date", "time"),
        "stock_value": ColumnInfo(
            "stock_value", "measure", "sum", True, "Value of stock on hand", "USD", 900
        ),
        "warehouse_name": ColumnInfo(
            "warehouse_name",
            "dimension",
            cardinality=2,
            values=["East Hub", "West Hub"],
        ),
    }
    return Catalog(
        database_id="1",
        schema="wh",
        database_name="warehouse",
        sources={"fact_sales": sales, "fact_stock": stock},
    )


def _resolve(intent: dict[str, Any], catalog: Catalog, question: str = "", **kw: Any):
    return resolve(
        ChartIntent.model_validate(intent), catalog, quote, question=question, **kw
    )


# ──────────────────────────────────────────────────────────────────────────────
# Intent coercion: small models emit loose shapes
# ──────────────────────────────────────────────────────────────────────────────


def test_metric_strings_are_parsed() -> None:
    assert MetricIntent.model_validate("SUM(line_amount)").column == "line_amount"
    distinct = MetricIntent.model_validate("count(distinct customer_id)")
    assert (distinct.aggregate, distinct.column) == ("count_distinct", "customer_id")
    star = MetricIntent.model_validate("COUNT(*)")
    assert (star.aggregate, star.column) == ("count", None)
    assert (
        MetricIntent.model_validate({"column": "x", "agg": "mean"}).aggregate == "avg"
    )


def test_intent_normalises_aliases() -> None:
    intent = ChartIntent.model_validate(
        {
            "action": "refine",
            "chart_type": "Donut Chart",
            "time_grain": "monthly",
            "dimensions": "region, fact_sales.sales_channel",
            "filters": {"region": ["West", "Midwest"]},
            "top_n": "5",
            "sort": "ascending",
            "time_range": "No filter",
        }
    )
    assert intent.action == "modify_chart"
    assert intent.chart_type == "pie"
    assert intent.time_grain == "month"
    assert intent.dimensions == ["region", "sales_channel"]
    assert intent.filters[0].operator == "IN"
    assert intent.top_n == 5
    assert intent.sort == "asc"
    assert intent.time_range is None


def test_unknown_values_fall_back_to_defaults() -> None:
    intent = ChartIntent.model_validate(
        {"action": "launch_rocket", "chart_type": "sankey", "top_n": -3}
    )
    assert intent.action == "create_chart"
    assert intent.chart_type == "auto"
    assert intent.top_n is None


@pytest.mark.parametrize(
    "phrase,expected_label",
    [
        ("last 30 days", "last 30 days"),
        ("past 6 months", "last 6 months"),
        ("this year", "this year"),
        ("ytd", "this year"),
        ("last quarter", "last quarter"),
        ("2024", "2024"),
        ("Q3 2024", "Q3 2024"),
        ("March 2025", "March 2025"),
        ("2024-01-01 to 2024-03-31", "2024-01-01 to 2024-03-31"),
        ("since 2025-02", "since 2025-02"),
    ],
)
def test_time_phrases(phrase: str, expected_label: str) -> None:
    parsed = parse_time_range(phrase)
    assert parsed is not None
    expression, label = parsed
    assert label == expected_label
    since, until = timerange.evaluate(expression)
    assert since is not None
    assert until is not None
    assert since < until


def test_explicit_ranges_are_end_exclusive() -> None:
    assert parse_time_range("2024")[0] == "2024-01-01 : 2025-01-01"  # type: ignore[index]
    assert parse_time_range("Q4 2024")[0] == "2024-10-01 : 2025-01-01"  # type: ignore[index]
    assert parse_time_range("2024-01-01 to 2024-03-31")[0] == "2024-01-01 : 2024-04-01"  # type: ignore[index]


@pytest.mark.parametrize("phrase", ["whenever", "2024-13-45", "2025 to 2024", ""])
def test_bad_time_phrases(phrase: str) -> None:
    assert parse_time_range(phrase) is None


# ──────────────────────────────────────────────────────────────────────────────
# Resolver
# ──────────────────────────────────────────────────────────────────────────────


def test_resolves_names_and_values_loosely(catalog: Catalog) -> None:
    res = _resolve(
        {
            "source": "Sales",
            "metrics": [{"column": "Line Amount"}],
            "dimensions": ["Region"],
            "filters": [
                {"column": "sales channel", "operator": "=", "value": "retial"}
            ],
        },
        catalog,
    )
    assert res.ok, res.problems
    spec = res.spec
    assert spec.source == "fact_sales"
    assert spec.metrics[0].expr == 'SUM("line_amount")'
    assert spec.dimensions == ["region"]
    assert spec.filters[0].column == "sales_channel"
    assert spec.filters[0].value == "Retail"


def test_description_synonyms(catalog: Catalog) -> None:
    res = _resolve(
        {"metrics": [{"column": "revenue"}], "dimensions": ["region"]}, catalog
    )
    assert res.ok
    assert res.spec.metrics[0].column == "line_amount"


def test_unknown_column_is_a_problem_with_suggestions(catalog: Catalog) -> None:
    res = _resolve({"metrics": [{"column": "profitability_index"}]}, catalog)
    assert not res.ok
    assert "Unknown column" in res.problems[0]
    assert res.question


def test_unknown_filter_value_asks_with_options(catalog: Catalog) -> None:
    res = _resolve(
        {
            "metrics": [{"column": "line_amount"}],
            "filters": [{"column": "sales_channel", "value": "Martian"}],
        },
        catalog,
    )
    assert not res.ok
    assert "not a value of sales_channel" in res.problems[0]
    assert any("Online" in option for option in res.options)


def test_business_metric_given_as_column(catalog: Catalog) -> None:
    res = _resolve(
        {
            "metrics": [{"column": "gross_margin", "aggregate": "avg"}],
            "dimensions": ["region"],
        },
        catalog,
    )
    assert res.ok
    metric = res.spec.metrics[0]
    assert metric.business == "gross_margin"
    assert metric.expr.startswith("100.0*")
    assert metric.unit == "%"


def test_how_many_customers_counts_distinct_ids(catalog: Catalog) -> None:
    res = _resolve(
        {
            "metrics": [{"column": None, "aggregate": "count_distinct"}],
            "dimensions": ["customer_segment"],
        },
        catalog,
        question="how many customers per segment",
    )
    assert res.ok
    metric = res.spec.metrics[0]
    assert metric.expr == 'COUNT(DISTINCT "customer_id")'
    assert metric.label == "Unique Customers"


def test_filter_on_every_value_becomes_a_breakdown(catalog: Catalog) -> None:
    res = _resolve(
        {
            "metrics": [{"column": "discount_pct", "aggregate": "avg"}],
            "filters": [
                {
                    "column": "sales_channel",
                    "operator": "IN",
                    "value": ["Direct", "Distributor", "Online", "Retail"],
                }
            ],
            "time_grain": "day",
        },
        catalog,
        question="average discount by channel over time",
    )
    assert res.ok
    assert res.spec.filters == []
    assert res.spec.dimensions == ["sales_channel"]
    # A daily grain the user never asked for is replaced by one that fits a year
    assert res.spec.time_grain == "P1M"


def test_explicit_daily_grain_is_kept(catalog: Catalog) -> None:
    res = _resolve(
        {"metrics": [{"column": "quantity"}], "time_grain": "day"},
        catalog,
        question="daily quantity",
    )
    assert res.spec.time_grain == "P1D"


def test_time_range_and_date_column(catalog: Catalog) -> None:
    res = _resolve(
        {
            "metrics": [{"column": "line_amount"}],
            "time_grain": "month",
            "time_range": "last year",
        },
        catalog,
    )
    assert res.ok
    spec = res.spec
    assert spec.time_column == "order_date"
    assert spec.time_range_label == "last year"
    assert spec.time_range.startswith("DATEADD(DATETRUNC")


def test_unparseable_period_is_reported_not_guessed(catalog: Catalog) -> None:
    res = _resolve(
        {"metrics": [{"column": "line_amount"}], "time_range": "the good old days"},
        catalog,
    )
    assert res.ok
    assert res.spec.time_range == "No filter"
    assert any("couldn't understand the period" in n for n in res.notes)


def test_high_cardinality_dimension_gets_a_top_n(catalog: Catalog) -> None:
    res = _resolve(
        {"metrics": [{"column": "quantity"}], "dimensions": ["product_name"]}, catalog
    )
    assert res.spec.top_n == 25
    assert any("showing the top 25" in n for n in res.notes)


def test_source_follows_the_columns(catalog: Catalog) -> None:
    res = _resolve(
        {
            "source": "fact_sales",
            "metrics": [{"column": "stock_value"}],
            "dimensions": ["warehouse_name"],
        },
        catalog,
    )
    assert res.ok
    assert res.spec.source == "fact_stock"


def test_non_numeric_sum_becomes_distinct_count(catalog: Catalog) -> None:
    res = _resolve({"metrics": [{"column": "region", "aggregate": "sum"}]}, catalog)
    assert res.spec.metrics[0].aggregate == "count_distinct"
    assert any("not numeric" in n for n in res.notes)


def test_spec_round_trips(catalog: Catalog) -> None:
    res = _resolve(
        {
            "metrics": [{"column": "line_amount"}],
            "dimensions": ["region"],
            "filters": [{"column": "sales_channel", "value": "Online"}],
        },
        catalog,
    )
    data = res.spec.to_dict()
    assert ResolvedSpec.from_dict(data) == res.spec
    intent = res.spec.to_intent_dict()
    assert intent["filters"][0] == {
        "column": "sales_channel",
        "operator": "==",
        "value": "Online",
    }


# ──────────────────────────────────────────────────────────────────────────────
# Chart choice and form data
# ──────────────────────────────────────────────────────────────────────────────


def _spec(catalog: Catalog, intent: dict[str, Any]) -> ResolvedSpec:
    res = _resolve(intent, catalog)
    assert res.ok, res.problems
    return res.spec


@pytest.mark.parametrize(
    "intent,expected",
    [
        ({"metrics": [{"column": "line_amount"}]}, "big_number"),
        (
            {"metrics": [{"column": "line_amount"}], "time_grain": "month"},
            "line",
        ),
        (
            {"metrics": [{"column": "line_amount"}], "dimensions": ["region"]},
            "bar",
        ),
        (
            {
                "metrics": [{"column": "line_amount"}],
                "dimensions": ["region", "sales_channel"],
            },
            "heatmap",
        ),
        (
            {
                "metrics": [{"column": "line_amount"}],
                "dimensions": ["region"],
                "chart_type": "pie",
            },
            "pie",
        ),
        (
            {
                "metrics": [{"column": "line_amount"}],
                "time_grain": "month",
                "chart_type": "big_number",
            },
            "big_number_trend",
        ),
        (
            {"metrics": [{"column": "discount_pct"}], "chart_type": "histogram"},
            "histogram",
        ),
    ],
)
def test_auto_and_requested_viz(
    catalog: Catalog, intent: dict[str, Any], expected: str
) -> None:
    spec = _spec(catalog, intent)
    viz, notes = choose_viz(spec, catalog.sources[spec.source])
    assert viz == expected
    assert notes == []


def test_incompatible_request_falls_back_with_reason(catalog: Catalog) -> None:
    spec = _spec(
        catalog,
        {
            "metrics": [{"column": "line_amount"}],
            "dimensions": ["product_name"],
            "chart_type": "pie",
        },
    )
    viz, notes = choose_viz(spec, catalog.sources[spec.source])
    assert viz == "bar"
    assert "more than 12 slices" in notes[0]


def test_pie_over_time_falls_back(catalog: Catalog) -> None:
    spec = _spec(
        catalog,
        {
            "metrics": [{"column": "line_amount"}],
            "time_grain": "month",
            "chart_type": "pie",
        },
    )
    viz, notes = choose_viz(spec, catalog.sources[spec.source])
    assert viz == "line"
    assert notes


def test_sql_binds_filters_and_period(catalog: Catalog) -> None:
    spec = _spec(
        catalog,
        {
            "metrics": [{"column": "line_amount"}],
            "dimensions": ["region"],
            "filters": [{"column": "sales_channel", "value": "Online"}],
            "time_range": "2025",
            "top_n": 3,
        },
    )
    src = catalog.sources["fact_sales"]
    query = build_query(spec, "bar", src.sql, "postgresql", quote)
    # Values are parameters, never pasted into the statement
    assert "Online" not in query.sql
    assert sorted(query.params.values(), key=str) == sorted(
        ["Online", date(2025, 1, 1), date(2026, 1, 1)], key=str
    )
    sql = render_sql(query)
    assert "\"sales_channel\" = 'Online'" in sql
    assert "\"order_date\" >= '2025-01-01'" in sql
    assert "\"order_date\" < '2026-01-01'" in sql
    assert 'SUM("line_amount") AS "Total Line Amount"' in sql
    assert sql.endswith('ORDER BY SUM("line_amount") DESC LIMIT 3')

    cfg = render_config(spec, "bar", src)
    assert cfg["x"] == "region"
    assert cfg["series"] is None
    assert cfg["metrics"][0]["key"] == "Total Line Amount"
    assert cfg["show_values"] is True


def test_time_series_limits_series(catalog: Catalog) -> None:
    spec = _spec(
        catalog,
        {
            "metrics": [{"column": "quantity"}],
            "dimensions": ["product_name"],
            "time_grain": "month",
        },
    )
    src = catalog.sources["fact_sales"]
    sql = render_sql(build_query(spec, "line", src.sql, "postgresql", quote))
    assert 'DATE_TRUNC(\'month\', "order_date") AS "order_date"' in sql
    # Only the top 10 products by quantity become lines
    assert '"product_name" IN (SELECT "k" FROM (' in sql
    assert 'ORDER BY SUM("quantity") DESC LIMIT 10) t)' in sql

    cfg = render_config(spec, "line", src)
    assert cfg["x"] == "order_date"
    assert cfg["x_is_time"] is True
    assert cfg["series"] == "product_name"
    assert cfg["time_grain"] == "P1M"


def test_in_filters_expand_and_strings_are_escaped(catalog: Catalog) -> None:
    spec = _spec(
        catalog,
        {
            "metrics": [{"column": "line_amount"}],
            "filters": [
                {"column": "region", "operator": "IN", "value": ["West", "Midwest"]},
                {"column": "product_name", "operator": "contains", "value": "O'Neil"},
            ],
        },
    )
    src = catalog.sources["fact_sales"]
    query = build_query(spec, "big_number", src.sql, "postgresql", quote)
    assert len(query.expanding) == 1
    sql = render_sql(query)
    assert "\"region\" IN ('West', 'Midwest')" in sql
    assert "ILIKE '%O''Neil%'" in sql
    assert "LIMIT" not in sql  # a single aggregated row


# ──────────────────────────────────────────────────────────────────────────────
# Narration
# ──────────────────────────────────────────────────────────────────────────────


def test_narrates_categories_with_shares(catalog: Catalog) -> None:
    spec = _spec(
        catalog, {"metrics": [{"column": "line_amount"}], "dimensions": ["region"]}
    )
    label = spec.metrics[0].label
    rows = [
        {"region": "West", label: 300.0},
        {"region": "East", label: 100.0},
        {"region": "North", label: 50.0},
        {"region": "South", label: 50.0},
    ]
    text = narrate(spec, "bar", rows)
    assert text.startswith("Top region by Total Line Amount: West $300 (60%)")
    assert "Lowest is South at $50" in text


def test_narrates_time_series(catalog: Catalog) -> None:
    spec = _spec(catalog, {"metrics": [{"column": "quantity"}], "time_grain": "month"})
    label = spec.metrics[0].label
    rows = [
        {"order_date": 1735689600000, label: 100},  # Jan 2025
        {"order_date": 1738368000000, label: 300},  # Feb 2025
        {"order_date": 1740787200000, label: 150},  # Mar 2025
    ]
    text = narrate(spec, "line", rows)
    assert "peaked at 300 units in Feb 2025" in text
    assert "lowest at 100 units in Jan 2025" in text
    assert "down 50.0% on the previous period" in text


def test_narrates_big_number(catalog: Catalog) -> None:
    spec = _spec(
        catalog, {"metrics": [{"column": "line_amount"}], "time_range": "2025"}
    )
    text = narrate(spec, "big_number", [{spec.metrics[0].label: 1_234_567}])
    assert text == "Total Line Amount is $1.2M for 2025."


# ──────────────────────────────────────────────────────────────────────────────
# Rules planner (LLM unavailable)
# ──────────────────────────────────────────────────────────────────────────────


def _rules(
    text: str, catalog: Catalog, previous: ResolvedSpec | None = None
) -> ResolvedSpec:
    intent = plan_with_rules(text, catalog, previous)
    res = resolve(intent, catalog, quote, previous, text)
    assert res.ok, (intent, res.problems)
    return res.spec


def test_rules_basic_breakdown(catalog: Catalog) -> None:
    spec = _rules("revenue by region", catalog)
    assert spec.metrics[0].column == "line_amount"
    assert spec.dimensions == ["region"]


def test_rules_column_names_are_not_chart_types(catalog: Catalog) -> None:
    assert (
        plan_with_rules("total line amount by region", catalog, None).chart_type
        == "auto"
    )
    assert (
        plan_with_rules(
            "line amount by region as a line chart", catalog, None
        ).chart_type
        == "line"
    )


def test_rules_top_n_with_dimension_before_by(catalog: Catalog) -> None:
    spec = _rules("top 5 product name by quantity", catalog)
    assert spec.dimensions == ["product_name"]
    assert spec.metrics[0].column == "quantity"
    assert spec.top_n == 5
    assert spec.sort_desc


def test_rules_trend_with_value_filter_and_period(catalog: Catalog) -> None:
    spec = _rules("monthly revenue for Online in 2025", catalog)
    assert spec.time_grain == "P1M"
    assert spec.time_range_label == "2025"
    assert [(f.column, f.value) for f in spec.filters] == [("sales_channel", "Online")]


def test_rules_ambiguous_value_uses_its_siblings(catalog: Catalog) -> None:
    # "Retail" is a channel and a segment; "Online" settles it
    spec = _rules("revenue by region for Online and Retail", catalog)
    assert spec.filters[0].column == "sales_channel"
    assert set(spec.filters[0].value) == {"Online", "Retail"}
    assert spec.metrics[0].column == "line_amount"  # not discount_pct via "Retail"


def test_rules_follow_up_modifies_previous(catalog: Catalog) -> None:
    first = _rules("revenue by region", catalog)
    pie = _rules("make it a pie", catalog, first)
    assert pie.chart_type == "pie"
    assert pie.dimensions == ["region"]

    # "Retail" is also a customer segment; the chart being modified decides
    by_segment = _rules("average discount by customer segment", catalog)
    excluded = _rules("excluding Retail", catalog, by_segment)
    assert (excluded.filters[0].column, excluded.filters[0].operator) == (
        "customer_segment",
        "!=",
    )
    assert excluded.dimensions == ["customer_segment"]


def test_rules_business_metric(catalog: Catalog) -> None:
    spec = _rules("gross margin by region", catalog)
    assert [m.business for m in spec.metrics] == ["gross_margin"]


def test_rules_actions(catalog: Catalog) -> None:
    assert plan_with_rules("show me the sql", catalog, None).action == "show_sql"
    assert (
        plan_with_rules("what data do you have", catalog, None).action
        == "describe_data"
    )
    assert plan_with_rules("delete all orders", catalog, None).action == "unsupported"
    save = plan_with_rules("save it to the Sales Overview dashboard", catalog, None)
    assert save.action == "save_chart"
    assert save.dashboard == "Sales Overview"


def test_rules_gibberish_asks(catalog: Catalog) -> None:
    previous = _rules("revenue by region", catalog)
    assert plan_with_rules("banana", catalog, previous).action == "clarify"
    assert plan_with_rules("banana", catalog, None).action == "clarify"


# ──────────────────────────────────────────────────────────────────────────────
# LLM output parsing and planner fallback
# ──────────────────────────────────────────────────────────────────────────────


def test_extract_json_tolerates_fences_and_prose() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": {"b": "}"}} hope that helps') == {"a": {"b": "}"}}
    with pytest.raises(LLMBadOutputError):
        extract_json("no json here")


def test_planner_falls_back_to_rules_when_llm_is_down(catalog: Catalog) -> None:
    from ai_chat import llm, service

    conv = {"messages": [], "database_id": "1", "schema": "wh"}
    with (
        patch.object(llm, "recently_down", return_value=None),
        patch.object(
            llm,
            "complete_json",
            side_effect=llm.LLMUnavailableError("server unreachable"),
        ),
    ):
        plan = service.make_plan("revenue by region", catalog, conv, None)
    assert plan.engine == "rules"
    assert "server unreachable" in plan.notes[0]
    assert plan.intent.dimensions == ["region"]


def test_planner_retries_invalid_json_then_falls_back(catalog: Catalog) -> None:
    from ai_chat import llm, service

    conv = {"messages": [], "database_id": "1", "schema": "wh"}
    with (
        patch.object(llm, "recently_down", return_value=None),
        patch.object(
            llm, "complete_json", side_effect=llm.LLMBadOutputError("prose")
        ) as complete,
    ):
        plan = service.make_plan("revenue by region", catalog, conv, None)
    assert complete.call_count == 2
    assert plan.engine == "rules"


def test_planner_skips_llm_during_cooldown(catalog: Catalog) -> None:
    from ai_chat import llm, service

    conv = {"messages": [], "database_id": "1", "schema": "wh"}
    with (
        patch.object(llm, "recently_down", return_value="it timed out"),
        patch.object(llm, "complete_json") as complete,
    ):
        plan = service.make_plan("revenue by region", catalog, conv, None)
    complete.assert_not_called()
    assert plan.engine == "rules"


def test_repair_round_fixes_unknown_names(catalog: Catalog) -> None:
    from ai_chat import llm, service

    bad = {
        "action": "create_chart",
        "metrics": [{"column": "profit"}],
        "dimensions": ["region"],
    }
    good = {
        "action": "create_chart",
        "metrics": [{"column": "line_amount"}],
        "dimensions": ["region"],
    }
    plan = service.Plan(ChartIntent.model_validate(bad), "llm", messages=[], raw=bad)
    with patch.object(llm, "complete_json", return_value=good):
        resolution, fixed = service._resolve_with_repair(
            plan, catalog, quote, None, "profit by region", lambda _s: None
        )
    assert resolution.ok
    assert resolution.spec.metrics[0].column == "line_amount"


def test_catalog_serialisation_round_trip(catalog: Catalog) -> None:
    clone = Catalog.from_dict(catalog.to_dict())
    assert clone.sources["fact_sales"].columns["region"].values == [
        "Midwest",
        "Northeast",
        "Southeast",
        "West",
    ]
    assert clone.default_source().name == "fact_sales"  # type: ignore[union-attr]
    assert clone.suggestions()


# ── place-named measures (jp_sales) ──────────────────────────────────────────


@pytest.fixture
def games() -> Catalog:
    src = SourceInfo(
        name="video_game_sales",
        label="Video Game Sales",
        description="",
        sql="SELECT * FROM public.video_game_sales",
        dataset_name="ai_video_game_sales",
        row_count=16595,
        main_measure="na_sales",
    )
    platforms = [f"P{i}" for i in range(29)] + ["PS4", "Wii"]
    columns = [
        ColumnInfo("na_sales", "measure", "sum", True, cardinality=400),
        ColumnInfo("eu_sales", "measure", "sum", True, cardinality=300),
        ColumnInfo("jp_sales", "measure", "sum", True, cardinality=200),
        ColumnInfo("global_sales", "measure", "sum", True, cardinality=600),
        ColumnInfo("name", "dimension", cardinality=11491),
        ColumnInfo("genre", "dimension", cardinality=12),
        ColumnInfo("platform", "dimension", cardinality=31, values=platforms),
    ]
    src.columns = {c.name: c for c in columns}
    return Catalog(
        database_id="1",
        schema="public",
        sources={"video_game_sales": src},
    )


def test_place_codes_make_readable_labels(games: Catalog) -> None:
    jp = games.sources["video_game_sales"].columns["jp_sales"]
    assert jp.label == "Japan Sales"
    assert jp.place == "Japan"
    assert games.sources["video_game_sales"].columns["genre"].place == ""


@pytest.mark.parametrize("measure", ["jp_sales", "global_sales"])
def test_place_used_as_filter_becomes_its_measure(games: Catalog, measure: str) -> None:
    res = _resolve(
        {
            "source": "video_game_sales",
            "metrics": [{"column": measure}],
            "dimensions": ["name"],
            "filters": [{"column": "platform", "value": "Japan"}],
        },
        games,
    )
    assert res.ok, res.problems
    assert res.spec is not None
    assert res.spec.filters == []
    assert [m.column for m in res.spec.metrics] == ["jp_sales"]
    assert res.spec.metrics[0].label == "Total Japan Sales"
    assert any("Japan Sales" in note for note in res.notes)


def test_place_with_a_count_keeps_the_count(games: Catalog) -> None:
    res = _resolve(
        {
            "source": "video_game_sales",
            "metrics": [{"aggregate": "count"}],
            "dimensions": ["genre"],
            "filters": [{"column": "region", "value": "jp"}],
        },
        games,
    )
    assert res.ok, res.problems
    assert res.spec is not None
    assert res.spec.metrics[0].aggregate == "count"
    assert [(f.column, f.operator, f.value) for f in res.spec.filters] == [
        ("jp_sales", ">", 0)
    ]


def test_values_beyond_the_prompt_list_are_still_validated(games: Catalog) -> None:
    res = _resolve(
        {
            "source": "video_game_sales",
            "metrics": [{"column": "global_sales"}],
            "filters": [{"column": "platform", "value": "Mars"}],
        },
        games,
    )
    assert not res.ok
    assert "not a value of platform" in res.problems[0]
    ok = _resolve(
        {
            "source": "video_game_sales",
            "metrics": [{"column": "global_sales"}],
            "filters": [{"column": "platform", "value": "ps4"}],
        },
        games,
    )
    assert ok.ok
    assert ok.spec is not None
    assert ok.spec.filters[0].value == "PS4"
    prompt = games.to_prompt("")
    assert "platform (31 values)" in prompt
    assert "jp_sales" in prompt


def test_rules_read_a_place_as_its_measure(games: Catalog) -> None:
    games.sources["video_game_sales"].columns["jp_sales"].description = "Japan Sales"
    intent = plan_with_rules("best selling genre in Japan", games, None)
    res = _resolve(intent.model_dump(), games)
    assert res.ok, res.problems
    assert res.spec is not None
    assert [m.column for m in res.spec.metrics] == ["jp_sales"]
    assert res.spec.dimensions == ["genre"]


def test_count_of_a_place_measure_counts_rows_with_sales(games: Catalog) -> None:
    res = _resolve(
        {
            "source": "video_game_sales",
            "metrics": [{"column": "jp_sales", "aggregate": "count"}],
            "dimensions": ["genre"],
        },
        games,
    )
    assert res.ok, res.problems
    assert res.spec is not None
    assert res.spec.metrics[0].expr == "COUNT(*)"
    assert [(f.column, f.operator, f.value) for f in res.spec.filters] == [
        ("jp_sales", ">", 0)
    ]


def test_full_names_match_coded_values(games: Catalog) -> None:
    res = _resolve(
        {
            "source": "video_game_sales",
            "metrics": [{"column": "eu_sales"}],
            "filters": [{"column": "platform", "value": "PlayStation 4"}],
        },
        games,
    )
    assert res.ok, res.problems
    assert res.spec is not None
    assert res.spec.filters[0].value == "PS4"


def test_unknown_value_options_start_with_the_closest(games: Catalog) -> None:
    res = _resolve(
        {
            "source": "video_game_sales",
            "metrics": [{"column": "eu_sales"}],
            "filters": [{"column": "platform", "value": "Wii Fit"}],
        },
        games,
    )
    assert not res.ok
    assert res.options[0] == "Platform: Wii"


@pytest.mark.parametrize(
    ("question", "sort_desc"),
    [("Japan's best selling game", True), ("worst selling game in Japan", False)],
)
def test_ranking_question_answered_with_max_becomes_a_ranking(
    games: Catalog, question: str, sort_desc: bool
) -> None:
    res = _resolve(
        {
            "source": "video_game_sales",
            "metrics": [{"column": "jp_sales", "aggregate": "max"}],
        },
        games,
        question=question,
    )
    assert res.ok, res.problems
    assert res.spec is not None
    assert res.spec.dimensions == ["name"]
    assert res.spec.metrics[0].aggregate == "sum"
    assert res.spec.top_n == 10
    assert res.spec.sort_desc is sort_desc


def test_plain_max_question_stays_a_number(catalog: Catalog) -> None:
    res = _resolve(
        {"metrics": [{"column": "discount_pct", "aggregate": "max"}]},
        catalog,
        question="what is the highest discount",
    )
    assert res.ok, res.problems
    assert res.spec is not None
    assert res.spec.dimensions == []


def test_ranking_by_a_dimension_uses_totals(games: Catalog) -> None:
    res = _resolve(
        {
            "source": "video_game_sales",
            "metrics": [{"column": "jp_sales", "aggregate": "max"}],
            "dimensions": ["genre"],
        },
        games,
        question="best selling genre in Japan",
    )
    assert res.ok, res.problems
    assert res.spec is not None
    assert res.spec.dimensions == ["genre"]
    assert res.spec.metrics[0].aggregate == "sum"


def test_breakdown_pinned_to_one_value_is_dropped(catalog: Catalog) -> None:
    res = _resolve(
        {
            "metrics": [{"column": "quantity"}],
            "dimensions": ["sales_channel", "product_name"],
            "filters": [{"column": "sales_channel", "value": "Retail"}],
        },
        catalog,
    )
    assert res.ok, res.problems
    assert res.spec is not None
    assert res.spec.dimensions == ["product_name"]
    assert res.spec.filters[0].column == "sales_channel"


# ── questions about the data itself ──────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Make me understand the database, the tables, the schema etc",
        "explain the tables",
        "How are the tables related?",
        "what does gross margin mean",
        "which tables do you have",
        "describe the data model",
        "tell me about this warehouse",
        "how is gross margin calculated",
    ],
)
def test_schema_questions_are_recognised(catalog: Catalog, text: str) -> None:
    from ai_chat.rules import is_schema_question

    assert is_schema_question(text)
    assert plan_with_rules(text, catalog, None).action == "describe_data"


@pytest.mark.parametrize(
    "text",
    [
        "revenue by region",
        "explain the revenue trend by month",
        "which region had the highest revenue",
        "top 5 products by quantity",
        "summarize sales data by region",
    ],
)
def test_data_questions_are_not_schema_questions(text: str) -> None:
    from ai_chat.rules import is_schema_question

    assert not is_schema_question(text)


def test_explanation_is_grounded_in_the_catalog(catalog: Catalog) -> None:
    from ai_chat import llm, service

    catalog.sources["fact_sales"].tables = ["fact_sales", "dim_customer"]
    catalog.sources["fact_sales"].grain = "one row per order line"
    with (
        patch.object(llm, "recently_down", return_value=None),
        patch.object(
            llm, "complete_json", return_value={"answer": "It holds sales."}
        ) as complete,
    ):
        msg = service._describe_data(catalog, "explain the database", use_llm=True)
    assert msg["type"] == "catalog"
    assert msg["text"] == "It holds sales."
    system = complete.call_args[0][0][0]["content"]
    assert "Net revenue USD" in system
    assert "combines: fact_sales joined to dim_customer" in system
    assert "grain: one row per order line" in system
    sales = msg["schema"][0]
    assert sales["name"] == "fact_sales"
    assert sales["tables"] == ["fact_sales", "dim_customer"]
    line_amount = next(m for m in sales["measures"] if m["name"] == "line_amount")
    assert line_amount["unit"] == "USD"
    assert sales["metrics"][0]["label"] == "Gross Margin"


def test_explanation_without_llm_is_factual(catalog: Catalog) -> None:
    from ai_chat import llm, service

    with patch.object(llm, "complete_json") as complete:
        msg = service._describe_data(catalog, "explain the database", use_llm=False)
    complete.assert_not_called()
    assert msg["text"].startswith("warehouse.wh has 2 data sources")
    assert "Gross Margin (Gross margin %)" in msg["text"]
    assert len(msg["schema"]) == 2


def test_answer_uses_the_result_rows(catalog: Catalog) -> None:
    from ai_chat import llm, service

    spec = _spec(
        catalog, {"metrics": [{"column": "line_amount"}], "dimensions": ["region"]}
    )
    rows = [{"region": "West", "Total Line Amount": 300.0}]
    with patch.object(
        llm, "complete_json", return_value={"answer": "West sells most."}
    ) as complete:
        answer = service._answer_question(
            "which region sells most?", spec, ["By: Region"], [], rows, "West leads"
        )
    assert answer == "West sells most."
    prompt = complete.call_args[0][0][0]["content"]
    assert "QUESTION: which region sells most?" in prompt
    assert '["West", 300.0]' in prompt
    assert "Total Line Amount in USD" in prompt
    assert "KEY FACTS (computed exactly from all rows): West leads" in prompt


def test_answer_falls_back_when_the_llm_fails(catalog: Catalog) -> None:
    from ai_chat import llm, service

    spec = _spec(catalog, {"metrics": [{"column": "line_amount"}]})
    with patch.object(
        llm, "complete_json", side_effect=llm.LLMUnavailableError("down")
    ):
        assert service._answer_question("total?", spec, [], [], [{"x": 1}], "") is None


def test_recent_periods_count_back_from_the_end_of_old_data(catalog: Catalog) -> None:
    # The fixture's sales end on 2025-10-19, long before "today"
    res = _resolve(
        {
            "metrics": [{"column": "line_amount"}],
            "time_grain": "month",
            "time_range": "last 6 months",
        },
        catalog,
        "monthly revenue over the last 6 months",
    )
    spec = res.spec
    assert spec.anchor_date == "2025-10-19"
    assert any("data ends on 2025-10-19" in note for note in res.notes)
    assert spec.time_grain == "P1M"
    sql = render_sql(build_query(spec, "line", "SELECT 1", "postgresql", quote))
    assert "\"order_date\" >= '2025-04-19'" in sql
    assert "\"order_date\" < '2025-10-20'" in sql
    # Fixed periods are never moved
    fixed = _resolve(
        {"metrics": [{"column": "line_amount"}], "time_range": "2024"}, catalog
    )
    assert fixed.spec.anchor_date is None
    assert ResolvedSpec.from_dict(spec.to_dict()) == spec


def test_model_text_is_tidied() -> None:
    from ai_chat.service import _tidy

    text = "**Overview** The data has: • Sales: orders • Stock: snapshots\n- Products"
    assert _tidy(text) == (
        "Overview The data has:\n• Sales: orders\n• Stock: snapshots\n• Products"
    )


def test_a_named_breakdown_beats_a_partial_match(catalog: Catalog) -> None:
    src = catalog.sources["fact_sales"]
    src.columns["warehouse_name"] = ColumnInfo(
        "warehouse_name", "dimension", cardinality=8, values=["East Hub", "West Hub"]
    )
    question = "which warehouse region had the highest revenue?"
    res = _resolve(
        {"metrics": [{"column": "line_amount"}], "dimensions": ["warehouse_name"]},
        catalog,
        question,
    )
    assert res.spec.dimensions == ["region"]
    assert "Grouped by Region, as asked." in res.notes
    # A breakdown the question names itself is kept
    kept = _resolve(
        {"metrics": [{"column": "line_amount"}], "dimensions": ["warehouse_name"]},
        catalog,
        "revenue by warehouse name",
    )
    assert kept.spec.dimensions == ["warehouse_name"]


def test_a_period_inside_one_bucket_is_a_single_number(catalog: Catalog) -> None:
    spec = _spec(
        catalog,
        {
            "metrics": [{"column": "quantity"}],
            "time_grain": "quarter",
            "time_range": "Q3 2025",
        },
    )
    assert spec.time_grain is None
    viz, _ = choose_viz(spec, catalog.sources["fact_sales"])
    assert viz == "big_number"
    # Several buckets stay a trend
    trend = _spec(
        catalog,
        {
            "metrics": [{"column": "quantity"}],
            "time_grain": "month",
            "time_range": "Q3 2025",
        },
    )
    assert trend.time_grain == "P1M"


def test_narrates_a_single_period(catalog: Catalog) -> None:
    spec = _spec(catalog, {"metrics": [{"column": "quantity"}], "time_grain": "month"})
    label = spec.metrics[0].label
    text = narrate(spec, "line", [{"order_date": "2025-07-01", label: 1421}])
    assert text == "Total Quantity was 1.4K units in Jul 2025."
