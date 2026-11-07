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
Celery task for the AI Auto Dashboard agent.
Stores progress in Superset's cache so the API can poll it.
"""

from __future__ import annotations

import logging
from typing import Any

from superset.extensions import cache_manager, celery_app
from superset.utils import json

logger = logging.getLogger(__name__)

TASK_CACHE_KEY_PREFIX = "ai_dashboard_task:"
TASK_TTL = 3600  # 1 hour
LOCK_PREFIX = "ai_dashboard_lock:"

# Progress message keyword → percentage shown in the UI
PROGRESS_STEPS = [
    ("profiling", 5),
    ("dataset", 10),
    ("c1", 20),
    ("c2", 30),
    ("c3", 40),
    ("c4", 50),
    ("c5", 60),
    ("chart", 75),
    ("summary", 85),
    ("layout", 95),
]


def lock_key(user_id: int) -> str:
    return f"{LOCK_PREFIX}{user_id}"


def _cache_key(task_id: str) -> str:
    return f"{TASK_CACHE_KEY_PREFIX}{task_id}"


def set_task_state(task_id: str, state: str, **kwargs: Any) -> None:
    payload = {"state": state, **kwargs}
    cache_manager.cache.set(
        _cache_key(task_id), json.dumps(payload, default=str), timeout=TASK_TTL
    )


def get_task_state(task_id: str) -> dict[str, Any]:
    raw = cache_manager.cache.get(_cache_key(task_id))
    if raw is None:
        return {"state": "NOT_FOUND"}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {"state": "FAILURE", "error": "corrupt task state in cache"}


def _progress_pct(message: str, current: int) -> int:
    lower = message.lower()
    for keyword, pct in PROGRESS_STEPS:
        if keyword in lower and pct > current:
            return pct
    return current


def run_generation(task_id: str, database_id: int, schema: str, user_id: int) -> None:
    """Run the agent as ``user_id`` and record progress/results in the cache."""
    from superset.ai_dashboard.agent import run_agent
    from superset.extensions import db, security_manager
    from superset.utils.core import override_user

    pct = [2]
    try:
        set_task_state(
            task_id, "RUNNING", progress_message="Agent starting…", progress=pct[0]
        )
        user = security_manager.get_user_by_id(user_id) if user_id else None
        if user is None:
            set_task_state(task_id, "FAILURE", error=f"User {user_id} not found")
            return

        def _on_progress(msg: str) -> None:
            pct[0] = _progress_pct(msg, pct[0])
            set_task_state(task_id, "RUNNING", progress_message=msg, progress=pct[0])

        with override_user(user):
            result = run_agent(
                database_id=database_id,
                schema=schema,
                progress_callback=_on_progress,
            )

        if not isinstance(result.get("dashboard_id"), int):
            set_task_state(
                task_id,
                "FAILURE",
                error=str(result.get("error") or "No dashboard was produced")[:500],
            )
            return

        insights = [i for i in result.get("insights", []) if isinstance(i, dict)]
        insights_summary = "; ".join(
            f"[{str(i.get('severity', 'info')).upper()}] "
            f"{i.get('title', '')}: {i.get('text', '')}"
            for i in insights[:5]
        )
        set_task_state(
            task_id,
            "SUCCESS",
            progress=100,
            dashboard_id=result["dashboard_id"],
            insights_summary=insights_summary,
            insights=insights,
            summary=result.get("summary", ""),
            mode=result.get("mode", ""),
            llm_used=result.get("llm_used", False),
            charts_created=result.get("charts_created"),
            fallback_reason=result.get("fallback_reason", ""),
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("AI dashboard task %s failed", task_id)
        db.session.rollback()
        set_task_state(task_id, "FAILURE", error=str(exc)[:500])
    finally:
        try:
            cache_manager.cache.delete(lock_key(user_id))
        except Exception:  # pylint: disable=broad-except
            logger.debug("Could not release AI dashboard lock", exc_info=True)


@celery_app.task(bind=True, name="superset.ai_dashboard.tasks.generate_dashboard")
def generate_dashboard_task(
    self: Any,  # pylint: disable=unused-argument
    task_id: str,
    database_id: int,
    schema: str,
    user_id: int,
) -> None:
    """Celery entry point; see ``run_generation``."""
    run_generation(task_id, database_id, schema, user_id)
