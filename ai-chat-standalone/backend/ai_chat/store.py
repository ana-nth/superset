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
Conversation, job, lock and rate-limit state for the chat, kept in the key-value
cache (``kv.py``). Keys embed the user id, so a conversation or job id from
another user resolves to nothing.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from ai_chat import config
from ai_chat.kv import kv

logger = logging.getLogger(__name__)

MAX_MESSAGES = 100
MAX_CONVERSATIONS = 30
JOB_TTL = 3600
LOCK_TTL = 300


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


class StoreUnavailableError(Exception):
    """The cache backend cannot keep values (e.g. Redis is down)."""


def check_cache() -> None:
    """Fail fast when the cache cannot hold chat state."""
    key = f"ai_chat:probe:{new_id()}"
    try:
        kv().set(key, 1, 30)
        ok = kv().get(key) == 1
        kv().delete(key)
    except Exception as exc:  # pylint: disable=broad-except
        raise StoreUnavailableError(str(exc)) from exc
    if not ok:
        raise StoreUnavailableError("the cache does not store values")


# ── conversations ────────────────────────────────────────────────────────────


def _conv_key(user_id: str, conversation_id: str) -> str:
    return f"ai_chat:conv:{user_id}:{conversation_id}"


def _index_key(user_id: str) -> str:
    return f"ai_chat:convs:{user_id}"


def new_conversation(user_id: str, database_id: str, schema: str) -> dict[str, Any]:
    return {
        "id": new_id(),
        "user_id": user_id,
        "database_id": database_id,
        "schema": schema,
        "title": "",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "messages": [],
    }


def load_conversation(user_id: str, conversation_id: str) -> Optional[dict[str, Any]]:
    conv = kv().get(_conv_key(user_id, conversation_id))
    if not conv or conv.get("user_id") != user_id:
        return None
    return conv


def save_conversation(conv: dict[str, Any]) -> None:
    conv["messages"] = conv["messages"][-MAX_MESSAGES:]
    conv["updated_at"] = now_iso()
    user_id = conv["user_id"]
    ttl = config.conversation_ttl()
    kv().set(_conv_key(user_id, conv["id"]), conv, ttl)
    index = [c for c in (kv().get(_index_key(user_id)) or []) if c["id"] != conv["id"]]
    index.insert(
        0,
        {
            "id": conv["id"],
            "title": conv.get("title") or "New chat",
            "database_id": conv["database_id"],
            "schema": conv["schema"],
            "updated_at": conv["updated_at"],
        },
    )
    kv().set(_index_key(user_id), index[:MAX_CONVERSATIONS], ttl)


def list_conversations(user_id: str) -> list[dict[str, Any]]:
    index = kv().get(_index_key(user_id)) or []
    # Drop entries whose conversation expired
    alive = [c for c in index if kv().get(_conv_key(user_id, c["id"])) is not None]
    if len(alive) != len(index):
        kv().set(_index_key(user_id), alive, config.conversation_ttl())
    return alive


def delete_conversation(user_id: str, conversation_id: str) -> bool:
    existed = load_conversation(user_id, conversation_id) is not None
    kv().delete(_conv_key(user_id, conversation_id))
    index = [
        c for c in (kv().get(_index_key(user_id)) or []) if c["id"] != conversation_id
    ]
    kv().set(_index_key(user_id), index, config.conversation_ttl())
    return existed


def find_message(conv: dict[str, Any], message_id: str) -> Optional[dict[str, Any]]:
    return next((m for m in conv["messages"] if m.get("id") == message_id), None)


# ── jobs ─────────────────────────────────────────────────────────────────────


def _job_key(user_id: str, job_id: str) -> str:
    return f"ai_chat:job:{user_id}:{job_id}"


def set_job(user_id: str, job_id: str, **state: Any) -> None:
    current = kv().get(_job_key(user_id, job_id)) or {}
    current.update(state, job_id=job_id, updated_at=time.time())
    kv().set(_job_key(user_id, job_id), current, JOB_TTL)


def get_job(user_id: str, job_id: str) -> Optional[dict[str, Any]]:
    return kv().get(_job_key(user_id, job_id))


# ── concurrency & rate limiting ──────────────────────────────────────────────


def _lock_key(user_id: str, conversation_id: str) -> str:
    return f"ai_chat:lock:{user_id}:{conversation_id}"


def acquire_lock(user_id: str, conversation_id: str, job_id: str) -> Optional[str]:
    """Take the conversation lock; returns the job holding it when already taken."""
    key = _lock_key(user_id, conversation_id)
    if kv().add(key, job_id, LOCK_TTL):
        return None
    holder = kv().get(key)
    if holder:
        job = get_job(user_id, holder)
        if job and job.get("state") in ("PENDING", "RUNNING"):
            return holder
    # Stale lock (job finished or expired): take it over
    kv().set(key, job_id, LOCK_TTL)
    return None


def release_lock(user_id: str, conversation_id: str, job_id: str) -> None:
    key = _lock_key(user_id, conversation_id)
    try:
        if kv().get(key) == job_id:
            kv().delete(key)
    except Exception:  # pylint: disable=broad-except
        logger.debug("Could not release AI chat lock", exc_info=True)


def check_rate_limit(user_id: str) -> Optional[int]:
    """Count one message; returns seconds to wait when the user is over the limit."""
    limit = config.rate_limit()
    if limit <= 0:
        return None
    window = int(time.time() // 60)
    key = f"ai_chat:rate:{user_id}:{window}"
    count = (kv().get(key) or 0) + 1
    kv().set(key, count, 120)
    if count > limit:
        return 60 - int(time.time() % 60)
    return None
