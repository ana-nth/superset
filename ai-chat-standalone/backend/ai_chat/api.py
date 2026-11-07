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
REST API (all under ``/api``; interactive docs at ``/api/docs``).

Chat
  POST   /message                              send a message → 202 {job_id, …}
  GET    /job/{job_id}                         poll a message job
  GET    /catalog?database_id&schema&refresh   data summary + starter questions
  GET    /conversations                        the user's recent conversations
  GET    /conversations/{id}                   one conversation with its messages
  DELETE /conversations/{id}                   delete a conversation
  GET    /conversations/{id}/messages/{mid}/data   rows to draw a chat chart
  GET    /conversations/{id}/messages/{mid}/csv    the same rows as CSV
  POST   /save_chart                           save a chat chart (+ dashboard)

Data sources
  GET    /databases, POST /databases, POST /databases/test,
  DELETE /databases/{id}, GET /databases/{id}/schemas

Saved charts and dashboards
  GET /charts, GET|DELETE /charts/{id}, GET /charts/{id}/data|csv|sql
  GET|POST /dashboards, GET|PATCH|DELETE /dashboards/{id},
  DELETE /dashboards/{id}/charts/{chart_id}
"""

from __future__ import annotations

import csv
import hmac
import io
import logging
import re
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ai_chat import __version__, config, datasources, llm, store, tasks
from ai_chat.catalog import CatalogError, get_catalog
from ai_chat.metadb import Dashboard, DashboardChart, SavedChart, session_scope
from ai_chat.service import (
    chart_payload,
    chart_rows,
    chart_sql,
    ChatError,
    MAX_MESSAGE_CHARS,
    public_conversation,
    save_chart,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def api_error_response(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return JSONResponse({"message": exc.message, **exc.extra}, status_code=exc.status)


CHAT_ERROR_STATUS = {"forbidden": 403, "not_found": 404}


def _chat_error(exc: ChatError) -> ApiError:
    return ApiError(CHAT_ERROR_STATUS.get(exc.code, 400), str(exc), code=exc.code)


# ── identity ─────────────────────────────────────────────────────────────────


def current_user(request: Request) -> str:
    """The caller; checks the API key when ``AI_CHAT_API_KEYS`` is set."""
    keys = config.api_keys()
    if keys:
        supplied = request.headers.get("x-api-key", "")
        auth = request.headers.get("authorization", "")
        if not supplied and auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        if not any(hmac.compare_digest(supplied, key) for key in keys):
            raise ApiError(401, "A valid API key is required.")
    if config.trust_user_header():
        user = request.headers.get("x-user-id", "").strip()
        if user:
            return user[:250]
    return "default"


User = Annotated[str, Depends(current_user)]


# ── service info ─────────────────────────────────────────────────────────────


@router.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__}


@router.get("/config")
def app_config() -> dict[str, Any]:
    return {
        "version": __version__,
        "auth_required": bool(config.api_keys()),
        "allow_ui_connections": config.allow_ui_connections(),
        "provider": llm.provider(),
        "model": llm.model_name(),
        "max_message_chars": MAX_MESSAGE_CHARS,
    }


# ── databases ────────────────────────────────────────────────────────────────


class DatabaseIn(BaseModel):
    name: str = Field(max_length=250)
    uri: str = Field(max_length=4000)
    schemas: list[str] = []


class UriIn(BaseModel):
    uri: str = Field(max_length=4000)


@router.get("/databases")
def databases(_user: User) -> dict[str, Any]:
    return {"result": datasources.list_connections()}


@router.post("/databases", status_code=201)
def add_database(body: DatabaseIn, user: User) -> dict[str, Any]:
    if not config.allow_ui_connections():
        raise ApiError(403, "Adding connections from the UI is disabled.")
    try:
        return datasources.add_connection(body.name, body.uri, body.schemas, user)
    except datasources.DataSourceError as exc:
        raise ApiError(400, str(exc)) from exc


@router.post("/databases/test")
def test_database(body: UriIn, _user: User) -> dict[str, Any]:
    if not config.allow_ui_connections():
        raise ApiError(403, "Adding connections from the UI is disabled.")
    try:
        uri = datasources.validate_uri(body.uri, from_ui=True)
        return {"schemas": datasources.test_uri(uri)}
    except datasources.DataSourceError as exc:
        raise ApiError(400, str(exc)) from exc


@router.delete("/databases/{database_id}")
def delete_database(database_id: str, _user: User) -> dict[str, str]:
    if not config.allow_ui_connections():
        raise ApiError(403, "Managing connections from the UI is disabled.")
    try:
        datasources.delete_connection(database_id)
    except datasources.DataSourceError as exc:
        raise ApiError(400, str(exc)) from exc
    return {"message": "OK"}


@router.get("/databases/{database_id}/schemas")
def schemas(database_id: str, _user: User) -> dict[str, Any]:
    try:
        info = datasources.get_connection(database_id)
        return {"result": datasources.list_schemas(info)}
    except datasources.DataSourceError as exc:
        raise ApiError(400, str(exc)) from exc


@router.get("/catalog")
def catalog(
    database_id: str, schema: str, _user: User, refresh: bool = False
) -> dict[str, Any]:
    """Summarise what can be charted in a schema."""
    try:
        cat = get_catalog(database_id, schema.strip(), force=refresh)
    except CatalogError as exc:
        raise ApiError(400, str(exc)) from exc
    return {
        "database_name": cat.database_name,
        "schema": cat.schema,
        "sources": cat.summary(),
        "suggestions": cat.suggestions(),
    }


# ── chat ─────────────────────────────────────────────────────────────────────


class MessageIn(BaseModel):
    message: str = ""
    conversation_id: Optional[str] = None
    database_id: Optional[str] = None
    schema_name: Optional[str] = Field(default=None, alias="schema")
    reference_message_id: Optional[str] = None


@router.post("/message", status_code=202)
def message(body: MessageIn, user: User) -> Any:  # noqa: C901
    """Send a chat message; the answer is produced in the background."""
    text = body.message.strip()
    if not text:
        raise ApiError(400, "Please type a message.")
    if len(text) > MAX_MESSAGE_CHARS:
        raise ApiError(400, f"Messages are limited to {MAX_MESSAGE_CHARS} characters.")
    try:
        store.check_cache()
    except store.StoreUnavailableError as exc:
        logger.error("AI chat needs a working cache: %s", exc)
        raise ApiError(503, "Chat is unavailable: the cache (Redis) is down.") from exc

    wait = store.check_rate_limit(user)
    if wait is not None:
        raise ApiError(
            429,
            f"You're sending messages too quickly. Try again in {wait}s.",
            retry_after=wait,
        )

    if body.conversation_id:
        conv = store.load_conversation(user, body.conversation_id)
        if conv is None:
            raise ApiError(404, "Not found")
    else:
        schema = (body.schema_name or "").strip()
        if not body.database_id or not schema:
            raise ApiError(400, "Choose a database and schema first.")
        try:
            datasources.get_connection(body.database_id)
        except datasources.DataSourceError as exc:
            raise ApiError(400, str(exc)) from exc
        conv = store.new_conversation(user, body.database_id, schema)

    job_id = store.new_id()
    holder = store.acquire_lock(user, conv["id"], job_id)
    if holder is not None:
        raise ApiError(
            409,
            "I'm still answering your previous message.",
            job_id=holder,
            conversation_id=conv["id"],
        )

    user_msg = {
        "id": store.new_id(),
        "role": "user",
        "type": "text",
        "text": text,
        "created_at": store.now_iso(),
    }
    conv["messages"].append(user_msg)
    if not conv.get("title"):
        conv["title"] = text[:60] + ("…" if len(text) > 60 else "")
    # Lets a reloaded page resume waiting for this answer
    conv["pending_job_id"] = job_id
    try:
        store.save_conversation(conv)
        store.set_job(
            user, job_id, state="PENDING", stage="Queued", conversation_id=conv["id"]
        )
        tasks.start_turn(
            {
                "job_id": job_id,
                "user_id": user,
                "conversation_id": conv["id"],
                "user_message_id": user_msg["id"],
                "reference_message_id": body.reference_message_id or None,
            }
        )
    except Exception:
        store.release_lock(user, conv["id"], job_id)
        raise
    return {"job_id": job_id, "conversation_id": conv["id"], "user_message": user_msg}


@router.get("/job/{job_id}")
def job(job_id: str, user: User) -> dict[str, Any]:
    found = store.get_job(user, job_id)
    if found is None:
        return {"state": "NOT_FOUND", "job_id": job_id}
    return found


@router.get("/conversations")
def conversations(user: User) -> dict[str, Any]:
    return {"result": store.list_conversations(user)}


@router.get("/conversations/{conversation_id}")
def conversation(conversation_id: str, user: User) -> dict[str, Any]:
    conv = store.load_conversation(user, conversation_id)
    if conv is None:
        raise ApiError(404, "Not found")
    return {"result": public_conversation(conv)}


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, user: User) -> dict[str, str]:
    if not store.delete_conversation(user, conversation_id):
        raise ApiError(404, "Not found")
    return {"message": "OK"}


def _chat_chart(user: str, conversation_id: str, message_id: str) -> dict[str, Any]:
    conv = store.load_conversation(user, conversation_id)
    if conv is None:
        raise ApiError(404, "Not found")
    msg = store.find_message(conv, message_id)
    if msg is None or not msg.get("chart"):
        raise ApiError(404, "Not found")
    return msg["chart"]


def _payload(chart: dict[str, Any]) -> dict[str, Any]:
    try:
        return chart_payload(
            chart["database_id"],
            chart["schema"],
            chart["viz_type"],
            chart["spec"],
            chart["config"],
        )
    except ChatError as exc:
        raise _chat_error(exc) from exc


def _csv(chart: dict[str, Any], title: str) -> Response:
    try:
        rows = chart_rows(
            chart["database_id"], chart["schema"], chart["viz_type"], chart["spec"]
        )
    except ChatError as exc:
        raise _chat_error(exc) from exc
    buffer = io.StringIO()
    if rows:
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")[:80] or "chart"
    return Response(
        buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{slug}.csv"'},
    )


@router.get("/conversations/{conversation_id}/messages/{message_id}/data")
def message_data(conversation_id: str, message_id: str, user: User) -> dict[str, Any]:
    return _payload(_chat_chart(user, conversation_id, message_id))


@router.get("/conversations/{conversation_id}/messages/{message_id}/csv")
def message_csv(conversation_id: str, message_id: str, user: User) -> Response:
    chart = _chat_chart(user, conversation_id, message_id)
    return _csv(chart, chart.get("title", "chart"))


class SaveChartIn(BaseModel):
    conversation_id: str
    message_id: str
    title: Optional[str] = Field(default=None, max_length=250)
    dashboard_id: Optional[str] = None
    new_dashboard_title: Optional[str] = Field(default=None, max_length=500)


@router.post("/save_chart")
def save_chart_endpoint(body: SaveChartIn, user: User) -> dict[str, Any]:
    """Save a chart from a chat message, optionally adding it to a dashboard."""
    conv = store.load_conversation(user, body.conversation_id)
    if conv is None:
        raise ApiError(404, "Not found")
    msg = store.find_message(conv, body.message_id)
    if msg is None or not msg.get("chart"):
        raise ApiError(404, "Not found")
    try:
        result = save_chart(
            user,
            msg,
            title=(body.title or "").strip() or None,
            dashboard_id=body.dashboard_id or None,
            new_dashboard_title=(body.new_dashboard_title or "").strip() or None,
        )
    except ChatError as exc:
        raise _chat_error(exc) from exc
    # Reload before writing: a chat turn may have appended messages meanwhile
    latest = store.load_conversation(user, conv["id"]) or conv
    target = store.find_message(latest, msg["id"])
    if target is not None:
        target["chart"] = msg["chart"]
        store.save_conversation(latest)
    return result


# ── saved charts ─────────────────────────────────────────────────────────────


def _chart_dict(chart: SavedChart) -> dict[str, Any]:
    return {
        "id": chart.id,
        "title": chart.title,
        "viz_type": chart.viz_type,
        "database_id": chart.database_id,
        "schema": chart.schema,
        "config": chart.config,
        "spec": chart.spec,
        "created_at": chart.created_at.isoformat() if chart.created_at else None,
        "dashboards": [
            {"id": p.dashboard.id, "title": p.dashboard.title} for p in chart.placements
        ],
    }


def _owned_chart(user: str, chart_id: str) -> dict[str, Any]:
    with session_scope() as session:
        chart = session.get(SavedChart, chart_id)
        if chart is None or chart.owner != user:
            raise ApiError(404, "Not found")
        return _chart_dict(chart)


@router.get("/charts")
def list_charts(user: User) -> dict[str, Any]:
    with session_scope() as session:
        rows = session.scalars(
            select(SavedChart)
            .where(SavedChart.owner == user)
            .order_by(SavedChart.created_at.desc())
        ).all()
        return {"result": [_chart_dict(c) for c in rows]}


@router.get("/charts/{chart_id}")
def get_chart(chart_id: str, user: User) -> dict[str, Any]:
    return {"result": _owned_chart(user, chart_id)}


@router.delete("/charts/{chart_id}")
def delete_chart(chart_id: str, user: User) -> dict[str, str]:
    with session_scope() as session:
        chart = session.get(SavedChart, chart_id)
        if chart is None or chart.owner != user:
            raise ApiError(404, "Not found")
        session.delete(chart)
    return {"message": "OK"}


@router.get("/charts/{chart_id}/data")
def chart_data(chart_id: str, user: User) -> dict[str, Any]:
    return _payload(_owned_chart(user, chart_id))


@router.get("/charts/{chart_id}/csv")
def chart_csv(chart_id: str, user: User) -> Response:
    chart = _owned_chart(user, chart_id)
    return _csv(chart, chart["title"])


@router.get("/charts/{chart_id}/sql")
def chart_sql_endpoint(chart_id: str, user: User) -> dict[str, str]:
    try:
        return {"sql": chart_sql(_owned_chart(user, chart_id))}
    except ChatError as exc:
        raise _chat_error(exc) from exc


# ── dashboards ───────────────────────────────────────────────────────────────


class DashboardIn(BaseModel):
    title: str = Field(min_length=1, max_length=500)


def _owned_dashboard(session: Any, user: str, dashboard_id: str) -> Dashboard:
    dashboard = session.get(Dashboard, dashboard_id)
    if dashboard is None or dashboard.owner != user:
        raise ApiError(404, "Not found")
    return dashboard


@router.get("/dashboards")
def list_dashboards(user: User) -> dict[str, Any]:
    with session_scope() as session:
        counts = dict(
            session.execute(
                select(DashboardChart.dashboard_id, func.count()).group_by(
                    DashboardChart.dashboard_id
                )
            ).all()
        )
        rows = session.scalars(
            select(Dashboard)
            .where(Dashboard.owner == user)
            .order_by(Dashboard.updated_at.desc())
        ).all()
        return {
            "result": [
                {
                    "id": d.id,
                    "title": d.title,
                    "chart_count": counts.get(d.id, 0),
                    "updated_at": d.updated_at.isoformat() if d.updated_at else None,
                }
                for d in rows
            ]
        }


@router.post("/dashboards", status_code=201)
def create_dashboard(body: DashboardIn, user: User) -> dict[str, Any]:
    with session_scope() as session:
        dashboard = Dashboard(title=body.title.strip(), owner=user)
        session.add(dashboard)
        session.flush()
        return {"id": dashboard.id, "title": dashboard.title}


@router.get("/dashboards/{dashboard_id}")
def get_dashboard(dashboard_id: str, user: User) -> dict[str, Any]:
    with session_scope() as session:
        dashboard = _owned_dashboard(session, user, dashboard_id)
        return {
            "result": {
                "id": dashboard.id,
                "title": dashboard.title,
                "charts": [_chart_dict(p.chart) for p in dashboard.placements],
            }
        }


@router.patch("/dashboards/{dashboard_id}")
def rename_dashboard(
    dashboard_id: str, body: DashboardIn, user: User
) -> dict[str, Any]:
    with session_scope() as session:
        dashboard = _owned_dashboard(session, user, dashboard_id)
        dashboard.title = body.title.strip()
        return {"id": dashboard.id, "title": dashboard.title}


@router.delete("/dashboards/{dashboard_id}")
def delete_dashboard(dashboard_id: str, user: User) -> dict[str, str]:
    with session_scope() as session:
        session.delete(_owned_dashboard(session, user, dashboard_id))
    return {"message": "OK"}


@router.delete("/dashboards/{dashboard_id}/charts/{chart_id}")
def remove_from_dashboard(
    dashboard_id: str, chart_id: str, user: User
) -> dict[str, str]:
    with session_scope() as session:
        _owned_dashboard(session, user, dashboard_id)
        placement = session.get(DashboardChart, (dashboard_id, chart_id))
        if placement is None:
            raise ApiError(404, "Not found")
        session.delete(placement)
    return {"message": "OK"}
