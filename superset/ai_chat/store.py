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
Conversation, job, lock and rate-limit state for the chat, kept in the Superset
cache (``CACHE_CONFIG``). Keys embed the user id, so a conversation or job id
from another user resolves to nothing.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

MAX_MESSAGES = 100
MAX_CONVERSATIONS = 30
JOB_TTL = 3600
LOCK_TTL = 300


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def conversation_ttl() -> int:
    return _int_env("AI_CHAT_CONVERSATION_TTL", 7 * 24 * 3600)


def rate_limit() -> int:
    """Messages per user per minute (0 disables the limit)."""
    return _int_env("AI_CHAT_RATE_LIMIT", 20)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def _cache() -> Any:
    from superset.extensions import cache_manager

    return cache_manager.cache


class StoreUnavailableError(Exception):
    """The cache backend does not keep values (e.g. NullCache)."""


def check_cache() -> None:
    """Fail fast when the configured cache cannot hold chat state."""
    key = f"ai_chat:probe:{new_id()}"
    try:
        _cache().set(key, 1, timeout=30)
        ok = _cache().get(key) == 1
        _cache().delete(key)
    except Exception as exc:  # pylint: disable=broad-except
        raise StoreUnavailableError(str(exc)) from exc
    if not ok:
        raise StoreUnavailableError("the configured CACHE_CONFIG does not store values")


# ── conversations ────────────────────────────────────────────────────────────


def _conv_key(user_id: int, conversation_id: str) -> str:
    return f"ai_chat:conv:{user_id}:{conversation_id}"


def _index_key(user_id: int) -> str:
    return f"ai_chat:convs:{user_id}"


def new_conversation(user_id: int, database_id: int, schema: str) -> dict[str, Any]:
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


def load_conversation(user_id: int, conversation_id: str) -> Optional[dict[str, Any]]:
    conv = _cache().get(_conv_key(user_id, conversation_id))
    if not conv or conv.get("user_id") != user_id:
        return None
    return conv


def save_conversation(conv: dict[str, Any]) -> None:
    conv["messages"] = conv["messages"][-MAX_MESSAGES:]
    conv["updated_at"] = now_iso()
    user_id = conv["user_id"]
    _cache().set(_conv_key(user_id, conv["id"]), conv, timeout=conversation_ttl())
    index = [
        c for c in (_cache().get(_index_key(user_id)) or []) if c["id"] != conv["id"]
    ]
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
    _cache().set(
        _index_key(user_id), index[:MAX_CONVERSATIONS], timeout=conversation_ttl()
    )


def list_conversations(user_id: int) -> list[dict[str, Any]]:
    index = _cache().get(_index_key(user_id)) or []
    # Drop entries whose conversation expired
    alive = [c for c in index if _cache().get(_conv_key(user_id, c["id"])) is not None]
    if len(alive) != len(index):
        _cache().set(_index_key(user_id), alive, timeout=conversation_ttl())
    return alive


def delete_conversation(user_id: int, conversation_id: str) -> bool:
    existed = load_conversation(user_id, conversation_id) is not None
    _cache().delete(_conv_key(user_id, conversation_id))
    index = [
        c
        for c in (_cache().get(_index_key(user_id)) or [])
        if c["id"] != conversation_id
    ]
    _cache().set(_index_key(user_id), index, timeout=conversation_ttl())
    return existed


def find_message(conv: dict[str, Any], message_id: str) -> Optional[dict[str, Any]]:
    return next((m for m in conv["messages"] if m.get("id") == message_id), None)


# ── jobs ─────────────────────────────────────────────────────────────────────


def _job_key(user_id: int, job_id: str) -> str:
    return f"ai_chat:job:{user_id}:{job_id}"


def set_job(user_id: int, job_id: str, **state: Any) -> None:
    current = _cache().get(_job_key(user_id, job_id)) or {}
    current.update(state, job_id=job_id, updated_at=time.time())
    _cache().set(_job_key(user_id, job_id), current, timeout=JOB_TTL)


def get_job(user_id: int, job_id: str) -> Optional[dict[str, Any]]:
    return _cache().get(_job_key(user_id, job_id))


# ── concurrency & rate limiting ──────────────────────────────────────────────


def _lock_key(user_id: int, conversation_id: str) -> str:
    return f"ai_chat:lock:{user_id}:{conversation_id}"


def acquire_lock(user_id: int, conversation_id: str, job_id: str) -> Optional[str]:
    """Take the conversation lock; returns the job holding it when already taken."""
    key = _lock_key(user_id, conversation_id)
    if _cache().add(key, job_id, timeout=LOCK_TTL):
        return None
    holder = _cache().get(key)
    if holder:
        job = get_job(user_id, holder)
        if job and job.get("state") in ("PENDING", "RUNNING"):
            return holder
    # Stale lock (job finished or expired): take it over
    _cache().set(key, job_id, timeout=LOCK_TTL)
    return None


def release_lock(user_id: int, conversation_id: str, job_id: str) -> None:
    key = _lock_key(user_id, conversation_id)
    try:
        if _cache().get(key) == job_id:
            _cache().delete(key)
    except Exception:  # pylint: disable=broad-except
        logger.debug("Could not release AI chat lock", exc_info=True)


def check_rate_limit(user_id: int) -> Optional[int]:
    """Count one message; returns seconds to wait when the user is over the limit."""
    limit = rate_limit()
    if limit <= 0:
        return None
    window = int(time.time() // 60)
    key = f"ai_chat:rate:{user_id}:{window}"
    count = (_cache().get(key) or 0) + 1
    _cache().set(key, count, timeout=120)
    if count > limit:
        return 60 - int(time.time() % 60)
    return None
