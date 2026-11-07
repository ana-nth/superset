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
REST API for the AI Auto Dashboard feature.
POST /api/v1/ai_dashboard/generate  → enqueue Celery task, return task_id
GET  /api/v1/ai_dashboard/status/<task_id> → poll task state
"""

from __future__ import annotations

import logging
import threading
import uuid

from flask import current_app, request, Response
from flask_appbuilder.api import expose, safe
from flask_appbuilder.security.decorators import protect

from superset.ai_dashboard.tasks import (
    generate_dashboard_task,
    get_task_state,
    lock_key,
    run_generation,
    set_task_state,
)
from superset.utils.core import get_user_id
from superset.views.base_api import BaseSupersetApi

logger = logging.getLogger(__name__)

LOCK_TTL = 900


def _workers_available() -> bool:
    """True when at least one Celery worker answers a ping."""
    from superset.extensions import celery_app

    try:
        return bool(celery_app.control.ping(timeout=1.0))
    except Exception:  # pylint: disable=broad-except
        return False


def _run_in_thread(task_id: str, database_id: int, schema: str, user_id: int) -> None:
    """Fallback when no Celery worker is running: run in a web-server thread."""
    app = current_app._get_current_object()  # type: ignore[attr-defined]  # pylint: disable=protected-access

    def target() -> None:
        with app.app_context():
            run_generation(task_id, database_id, schema, user_id)

    threading.Thread(target=target, name=f"ai-dashboard-{task_id}", daemon=True).start()


class AIDashboardRestApi(BaseSupersetApi):
    allow_browser_login = True
    class_permission_name = "Dashboard"
    method_permission_name = {
        "generate": "write",
        "status": "read",
    }
    resource_name = "ai_dashboard"
    openapi_spec_tag = "AI Dashboard"

    @expose("/generate", methods=("POST",))
    @protect()
    @safe
    def generate(self) -> Response:
        """
        Enqueue an AI dashboard generation task.
        ---
        post:
          summary: Generate an AI dashboard
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  required: [database_id, schema]
                  properties:
                    database_id: {type: integer}
                    schema: {type: string}
          responses:
            200:
              description: Task enqueued
              content:
                application/json:
                  schema:
                    type: object
                    properties:
                      task_id: {type: string}
            400:
              description: Validation error
            409:
              description: Job already running for this user
        """
        from superset.daos.database import DatabaseDAO
        from superset.extensions import cache_manager

        body = request.get_json(silent=True) or {}
        try:
            database_id = int(body["database_id"])
        except (KeyError, TypeError, ValueError):
            return self.response_400(message="database_id is required")
        schema = str(body.get("schema") or "").strip()
        if not schema:
            return self.response_400(message="schema is required")
        if DatabaseDAO.find_by_id(database_id) is None:
            return self.response_400(message=f"Database {database_id} not found")

        user_id = get_user_id() or 0
        key = lock_key(user_id)
        existing_task = cache_manager.cache.get(key)
        if existing_task:
            existing_state = get_task_state(existing_task).get("state")
            if existing_state in ("PENDING", "RUNNING"):
                return self.response(
                    409,
                    message="An AI dashboard job is already running for your account",
                    task_id=existing_task,
                )

        task_id = str(uuid.uuid4())
        cache_manager.cache.set(key, task_id, timeout=LOCK_TTL)
        set_task_state(task_id, "PENDING", progress_message="Queued…", progress=1)

        kwargs = {
            "task_id": task_id,
            "database_id": database_id,
            "schema": schema,
            "user_id": user_id,
        }
        queued = False
        if _workers_available():
            try:
                generate_dashboard_task.apply_async(kwargs=kwargs)
                queued = True
            except Exception:  # pylint: disable=broad-except
                logger.warning(
                    "Celery enqueue failed; running in-process", exc_info=True
                )
        if not queued:
            _run_in_thread(task_id, database_id, schema, user_id)

        return self.response(200, task_id=task_id)

    @expose("/status/<string:task_id>", methods=("GET",))
    @protect()
    @safe
    def status(self, task_id: str) -> Response:
        """
        Poll the status of a generation task.
        ---
        get:
          summary: Get task status
          parameters:
            - name: task_id
              in: path
              required: true
              schema: {type: string}
          responses:
            200:
              description: Task state
        """
        state = get_task_state(task_id)
        return self.response(200, **state)
