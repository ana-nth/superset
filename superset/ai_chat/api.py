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
REST API for the AI Chart Chat.

POST   /api/v1/ai_chat/message                 send a message → 202 {job_id, …}
GET    /api/v1/ai_chat/job/<job_id>            poll a message job
GET    /api/v1/ai_chat/catalog                 data summary + starter questions
GET    /api/v1/ai_chat/conversations           the user's recent conversations
GET    /api/v1/ai_chat/conversations/<id>      one conversation with its messages
DELETE /api/v1/ai_chat/conversations/<id>      delete a conversation
POST   /api/v1/ai_chat/save_chart              save a chart from the chat
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from flask import current_app, request, Response
from flask_appbuilder.api import expose, safe
from flask_appbuilder.security.decorators import protect

from superset.ai_chat import store
from superset.ai_chat.service import MAX_MESSAGE_CHARS
from superset.utils.core import get_user_id
from superset.views.base_api import BaseSupersetApi

logger = logging.getLogger(__name__)


def _workers_available() -> bool:
    from superset.extensions import celery_app

    try:
        return bool(celery_app.control.ping(timeout=1.0))
    except Exception:  # pylint: disable=broad-except
        return False


def _start(kwargs: dict[str, Any]) -> None:
    """Queue the turn on Celery, or run it in a web-server thread without workers."""
    from superset.ai_chat.tasks import chat_turn_task, run_turn

    if _workers_available():
        try:
            chat_turn_task.apply_async(kwargs=kwargs)
            return
        except Exception:  # pylint: disable=broad-except
            logger.warning("Celery enqueue failed; answering in-process", exc_info=True)
    app = current_app._get_current_object()  # pylint: disable=protected-access

    def target() -> None:
        with app.app_context():
            run_turn(**kwargs)

    threading.Thread(
        target=target, name=f"ai-chat-{kwargs['job_id']}", daemon=True
    ).start()


def _int_arg(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class AIChatRestApi(BaseSupersetApi):
    allow_browser_login = True
    class_permission_name = "Chart"
    method_permission_name = {
        "message": "write",
        "job": "read",
        "catalog": "read",
        "conversations": "read",
        "conversation": "read",
        "delete_conversation": "write",
        "save_chart": "write",
    }
    resource_name = "ai_chat"
    openapi_spec_tag = "AI Chat"

    @staticmethod
    def _user_id() -> int:
        return int(get_user_id() or 0)

    @expose("/message", methods=("POST",))
    @protect()
    @safe
    def message(self) -> Response:  # noqa: C901
        """
        Send a chat message; the answer is produced in the background.
        ---
        post:
          summary: Send a chat message
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  required: [message]
                  properties:
                    message: {type: string}
                    conversation_id: {type: string}
                    database_id: {type: integer}
                    schema: {type: string}
                    reference_message_id: {type: string}
          responses:
            202:
              description: Job started
            400:
              description: Invalid request
            404:
              description: Conversation not found
            409:
              description: Another message is still being answered
            429:
              description: Too many messages
            503:
              description: Chat state storage is unavailable
        """
        from superset.daos.database import DatabaseDAO

        body = request.get_json(silent=True) or {}
        text = str(body.get("message") or "").strip()
        if not text:
            return self.response_400(message="Please type a message.")
        if len(text) > MAX_MESSAGE_CHARS:
            return self.response_400(
                message=f"Messages are limited to {MAX_MESSAGE_CHARS} characters."
            )
        user_id = self._user_id()
        try:
            store.check_cache()
        except store.StoreUnavailableError as exc:
            logger.error("AI chat needs a working cache: %s", exc)
            return self.response(
                503, message="Chat is unavailable: Superset's cache is not configured."
            )

        wait = store.check_rate_limit(user_id)
        if wait is not None:
            return self.response(
                429,
                message=f"You're sending messages too quickly. Try again in {wait}s.",
                retry_after=wait,
            )

        conversation_id = body.get("conversation_id")
        if conversation_id:
            conv = store.load_conversation(user_id, str(conversation_id))
            if conv is None:
                return self.response_404()
        else:
            database_id = _int_arg(body.get("database_id"))
            schema = str(body.get("schema") or "").strip()
            if database_id is None or not schema:
                return self.response_400(message="Choose a database and schema first.")
            if DatabaseDAO.find_by_id(database_id) is None:
                return self.response_400(message=f"Database {database_id} not found.")
            conv = store.new_conversation(user_id, database_id, schema)

        job_id = store.new_id()
        holder = store.acquire_lock(user_id, conv["id"], job_id)
        if holder is not None:
            return self.response(
                409,
                message="I'm still answering your previous message.",
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
                user_id,
                job_id,
                state="PENDING",
                stage="Queued",
                conversation_id=conv["id"],
            )
            reference = body.get("reference_message_id")
            _start(
                {
                    "job_id": job_id,
                    "user_id": user_id,
                    "conversation_id": conv["id"],
                    "user_message_id": user_msg["id"],
                    "reference_message_id": str(reference) if reference else None,
                }
            )
        except Exception:  # pylint: disable=broad-except
            store.release_lock(user_id, conv["id"], job_id)
            raise
        return self.response(
            202, job_id=job_id, conversation_id=conv["id"], user_message=user_msg
        )

    @expose("/job/<string:job_id>", methods=("GET",))
    @protect()
    @safe
    def job(self, job_id: str) -> Response:
        """
        Poll a message job.
        ---
        get:
          summary: Get the state of a chat message job
          parameters:
            - name: job_id
              in: path
              required: true
              schema: {type: string}
          responses:
            200:
              description: Job state (PENDING, RUNNING, SUCCESS, FAILURE, NOT_FOUND)
        """
        job = store.get_job(self._user_id(), job_id)
        if job is None:
            return self.response(200, state="NOT_FOUND", job_id=job_id)
        return self.response(200, **job)

    @expose("/catalog", methods=("GET",))
    @protect()
    @safe
    def catalog(self) -> Response:
        """
        Summarise what can be charted in a schema.
        ---
        get:
          summary: Data catalog and starter questions
          parameters:
            - name: database_id
              in: query
              required: true
              schema: {type: integer}
            - name: schema
              in: query
              required: true
              schema: {type: string}
            - name: refresh
              in: query
              schema: {type: boolean}
          responses:
            200:
              description: Sources and suggestions
            400:
              description: Invalid request or schema cannot be charted
        """
        from superset.ai_chat.catalog import CatalogError, get_catalog

        database_id = _int_arg(request.args.get("database_id"))
        schema = (request.args.get("schema") or "").strip()
        if database_id is None or not schema:
            return self.response_400(message="database_id and schema are required")
        refresh = (request.args.get("refresh") or "").lower() in ("1", "true")
        try:
            catalog = get_catalog(database_id, schema, force=refresh)
        except CatalogError as exc:
            return self.response_400(message=str(exc))
        return self.response(
            200,
            database_name=catalog.database_name,
            schema=catalog.schema,
            sources=catalog.summary(),
            suggestions=catalog.suggestions(),
        )

    @expose("/conversations", methods=("GET",))
    @protect()
    @safe
    def conversations(self) -> Response:
        """
        List the user's recent conversations.
        ---
        get:
          summary: Recent conversations
          responses:
            200:
              description: Conversations, most recent first
        """
        return self.response(200, result=store.list_conversations(self._user_id()))

    @expose("/conversations/<string:conversation_id>", methods=("GET",))
    @protect()
    @safe
    def conversation(self, conversation_id: str) -> Response:
        """
        Get a conversation with its messages.
        ---
        get:
          summary: One conversation
          parameters:
            - name: conversation_id
              in: path
              required: true
              schema: {type: string}
          responses:
            200:
              description: The conversation
            404:
              description: Not found
        """
        from superset.ai_chat.service import public_conversation

        conv = store.load_conversation(self._user_id(), conversation_id)
        if conv is None:
            return self.response_404()
        return self.response(200, result=public_conversation(conv))

    @expose("/conversations/<string:conversation_id>", methods=("DELETE",))
    @protect()
    @safe
    def delete_conversation(self, conversation_id: str) -> Response:
        """
        Delete a conversation.
        ---
        delete:
          summary: Delete a conversation
          parameters:
            - name: conversation_id
              in: path
              required: true
              schema: {type: string}
          responses:
            200:
              description: Deleted
            404:
              description: Not found
        """
        if not store.delete_conversation(self._user_id(), conversation_id):
            return self.response_404()
        return self.response(200, message="OK")

    @expose("/save_chart", methods=("POST",))
    @protect()
    @safe
    def save_chart(self) -> Response:
        """
        Save a chart from a chat message, optionally adding it to a dashboard.
        ---
        post:
          summary: Save a chat chart
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  required: [conversation_id, message_id]
                  properties:
                    conversation_id: {type: string}
                    message_id: {type: string}
                    slice_name: {type: string}
                    dashboard_id: {type: integer}
                    new_dashboard_title: {type: string}
                    query_context: {type: string}
          responses:
            200:
              description: Saved chart (and dashboard) ids and URLs
            400:
              description: Invalid request
            403:
              description: Not allowed to edit the dashboard
            404:
              description: Conversation or message not found
        """
        from superset.ai_chat.service import ChatError, save_chart

        body = request.get_json(silent=True) or {}
        user_id = self._user_id()
        conv = store.load_conversation(user_id, str(body.get("conversation_id") or ""))
        if conv is None:
            return self.response_404()
        message = store.find_message(conv, str(body.get("message_id") or ""))
        if message is None or not message.get("chart"):
            return self.response_404()
        title = str(body.get("slice_name") or "").strip() or None
        new_dashboard = str(body.get("new_dashboard_title") or "").strip() or None
        query_context = body.get("query_context")
        try:
            result = save_chart(
                conv,
                message,
                title=title,
                dashboard_id=_int_arg(body.get("dashboard_id")),
                new_dashboard_title=new_dashboard,
                client_query_context=query_context
                if isinstance(query_context, str)
                else None,
            )
        except ChatError as exc:
            status = {"forbidden": 403, "not_found": 404}.get(exc.code, 400)
            return self.response(status, message=str(exc))
        # Reload before writing: a chat turn may have appended messages meanwhile
        latest = store.load_conversation(user_id, conv["id"]) or conv
        target = store.find_message(latest, message["id"])
        if target is not None:
            target["chart"] = message["chart"]
            store.save_conversation(latest)
        return self.response(200, **result)
