# Auto Dashboard

AI-driven Superset dashboard generator using DeepAgents with models from OpenRouter (default), a local OpenAI-compatible server (Ollama/vLLM) or Claude to run 5 checks (C1–C5), formulate hypotheses, and build native dashboards.

## How it works
The five checks (C1 data quality, C2 trends, C3 segmentation/Pareto, C4 anomalies,
C5 KPIs) are computed with SQL by a deterministic builder
(`superset/ai_dashboard/builder.py`). It profiles the schema (using `semantics.yaml`
when it describes that schema, otherwise database introspection), creates one
denormalised virtual dataset per fact table, validates every chart by running its
query, and saves a native dashboard with a KPI row, one section per check, and
native filters. A dashboard is produced even if the LLM is down.

Modes (`AI_DASH_MODE`):
- `hybrid` (default): builder + LLM-written executive summary (template summary if the LLM fails).
- `rules`: builder only, no LLM calls.
- `agent`: the original deepagents flow (one subagent per check). If it errors or does not
  produce a dashboard, the builder takes over and the UI shows the reason.

If no Celery worker answers, the API runs the job in a web-server thread instead.

## Environment Variables
Set in `docker/.env` or `docker/.env-local`:
- `AI_DASH_MODE`: `hybrid` (default), `rules` or `agent`
- `AI_DASH_LLM_TIMEOUT`: Seconds per LLM call (default: `120`)
- `AI_DASH_RECURSION_LIMIT`: Max agent steps in `agent` mode (default: `120`)
- `AI_DASH_SEMANTICS_PATH`: Optional path to a semantics.yaml (a copy ships in `superset/ai_dashboard/`)
- `AI_DASH_PROVIDER`: `openrouter` (default), `opensource` (Ollama/vLLM) or `anthropic`
- `OPENROUTER_API_KEY`: OpenRouter key (put it in `docker/.env`, which git ignores)
- `AI_DASH_MODEL`: Main model (OpenRouter default: `nvidia/nemotron-3-ultra-550b-a55b:free`;
  Ollama default: `llama3.1:8b`)
- `AI_DASH_SUBMODEL`: Subagent model (OpenRouter default: `google/gemma-4-26b-a4b-it:free`;
  Ollama default: `llama3.2:3b`)
- `AI_DASH_BASE_URL`: OpenAI-compatible endpoint (default: `https://openrouter.ai/api/v1`, or
  `http://host.docker.internal:11434/v1` for `opensource`)
- `AI_DASH_API_KEY`: API key for an `opensource` endpoint (default: `ollama`)
- `AI_DASH_DB_PASSWORD`: Password for `ai_dash_ro` role (default: `ai_dash_ro_secret`)
- `AI_DASH_MAX_SQL_CALLS`: Max SQL queries budget (default: `25`)
- `AI_DASH_MAX_CHARTS`: Max analysis charts (default: `14`; up to 8 KPI tiles are added on top)
- `AI_DASH_MAX_PROBES`: Max follow-up hypotheses (default: `8`)

## Setup
0. Load the sample data (the warehouse container only creates empty tables):
   `python -m venv /tmp/seedvenv && /tmp/seedvenv/bin/pip install "psycopg[binary]" pyyaml && /tmp/seedvenv/bin/python warehouse/seed.py`
1. Apply read-only role to warehouse:
   `docker exec -i superset_warehouse psql -U warehouse -d warehouse < warehouse/ro_role.sql`
2. Register database in Superset:
   Name `warehouse`, URI `postgresql+psycopg2://ai_dash_ro:<pwd>@warehouse:5432/warehouse`, DML disabled.
3. Start stack & frontend dev server:
   `docker compose up -d` (dev server: `docker compose up -d superset-node` on port 9010).

## How to Run
1. Navigate to Superset Dashboards list (`http://localhost:8088/dashboard/list/`).
2. Click **✦ Auto Dashboard** in the header.
3. Select `warehouse` database and `wh` schema, then click **Generate**.
4. View real-time progress. On completion, click **Open Dashboard** or download ZIP/JSON.

## Testing
- Backend: `docker exec -w /app superset_app python -m pytest tests/unit_tests/ai_dashboard -q`
- Frontend: `cd superset-frontend && npx jest src/features/autoDashboard`

After changing backend code, restart the worker (`docker restart superset_worker`);
Celery does not auto-reload.

## Troubleshooting
- **"Connection error"**: the LLM endpoint is unreachable. In `hybrid` mode the dashboard is
  still built; check `OPENROUTER_API_KEY` in `docker/.env` (or, with `opensource`, that Ollama
  is reachable from containers at `AI_DASH_BASE_URL`). Free OpenRouter models are rate limited,
  so a "rate limited" error is temporary.
- **"No table in schema … has numeric measures"**: the selected schema is empty or has no
  numeric columns. Seed the warehouse (setup step 0) or pick another schema.

---

# AI Chart Chat

A chat page (**AI Chat** in the top menu, or **AI Chat** on the Charts list; URL `/chart/chat/`)
where users describe a chart in plain English and refine it with follow-ups.

## How it works
1. **Catalog** (`superset/ai_chat/catalog.py`): the selected schema is profiled with the Auto
   Dashboard builder into *sources* (fact tables joined to their dimensions, plus standalone
   tables), with column roles, units, `semantics.yaml` descriptions and business metrics, the
   values of low-cardinality dimensions and each source's date coverage. Cached for 30 minutes.
2. **Planner**: the LLM turns the message into a small JSON intent (action, source, metrics,
   dimensions, time grain/period, filters, chart type, top N). Few-shot examples are generated
   from the user's own catalog. The LLM never writes SQL or form data.
3. **Resolver**: every name is checked against the catalog: near-miss spellings and wrong
   value casing are repaired (`retial` → `Retail`), "how many customers" becomes a distinct
   count, SUM over text becomes a distinct count, and high-cardinality breakdowns get a top 25.
   Filter values are checked against up to 300 known values per column, including initials
   (`PlayStation 4` → `PS4`). Place codes in column names are understood (`jp_sales` is
   shown as "Japan Sales"), so "in Japan" uses that measure instead of an invented
   `platform = Japan` filter. "Best/top/which X" questions rank X by total rather than
   returning a single MAX, and a breakdown filtered to one value is dropped.
   Unknown names get one LLM repair round, then a clarifying question with clickable options
   (closest values first).
4. **Chart decider**: an explicit chart type is used when the data fits; otherwise the best
   type is chosen and the reason is shown (e.g. a pie over 200 products becomes a bar chart).
5. **Validation & answer**: the chart query runs once through Superset (permissions and RLS
   apply). Empty results explain the filters and the data's date coverage. The answer text
   (peaks, shares, period-over-period change) is computed from the result rows.

Charts are previews until the user clicks **Save** (or types "save it to <dashboard>"). Saving
can add the chart to one of the user's dashboards or create a new one. **Edit in Explore**
opens the unsaved chart in Explore; **SQL** shows the query.

Supported requests include: breakdowns, trends at any grain, periods ("last 90 days", "Q3 2025",
"2024-01-01 to 2024-03-31"), value filters and exclusions, top/bottom N, business metrics
from `semantics.yaml`, KPI numbers, and pie, bar, line, area, table, heatmap, histogram,
treemap, funnel and scatter charts; follow-ups such as "make it a pie", "only Online", "by
quarter instead", "remove the filters", "show all time"; plus "what data do you have",
"show the SQL", and saving.

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
derived breakdown. Derived columns live on the chart only (Superset: adhoc SQL columns;
open the chart in Explore to see them), not on the dataset.

## Failure handling
- LLM unreachable, slow or returning invalid JSON: a keyword planner answers instead and the
  user is told. After a failure the LLM is skipped for `AI_CHAT_LLM_COOLDOWN` seconds.
- Off-topic or data-changing requests ("delete all orders") are declined.
- Query, permission and dataset errors are shown as specific messages, with a retry option.
- One message is answered at a time per conversation (409 otherwise). Users are limited to
  `AI_CHAT_RATE_LIMIT` messages per minute (429).
- Conversations live in the Superset cache for `AI_CHAT_CONVERSATION_TTL` (7 days) and can be
  reopened from the sidebar or a `?conversation=<id>` link. A page reload resumes waiting.
- Messages run on Celery (`superset.ai_chat.tasks` must be in `CeleryConfig.imports`), or in a
  web-server thread when no worker answers.

## Environment variables
`AI_CHAT_*` falls back to the matching `AI_DASH_*` value:
- `AI_CHAT_PROVIDER` (`openrouter`), `AI_CHAT_MODEL` (`google/gemma-4-31b-it:free`),
  `AI_CHAT_BASE_URL`, `AI_CHAT_API_KEY`; `OPENROUTER_API_KEY` for OpenRouter
- Set `AI_CHAT_MODEL` explicitly: otherwise it falls back to `AI_DASH_MODEL`, and the chat
  needs a model that supports JSON mode (`response_format`)
- `AI_CHAT_LLM_TIMEOUT` (60), `AI_CHAT_LLM_COOLDOWN` (60), `AI_CHAT_RATE_LIMIT` (20/min),
  `AI_CHAT_CATALOG_TTL` (1800), `AI_CHAT_CONVERSATION_TTL` (604800)

With Ollama, keep the server's context window at 4096 tokens or more (`OLLAMA_CONTEXT_LENGTH`);
the chat prompt is about 2–3K tokens.

## Testing
- Backend: `docker exec -w /app superset_app python -m pytest tests/unit_tests/ai_chat -q`
- Frontend: `cd superset-frontend && npx jest src/features/chartChat`

After backend changes restart the worker (`docker restart superset_worker`). After adding the
feature run `superset init` once so roles get the **AI Chat** menu permission.
