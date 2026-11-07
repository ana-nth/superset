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
The FastAPI application: ``uvicorn ai_chat.main:app``.

When ``AI_CHAT_STATIC_DIR`` points at the built frontend, the same process also
serves the UI (single-container mode); otherwise nginx serves it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ai_chat import __version__, api, config, datasources, metadb

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    metadb.init_db()
    datasources.sync_configured_connections()
    yield


def _mount_frontend(app: FastAPI, folder: str) -> None:
    root = os.path.realpath(folder)
    index = os.path.join(root, "index.html")
    if not os.path.isfile(index):
        logger.warning("AI_CHAT_STATIC_DIR %s has no index.html", folder)
        return
    assets = os.path.join(root, "assets")
    if os.path.isdir(assets):
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        candidate = os.path.realpath(os.path.join(root, path))
        if candidate.startswith(root + os.sep) and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(index)  # client-side routes


def create_app() -> FastAPI:
    logging.basicConfig(
        level=os.environ.get("AI_CHAT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = FastAPI(
        title="AI Chart Chat",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    origins = config.cors_origins()
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_exception_handler(api.ApiError, api.api_error_response)
    app.include_router(api.router)
    if config.static_dir():
        _mount_frontend(app, config.static_dir())
    return app


app = create_app()
