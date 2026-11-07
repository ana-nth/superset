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
Background execution of chat turns: a Celery worker when one answers (Redis is
the broker), otherwise a thread in the API process.

Run a worker with::

    celery -A ai_chat.tasks:celery_app worker --loglevel=INFO
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from celery import Celery

from ai_chat import config, store

logger = logging.getLogger(__name__)

celery_app = Celery("ai_chat", broker=config.redis_url() or "memory://")
celery_app.conf.update(
    task_ignore_result=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    worker_hijack_root_logger=False,
)

PING_CACHE_SECONDS = 15
_ping: dict[str, float] = {"at": 0.0}


def run_turn(
    job_id: str,
    user_id: str,
    conversation_id: str,
    user_message_id: str,
    reference_message_id: Optional[str] = None,
) -> None:
    """Answer message ``user_message_id``; always ends the job."""
    from ai_chat.service import ChatError, handle_message, public_message

    conv: Optional[dict[str, Any]] = None
    try:
        store.set_job(user_id, job_id, state="RUNNING", stage="Starting")
        conv = store.load_conversation(user_id, conversation_id)
        if conv is None:
            # Nothing to answer into; the job ends and the lock is released below
            store.set_job(
                user_id,
                job_id,
                state="FAILURE",
                error="The conversation no longer exists.",
            )
            return
        user_msg = store.find_message(conv, user_message_id)
        text = (user_msg or {}).get("text", "")

        def progress(stage: str) -> None:
            store.set_job(user_id, job_id, state="RUNNING", stage=stage)

        try:
            reply = handle_message(conv, text, reference_message_id, progress)
        except ChatError as exc:
            reply = {
                "id": store.new_id(),
                "role": "assistant",
                "type": "error",
                "text": str(exc),
                "error_code": exc.code,
                "created_at": store.now_iso(),
            }
        reply["reply_to"] = user_message_id
        # Reload: the user may have deleted the chat or saved a chart meanwhile
        latest = store.load_conversation(user_id, conversation_id)
        if latest is None:
            store.set_job(
                user_id, job_id, state="FAILURE", error="The conversation was deleted."
            )
            return
        # Keep charts this turn saved ("save it to Sales") marked as saved
        for msg in conv["messages"]:
            if (msg.get("chart") or {}).get("saved_chart_id"):
                target = store.find_message(latest, msg["id"])
                if target is not None and target.get("chart"):
                    target["chart"] = msg["chart"]
        latest["messages"].append(reply)
        latest["pending_job_id"] = None
        store.save_conversation(latest)
        store.set_job(
            user_id,
            job_id,
            state="SUCCESS",
            stage="Done",
            message=public_message(reply),
        )
    except Exception:  # pylint: disable=broad-except
        logger.exception("AI chat job %s failed", job_id)
        reply = {
            "id": store.new_id(),
            "role": "assistant",
            "type": "error",
            "text": "Something went wrong while answering. Please try again "
            f"(reference {job_id[:8]}).",
            "error_code": "internal",
            "reply_to": user_message_id,
            "created_at": store.now_iso(),
        }
        try:
            latest = store.load_conversation(user_id, conversation_id)
            if latest is not None:
                latest["messages"].append(reply)
                latest["pending_job_id"] = None
                store.save_conversation(latest)
        except Exception:  # pylint: disable=broad-except
            logger.warning("Could not record the AI chat failure", exc_info=True)
        store.set_job(user_id, job_id, state="SUCCESS", stage="Done", message=reply)
    finally:
        store.release_lock(user_id, conversation_id, job_id)


@celery_app.task(name="ai_chat.chat_turn", soft_time_limit=300)
def chat_turn_task(
    job_id: str,
    user_id: str,
    conversation_id: str,
    user_message_id: str,
    reference_message_id: Optional[str] = None,
) -> None:
    run_turn(job_id, user_id, conversation_id, user_message_id, reference_message_id)


def workers_available() -> bool:
    """True when a Celery worker answers (remembered briefly to keep sends fast)."""
    if not config.redis_url():
        return False  # in-memory state is not shared with other processes
    if time.monotonic() - _ping["at"] < PING_CACHE_SECONDS:
        return True
    try:
        alive = bool(celery_app.control.ping(timeout=1.0))
    except Exception:  # pylint: disable=broad-except
        alive = False
    if alive:
        _ping["at"] = time.monotonic()
    return alive


def start_turn(kwargs: dict[str, Any]) -> None:
    """Queue the turn on Celery, or answer it in a thread without workers."""
    if workers_available():
        try:
            chat_turn_task.apply_async(kwargs=kwargs)
            return
        except Exception:  # pylint: disable=broad-except
            logger.warning("Celery enqueue failed; answering in-process", exc_info=True)
    threading.Thread(
        target=run_turn, kwargs=kwargs, name=f"ai-chat-{kwargs['job_id']}", daemon=True
    ).start()
