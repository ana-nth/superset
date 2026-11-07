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
The SQL that differs between databases: time buckets, row limits and
case-insensitive matching. Weeks start on Monday everywhere.

Supported families: PostgreSQL-like (PostgreSQL, Redshift, DuckDB, Snowflake,
Trino/Presto), MySQL/MariaDB, SQLite, SQL Server, Oracle and BigQuery. Other
dialects use the PostgreSQL forms, which many engines accept.
"""

from __future__ import annotations

PG_LIKE = {
    "postgresql",
    "redshift",
    "redshift_connector",
    "duckdb",
    "snowflake",
    "trino",
    "presto",
    "databricks",
    "cockroachdb",
}
NATIVE_ILIKE = {"postgresql", "redshift", "redshift_connector", "duckdb", "snowflake"}

# ISO 8601 grain → unit name
GRAIN_UNITS = {
    "PT1H": "hour",
    "P1D": "day",
    "P1W": "week",
    "P1M": "month",
    "P3M": "quarter",
    "P1Y": "year",
}


def family(dialect: str) -> str:
    if dialect in ("mysql", "mariadb"):
        return "mysql"
    if dialect in ("sqlite", "mssql", "oracle", "bigquery"):
        return dialect
    return "postgresql"


def time_bucket(col: str, grain: str, dialect: str) -> str:  # noqa: C901
    """Truncate the (quoted) column ``col`` to the start of its ``grain`` period."""
    unit = GRAIN_UNITS.get(grain, "month")
    fam = family(dialect)
    if fam == "mysql":
        return {
            "hour": f"DATE_FORMAT({col}, '%Y-%m-%d %H:00:00')",
            "day": f"DATE({col})",
            "week": f"DATE(DATE_SUB({col}, INTERVAL WEEKDAY({col}) DAY))",
            "month": f"DATE(DATE_FORMAT({col}, '%Y-%m-01'))",
            "quarter": f"(MAKEDATE(YEAR({col}), 1) "
            f"+ INTERVAL QUARTER({col}) - 1 QUARTER)",
            "year": f"DATE(DATE_FORMAT({col}, '%Y-01-01'))",
        }[unit]
    if fam == "sqlite":
        return {
            "hour": f"STRFTIME('%Y-%m-%d %H:00:00', {col})",
            "day": f"DATE({col})",
            "week": f"DATE({col}, '-' || ((CAST(STRFTIME('%w', {col}) AS INTEGER) + 6)"
            " % 7) || ' days')",
            "month": f"DATE({col}, 'start of month')",
            "quarter": f"DATE({col}, 'start of month', '-' || "
            f"((CAST(STRFTIME('%m', {col}) AS INTEGER) - 1) % 3) || ' months')",
            "year": f"DATE({col}, 'start of year')",
        }[unit]
    if fam == "mssql":
        if unit == "week":
            return (
                f"DATEADD(day, -((DATEPART(weekday, {col}) + @@DATEFIRST - 2) % 7), "
                f"CAST({col} AS DATE))"
            )
        return f"DATEADD({unit}, DATEDIFF({unit}, 0, {col}), 0)"
    if fam == "oracle":
        fmt = {
            "hour": "HH",
            "day": "DD",
            "week": "IW",
            "month": "MM",
            "quarter": "Q",
            "year": "YYYY",
        }[unit]
        return f"TRUNC({col}, '{fmt}')"
    if fam == "bigquery":
        if unit == "hour":
            return f"TIMESTAMP_TRUNC(CAST({col} AS TIMESTAMP), HOUR)"
        part = "ISOWEEK" if unit == "week" else unit.upper()
        return f"DATE_TRUNC(DATE({col}), {part})"
    return f"DATE_TRUNC('{unit}', {col})"


def limit(sql: str, n: int, dialect: str, ordered: bool) -> str:
    """Append a row limit to ``sql``."""
    fam = family(dialect)
    if fam == "mssql":
        order = "" if ordered else " ORDER BY (SELECT NULL)"
        return f"{sql}{order} OFFSET 0 ROWS FETCH NEXT {int(n)} ROWS ONLY"
    if fam == "oracle":
        return f"{sql} FETCH FIRST {int(n)} ROWS ONLY"
    return f"{sql} LIMIT {int(n)}"


def ilike(col: str, param: str, dialect: str) -> str:
    """Case-insensitive LIKE of ``col`` against the bound ``param``."""
    if dialect in NATIVE_ILIKE:
        return f"CAST({col} AS VARCHAR) ILIKE {param}"
    return f"LOWER({col}) LIKE LOWER({param})"
