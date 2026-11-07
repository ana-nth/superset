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
Run read-only queries and turn their rows into JSON-safe dicts.

Every query runs in its own transaction that is rolled back afterwards; on
PostgreSQL it is also declared READ ONLY and gets a statement timeout. Filter
values are always bound parameters named ``:aicp<N>``; any other colon in the
SQL is literal (casts, time formats).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import bindparam, Connection, Engine, text
from sqlalchemy.engine import Dialect
from sqlalchemy.sql.elements import TextClause

from ai_chat import config
from ai_chat.kv import kv

logger = logging.getLogger(__name__)

BIND_PREFIX = "aicp"
_BIND_RE = re.compile(rf":({BIND_PREFIX}\d+)\b")
_LITERAL_COLON_RE = re.compile(rf":(?!{BIND_PREFIX}\d+\b)")
MAX_CACHED_ROWS = 20000


@dataclass
class Query:
    sql: str
    params: dict[str, Any] = field(default_factory=dict)
    expanding: set[str] = field(default_factory=set)

    def bind(self, value: Any) -> str:
        """Register ``value`` as a parameter and return its placeholder."""
        name = f"{BIND_PREFIX}{len(self.params)}"
        self.params[name] = value
        if isinstance(value, (list, tuple)):
            self.expanding.add(name)
            self.params[name] = list(value)
        return f":{name}"


def quoter(dialect: Dialect) -> Callable[[str], str]:
    """
    Quote identifiers for SQL that runs through ``text()``.

    The dialect's quoting doubles "%" for pyformat drivers (psycopg2, pymysql),
    and ``text()`` doubles it again, so a label like "Gross Margin (%)" would
    reach the database as "Gross Margin (%%)". Leave the escaping to ``text()``.
    """
    preparer = dialect.identifier_preparer
    doubles = bool(getattr(preparer, "_double_percents", False))

    def quote(ident: str) -> str:
        quoted = preparer.quote(ident)
        return quoted.replace("%%", "%") if doubles else quoted

    return quote


def statement(query: Query) -> TextClause:
    stmt = text(_LITERAL_COLON_RE.sub(r"\\:", query.sql))
    if query.expanding:
        stmt = stmt.bindparams(
            *[bindparam(name, expanding=True) for name in sorted(query.expanding)]
        )
    return stmt


def _prepare(conn: Connection) -> None:
    timeout_ms = max(config.query_timeout(), 1) * 1000
    dialect = conn.dialect.name
    if dialect == "postgresql":
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        conn.exec_driver_sql(f"SET LOCAL statement_timeout = {timeout_ms}")
    elif dialect in ("mysql", "mariadb"):
        try:
            conn.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME = {timeout_ms}")
        except Exception:  # pylint: disable=broad-except
            logger.debug("MAX_EXECUTION_TIME is not supported", exc_info=True)


def jsonable(value: Any) -> Any:  # noqa: C901
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        if value == value.to_integral_value() and abs(value) < 2**53:
            return int(value)
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "<binary>"
    return str(value)


def run(
    engine: Engine, query: Query, max_rows: Optional[int] = None
) -> list[dict[str, Any]]:
    """Execute ``query`` read-only and return at most ``max_rows`` rows."""
    limit = max_rows or config.max_rows()
    with engine.connect() as conn:
        try:
            _prepare(conn)
            result = conn.execute(statement(query), query.params)
            rows = result.mappings().fetchmany(limit)
            out = [{str(k): jsonable(v) for k, v in row.items()} for row in rows]
        finally:
            conn.rollback()
    return out


def _cache_key(database_id: str, query: Query) -> str:
    payload = json.dumps(
        [database_id, query.sql, sorted(query.params.items())], default=str
    )
    return "ai_chat:result:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cached_run(database_id: str, engine: Engine, query: Query) -> list[dict[str, Any]]:
    """``run`` with a short-lived result cache (charts render right after chatting)."""
    key = _cache_key(database_id, query)
    try:
        hit = kv().get(key)
        if hit is not None:
            return hit
    except Exception:  # pylint: disable=broad-except
        logger.debug("Result cache unreadable", exc_info=True)
    rows = run(engine, query)
    if len(rows) <= MAX_CACHED_ROWS:
        try:
            kv().set(key, rows, config.result_ttl())
        except Exception:  # pylint: disable=broad-except
            logger.debug("Result could not be cached", exc_info=True)
    return rows


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "(" + ", ".join(_literal(v) for v in value) + ")"
    if isinstance(value, (date, datetime)):
        return f"'{value.isoformat()}'"
    return "'" + str(value).replace("'", "''") + "'"


def render_sql(query: Query) -> str:
    """The query with its parameters inlined, for display only (never executed)."""
    return _BIND_RE.sub(lambda m: _literal(query.params.get(m.group(1))), query.sql)
