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
"""Background execution of chat turns (Celery, or a web-server thread fallback)."""

from __future__ import annotations

import logging
from typing import Any, Optional

from superset.ai_chat import store
from superset.extensions import celery_app

logger = logging.getLogger(__name__)


def run_turn(
    job_id: str,
    user_id: int,
    conversation_id: str,
    user_message_id: str,
    reference_message_id: Optional[str] = None,
) -> None:
    """Answer message ``user_message_id`` as ``user_id``; always ends the job."""
    from superset.ai_chat.service import ChatError, handle_message, public_message
    from superset.extensions import db, security_manager
    from superset.utils.core import override_user

    conv: Optional[dict[str, Any]] = None
    try:
        store.set_job(user_id, job_id, state="RUNNING", stage="Starting")
        user = security_manager.get_user_by_id(user_id)
        conv = store.load_conversation(user_id, conversation_id)
        if user is None or conv is None:
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

        with override_user(user):
            try:
                reply = handle_message(conv, text, reference_message_id, progress)
            except ChatError as exc:
                db.session.rollback()
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
        try:
            db.session.rollback()
        except Exception:  # pylint: disable=broad-except
            logger.debug("Rollback after AI chat failure failed", exc_info=True)
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


@celery_app.task(name="superset.ai_chat.tasks.chat_turn", soft_time_limit=300)
def chat_turn_task(
    job_id: str,
    user_id: int,
    conversation_id: str,
    user_message_id: str,
    reference_message_id: Optional[str] = None,
) -> None:
    run_turn(job_id, user_id, conversation_id, user_message_id, reference_message_id)
