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
Database connections the chat can query.

Connections come from configuration (``AI_CHAT_DATABASES`` / ``AI_CHAT_DATABASES_FILE``,
synced on startup and marked *managed*) or are added from the UI. URIs are stored
encrypted with ``AI_CHAT_SECRET_KEY`` and never returned with their password.
Use a read-only database user: the chat only issues SELECTs, but the database is
the real guard.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml
from sqlalchemy import create_engine, Engine, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import NullPool

from ai_chat import config
from ai_chat.metadb import DatabaseConnection, session_scope

logger = logging.getLogger(__name__)

SYSTEM_SCHEMAS = {
    "information_schema",
    "pg_catalog",
    "pg_toast",
    "mysql",
    "performance_schema",
    "sys",
    "crdb_internal",
    "pg_extension",
}


class DataSourceError(Exception):
    """A connection problem with a message that is safe to show to the user."""


@dataclass
class ConnectionInfo:
    id: str
    name: str
    uri: str
    schemas: list[str] = field(default_factory=list)
    managed: bool = False

    @property
    def backend(self) -> str:
        return make_url(self.uri).get_backend_name()


# ── secrets ──────────────────────────────────────────────────────────────────


def _fernet() -> Any:
    from cryptography.fernet import Fernet

    digest = hashlib.sha256(config.secret_key().encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(uri: str) -> str:
    return _fernet().encrypt(uri.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise DataSourceError(
            "This connection was saved with a different AI_CHAT_SECRET_KEY; "
            "add it again."
        ) from exc


def masked(uri: str) -> str:
    try:
        return make_url(uri).render_as_string(hide_password=True)
    except ArgumentError:
        return "<invalid uri>"


# ── validation ───────────────────────────────────────────────────────────────


def validate_uri(uri: str, from_ui: bool) -> str:
    uri = uri.strip()
    try:
        url = make_url(uri)
    except ArgumentError as exc:
        raise DataSourceError(
            "That is not a valid SQLAlchemy URI, e.g. "
            "postgresql+psycopg2://user:password@host:5432/dbname"
        ) from exc
    if from_ui and url.get_backend_name() == "sqlite":
        if not config.allow_sqlite_connections():
            raise DataSourceError(
                "SQLite connections read local files and are disabled; set "
                "AI_CHAT_ALLOW_SQLITE_CONNECTIONS=true to allow them."
            )
    return uri


def _describe(exc: Exception) -> str:
    text = str(getattr(exc, "orig", None) or exc).strip().splitlines()
    first = text[0] if text else type(exc).__name__
    if isinstance(exc, ModuleNotFoundError) or "No module named" in first:
        return (
            f"The database driver is not installed ({first}). Add it to the "
            "backend image with EXTRA_PIP_PACKAGES."
        )
    return first[:300]


def test_uri(uri: str) -> list[str]:
    """Connect once and return the visible schemas."""
    try:
        engine = create_engine(uri, poolclass=NullPool)
        try:
            with engine.connect() as conn:
                return _visible_schemas(inspect(conn).get_schema_names())
        finally:
            engine.dispose()
    except Exception as exc:  # pylint: disable=broad-except
        raise DataSourceError(f"Could not connect: {_describe(exc)}") from exc


def _visible_schemas(names: list[str]) -> list[str]:
    return sorted(
        n
        for n in names
        if n.lower() not in SYSTEM_SCHEMAS and not n.lower().startswith("pg_")
    )


# ── registry ─────────────────────────────────────────────────────────────────


def _info(row: DatabaseConnection) -> ConnectionInfo:
    return ConnectionInfo(
        id=row.id,
        name=row.name,
        uri=decrypt(row.uri_encrypted),
        schemas=list(row.schemas or []),
        managed=row.managed,
    )


def get_connection(database_id: str) -> ConnectionInfo:
    with session_scope() as session:
        row = session.get(DatabaseConnection, database_id)
        if row is None:
            raise DataSourceError(f"Database {database_id} was not found.")
        return _info(row)


def list_connections() -> list[dict[str, Any]]:
    out = []
    with session_scope() as session:
        rows = session.scalars(
            select(DatabaseConnection).order_by(DatabaseConnection.name)
        ).all()
        for row in rows:
            try:
                uri = decrypt(row.uri_encrypted)
            except DataSourceError:
                uri = ""
            out.append(
                {
                    "id": row.id,
                    "name": row.name,
                    "backend": make_url(uri).get_backend_name() if uri else "",
                    "uri": masked(uri) if uri else "",
                    "schemas": list(row.schemas or []),
                    "managed": row.managed,
                }
            )
    return out


def add_connection(
    name: str, uri: str, schemas: Optional[list[str]], user: str
) -> dict[str, Any]:
    name = name.strip()[:250]
    if not name:
        raise DataSourceError("Give the connection a name.")
    uri = validate_uri(uri, from_ui=True)
    test_uri(uri)
    with session_scope() as session:
        exists = session.scalar(
            select(DatabaseConnection).where(DatabaseConnection.name == name)
        )
        if exists is not None:
            raise DataSourceError(f"A connection named “{name}” already exists.")
        row = DatabaseConnection(
            name=name,
            uri_encrypted=encrypt(uri),
            schemas=[s.strip() for s in schemas or [] if s.strip()],
            created_by=user,
        )
        session.add(row)
        session.flush()
        return {"id": row.id, "name": row.name}


def delete_connection(database_id: str) -> None:
    with session_scope() as session:
        row = session.get(DatabaseConnection, database_id)
        if row is None:
            raise DataSourceError(f"Database {database_id} was not found.")
        if row.managed:
            raise DataSourceError(
                "This connection comes from the server configuration and cannot "
                "be deleted here."
            )
        session.delete(row)
    dispose_engine(database_id)


def _configured() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    path = config.databases_file()
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or []
            entries += data.get("databases", []) if isinstance(data, dict) else data
        except (OSError, yaml.YAMLError) as exc:
            logger.error("Cannot read AI_CHAT_DATABASES_FILE %s: %s", path, exc)
    raw = config.databases_json()
    if raw:
        try:
            entries += json.loads(raw)
        except ValueError as exc:
            logger.error("AI_CHAT_DATABASES is not valid JSON: %s", exc)
    return [
        e for e in entries if isinstance(e, dict) and e.get("name") and e.get("uri")
    ]


def sync_configured_connections() -> None:
    """Upsert configured connections; ones removed from config become editable."""
    entries = _configured()
    names = {str(e["name"]).strip() for e in entries}
    with session_scope() as session:
        rows = {r.name: r for r in session.scalars(select(DatabaseConnection)).all()}
        for entry in entries:
            name = str(entry["name"]).strip()[:250]
            try:
                uri = validate_uri(str(entry["uri"]), from_ui=False)
            except DataSourceError as exc:
                logger.error("Skipping configured database %s: %s", name, exc)
                continue
            schemas = [str(s) for s in entry.get("schemas") or []]
            row = rows.get(name)
            if row is None:
                session.add(
                    DatabaseConnection(
                        name=name,
                        uri_encrypted=encrypt(uri),
                        schemas=schemas,
                        managed=True,
                        created_by="config",
                    )
                )
            else:
                row.uri_encrypted = encrypt(uri)
                row.schemas = schemas
                row.managed = True
                dispose_engine(row.id)
        for name, row in rows.items():
            if row.managed and name not in names:
                row.managed = False


# ── engines ──────────────────────────────────────────────────────────────────

_ENGINES: dict[str, tuple[str, Engine]] = {}
_ENGINES_LOCK = threading.Lock()


def get_engine(info: ConnectionInfo) -> Engine:
    fingerprint = hashlib.sha256(info.uri.encode("utf-8")).hexdigest()
    with _ENGINES_LOCK:
        cached = _ENGINES.get(info.id)
        if cached and cached[0] == fingerprint:
            return cached[1]
        if cached:
            cached[1].dispose()
        try:
            engine = create_engine(info.uri, pool_pre_ping=True, pool_recycle=1800)
        except Exception as exc:  # pylint: disable=broad-except
            raise DataSourceError(
                f"Could not open {info.name}: {_describe(exc)}"
            ) from exc
        _ENGINES[info.id] = (fingerprint, engine)
        return engine


def dispose_engine(database_id: str) -> None:
    with _ENGINES_LOCK:
        cached = _ENGINES.pop(database_id, None)
    if cached:
        cached[1].dispose()


def list_schemas(info: ConnectionInfo) -> list[str]:
    try:
        with get_engine(info).connect() as conn:
            names = _visible_schemas(inspect(conn).get_schema_names())
    except DataSourceError:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        raise DataSourceError(f"Could not read schemas: {_describe(exc)}") from exc
    if info.schemas:
        allowed = set(info.schemas)
        names = [n for n in names if n in allowed]
    return names


def check_schema(info: ConnectionInfo, schema: str) -> None:
    if info.schemas and schema not in info.schemas:
        raise DataSourceError(
            f"Schema '{schema}' is not enabled for {info.name}. "
            f"Available: {', '.join(info.schemas)}."
        )
