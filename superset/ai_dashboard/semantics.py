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
"""Locate and load the warehouse semantics.yaml data dictionary."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))

# First match wins: explicit env var, repo-level warehouse/, Docker mount, packaged copy
CANDIDATE_PATHS = [
    os.environ.get("AI_DASH_SEMANTICS_PATH", ""),
    os.path.join(_HERE, "..", "..", "warehouse", "semantics.yaml"),
    "/app/warehouse/semantics.yaml",
    os.path.join(_HERE, "semantics.yaml"),
]

_CACHE: dict[str, Any] = {}


def semantics_path() -> Optional[str]:
    """Return the first semantics.yaml that exists, if any."""
    for path in CANDIDATE_PATHS:
        if path and os.path.exists(path):
            return os.path.abspath(path)
    return None


def load_semantics() -> dict[str, Any]:
    """Return the parsed semantics.yaml, or {} when none is available."""
    path = semantics_path()
    if path is None:
        return {}
    if path not in _CACHE:
        try:
            with open(path, encoding="utf-8") as handle:
                _CACHE[path] = yaml.safe_load(handle) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("Cannot load semantics from %s: %s", path, exc)
            _CACHE[path] = {}
    return _CACHE[path]
