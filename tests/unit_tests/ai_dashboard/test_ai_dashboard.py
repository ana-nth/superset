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
Unit tests for the AI Auto Dashboard feature.

Run with:
  pytest tests/unit_tests/ai_dashboard/ -v
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from superset.utils import json

# ──────────────────────────────────────────────────────────────────────────────
# 1. SQL Guard tests
# ──────────────────────────────────────────────────────────────────────────────


def _guard(sql: str) -> str:
    """Thin wrapper around the private guard function."""
    from superset.ai_dashboard.tools import _parse_and_validate_sql

    return _parse_and_validate_sql(sql)


class TestSQLGuard:
    def test_simple_select_passes(self) -> None:
        sql = _guard("SELECT 1")
        assert "SELECT" in sql.upper()

    def test_select_with_limit_clamps_to_200(self) -> None:
        sql = _guard("SELECT * FROM wh.dim_product LIMIT 999")
        assert "LIMIT 200" in sql

    def test_small_limit_is_kept(self) -> None:
        sql = _guard("SELECT * FROM wh.dim_product LIMIT 5")
        assert sql.endswith("LIMIT 5")

    def test_select_without_limit_gets_limit(self) -> None:
        sql = _guard("SELECT id FROM wh.dim_product")
        assert "LIMIT" in sql.upper()

    def test_with_cte_passes(self) -> None:
        sql = _guard("WITH cte AS (SELECT 1 AS x) SELECT * FROM cte")
        assert sql  # no exception

    def test_insert_rejected(self) -> None:
        with pytest.raises(ValueError, match="Only SELECT/WITH"):
            _guard("INSERT INTO wh.dim_product VALUES (1)")

    def test_update_rejected(self) -> None:
        with pytest.raises(ValueError, match="Only SELECT/WITH"):
            _guard("UPDATE wh.dim_product SET sku='X' WHERE product_id=1")

    def test_delete_rejected(self) -> None:
        with pytest.raises(ValueError, match="Only SELECT/WITH"):
            _guard("DELETE FROM wh.dim_product WHERE product_id=1")

    def test_drop_rejected(self) -> None:
        with pytest.raises(ValueError, match="Only SELECT/WITH"):
            _guard("DROP TABLE wh.dim_product")

    def test_multi_statement_rejected(self) -> None:
        with pytest.raises(ValueError, match="single SQL statement"):
            _guard("SELECT 1; SELECT 2")

    def test_truncate_rejected(self) -> None:
        with pytest.raises(ValueError, match="Only SELECT/WITH"):
            _guard("TRUNCATE wh.dim_product")


# ──────────────────────────────────────────────────────────────────────────────
# 2. Chart spec → params builder tests
# ──────────────────────────────────────────────────────────────────────────────


class TestChartSpecToParams:
    def _build(self, **kwargs: Any) -> dict[str, Any]:
        from superset.ai_dashboard.tools import _build_params, ChartSpec

        return _build_params(ChartSpec(**kwargs))

    def test_big_number_total_uses_first_metric_as_adhoc(self) -> None:
        p = self._build(
            dataset=1,
            viz_type="big_number_total",
            metrics=["SUM(line_amount)"],
            title="Total Revenue",
        )
        assert p["metric"]["expressionType"] == "SQL"
        assert p["metric"]["sqlExpression"] == "SUM(line_amount)"
        assert "subheader" in p

    def test_bare_column_metric_is_summed(self) -> None:
        p = self._build(dataset=1, viz_type="pie", metrics=["line_amount"], title="x")
        assert p["metric"]["sqlExpression"] == "SUM(line_amount)"

    def test_line_chart_has_x_axis(self) -> None:
        p = self._build(
            dataset=1,
            viz_type="echarts_timeseries_line",
            metrics=["SUM(line_amount)"],
            time_column="order_date",
            title="Revenue trend",
        )
        assert p["x_axis"] == "order_date"
        assert p["time_grain_sqla"] == "P1M"
        assert p["datasource"] == "1__table"

    def test_legacy_bar_maps_to_categorical_echarts_bar(self) -> None:
        p = self._build(
            dataset=1,
            viz_type="bar",
            metrics=["SUM(qty_on_hand)"],
            groupby=["category"],
            title="Stock by category",
        )
        assert p["viz_type"] == "echarts_timeseries_bar"
        assert p["x_axis"] == "category"
        assert p["metrics"][0]["sqlExpression"] == "SUM(qty_on_hand)"

    def test_heatmap_extracts_groupby(self) -> None:
        p = self._build(
            dataset=1,
            viz_type="heatmap_v2",
            metrics=["SUM(stock_value)"],
            groupby=["warehouse_name", "category"],
            title="Stock heatmap",
        )
        assert p["x_axis"] == "warehouse_name"
        assert p["groupby"] == "category"

    def test_invalid_viz_type_raises(self) -> None:
        with pytest.raises(ValueError, match="viz_type must be one of"):
            self._build(dataset=1, viz_type="invalid_type", title="X")

    def test_query_for_timeseries_uses_base_axis(self) -> None:
        from superset.ai_dashboard.tools import _query_for

        p = self._build(
            dataset=1,
            viz_type="echarts_timeseries_line",
            metrics=["SUM(line_amount)"],
            time_column="order_date",
            title="t",
        )
        query = _query_for(p)
        assert query["columns"][0]["columnType"] == "BASE_AXIS"
        assert query["metrics"][0]["sqlExpression"] == "SUM(line_amount)"


# ──────────────────────────────────────────────────────────────────────────────
# 3. Layout and dashboard assembly
# ──────────────────────────────────────────────────────────────────────────────


CHART_SPECS = [
    {"chart_id": 1, "viz_type": "big_number_total", "title": "KPI 1"},
    {"chart_id": 2, "viz_type": "big_number_total", "title": "KPI 2"},
    {"chart_id": 3, "viz_type": "echarts_timeseries_line", "title": "Trend"},
    {"chart_id": 4, "viz_type": "echarts_timeseries_bar", "title": "Pareto"},
    {"chart_id": 5, "viz_type": "pie", "title": "Channel share"},
]


def _chart_ids(layout: dict[str, Any]) -> list[int]:
    return [
        v["meta"]["chartId"]
        for v in layout.values()
        if isinstance(v, dict) and v.get("type") == "CHART"
    ]


class TestPositionJson:
    def test_widths(self) -> None:
        from superset.ai_dashboard.tools import _chart_width

        assert _chart_width("big_number_total") == 4
        assert _chart_width("echarts_timeseries_line") == 12
        assert _chart_width("pie") == 6

    def test_layout_places_every_chart_once_and_rows_fit(self) -> None:
        sections = [
            {
                "title": "C1 Data Quality",
                "chart_ids": [3],
                "insight": "1% null ship_date",
            },
            {"title": "C2 Trend", "chart_ids": [3, 4], "insight": "Q4 is 2.5× avg"},
        ]
        with patch(
            "superset.ai_dashboard.tools.save_dashboard", return_value=99
        ) as save:
            from superset.ai_dashboard.tools import build_dashboard

            result = build_dashboard("Test Dashboard", sections, CHART_SPECS)

        assert result == {"dashboard_id": 99}
        _title, layout, _filters, chart_ids = save.call_args[0]
        assert layout["DASHBOARD_VERSION_KEY"] == "v2"
        assert layout["ROOT_ID"]["children"] == ["GRID_ID"]
        # Chart 3 is listed twice but placed once; chart 5 was in no section
        assert sorted(_chart_ids(layout)) == [1, 2, 3, 4, 5]
        assert sorted(chart_ids) == [1, 2, 3, 4, 5]
        for node in layout.values():
            if isinstance(node, dict) and node.get("type") == "ROW":
                width = sum(layout[c]["meta"]["width"] for c in node["children"])
                assert width <= 12
                assert all(
                    layout[c]["parents"][-1] == node["id"] for c in node["children"]
                )

    def test_kpis_come_first(self) -> None:
        with patch(
            "superset.ai_dashboard.tools.save_dashboard", return_value=1
        ) as save:
            from superset.ai_dashboard.tools import build_dashboard

            build_dashboard("t", [{"title": "s", "chart_ids": [3]}], CHART_SPECS)
        layout = save.call_args[0][1]
        first_row = layout[layout["GRID_ID"]["children"][0]]
        first_ids = [layout[c]["meta"]["chartId"] for c in first_row["children"]]
        assert first_ids == [1, 2]

    def test_native_filters(self) -> None:
        with patch(
            "superset.ai_dashboard.tools.save_dashboard", return_value=42
        ) as save:
            from superset.ai_dashboard.tools import build_dashboard

            build_dashboard(
                "Filter Test",
                [],
                [],
                date_column="order_date",
                dim_filters=["warehouse_name", "category"],
                dataset_id=7,
            )
        filters = save.call_args[0][2]
        assert len(filters) == 3
        assert [f["filterType"] for f in filters] == [
            "filter_time",
            "filter_select",
            "filter_select",
        ]
        assert filters[1]["targets"][0] == {
            "column": {"name": "warehouse_name"},
            "datasetId": 7,
        }
        assert all(f["scope"]["rootPath"] == ["ROOT_ID"] for f in filters)


# ──────────────────────────────────────────────────────────────────────────────
# 4. Builder helpers
# ──────────────────────────────────────────────────────────────────────────────


class TestBuilderHelpers:
    def test_fmt(self) -> None:
        from superset.ai_dashboard.builder import _fmt

        assert _fmt(1_234_567) == "1.2M"
        assert _fmt(12_300) == "12.3K"
        assert _fmt(5) == "5"
        assert _fmt(float("nan")) == "n/a"

    def test_main_measure_prefers_amount_columns(self) -> None:
        from superset.ai_dashboard.builder import FactView, TableMeta

        view = FactView(
            fact=TableMeta(name="f"),
            sql="",
            dataset_name="ai_f",
            time_col="d",
            measures={"quantity": "sum", "discount_pct": "avg", "line_amount": "sum"},
            dimensions=[],
        )
        assert view.main_measure == "line_amount"

    def test_main_measure_prefers_total_columns(self) -> None:
        from superset.ai_dashboard.builder import FactView, TableMeta

        view = FactView(
            fact=TableMeta(name="f"),
            sql="",
            dataset_name="ai_f",
            time_col="",
            measures={"na_sales": "sum", "eu_sales": "sum", "global_sales": "sum"},
            dimensions=[],
        )
        assert view.main_measure == "global_sales"

    def test_infer_role(self) -> None:
        from superset.ai_dashboard.builder import Builder

        pk = {"orders_id", "id"}
        assert Builder._infer_role("order_date", "DATE", pk) == "time"
        assert Builder._infer_role("customer_id", "INTEGER", pk) == "id"
        assert Builder._infer_role("amount", "NUMERIC(12, 2)", pk) == "measure"
        assert Builder._infer_role("region", "TEXT", pk) == "dimension"
        assert Builder._infer_role("is_active", "BOOLEAN", pk) == "flag"

    def test_coordinates_and_codes_are_not_measures(self) -> None:
        from superset.ai_dashboard.builder import Builder

        pk: set[str] = set()
        for col in ("lon", "LAT", "longitude", "zip_code", "year", "qtr", "postcode"):
            assert Builder._infer_role(col, "DOUBLE PRECISION", pk) == "id", col
        assert Builder._infer_role("sales", "DOUBLE PRECISION", pk) == "measure"
        assert Builder._infer_role("quantity_ordered", "BIGINT", pk) == "measure"

    def test_infer_agg(self) -> None:
        from superset.ai_dashboard.builder import Builder

        assert Builder._infer_agg("unit_price") == "avg"
        assert Builder._infer_agg("discount_pct") == "avg"
        assert Builder._infer_agg("age") == "avg"
        assert Builder._infer_agg("usage") == "sum"
        assert Builder._infer_agg("line_amount") == "sum"

    def test_semantics_found(self) -> None:
        from superset.ai_dashboard.semantics import load_semantics

        data = load_semantics()
        assert data.get("schema") == "wh"
        assert "fact_sales_order_line" in data["tables"]


# ──────────────────────────────────────────────────────────────────────────────
# 5. Orchestration: modes and fallbacks
# ──────────────────────────────────────────────────────────────────────────────


class FakeMsg:
    def __init__(self, content: str) -> None:
        self.content = content


FINAL_OUTPUT = json.dumps(
    {
        "dashboard_id": 99,
        "insights": [
            {
                "title": "Null ship_date",
                "text": "1.2% NULL",
                "severity": "warning",
                "chart_id": 10,
            },
            {
                "title": "Q4",
                "text": "Q4 revenue 2.8× Q1",
                "severity": "info",
                "chart_id": 11,
            },
            {
                "title": "Pareto",
                "text": "Top 20% = 78%",
                "severity": "info",
                "chart_id": 12,
            },
            {
                "title": "Adj",
                "text": "WH-MID1 5× median",
                "severity": "critical",
                "chart_id": 13,
            },
            {"title": "Fill Rate", "text": "91%", "severity": "info", "chart_id": 14},
        ],
    }
)


class TestOrchestration:
    def test_agent_mode_returns_agent_json(self) -> None:
        fake_agent = MagicMock()
        fake_agent.invoke.return_value = {
            "messages": [FakeMsg("Done: " + FINAL_OUTPUT)]
        }
        env = {
            "AI_DASH_MODE": "agent",
            "AI_DASH_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "test-key",
        }
        with (
            patch.dict(os.environ, env),
            patch("deepagents.create_deep_agent", return_value=fake_agent),
        ):
            from superset.ai_dashboard import agent as ag

            result = ag.run_agent(database_id=1, schema="wh")

        assert result["dashboard_id"] == 99
        assert len(result["insights"]) == 5
        assert result["mode"] == "agent"

    def test_agent_failure_falls_back_to_builder(self) -> None:
        fake_agent = MagicMock()
        fake_agent.invoke.side_effect = ConnectionError("Connection error.")
        env = {
            "AI_DASH_MODE": "agent",
            "AI_DASH_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "test-key",
        }
        built = {"dashboard_id": 7, "insights": [], "mode": "hybrid"}
        with (
            patch.dict(os.environ, env),
            patch("deepagents.create_deep_agent", return_value=fake_agent),
            patch(
                "superset.ai_dashboard.agent._run_builder", return_value=dict(built)
            ) as run_builder,
            patch("superset.extensions.db"),
        ):
            from superset.ai_dashboard import agent as ag

            result = ag.run_agent(database_id=1, schema="wh")

        run_builder.assert_called_once()
        assert result["dashboard_id"] == 7
        assert "Connection error" in result["fallback_reason"]

    def test_rules_mode_never_calls_llm(self) -> None:
        builder = MagicMock()
        builder.all_insights.return_value = [
            {"title": "Pareto", "text": "Top 20% = 78%", "severity": "warning"}
        ]
        builder.build_dashboard.return_value = 5
        builder.kpis, builder.sections = [], []
        with (
            patch.dict(os.environ, {"AI_DASH_MODE": "rules"}),
            patch("superset.ai_dashboard.builder.Builder", return_value=builder),
            patch("superset.ai_dashboard.agent._make_models") as make_models,
        ):
            from superset.ai_dashboard import agent as ag

            result = ag.run_agent(database_id=1, schema="wh")

        make_models.assert_not_called()
        assert result["dashboard_id"] == 5
        assert result["mode"] == "rules"
        assert "Top 20% = 78%" in result["summary"]

    def test_hybrid_uses_template_summary_when_llm_down(self) -> None:
        builder = MagicMock()
        builder.all_insights.return_value = [
            {"title": "Spike", "text": "6.8σ", "severity": "critical"}
        ]
        builder.build_dashboard.return_value = 6
        builder.kpis, builder.sections = [], []
        with (
            patch.dict(os.environ, {"AI_DASH_MODE": "hybrid"}),
            patch("superset.ai_dashboard.builder.Builder", return_value=builder),
            patch(
                "superset.ai_dashboard.agent._make_models",
                side_effect=ConnectionError("down"),
            ),
        ):
            from superset.ai_dashboard import agent as ag

            result = ag.run_agent(database_id=1, schema="wh")

        assert result["dashboard_id"] == 6
        assert result["llm_used"] is False
        assert "6.8σ" in result["summary"]

    def test_parse_final_json_rejects_non_integer_ids(self) -> None:
        from superset.ai_dashboard.agent import _parse_final_json

        assert _parse_final_json('{"dashboard_id": "abc"}') is None
        assert _parse_final_json('text {"dashboard_id": "12"} more') == {
            "dashboard_id": 12,
            "insights": [],
        }


# ──────────────────────────────────────────────────────────────────────────────
# 6. Task progress
# ──────────────────────────────────────────────────────────────────────────────


def test_progress_pct_is_monotonic() -> None:
    from superset.ai_dashboard.tasks import _progress_pct

    assert _progress_pct("Running C3 segmentation…", 10) == 40
    assert _progress_pct("Running C1 data quality checks…", 40) == 40
    assert _progress_pct("something else", 25) == 25


class TestModels:
    def test_openrouter_defaults(self) -> None:
        from superset.ai_dashboard import agent as ag

        cleared = {k: "" for k in os.environ if k.startswith("AI_DASH_")}
        with patch.dict(os.environ, {**cleared, "OPENROUTER_API_KEY": "sk-or-test"}):
            main, sub = ag._make_models()
        assert main.model_name == "nvidia/nemotron-3-ultra-550b-a55b:free"
        assert sub.model_name == "google/gemma-4-26b-a4b-it:free"
        assert main.openai_api_base == "https://openrouter.ai/api/v1"

    def test_openrouter_requires_key(self) -> None:
        from superset.ai_dashboard import agent as ag

        cleared = {k: "" for k in os.environ if k.startswith("AI_DASH_")}
        with patch.dict(os.environ, {**cleared, "OPENROUTER_API_KEY": ""}):
            with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
                ag._make_models()
