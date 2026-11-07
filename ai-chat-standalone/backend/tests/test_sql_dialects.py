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
"""Time ranges, dialect SQL and query execution details."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest
import sqlglot
from sqlalchemy import create_engine

from ai_chat import dialects, executor, timerange
from ai_chat.executor import Query

TODAY = date(2025, 5, 14)  # a Wednesday


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("No filter", (None, None)),
        (
            'DATEADD(DATETIME("today"), -30, day) : DATEADD(DATETIME("today"), 1, day)',
            (date(2025, 4, 14), date(2025, 5, 15)),
        ),
        (
            'DATETRUNC(DATETIME("today"), quarter) : DATEADD(DATETIME("today"), 1, day)',
            (date(2025, 4, 1), date(2025, 5, 15)),
        ),
        (
            'DATEADD(DATETRUNC(DATETIME("today"), week), -1, week) : '
            'DATETRUNC(DATETIME("today"), week)',
            (date(2025, 5, 5), date(2025, 5, 12)),
        ),
        (
            'DATEADD(DATETRUNC(DATETIME("today"), month), -1, month) : '
            'DATETRUNC(DATETIME("today"), month)',
            (date(2025, 4, 1), date(2025, 5, 1)),
        ),
        ("2024-01-01 : 2025-01-01", (date(2024, 1, 1), date(2025, 1, 1))),
        (
            '2025-02-01 : DATEADD(DATETIME("today"), 1, day)',
            (date(2025, 2, 1), date(2025, 5, 15)),
        ),
    ],
)
def test_time_range_evaluation(expression: str, expected: tuple) -> None:
    assert timerange.evaluate(expression, today=TODAY) == expected


@pytest.mark.parametrize(
    "expression", ["2025-01-01 : 2024-01-01", "yesterday", "DATEADD(x, 1, day) : "]
)
def test_bad_time_ranges(expression: str) -> None:
    with pytest.raises(ValueError):
        timerange.evaluate(expression, today=TODAY)


def test_month_arithmetic_clamps_the_day() -> None:
    start, _ = timerange.evaluate(
        'DATEADD(DATETIME("today"), -1, month) : DATETIME("today")',
        today=date(2025, 3, 31),
    )
    assert start == date(2025, 2, 28)


@pytest.mark.parametrize(
    "grain,expected",
    [
        ("P1D", "2025-05-14"),
        ("P1W", "2025-05-12"),
        ("P1M", "2025-05-01"),
        ("P3M", "2025-04-01"),
        ("P1Y", "2025-01-01"),
        ("PT1H", "2025-05-14 10:00:00"),
    ],
)
def test_sqlite_time_buckets(grain: str, expected: str) -> None:
    conn = sqlite3.connect(":memory:")
    expr = dialects.time_bucket("'2025-05-14 10:37:00'", grain, "sqlite")
    assert conn.execute(f"SELECT {expr}").fetchone()[0] == expected


@pytest.mark.parametrize(
    "dialect,sqlglot_dialect",
    [
        ("postgresql", "postgres"),
        ("mysql", "mysql"),
        ("mssql", "tsql"),
        ("oracle", "oracle"),
        ("bigquery", "bigquery"),
        ("snowflake", "snowflake"),
    ],
)
@pytest.mark.parametrize("grain", ["PT1H", "P1D", "P1W", "P1M", "P3M", "P1Y"])
def test_time_buckets_parse_in_their_dialect(
    dialect: str, sqlglot_dialect: str, grain: str
) -> None:
    bucket = dialects.time_bucket("order_date", grain, dialect)
    sql = dialects.limit(
        f"SELECT {bucket} AS t FROM sales GROUP BY {bucket} ORDER BY {bucket}",
        10,
        dialect,
        ordered=True,
    )
    sqlglot.parse_one(sql, read=sqlglot_dialect)


def test_literal_colons_are_not_parameters() -> None:
    engine = create_engine("sqlite://")
    query = Query("SELECT '10:30' AS t, :aicp0 AS v")
    query.params["aicp0"] = 5
    assert executor.run(engine, query) == [{"t": "10:30", "v": 5}]


def test_expanding_parameters_run() -> None:
    engine = create_engine("sqlite://")
    query = Query("")
    query.sql = (
        "SELECT COUNT(*) AS n FROM (SELECT 'a' AS x UNION ALL SELECT 'b' "
        f"UNION ALL SELECT 'c') s WHERE x IN {query.bind(['a', 'c'])}"
    )
    assert executor.run(engine, query) == [{"n": 2}]


def test_values_become_json_safe() -> None:
    from decimal import Decimal

    assert executor.jsonable(Decimal("3.0")) == 3
    assert executor.jsonable(Decimal("3.25")) == 3.25
    assert executor.jsonable(date(2025, 1, 2)) == "2025-01-02"
    assert executor.jsonable(float("nan")) is None


def test_percent_labels_survive_pyformat_drivers() -> None:
    from sqlalchemy.dialects import mysql, postgresql

    for dialect in (postgresql.psycopg2.dialect(), mysql.pymysql.dialect()):
        quote = executor.quoter(dialect)
        label = quote("Gross Margin (%)")
        assert "%%" not in label
        # text() then escapes it once for the driver, which turns it back into "%"
        compiled = str(
            executor.statement(Query(f"SELECT 1 AS {label}")).compile(dialect=dialect)
        )
        assert "Gross Margin (%%)" in compiled


def test_percent_labels_round_trip_on_sqlite() -> None:
    engine = create_engine("sqlite://")
    label = executor.quoter(engine.dialect)("Gross Margin (%)")
    assert executor.run(engine, Query(f"SELECT 1 AS {label}")) == [
        {"Gross Margin (%)": 1}
    ]
