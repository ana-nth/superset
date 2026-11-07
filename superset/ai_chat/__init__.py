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
AI Chart Chat: create Superset charts from natural-language questions.

Pipeline for each message (see ``service.py``):

1. ``catalog``  – profile the selected schema into sources, columns, sample values
2. ``planner``  – the LLM turns the message into a ``ChartIntent`` (JSON only);
                  ``rules`` is the keyword fallback when the LLM is unavailable
3. ``resolver`` – validate every name against the catalog and normalise the intent
                  into a ``ResolvedSpec`` (or a clarification question)
4. ``charts``   – choose the visualization and build Superset form data
5. ``narrate``  – run the chart query once and describe the result with real numbers

The LLM never writes SQL or form data, so a wrong answer can only pick the wrong
(existing) columns; it cannot break or inject into a query.
"""
