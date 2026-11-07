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
Optional data dictionaries (``semantics.yaml`` files) that describe a schema:
table and column descriptions, roles, units, joins and business metrics.

Every ``*.yaml`` / ``*.yml`` file in ``AI_CHAT_SEMANTICS_DIR`` is read; a file
applies to the schema named by its top-level ``schema`` key. When several files
describe the same schema, the one whose ``database_name`` equals the connection
name wins. Entries naming columns or tables that do not exist are ignored.
"""

from __future__ import annotations

import glob
import logging
import os
from typing import Any, Optional

import yaml

from ai_chat import config

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _load(path: str) -> dict[str, Any]:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    cached = _CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Cannot load semantics from %s: %s", path, exc)
        data = {}
    if not isinstance(data, dict):
        data = {}
    _CACHE[path] = (mtime, data)
    return data


def load_semantics(schema: str, database_name: Optional[str] = None) -> dict[str, Any]:
    """The semantics describing ``schema``, or {} when none does."""
    folder = config.semantics_dir()
    paths = sorted(
        glob.glob(os.path.join(folder, "*.yaml"))
        + glob.glob(os.path.join(folder, "*.yml"))
    )
    matches = [d for d in (_load(p) for p in paths) if d.get("schema") == schema]
    for data in matches:
        if database_name and data.get("database_name") == database_name:
            return data
    return matches[0] if matches else {}
