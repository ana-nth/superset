<!--
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
-->

# AI Chart Chat (standalone)

Ask any SQL database questions in plain English and get **answers**: a written
answer computed from the real query result plus a chart for data questions, and a
grounded explanation of tables, relationships, columns and metric definitions for
questions about the data itself. You can refine charts
with follow-ups ("make it a pie", "only Online", "by quarter instead") and save them to
dashboards.

This is the AI Chart Chat from the Superset fork, extracted into a self-contained app.
It has **no dependency on Apache Superset**: it brings its own schema profiler, SQL
generator, chart renderer, storage and dashboards, so it runs as-is in any environment
that has Docker.

## Quick start

```bash
cd ai-chat-standalone
cp .env.example .env                       # optional: adjust LLM, secrets, port
docker compose --profile demo up -d --build
```

Open <http://localhost:8300>. The `demo` profile starts a sample manufacturing
warehouse (≈250k rows, schema `wh`) that is already connected as **Demo warehouse**.

The default LLM is `google/gemma-4-31b-it:free` on OpenRouter. Put your key in
`.env` (`OPENROUTER_API_KEY=...`, copied from `.env.example`). To use Ollama instead, set
`AI_CHAT_PROVIDER=opensource` and the Ollama lines in `.env.example`. If the model is
unreachable, the chat keeps working with a keyword planner and says so.

Without the demo, `docker compose up -d --build` starts only the app. Connect your own
database from the **Databases** button in the chat header, or in
`config/databases.yaml`.

Stop with `docker compose --profile demo down` (add `-v` to delete the data volumes).

## Containers

| Service | Purpose |
|---|---|
| `web` | nginx: the built React UI, proxies `/api` to `api`. Published on `AI_CHAT_PORT` (8300). |
| `api` | FastAPI REST API (`/api/docs` for Swagger). |
| `worker` | Celery worker that answers chat messages (LLM call + queries) off the web process. |
| `redis` | Conversations, jobs, caches and the Celery broker. |
| `metadb` | PostgreSQL for saved charts, dashboards and database connections. |
| `demo-warehouse`, `demo-seed` | *(profile `demo`)* sample warehouse + a one-shot loader that seeds it and creates the read-only `ai_chat_ro` role. |
| `ollama` | *(profile `ollama`)* bundled LLM server, if the host has none. Set `AI_CHAT_BASE_URL=http://ollama:11434/v1`, then run `docker compose exec ollama ollama pull llama3.1:8b`. |

If no worker answers, the API answers messages in a background thread instead.

## How a message is answered

1. **Catalog** (`catalog.py`, `profiler.py`): the schema is profiled into *sources*.
   A source is either a fact table joined to its dimensions or a standalone table. The
   catalog records column roles, units, `semantics.yaml` descriptions and business
   metrics, the values of low-cardinality dimensions, and each source's date coverage.
   It is cached for 30 minutes; the ⟳ button re-reads it.
2. **Planner** (`service.py`, `prompts.py`, `llm.py`): the LLM turns the message into a
   small JSON intent (action, source, metrics, dimensions, grain, period, filters, chart
   type, top N). **The LLM never writes SQL.** Few-shot examples are generated from your
   own catalog. If the LLM is down or returns bad JSON, `rules.py` (a keyword planner)
   answers instead.
3. **Resolver** (`resolver.py`): checks every name against the catalog and repairs near
   misses (`retial` → `Retail`, `PlayStation 4` → `PS4`, `jp_sales` read as "Japan").
   Unknown names get one LLM repair round, then a clarifying question with clickable
   options.
4. **Chart** (`charts.py`): honours the requested chart type when the data fits.
   Otherwise it picks the best type and says why (a pie over 200 products becomes a top-25
   bar).
5. **SQL** (`sqlgen.py`, `dialects.py`): builds the query from quoted identifiers.
   Filter values and dates are bound parameters. Time buckets and limits are generated
   per dialect.
6. **Run and narrate** (`executor.py`, `narrate.py`): the query runs read-only. The
   answer text (peaks, shares, period-over-period change) is computed from the rows,
   never by the LLM.

### Answers, not just charts

- **Data questions** ("which region sold the most in 2025?", "how many orders were
  cancelled last quarter?") get a direct written answer first, then the chart. The
  LLM writes the answer from the query's actual rows plus exactly computed key facts,
  and is told never to invent or recompute numbers; the computed facts are shown
  under it. If the LLM is down, the computed facts are the answer.
- **Questions about the data** ("help me understand the database", "how are the
  tables related?", "what does gross margin mean?") get an explanation grounded only
  in the profiled catalog and `semantics.yaml`, plus a browsable view of every source:
  what one row is, the tables it joins, measures (meaning, unit), breakdowns (distinct
  values, examples) and business metrics. These questions are recognised even when the
  model plans them as a chart. Without an LLM a factual overview is shown.
- **Old data**: a relative period ("last 6 months") that starts after the data ends
  counts back from the last date with data, and the answer says so. Fixed periods
  ("2024", "Q3 2025") are never moved.
- A period that fits in one bucket ("cancelled orders last quarter" by quarter)
  becomes a single number, and "which warehouse region…" groups by the column the
  question names.

Set `AI_CHAT_ANSWER_WITH_LLM=false` to skip the extra answer call (faster, facts only).

Supported: breakdowns, trends at any grain, periods ("last 90 days", "Q3 2025",
"2024-01-01 to 2024-03-31"), value filters and exclusions, top/bottom N, business
metrics, KPI numbers. Chart types are big number (with or without trend), line, bar,
area, pie, table, heatmap, histogram, treemap, funnel and bubble. You can also ask for
"show the SQL", "what data do you have" and "save it to <dashboard>".

### Derived breakdowns
Questions can group by values that no column holds. The planner picks a derivation from a
fixed menu and the chat compiles it to SQL for the database's dialect, so the LLM still
never writes SQL:

| Ask for | Values (sorted in this order) |
|---|---|
| "by day of week", "by weekday" | `1 Mon` … `7 Sun` |
| "weekend vs weekday", "only weekends" | Weekday, Weekend |
| "by month of year", "seasonality" | `01 Jan` … `12 Dec` |
| "by quarter of year" | Q1 … Q4 |
| "by hour of day" | 00 … 23 |
| "<measure> bands 50, 200, 500" | `1. under 50`, `2. 50-200`, … `4. 500+` |
| "group regions into East (Northeast, Southeast) and West (…)" | East, West, Other |

Bands without edges are split into about five round ranges from the column's profiled
minimum and maximum. Derived values also work as filters ("only Saturdays", "weekdays
only") and in follow-ups. "By month" is still a monthly trend; "month of year" is the
derived breakdown. Derived columns are computed in the chart's query; nothing is stored
in your database.

## Databases

Any database with a SQLAlchemy driver works. Generated SQL is tailored for
PostgreSQL-like engines (PostgreSQL, Redshift, DuckDB, Snowflake, Trino),
MySQL/MariaDB, SQLite, SQL Server, Oracle and BigQuery.

- **Drivers**: the image includes PostgreSQL (`psycopg2`) and MySQL (`pymysql`). Add more
  at build time, e.g. `EXTRA_PIP_PACKAGES="pymssql snowflake-sqlalchemy"` in `.env`.
- **From configuration**: `config/databases.yaml` is synced on every start. These
  connections are marked *server config* and cannot be deleted in the UI.
- **From the UI**: **Databases** → name + SQLAlchemy URI → *Test connection* → *Connect*.
  URIs are stored encrypted with `AI_CHAT_SECRET_KEY`. Set
  `AI_CHAT_ALLOW_UI_CONNECTIONS=false` to disable this. SQLite URIs are refused from the
  UI because they read server files.
- **Schemas**: an optional per-connection allow-list limits what users can chat about.
- **Use a read-only database user.** The app only issues SELECTs, runs each query in a
  rolled-back transaction (`READ ONLY` with a statement timeout on PostgreSQL), and
  never puts user text into SQL. The database is still the real guard.

### Semantics (optional, recommended)

A `semantics/*.yaml` file describes a schema. It gives table and column descriptions,
roles (`measure`, `dimension`, `time`, `id`), aggregates, units, joins that are not
foreign keys, and **business metrics** (SQL expressions such as gross margin). See
`semantics/wh.yaml`. A file applies to the schema named by its `schema:` key. When
several files describe the same schema, the one whose `database_name` matches the
connection name wins.

## Configuration

All settings are environment variables (set in `.env` for Docker):

| Variable | Default | |
|---|---|---|
| `AI_CHAT_PORT` | `8300` | Published UI port |
| `AI_CHAT_PROVIDER` | `openrouter` | `openrouter`, `opensource` (any other OpenAI-compatible server) or `anthropic` |
| `AI_CHAT_MODEL` | `google/gemma-4-31b-it:free` | Model name; must support JSON mode (`llama3.1:8b` for Ollama, `claude-sonnet-5-5` for Anthropic) |
| `AI_CHAT_BASE_URL` | `https://openrouter.ai/api/v1` | OpenAI-compatible endpoint (Ollama: `http://host.docker.internal:11434/v1`) |
| `OPENROUTER_API_KEY` / `AI_CHAT_API_KEY` / `ANTHROPIC_API_KEY` | – / `ollama` / – | LLM credentials |
| `AI_CHAT_ANSWER_WITH_LLM` | `true` | Write a direct answer to data questions from the result rows |
| `AI_CHAT_LLM_TIMEOUT`, `AI_CHAT_LLM_COOLDOWN` | `60`, `60` | Seconds per call; after a failure the LLM is skipped for the cooldown |
| `AI_CHAT_SECRET_KEY` | dev value | Encrypts stored connection URIs. **Change it.** |
| `AI_CHAT_API_KEYS` | – | Comma-separated keys. When set, every call needs `X-API-Key` (the UI asks for it). |
| `AI_CHAT_TRUST_USER_HEADER` | `false` | Take the user id from `X-User-Id`, set by your auth proxy, so each user gets their own chats, charts and dashboards. Only enable this behind a proxy that overwrites the header. |
| `AI_CHAT_ALLOW_UI_CONNECTIONS` | `true` | Users may add or delete connections |
| `AI_CHAT_CORS_ORIGINS` | – | Origins allowed to call the API directly (embedding) |
| `AI_CHAT_RATE_LIMIT` | `20` | Messages per user per minute |
| `AI_CHAT_QUERY_TIMEOUT` | `60` | Seconds per query (PostgreSQL/MySQL) |
| `AI_CHAT_MAX_ROWS`, `AI_CHAT_MAX_TABLES` | `50000`, `30` | Result and profiling limits |
| `AI_CHAT_CATALOG_TTL`, `AI_CHAT_CONVERSATION_TTL`, `AI_CHAT_RESULT_TTL` | `1800`, `604800`, `600` | Cache lifetimes (seconds) |
| `AI_CHAT_WORKER_CONCURRENCY` | `4` | Parallel chat turns per worker |
| `EXTRA_PIP_PACKAGES` | – | Extra database drivers baked into the backend image |

Outside Docker you also set `AI_CHAT_METADATA_DB_URL` (default `sqlite:///./ai_chat.db`),
`AI_CHAT_REDIS_URL` (empty means in-memory, single process only),
`AI_CHAT_DATABASES` (JSON) or `AI_CHAT_DATABASES_FILE`, `AI_CHAT_SEMANTICS_DIR` and
`AI_CHAT_STATIC_DIR`. Set `AI_CHAT_STATIC_DIR` to a built frontend `dist/` and the API
serves the UI itself.

## Using it from other projects

- **As a service**: run the containers and call the REST API (`/api/docs`). A turn is
  `POST /api/message` → poll `GET /api/job/{id}` → `GET
  /api/conversations/{cid}/messages/{mid}/data` for the rows. The message's
  `chart.config` says which columns are the x axis, series and metrics.
- **In a page**: embed `http://<host>:8300/?conversation=…`, `/charts/<id>` or
  `/dashboards/<id>` in an iframe. Alternatively, build the frontend with
  `VITE_API_BASE=https://chat-api.example.com` and allow your origin with
  `AI_CHAT_CORS_ORIGINS`.
- **As a library**: `pip install ./backend` and use `ai_chat.service.handle_message`, or
  reuse the pure parts (`intent`, `resolver`, `rules`, `sqlgen`, `narrate`) with your
  own storage.

## Development

```bash
# Backend (SQLite metadata + in-memory state, no Docker needed)
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,postgres]"
AI_CHAT_DATABASES='[{"name":"Local","uri":"postgresql+psycopg2://user:pw@localhost:5432/db"}]' \
  uvicorn ai_chat.main:app --reload            # http://localhost:8000/api/docs

# Frontend (proxies /api to :8000)
cd frontend && npm install && npm run dev      # http://localhost:5173
```

Tests and checks:

```bash
cd backend && pytest -q && ruff check . && ruff format --check . && mypy ai_chat
cd frontend && npm test && npm run build
```

The backend suite needs no database, Redis or LLM. Its end-to-end tests build a SQLite
star schema and drive the HTTP API through profiling, chat, chart data, SQL, saving to
dashboards, CSV export and API keys.

## Differences from the Superset version

| Superset fork | Standalone |
|---|---|
| Profiling via Superset `Database` + Auto Dashboard builder | `profiler.py` on SQLAlchemy `inspect()` |
| Virtual datasets + `ChartDataCommand` | `sqlgen.py` + `executor.py` (bound parameters, dialect-aware) |
| Superset viz plugins (`SuperChart`) | ECharts renderer for the same chart types |
| Charts/dashboards saved as Superset slices | Own `ai_chat_chart` / `ai_chat_dashboard` tables, re-queried on view |
| "Edit in Explore" | **CSV** export and SQL view |
| Superset users, roles and RLS | API keys + optional trusted `X-User-Id`; access is the DB user's grants + schema allow-list |
| LangChain clients | OpenAI / Anthropic SDKs directly |
| `get_since_until` | `timerange.py` (same range syntax, so saved "last 30 days" charts keep moving) |

Saved charts store the resolved spec, not SQL, so they adapt when the schema is
re-profiled. Tables are created on first start; there are no migrations yet.
