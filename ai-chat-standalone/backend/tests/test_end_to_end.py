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
The whole service against a real (SQLite) star schema: profiling, chatting
through the HTTP API with the LLM down (rules planner), chart data, SQL, saving
to dashboards and CSV export. No Redis, LLM or external database is needed.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_chat import datasources, llm, metadb
from ai_chat.kv import kv, MemoryKV, reset_kv

REGIONS = ["North", "South", "East", "West"]
CHANNELS = ["Online", "Retail", "Direct"]


def _make_warehouse(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE dim_region (
            region_id INTEGER PRIMARY KEY, region TEXT, country TEXT
        );
        CREATE TABLE dim_channel (
            channel_id INTEGER PRIMARY KEY, sales_channel TEXT
        );
        CREATE TABLE fact_sales (
            sale_id INTEGER PRIMARY KEY,
            order_date DATE,
            region_id INTEGER REFERENCES dim_region(region_id),
            channel_id INTEGER REFERENCES dim_channel(channel_id),
            line_amount NUMERIC(12, 2),
            quantity INTEGER,
            unit_price NUMERIC(10, 2)
        );
        """
    )
    conn.executemany(
        "INSERT INTO dim_region VALUES (?, ?, ?)",
        [(i + 1, r, "US" if i < 2 else "CA") for i, r in enumerate(REGIONS)],
    )
    conn.executemany(
        "INSERT INTO dim_channel VALUES (?, ?)",
        [(i + 1, c) for i, c in enumerate(CHANNELS)],
    )
    rows = []
    start = date(2025, 1, 1)
    for i in range(730):
        day = start + timedelta(days=i // 2)
        region = i % 4 + 1
        channel = i % 3 + 1
        quantity = 1 + i % 7
        price = 10.0 + region * 5
        rows.append(
            (i + 1, day.isoformat(), region, channel, quantity * price, quantity, price)
        )
    conn.executemany("INSERT INTO fact_sales VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    warehouse = tmp_path / "warehouse.db"
    _make_warehouse(warehouse)
    semantics = tmp_path / "semantics"
    semantics.mkdir()
    (semantics / "main.yaml").write_text(
        "schema: main\n"
        "tables:\n"
        "  fact_sales:\n"
        "    columns:\n"
        "      line_amount: {role: measure, description: Net revenue, unit: USD}\n"
        "business_metrics:\n"
        "  avg_order_value:\n"
        "    sql: SUM(line_amount) / COUNT(*)\n"
        "    meaning: Revenue per order line\n"
        "    unit: USD\n"
    )
    monkeypatch.setenv("AI_CHAT_METADATA_DB_URL", f"sqlite:///{tmp_path / 'meta.db'}")
    monkeypatch.setenv("AI_CHAT_SEMANTICS_DIR", str(semantics))
    monkeypatch.setenv(
        "AI_CHAT_DATABASES",
        json.dumps([{"name": "Test warehouse", "uri": f"sqlite:///{warehouse}"}]),
    )
    monkeypatch.delenv("AI_CHAT_REDIS_URL", raising=False)
    monkeypatch.delenv("AI_CHAT_API_KEYS", raising=False)
    reset_kv(MemoryKV())
    metadb.reset()
    # The LLM is "down": every message is planned by the keyword rules
    kv().set(llm.DOWN_KEY, "no model in tests", 3600)

    from ai_chat.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
    metadb.reset()
    reset_kv(None)


def _database_id(client: TestClient) -> str:
    result = client.get("/api/databases").json()["result"]
    assert [d["name"] for d in result] == ["Test warehouse"]
    assert result[0]["managed"] is True
    return result[0]["id"]


def _ask(
    client: TestClient, text: str, conversation_id: str | None = None, **extra: Any
) -> tuple[str, dict[str, Any]]:
    body: dict[str, Any] = {"message": text, **extra}
    if conversation_id:
        body["conversation_id"] = conversation_id
    response = client.post("/api/message", json=body)
    assert response.status_code == 202, response.text
    sent = response.json()
    deadline = time.time() + 30
    while time.time() < deadline:
        job = client.get(f"/api/job/{sent['job_id']}").json()
        if job["state"] == "SUCCESS":
            return sent["conversation_id"], job["message"]
        assert job["state"] in ("PENDING", "RUNNING"), job
        time.sleep(0.05)
    raise AssertionError("the chat job did not finish")


def test_catalog_profiles_the_star_schema(client: TestClient) -> None:
    db_id = _database_id(client)
    assert client.get(f"/api/databases/{db_id}/schemas").json()["result"] == ["main"]
    catalog = client.get(
        "/api/catalog", params={"database_id": db_id, "schema": "main"}
    ).json()
    sales = catalog["sources"][0]
    assert sales["name"] == "fact_sales"
    assert sales["time_min"] == "2025-01-01"
    # Dimensions come from the joined tables
    assert {"region", "country", "sales_channel"} <= set(sales["dimensions"])
    assert {"line_amount", "quantity", "unit_price"} <= set(sales["measures"])
    assert sales["metrics"] == ["avg_order_value"]
    assert catalog["suggestions"]


def test_chat_creates_a_chart_and_follow_ups(client: TestClient) -> None:
    db_id = _database_id(client)
    conv_id, msg = _ask(
        client, "total line amount by region", database_id=db_id, schema="main"
    )
    assert msg["type"] == "chart", msg
    assert msg["engine"] == "rules"
    chart = msg["chart"]
    assert chart["viz_type"] == "bar"
    assert chart["config"]["x"] == "region"
    assert "Top region by Total Line Amount" in msg["insight"]

    data = client.get(f"/api/conversations/{conv_id}/messages/{msg['id']}/data").json()
    regions = [row["region"] for row in data["rows"]]
    assert sorted(regions) == sorted(REGIONS)
    totals = [row["Total Line Amount"] for row in data["rows"]]
    assert totals == sorted(totals, reverse=True)

    # A follow-up modifies the previous chart
    _, pie = _ask(client, "make it a pie", conv_id)
    assert pie["chart"]["viz_type"] == "pie"

    _, sql = _ask(client, "show the sql", conv_id)
    assert sql["type"] == "sql"
    assert 'SUM(line_amount) AS "Total Line Amount"' in sql["sql"]
    assert sql["sql"].endswith("LIMIT 12")  # the pie's slice limit

    history = client.get(f"/api/conversations/{conv_id}").json()["result"]
    assert len(history["messages"]) == 6
    assert all("_intent" not in m for m in history["messages"])


def test_monthly_trend_with_filter_and_period(client: TestClient) -> None:
    db_id = _database_id(client)
    conv_id, msg = _ask(
        client,
        "monthly quantity trend for Online in 2025",
        database_id=db_id,
        schema="main",
    )
    assert msg["type"] == "chart", msg
    assert msg["chart"]["viz_type"] == "line"
    rows = client.get(f"/api/conversations/{conv_id}/messages/{msg['id']}/data").json()[
        "rows"
    ]
    months = [row["order_date"] for row in rows]
    assert months[0] == "2025-01-01"
    assert len(months) == 12
    assert months == sorted(months)


def test_business_metric_and_histogram(client: TestClient) -> None:
    db_id = _database_id(client)
    conv_id, msg = _ask(
        client, "avg order value by sales channel", database_id=db_id, schema="main"
    )
    assert msg["type"] == "chart", msg
    assert msg["chart"]["spec"]["metrics"][0]["business"] == "avg_order_value"

    _, hist = _ask(client, "distribution of unit price", conv_id)
    assert hist["chart"]["viz_type"] == "histogram", hist
    data = client.get(f"/api/conversations/{conv_id}/messages/{hist['id']}/data").json()
    bins = data["histogram"]
    assert len(bins["bins"]) == 20
    # A follow-up keeps the previous breakdown: one series per channel
    assert sorted(series["name"] for series in bins["series"]) == sorted(CHANNELS)
    assert sum(sum(series["counts"]) for series in bins["series"]) == 730


def test_save_to_a_new_dashboard_from_chat(client: TestClient) -> None:
    db_id = _database_id(client)
    conv_id, msg = _ask(
        client, "total line amount by region", database_id=db_id, schema="main"
    )
    _, saved = _ask(client, "save it to Sales Overview", conv_id)
    assert saved["type"] == "saved", saved
    assert saved["saved"]["dashboard_created"] is True

    dashboards = client.get("/api/dashboards").json()["result"]
    assert [(d["title"], d["chart_count"]) for d in dashboards] == [
        ("Sales Overview", 1)
    ]
    dashboard = client.get(f"/api/dashboards/{dashboards[0]['id']}").json()["result"]
    chart_id = dashboard["charts"][0]["id"]
    rows = client.get(f"/api/charts/{chart_id}/data").json()["rows"]
    assert len(rows) == 4

    csv = client.get(f"/api/charts/{chart_id}/csv")
    assert csv.headers["content-type"].startswith("text/csv")
    assert csv.text.splitlines()[0] == "region,Total Line Amount"

    # Saving again through the API reuses the chart
    again = client.post(
        "/api/save_chart",
        json={
            "conversation_id": conv_id,
            "message_id": msg["id"],
            "new_dashboard_title": "Exec",
        },
    ).json()
    assert again["chart_id"] == chart_id
    assert len(client.get("/api/charts").json()["result"]) == 1


def test_errors_are_reported(client: TestClient) -> None:
    db_id = _database_id(client)
    _, msg = _ask(client, "delete all orders", database_id=db_id, schema="main")
    assert msg["type"] == "text"
    assert "can't change data" in msg["text"]

    bad = client.get("/api/catalog", params={"database_id": db_id, "schema": "nope"})
    assert bad.status_code == 400

    # SQLite reads local files, so the UI may not add it
    refused = client.post(
        "/api/databases", json={"name": "x", "uri": "sqlite:////etc/passwd"}
    )
    assert refused.status_code == 400
    assert "SQLite" in refused.json()["message"]

    assert client.get("/api/conversations/missing").status_code == 404


def test_api_keys_are_enforced(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_CHAT_API_KEYS", "secret-1,secret-2")
    assert client.get("/api/databases").status_code == 401
    assert (
        client.get("/api/databases", headers={"X-API-Key": "nope"}).status_code == 401
    )
    ok = client.get("/api/databases", headers={"Authorization": "Bearer secret-2"})
    assert ok.status_code == 200
    assert client.get("/api/config").json()["auth_required"] is True


def test_configured_connections_keep_their_secret(client: TestClient) -> None:
    info = datasources.get_connection(_database_id(client))
    with metadb.session_scope() as session:
        row = session.get(metadb.DatabaseConnection, info.id)
        assert row is not None
        assert "warehouse.db" not in row.uri_encrypted  # stored encrypted


def _fake_llm(monkeypatch: pytest.MonkeyPatch, replies: list[dict[str, Any]]) -> list:
    """Bring the LLM "up" with scripted JSON replies; returns the prompts sent."""
    calls: list = []
    queue = iter(replies)

    def complete_json(messages: list[dict[str, str]]) -> dict[str, Any]:
        calls.append(messages)
        return next(queue)

    kv().delete(llm.DOWN_KEY)
    monkeypatch.setattr(llm, "complete_json", complete_json)
    return calls


def test_schema_question_is_explained_even_if_planned_as_a_chart(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_id = _database_id(client)
    # The model wrongly plans a big number, as small models do for this message
    calls = _fake_llm(
        monkeypatch,
        [
            {"action": "create_chart", "metrics": [{"column": "line_amount"}]},
            {"answer": "This database tracks sales order lines by region."},
        ],
    )
    _, msg = _ask(
        client,
        "Make me understand the database, the tables, the schema etc",
        database_id=db_id,
        schema="main",
    )
    assert msg["type"] == "catalog", msg
    assert msg["text"] == "This database tracks sales order lines by region."
    sales = msg["schema"][0]
    assert sales["tables"] == ["fact_sales", "dim_channel", "dim_region"]
    assert {d["name"] for d in sales["dimensions"]} >= {"region", "sales_channel"}
    region = next(d for d in sales["dimensions"] if d["name"] == "region")
    assert sorted(region["values"]) == sorted(REGIONS)
    assert "fact_sales joined to dim_channel, dim_region" in calls[1][0]["content"]


def test_data_question_gets_a_direct_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_id = _database_id(client)
    calls = _fake_llm(
        monkeypatch,
        [
            {
                "action": "create_chart",
                "source": "fact_sales",
                "metrics": [{"column": "line_amount"}],
                "dimensions": ["region"],
                "reply": "Here is revenue by region.",
            },
            {"answer": "West has the highest revenue."},
        ],
    )
    _, msg = _ask(
        client,
        "which region has the highest revenue?",
        database_id=db_id,
        schema="main",
    )
    assert msg["type"] == "chart", msg
    assert msg["text"] == "West has the highest revenue."
    assert msg["insight"].startswith("Top region by Total Line Amount: West")
    prompt = calls[1][0]["content"]
    assert "QUESTION: which region has the highest revenue?" in prompt
    assert '["West", ' in prompt  # the real query result is in the prompt


def test_schema_question_without_llm_is_a_factual_overview(client: TestClient) -> None:
    db_id = _database_id(client)
    _, msg = _ask(
        client, "how are the tables related?", database_id=db_id, schema="main"
    )
    assert msg["type"] == "catalog"
    assert msg["text"].startswith("Test warehouse.main has")
    assert "It joins fact_sales to dim_channel, dim_region." in msg["text"]


# ──────────────────────────────────────────────────────────────────────────────
# Derived dimensions (weekday, bands, groups) through the whole pipeline
# ──────────────────────────────────────────────────────────────────────────────


def _rows(client: TestClient, conv_id: str, msg: dict[str, Any]) -> list[dict]:
    url = f"/api/conversations/{conv_id}/messages/{msg['id']}/data"
    return client.get(url).json()["rows"]


def _warehouse_rows(tmp_path: Path) -> list[tuple[str, float, float]]:
    conn = sqlite3.connect(tmp_path / "warehouse.db")
    rows = conn.execute("SELECT order_date, line_amount, unit_price FROM fact_sales")
    data = [(str(d), float(a), float(p)) for d, a, p in rows]
    conn.close()
    return data


def test_revenue_by_day_of_week_and_only_weekends(
    client: TestClient, tmp_path: Path
) -> None:
    db_id = _database_id(client)
    conv_id, msg = _ask(
        client, "total line amount by day of week", database_id=db_id, schema="main"
    )
    assert msg["type"] == "chart", msg
    chart = msg["chart"]
    assert chart["viz_type"] == "bar"
    assert chart["config"]["x"] == "Day of Week"
    assert "By: Day of Week" in chart["interpretation"]

    rows = _rows(client, conv_id, msg)
    days = ["1 Mon", "2 Tue", "3 Wed", "4 Thu", "5 Fri", "6 Sat", "7 Sun"]
    assert [r["Day of Week"] for r in rows] == days  # Mon→Sun, not by value
    expected: dict[str, float] = dict.fromkeys(days, 0.0)
    for day, amount, _ in _warehouse_rows(tmp_path):
        expected[days[date.fromisoformat(day).weekday()]] += amount
    for row in rows:
        assert row["Total Line Amount"] == pytest.approx(expected[row["Day of Week"]])

    _, weekends = _ask(client, "only weekends", conv_id)
    assert weekends["type"] == "chart", weekends
    assert [r["Day of Week"] for r in _rows(client, conv_id, weekends)] == [
        "6 Sat",
        "7 Sun",
    ]
    assert "Filter: Weekday / Weekend = Weekend" in weekends["chart"]["interpretation"]

    _, sql = _ask(client, "show the sql", conv_id)
    assert "CASE" in sql["sql"]
    assert "STRFTIME('%w', order_date)" in sql["sql"]


def test_counts_by_typed_price_bands(client: TestClient, tmp_path: Path) -> None:
    db_id = _database_id(client)
    conv_id, msg = _ask(
        client,
        "number of rows by unit price bands 20, 30",
        database_id=db_id,
        schema="main",
    )
    assert msg["type"] == "chart", msg
    rows = _rows(client, conv_id, msg)
    prices = [p for _, _, p in _warehouse_rows(tmp_path)]
    assert {r["Unit Price Band"]: r["Count"] for r in rows} == {
        "1. under 20": sum(p < 20 for p in prices),
        "2. 20-30": sum(20 <= p < 30 for p in prices),
        "3. 30+": sum(p >= 30 for p in prices),
    }
    assert [r["Unit Price Band"] for r in rows] == ["1. under 20", "2. 20-30", "3. 30+"]


def test_llm_groups_of_values(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_id = _database_id(client)
    _fake_llm(
        monkeypatch,
        [
            {
                "action": "create_chart",
                "source": "fact_sales",
                "metrics": [{"column": "line_amount", "aggregate": "sum"}],
                "dimensions": [
                    {
                        "derive": "group",
                        "column": "region",
                        "groups": {"Coasts": ["east", "West"]},
                    }
                ],
                "reply": "Revenue for the coasts and the rest.",
            },
            {"answer": "The coasts bring in about half of the revenue."},
        ],
    )
    conv_id, msg = _ask(
        client, "revenue for East+West vs the rest", database_id=db_id, schema="main"
    )
    assert msg["type"] == "chart", msg
    groups = {r["Region Group"] for r in _rows(client, conv_id, msg)}
    assert groups == {"Coasts", "Other"}
