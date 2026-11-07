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
Evaluate the time-range expressions ``intent.parse_time_range`` produces into
concrete ``[start, end)`` dates.

Ranges are stored as expressions, not dates, so a saved "last 30 days" chart
keeps moving with time. The grammar is the subset of Superset's time-range
syntax the planner emits::

    range := "No filter" | term? " : " term?
    term  := DATETIME("today") | DATEADD(term, <int>, <unit>)
           | DATETRUNC(term, <unit>) | YYYY-MM-DD
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional

UNITS = ("day", "week", "month", "quarter", "year")


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    # Clamp the day to the target month's length
    next_month = date(year + (month == 12), month % 12 + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    return date(year, month, min(day.day, last_day))


def _add(day: date, amount: int, unit: str) -> date:
    if unit == "day":
        return day + timedelta(days=amount)
    if unit == "week":
        return day + timedelta(weeks=amount)
    if unit == "month":
        return _add_months(day, amount)
    if unit == "quarter":
        return _add_months(day, 3 * amount)
    return _add_months(day, 12 * amount)


def _trunc(day: date, unit: str) -> date:
    if unit == "day":
        return day
    if unit == "week":
        return day - timedelta(days=day.weekday())
    if unit == "month":
        return day.replace(day=1)
    if unit == "quarter":
        return date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)
    return date(day.year, 1, 1)


def _split_args(text: str) -> list[str]:
    """Split on top-level commas."""
    parts: list[str] = []
    depth = 0
    current = ""
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    parts.append(current.strip())
    return parts


def _unit(text: str) -> str:
    unit = text.strip().strip("'\"").lower().rstrip("s")
    if unit not in UNITS:
        raise ValueError(f"unknown unit {text!r}")
    return unit


def _term(text: str, today: date) -> date:
    text = text.strip()
    if re.fullmatch(r'DATETIME\(\s*"today"\s*\)', text, re.IGNORECASE):
        return today
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text)
    match = re.fullmatch(r"(DATEADD|DATETRUNC)\((.*)\)", text, re.IGNORECASE | re.S)
    if not match:
        raise ValueError(f"cannot read {text!r}")
    args = _split_args(match.group(2))
    if match.group(1).upper() == "DATEADD":
        if len(args) != 3:
            raise ValueError("DATEADD takes 3 arguments")
        return _add(_term(args[0], today), int(args[1]), _unit(args[2]))
    if len(args) != 2:
        raise ValueError("DATETRUNC takes 2 arguments")
    return _trunc(_term(args[0], today), _unit(args[1]))


def evaluate(
    time_range: Optional[str], today: Optional[date] = None
) -> tuple[Optional[date], Optional[date]]:
    """Return ``(start, end)`` (end exclusive); ``(None, None)`` means all time."""
    if not time_range or time_range.strip().lower() == "no filter":
        return None, None
    if " : " not in time_range:
        raise ValueError(f"not a range: {time_range!r}")
    today = today or date.today()
    left, right = time_range.split(" : ", 1)
    start = _term(left, today) if left.strip() else None
    end = _term(right, today) if right.strip() else None
    if start and end and start >= end:
        raise ValueError("the start of the period is after its end")
    return start, end
