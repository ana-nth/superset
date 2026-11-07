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
Prepare the demo warehouse (run by the ``demo-seed`` container):

1. load the sample data with ``seed.py`` unless it is already there
   (set ``AI_CHAT_DEMO_RESEED=true`` to reload it)
2. create the read-only ``ai_chat_ro`` role the chat connects with
"""

from __future__ import annotations

import os
import sys
import time

import psycopg
from psycopg import sql

HERE = os.path.dirname(os.path.abspath(__file__))
DSN = os.environ["WAREHOUSE_DSN"]


def wait_for_database(timeout: int = 90) -> None:
    deadline = time.time() + timeout
    while True:
        try:
            with psycopg.connect(DSN, connect_timeout=3):
                return
        except psycopg.OperationalError:
            if time.time() > deadline:
                raise
            time.sleep(2)


def has_data() -> bool:
    with psycopg.connect(DSN) as conn:
        try:
            row = conn.execute("SELECT COUNT(*) FROM wh.fact_sales_order_line").fetchone()
        except psycopg.Error:
            return False
    return bool(row and row[0])


def main() -> None:
    wait_for_database()
    reseed = os.environ.get("AI_CHAT_DEMO_RESEED", "").lower() in ("1", "true", "yes")
    if reseed or not has_data():
        sys.path.insert(0, HERE)
        import seed  # noqa: PLC0415  (reads WAREHOUSE_DSN on import)

        seed.main()
    else:
        print("Demo data already loaded (set AI_CHAT_DEMO_RESEED=true to reload).")

    password = os.environ.get("AI_CHAT_DEMO_RO_PASSWORD", "ai_chat_ro_secret")
    with psycopg.connect(DSN, autocommit=True) as conn:
        with open(os.path.join(HERE, "ro_role.sql"), encoding="utf-8") as handle:
            conn.execute(handle.read())
        conn.execute(
            sql.SQL("ALTER ROLE ai_chat_ro WITH PASSWORD {}").format(
                sql.Literal(password)
            )
        )
    print("Read-only role ai_chat_ro is ready.")


if __name__ == "__main__":
    main()
