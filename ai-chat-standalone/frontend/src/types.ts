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

export type MessageType =
  | 'text'
  | 'chart'
  | 'no_data'
  | 'clarification'
  | 'catalog'
  | 'sql'
  | 'saved'
  | 'error';

export type VizType =
  | 'big_number'
  | 'big_number_trend'
  | 'line'
  | 'bar'
  | 'area'
  | 'pie'
  | 'table'
  | 'heatmap'
  | 'histogram'
  | 'treemap'
  | 'funnel'
  | 'bubble';

/** A result column holding a metric. */
export interface MetricRef {
  key: string;
  label: string;
  unit: string;
}

/** Which result columns play which role (see backend charts.render_config). */
export interface ChartConfig {
  viz_type: VizType;
  metrics: MetricRef[];
  dimensions: string[];
  time_column: string | null;
  time_grain: string | null;
  subheader: string;
  sort_desc: boolean;
  // line, bar, area, heatmap, big number trend
  x?: string;
  x_is_time?: boolean;
  series?: string | null;
  stack?: boolean;
  show_values?: boolean;
  label_rotate?: number;
  // heatmap
  y?: string;
  // pie, funnel, treemap
  categories?: string[];
  // histogram
  column?: string;
  column_label?: string;
  bins?: number;
  // bubble
  entity?: string;
  x_metric?: MetricRef;
  y_metric?: MetricRef;
  size_metric?: MetricRef;
  // table
  columns?: string[];
}

export type Cell = string | number | boolean | null;
export type Row = Record<string, Cell>;

export interface HistogramData {
  bins: { start: number; end: number }[];
  series: { name: string; counts: number[] }[];
}

export interface ChartData {
  rows?: Row[];
  histogram?: HistogramData;
  row_count: number;
}

export interface SaveResult {
  chart_id: string;
  chart_title: string;
  chart_url: string;
  dashboard_id?: string;
  dashboard_title?: string;
  dashboard_url?: string;
  dashboard_created?: boolean;
}

export interface ChatChart {
  title: string;
  viz_type: VizType;
  viz_label: string;
  database_id: string;
  schema: string;
  config: ChartConfig;
  row_count: number;
  interpretation: string[];
  saved_chart_id?: string;
  saved_title?: string;
  dashboards?: { id: string; title: string }[];
}

/** One data source as explained to the user (backend Catalog.overview). */
export interface SchemaSource {
  name: string;
  label: string;
  description: string;
  grain: string;
  rows: number;
  tables: string[];
  time_column: string;
  time_min: string;
  time_max: string;
  time_columns: string[];
  measures: { name: string; label: string; unit: string; agg: string; description: string }[];
  dimensions: {
    name: string;
    label: string;
    description: string;
    cardinality: number;
    values: string[];
  }[];
  metrics: { name: string; label: string; meaning: string; unit: string }[];
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
  schema?: SchemaSource[];
  saved?: SaveResult;
  engine?: 'llm' | 'rules';
  error_code?: string;
  reply_to?: string;
  /** Client-only: a user message not yet acknowledged by the server */
  pending?: boolean;
}

export interface Conversation {
  id: string;
  title: string;
  database_id: string;
  schema: string;
  updated_at: string;
  pending_job_id?: string | null;
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

export interface DatabaseRecord {
  id: string;
  name: string;
  backend: string;
  uri: string;
  schemas: string[];
  managed: boolean;
}

export interface SavedChartRecord {
  id: string;
  title: string;
  viz_type: VizType;
  database_id: string;
  schema: string;
  config: ChartConfig;
  created_at: string | null;
  dashboards: { id: string; title: string }[];
}

export interface DashboardSummary {
  id: string;
  title: string;
  chart_count: number;
  updated_at: string | null;
}

export interface DashboardDetail {
  id: string;
  title: string;
  charts: SavedChartRecord[];
}

export interface AppConfig {
  version: string;
  auth_required: boolean;
  allow_ui_connections: boolean;
  provider: string;
  model: string;
  max_message_chars: number;
}
