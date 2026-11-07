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
The application's own metadata database: database connections, saved charts and
dashboards. SQLite by default, PostgreSQL in Docker (``AI_CHAT_METADATA_DB_URL``).
Tables are created on startup.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    create_engine,
    DateTime,
    Engine,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    Session,
    sessionmaker,
)

from ai_chat import config


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class DatabaseConnection(Base):
    """A SQL database the chat can query."""

    __tablename__ = "ai_chat_database"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(250), unique=True)
    uri_encrypted: Mapped[str] = mapped_column(Text)
    # Schemas users may chat about; empty allows every non-system schema
    schemas: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Defined by configuration (AI_CHAT_DATABASES), not editable from the UI
    managed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(250), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SavedChart(Base):
    """A chart saved from the chat: its resolved spec, re-queried on every view."""

    __tablename__ = "ai_chat_chart"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(250))
    owner: Mapped[str] = mapped_column(String(250), index=True)
    database_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ai_chat_database.id", ondelete="CASCADE")
    )
    schema: Mapped[str] = mapped_column(String(250))
    viz_type: Mapped[str] = mapped_column(String(50))
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    placements: Mapped[list[DashboardChart]] = relationship(
        back_populates="chart", cascade="all, delete-orphan"
    )


class Dashboard(Base):
    __tablename__ = "ai_chat_dashboard"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(500))
    owner: Mapped[str] = mapped_column(String(250), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    placements: Mapped[list[DashboardChart]] = relationship(
        back_populates="dashboard",
        cascade="all, delete-orphan",
        order_by="DashboardChart.position",
    )


class DashboardChart(Base):
    __tablename__ = "ai_chat_dashboard_chart"

    dashboard_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("ai_chat_dashboard.id", ondelete="CASCADE"),
        primary_key=True,
    )
    chart_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ai_chat_chart.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)

    dashboard: Mapped[Dashboard] = relationship(back_populates="placements")
    chart: Mapped[SavedChart] = relationship(back_populates="placements")


_ENGINE: Optional[Engine] = None
_SESSION: Optional[sessionmaker[Session]] = None


def engine() -> Engine:
    global _ENGINE, _SESSION
    if _ENGINE is None:
        url = config.metadata_db_url()
        kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        new_engine = create_engine(url, **kwargs)
        _create_tables(new_engine)
        _SESSION = sessionmaker(bind=new_engine, expire_on_commit=False)
        _ENGINE = new_engine
    return _ENGINE


def _create_tables(target: Engine) -> None:
    """Create missing tables; the API and worker may race on first start."""
    for attempt in range(3):
        try:
            Base.metadata.create_all(target)
            return
        except OperationalError:
            if attempt == 2:
                raise
            time.sleep(1 + attempt)
        except IntegrityError:
            time.sleep(0.5)  # another process created them concurrently


def init_db() -> None:
    engine()


def reset() -> None:
    """Forget the engine (tests switch databases)."""
    global _ENGINE, _SESSION
    if _ENGINE is not None:
        _ENGINE.dispose()
    _ENGINE = None
    _SESSION = None


@contextmanager
def session_scope() -> Iterator[Session]:
    engine()
    assert _SESSION is not None
    session = _SESSION()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
