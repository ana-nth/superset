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

# Setup: Superset + AI Chat + AI Auto Dashboard (Docker)

Step-by-step instructions to run this Superset 6.0.0 fork locally with Docker. The AI
features use free models on [OpenRouter](https://openrouter.ai) by default.

## What you get

| URL | What |
|---|---|
| http://localhost:8088 | Superset (login `admin` / `admin`) |
| http://localhost | Same app through nginx |
| http://localhost:8088/chart/chat/ | AI Chat (ask questions, get charts) |
| http://localhost:8088/dashboard/list/ → **✦ Auto Dashboard** | AI Auto Dashboard |
| `localhost:5433` | Sample warehouse (PostgreSQL, user/password `warehouse`) |

## 1. Prerequisites

- Docker Engine with the Compose v2 plugin (`docker compose version` should work)
- About 16 GB RAM free and 20 GB disk (the frontend dev build alone uses ~6 GB RAM)
- Free ports: 80, 8088, 8081, 8082, and on 127.0.0.1: 5432, 5433, 6379, 9010
- An OpenRouter API key: create one at https://openrouter.ai/keys
- Python 3.9+ on the host (only to load the sample data in step 6)

## 2. Get the code

```bash
git clone https://github.com/ana-nth/superset.git
cd superset
```

All commands below are run from this folder.

## 3. Create the environment file and add your key

```bash
cp docker/.env.example docker/.env
```

Open `docker/.env` and set your key (no quotes, no spaces around `=`):

```
OPENROUTER_API_KEY=sk-or-v1-...
```

`docker/.env` is git-ignored, so the key is never committed. The models are set in the
same file; the defaults are free models that were tested with this project:

| Setting | Default | Used for |
|---|---|---|
| `AI_CHAT_MODEL` | `nvidia/nemotron-3.5-super-vl:free` | AI Chat (must support JSON mode) |
| `AI_DASH_MODEL` | `nvidia/nemotron-3-ultra-550b-a55b:free` | Auto Dashboard summary / agent |
| `AI_DASH_SUBMODEL` | `nvidia/nemotron-3.5-lightning:free` | Auto Dashboard sub-agents (`agent` mode) |

## 4. Install the AI Python packages

The containers install `docker/requirements-local.txt` every time they start. Create it
from the committed list:

```bash
cp docker/requirements-ai.txt docker/requirements-local.txt
```

Without this, `superset_app` and `superset_worker` crash with
`ModuleNotFoundError: No module named 'pydantic'`.

## 5. Start everything

```bash
docker compose up -d
```

The first run builds the images and installs the frontend packages, which takes 10–20
minutes. Later starts take a minute or two. Watch progress with:

```bash
docker compose ps                      # all services and their health
docker logs -f superset_init           # database migrations + admin user (exits when done)
docker logs -f superset_node           # frontend build; wait for "compiled successfully"
```

Check the backend is up:

```bash
curl -f http://localhost:8088/health   # prints OK
```

The UI works once `superset_node` prints `webpack ... compiled successfully`.

## 6. Load the sample warehouse data

The `superset_warehouse` container starts with empty tables. Fill them (≈250K rows):

```bash
python3 -m venv /tmp/seedvenv
/tmp/seedvenv/bin/pip install "psycopg[binary]" pyyaml
/tmp/seedvenv/bin/python warehouse/seed.py
```

Then create the read-only role the AI features query with:

```bash
docker exec -i superset_warehouse psql -U warehouse -d warehouse < warehouse/ro_role.sql
```

## 7. Connect the warehouse in Superset

```bash
docker exec superset_app superset set-database-uri \
  -d warehouse \
  -u "postgresql+psycopg2://ai_dash_ro:ai_dash_ro_secret@warehouse:5432/warehouse"
```

(Or in the UI: **Settings → Database Connections → + Database**, name `warehouse`, with
the same URI. Leave DML disabled.)

## 8. Try it

1. Open http://localhost:8088 and log in as `admin` / `admin`.
2. **AI Chat:** go to **Charts → AI Chat** (or http://localhost:8088/chart/chat/), pick the
   `warehouse` database and `wh` schema, and ask e.g.
   *"Show monthly revenue by customer segment in 2025"*.
3. **Auto Dashboard:** go to **Dashboards → ✦ Auto Dashboard**, pick `warehouse` / `wh`,
   click **Generate**. It takes about 30–60 seconds and opens a dashboard with ~19 charts
   and an AI-written executive summary.

> The sample data ends on 2025-10-19, so "last 12 months" questions return no rows. Ask
> about 2025 or "all time" instead.

## Everyday commands

```bash
docker compose ps                                  # status
docker compose logs -f superset superset-worker   # backend logs
docker compose restart superset-worker             # after changing Python code (Celery doesn't auto-reload)
docker compose stop                                # stop, keep data
docker compose up -d                               # start again
docker compose down                                # remove containers, keep data volumes
docker compose down -v                             # remove containers AND all data (full reset)
```

**After editing `docker/.env`**, recreate the containers. A plain restart does not reload
the file:

```bash
docker compose up -d --force-recreate superset superset-worker superset-worker-beat
```

## Switching models or providers

All settings live in `docker/.env` (recreate the containers afterwards, see above).

- **Another OpenRouter model:** change `AI_CHAT_MODEL` / `AI_DASH_MODEL` /
  `AI_DASH_SUBMODEL`. The chat model must support `response_format` (JSON mode); check the
  model page on openrouter.ai.
- **Local Ollama instead:** set `AI_CHAT_PROVIDER=opensource` and `AI_DASH_PROVIDER=opensource`,
  `AI_CHAT_BASE_URL=http://host.docker.internal:11434/v1` and
  `AI_DASH_BASE_URL=http://host.docker.internal:11434/v1`, and model names such as
  `llama3.1:8b`.
- **Auto Dashboard mode:** `AI_DASH_MODE=hybrid` (default: rules-built dashboard + AI
  summary), `rules` (no AI), or `agent` (multi-agent, many more LLM calls).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No module named 'pydantic'` in `docker logs superset_app` | Do step 4, then `docker compose restart superset superset-worker superset-worker-beat` |
| Blank page / UI never loads | `docker logs superset_node`: wait for `compiled successfully`, then hard-refresh (Ctrl+Shift+R). `Can't resolve '@superset-ui/core'` means `superset-frontend/packages/` or `plugins/` is missing its source |
| Chat says "the AI model is rate limited" | Free models share a busy pool. Wait a minute or switch model; the chat keeps working with its keyword planner meanwhile |
| "the AI model rejected the API key" | Check `OPENROUTER_API_KEY` in `docker/.env` (no quotes), then recreate the containers |
| "no endpoints found" from OpenRouter | Free models may require allowing prompt logging in your OpenRouter privacy settings |
| `port is already allocated` | Another app (or another Superset copy) is using the port. Stop it, or change the port in `docker-compose.yml` |
| Containers named `superset_*` from another folder get replaced | All copies use compose project `superset`; `docker compose up` in one folder takes over the containers of the other (data volumes are shared) |

More detail: [AUTO_DASHBOARD_README.md](AUTO_DASHBOARD_README.md) and
[ai-chat-standalone/README.md](ai-chat-standalone/README.md) (a standalone version of the
chat without Superset).
