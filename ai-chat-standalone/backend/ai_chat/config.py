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
Settings, read from ``AI_CHAT_*`` environment variables at call time (so tests
and long-running workers see changes without a restart of the import).

See ``README.md`` for the full list.
"""

from __future__ import annotations

import os


def env_str(name: str, default: str = "") -> str:
    value = os.environ.get(f"AI_CHAT_{name}")
    return value if value not in (None, "") else default


def env_int(name: str, default: int) -> int:
    try:
        return int(env_str(name, str(default)))
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    value = env_str(name, "")
    if not value:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def env_list(name: str) -> list[str]:
    return [v.strip() for v in env_str(name).split(",") if v.strip()]


# ── storage ──────────────────────────────────────────────────────────────────


def metadata_db_url() -> str:
    """Where saved charts, dashboards and database connections live."""
    return env_str("METADATA_DB_URL", "sqlite:///./ai_chat.db")


def redis_url() -> str:
    """Conversations, jobs and caches; empty keeps them in process memory."""
    return env_str("REDIS_URL")


def secret_key() -> str:
    """Encrypts stored connection URIs. Change it in production."""
    return env_str("SECRET_KEY", "change-me-ai-chat-dev-secret")


# ── data sources ─────────────────────────────────────────────────────────────


def databases_json() -> str:
    """JSON list of {name, uri, schemas} connections managed by configuration."""
    return env_str("DATABASES")


def databases_file() -> str:
    """YAML/JSON file with the same list as ``AI_CHAT_DATABASES``."""
    return env_str("DATABASES_FILE")


def allow_ui_connections() -> bool:
    """Whether users may add database connections from the UI."""
    return env_bool("ALLOW_UI_CONNECTIONS", True)


def allow_sqlite_connections() -> bool:
    """SQLite URIs read local files, so they are refused from the UI by default."""
    return env_bool("ALLOW_SQLITE_CONNECTIONS", False)


def semantics_dir() -> str:
    return env_str("SEMANTICS_DIR", "./semantics")


def max_tables() -> int:
    return env_int("MAX_TABLES", 30)


def query_timeout() -> int:
    """Seconds a chart query may run (enforced on PostgreSQL and MySQL)."""
    return env_int("QUERY_TIMEOUT", 60)


def max_rows() -> int:
    return env_int("MAX_ROWS", 50000)


# ── chat ─────────────────────────────────────────────────────────────────────


def catalog_ttl() -> int:
    return env_int("CATALOG_TTL", 1800)


def conversation_ttl() -> int:
    return env_int("CONVERSATION_TTL", 7 * 24 * 3600)


def result_ttl() -> int:
    return env_int("RESULT_TTL", 600)


def rate_limit() -> int:
    """Messages per user per minute (0 disables the limit)."""
    return env_int("RATE_LIMIT", 20)


# ── access ───────────────────────────────────────────────────────────────────


def api_keys() -> list[str]:
    """When set, every API call needs one of these keys (X-API-Key header)."""
    return env_list("API_KEYS")


def trust_user_header() -> bool:
    """Take the user id from X-User-Id (set by a trusted reverse proxy)."""
    return env_bool("TRUST_USER_HEADER", False)


def cors_origins() -> list[str]:
    return env_list("CORS_ORIGINS")


def static_dir() -> str:
    """Built frontend to serve from the API process (single-container mode)."""
    return env_str("STATIC_DIR")
