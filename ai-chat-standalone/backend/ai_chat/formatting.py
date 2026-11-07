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
"""Human-readable names and numbers."""

from __future__ import annotations

import math
from typing import Optional


def humanize(name: str) -> str:
    return name.replace("_", " ").strip().title()


def fmt(value: Optional[float]) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    abs_v = abs(value)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs_v >= div:
            return f"{value / div:.1f}{suffix}"
    if float(value).is_integer():
        return f"{int(value)}"
    return f"{value:.2f}"
