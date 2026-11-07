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
A small key-value cache with expiry: Redis when ``AI_CHAT_REDIS_URL`` is set,
otherwise process memory (fine for a single process; use Redis whenever a
worker or several API processes run).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional, Protocol

from ai_chat import config


class KV(Protocol):
    def get(self, key: str) -> Any: ...

    def set(self, key: str, value: Any, ttl: int) -> None: ...

    def add(self, key: str, value: Any, ttl: int) -> bool:
        """Set only when absent; True when the value was stored."""
        ...

    def delete(self, key: str) -> None: ...


class MemoryKV:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def _alive(self, key: str) -> Optional[str]:
        item = self._data.get(key)
        if item is None:
            return None
        expires, raw = item
        if expires and expires < time.time():
            self._data.pop(key, None)
            return None
        return raw

    def get(self, key: str) -> Any:
        with self._lock:
            raw = self._alive(key)
        return None if raw is None else json.loads(raw)

    def set(self, key: str, value: Any, ttl: int) -> None:
        with self._lock:
            self._data[key] = (time.time() + ttl if ttl else 0, json.dumps(value))

    def add(self, key: str, value: Any, ttl: int) -> bool:
        with self._lock:
            if self._alive(key) is not None:
                return False
            self._data[key] = (time.time() + ttl if ttl else 0, json.dumps(value))
            return True

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


class RedisKV:
    def __init__(self, url: str) -> None:
        import redis

        self._client = redis.Redis.from_url(url, socket_timeout=5)

    def get(self, key: str) -> Any:
        raw = self._client.get(key)
        return None if raw is None else json.loads(raw)

    def set(self, key: str, value: Any, ttl: int) -> None:
        self._client.set(key, json.dumps(value), ex=ttl or None)

    def add(self, key: str, value: Any, ttl: int) -> bool:
        return bool(self._client.set(key, json.dumps(value), ex=ttl or None, nx=True))

    def delete(self, key: str) -> None:
        self._client.delete(key)


_INSTANCE: Optional[KV] = None
_INSTANCE_LOCK = threading.Lock()


def kv() -> KV:
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                url = config.redis_url()
                _INSTANCE = RedisKV(url) if url else MemoryKV()
    return _INSTANCE


def reset_kv(instance: Optional[KV] = None) -> None:
    """Replace the cache (tests)."""
    global _INSTANCE
    _INSTANCE = instance
