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
import {
  AppConfig,
  CatalogSummary,
  ChartData,
  ChatMessage,
  Conversation,
  DashboardDetail,
  DashboardSummary,
  DatabaseRecord,
  JobStatus,
  SavedChartRecord,
  SaveResult,
} from './types';

/** Set VITE_API_BASE to call a backend on another origin. */
const BASE = `${import.meta.env.VITE_API_BASE ?? ''}/api`;
const KEY_STORAGE = 'ai-chat-api-key';
export const AUTH_REQUIRED_EVENT = 'ai-chat-auth-required';

/** An API failure with the HTTP status and the server's message. */
export class ApiError extends Error {
  status: number;

  body: Record<string, unknown>;

  constructor(message: string, status: number, body: Record<string, unknown>) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export function getApiKey(): string {
  try {
    return localStorage.getItem(KEY_STORAGE) ?? '';
  } catch {
    return '';
  }
}

export function setApiKey(key: string): void {
  try {
    if (key) localStorage.setItem(KEY_STORAGE, key);
    else localStorage.removeItem(KEY_STORAGE);
  } catch {
    // Private mode: the key lasts until reload
  }
}

function headers(json: boolean): HeadersInit {
  const out: Record<string, string> = {};
  if (json) out['Content-Type'] = 'application/json';
  const key = getApiKey();
  if (key) out['X-API-Key'] = key;
  return out;
}

async function fail(response: Response, fallback: string): Promise<never> {
  let body: Record<string, unknown> = {};
  try {
    body = await response.json();
  } catch {
    body = {};
  }
  if (response.status === 401) {
    window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
  }
  const message =
    typeof body.message === 'string'
      ? body.message
      : typeof body.detail === 'string'
        ? body.detail
        : fallback;
  throw new ApiError(message, response.status, body);
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  fallback = 'The request failed.',
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      method,
      headers: headers(body !== undefined),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError('Cannot reach the server.', 0, {});
  }
  if (!response.ok) return fail(response, fallback);
  return (await response.json()) as T;
}

/** Download a CSV endpoint as a file (fetch keeps the API key header). */
async function download(path: string, fallback: string): Promise<void> {
  const response = await fetch(`${BASE}${path}`, { headers: headers(false) });
  if (!response.ok) return fail(response, fallback);
  const disposition = response.headers.get('Content-Disposition') ?? '';
  const name = /filename="([^"]+)"/.exec(disposition)?.[1] ?? 'chart.csv';
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}

const q = encodeURIComponent;

export interface SendMessagePayload {
  message: string;
  conversation_id?: string;
  database_id?: string;
  schema?: string;
  reference_message_id?: string;
}

export interface SendMessageResult {
  job_id: string;
  conversation_id: string;
  user_message: ChatMessage;
}

export const api = {
  config: () => request<AppConfig>('GET', '/config'),

  // ── data sources ──
  databases: () =>
    request<{ result: DatabaseRecord[] }>('GET', '/databases').then(
      r => r.result,
    ),
  addDatabase: (name: string, uri: string, schemas: string[]) =>
    request<{ id: string; name: string }>(
      'POST',
      '/databases',
      { name, uri, schemas },
      'Could not add the database.',
    ),
  testDatabase: (uri: string) =>
    request<{ schemas: string[] }>(
      'POST',
      '/databases/test',
      { uri },
      'Could not connect.',
    ),
  deleteDatabase: (id: string) =>
    request<unknown>('DELETE', `/databases/${q(id)}`),
  schemas: (databaseId: string) =>
    request<{ result: string[] }>(
      'GET',
      `/databases/${q(databaseId)}/schemas`,
      undefined,
      'Could not load schemas.',
    ).then(r => r.result),
  catalog: (databaseId: string, schema: string, refresh = false) =>
    request<CatalogSummary>(
      'GET',
      `/catalog?database_id=${q(databaseId)}&schema=${q(schema)}&refresh=${refresh}`,
      undefined,
      'Could not read this schema.',
    ),

  // ── chat ──
  sendMessage: (payload: SendMessagePayload) =>
    request<SendMessageResult>(
      'POST',
      '/message',
      payload,
      'Failed to send the message.',
    ),
  job: (jobId: string) => request<JobStatus>('GET', `/job/${q(jobId)}`),
  conversations: () =>
    request<{ result: Conversation[] }>('GET', '/conversations').then(
      r => r.result,
    ),
  conversation: (id: string) =>
    request<{ result: Conversation }>(
      'GET',
      `/conversations/${q(id)}`,
      undefined,
      'Could not load the conversation.',
    ).then(r => r.result),
  deleteConversation: (id: string) =>
    request<unknown>('DELETE', `/conversations/${q(id)}`),
  messageData: (conversationId: string, messageId: string) =>
    request<ChartData>(
      'GET',
      `/conversations/${q(conversationId)}/messages/${q(messageId)}/data`,
      undefined,
      'Failed to load chart data.',
    ),
  messageCsv: (conversationId: string, messageId: string) =>
    download(
      `/conversations/${q(conversationId)}/messages/${q(messageId)}/csv`,
      'Could not export the data.',
    ),
  saveChart: (payload: {
    conversation_id: string;
    message_id: string;
    title?: string;
    dashboard_id?: string;
    new_dashboard_title?: string;
  }) =>
    request<SaveResult>(
      'POST',
      '/save_chart',
      payload,
      'Failed to save the chart.',
    ),

  // ── saved charts ──
  charts: () =>
    request<{ result: SavedChartRecord[] }>('GET', '/charts').then(
      r => r.result,
    ),
  chart: (id: string) =>
    request<{ result: SavedChartRecord }>(
      'GET',
      `/charts/${q(id)}`,
      undefined,
      'Chart not found.',
    ).then(r => r.result),
  deleteChart: (id: string) => request<unknown>('DELETE', `/charts/${q(id)}`),
  chartData: (id: string) =>
    request<ChartData>(
      'GET',
      `/charts/${q(id)}/data`,
      undefined,
      'Failed to load chart data.',
    ),
  chartSql: (id: string) =>
    request<{ sql: string }>('GET', `/charts/${q(id)}/sql`).then(r => r.sql),
  chartCsv: (id: string) =>
    download(`/charts/${q(id)}/csv`, 'Could not export the data.'),

  // ── dashboards ──
  dashboards: () =>
    request<{ result: DashboardSummary[] }>('GET', '/dashboards').then(
      r => r.result,
    ),
  createDashboard: (title: string) =>
    request<{ id: string; title: string }>('POST', '/dashboards', { title }),
  dashboard: (id: string) =>
    request<{ result: DashboardDetail }>(
      'GET',
      `/dashboards/${q(id)}`,
      undefined,
      'Dashboard not found.',
    ).then(r => r.result),
  renameDashboard: (id: string, title: string) =>
    request<unknown>('PATCH', `/dashboards/${q(id)}`, { title }),
  deleteDashboard: (id: string) =>
    request<unknown>('DELETE', `/dashboards/${q(id)}`),
  removeFromDashboard: (dashboardId: string, chartId: string) =>
    request<unknown>(
      'DELETE',
      `/dashboards/${q(dashboardId)}/charts/${q(chartId)}`,
    ),
};
