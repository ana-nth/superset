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

# One image for the API (uvicorn) and the worker (celery).
FROM python:3.12-slim

# More database drivers, e.g. "pymssql snowflake-sqlalchemy sqlalchemy-bigquery"
ARG EXTRA_PIP_PACKAGES=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so code changes do not reinstall them
COPY pyproject.toml ./
RUN mkdir ai_chat && touch ai_chat/__init__.py \
    && pip install ".[postgres,mysql]" ${EXTRA_PIP_PACKAGES} \
    && pip uninstall -y ai-chat \
    && rm -rf ai_chat

COPY ai_chat ./ai_chat
RUN pip install --no-deps . \
    && useradd --create-home --uid 1000 aichat \
    && mkdir -p /app/config /app/semantics \
    && chown -R aichat /app

USER aichat
EXPOSE 8000
CMD ["uvicorn", "ai_chat.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
