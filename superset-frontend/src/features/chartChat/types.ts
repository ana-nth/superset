/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */
import { QueryFormData } from '@superset-ui/core';

export type MessageType =
  | 'text'
  | 'chart'
  | 'no_data'
  | 'clarification'
  | 'catalog'
  | 'sql'
  | 'saved'
  | 'error';

export interface SavedChart {
  chart_id: number;
  chart_title: string;
  chart_url: string;
  dashboard_id?: number;
  dashboard_title?: string;
  dashboard_url?: string;
  dashboard_created?: boolean;
}

export interface ChatChart {
  title: string;
  viz_type: string;
  viz_label: string;
  dataset_id: number;
  form_data: QueryFormData;
  row_count: number;
  interpretation: string[];
  saved_chart_id?: number;
  saved_title?: string;
  dashboards?: { id: number; title: string }[];
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  type: MessageType;
  text: string;
  created_at?: string;
  insight?: string;
  notes?: string[];
  options?: string[];
  details?: string[];
  chart?: ChatChart;
  sql?: string;
  saved?: SavedChart;
  engine?: 'llm' | 'rules';
  error_code?: string;
  reply_to?: string;
  /** Client-only: a user message not yet acknowledged by the server */
  pending?: boolean;
}

export interface Conversation {
  id: string;
  title: string;
  database_id: number;
  schema: string;
  updated_at: string;
  messages?: ChatMessage[];
}

export type JobState =
  | 'PENDING'
  | 'RUNNING'
  | 'SUCCESS'
  | 'FAILURE'
  | 'NOT_FOUND';

export interface JobStatus {
  state: JobState;
  stage?: string;
  message?: ChatMessage;
  error?: string;
}

export interface CatalogSource {
  name: string;
  label: string;
  rows: number;
  time_min: string;
  time_max: string;
  measures: string[];
  dimensions: string[];
  metrics: string[];
}

export interface CatalogSummary {
  database_name: string;
  schema: string;
  sources: CatalogSource[];
  suggestions: string[];
}

export interface Option<T> {
  value: T;
  label: string;
}
