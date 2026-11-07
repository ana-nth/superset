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
  getClientErrorObject,
  QueryFormData,
  SupersetClient,
  t,
} from '@superset-ui/core';
import { buildV1ChartDataPayload } from 'src/explore/exploreUtils';
import {
  CatalogSummary,
  ChatMessage,
  Conversation,
  JobStatus,
  SavedChart,
} from './types';

const BASE = '/api/v1/ai_chat';

/** An API failure with the HTTP status and the server's message. */
export class ChatApiError extends Error {
  status: number;

  body: Record<string, unknown>;

  constructor(message: string, status: number, body: Record<string, unknown>) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function toApiError(err: unknown, fallback: string) {
  const response = err as Response;
  let body: Record<string, unknown> = {};
  if (response && typeof response.clone === 'function') {
    try {
      body = await response.clone().json();
    } catch {
      body = {};
    }
  }
  let message = typeof body.message === 'string' ? body.message : '';
  if (!message) {
    try {
      const parsed = await getClientErrorObject(
        err as Parameters<typeof getClientErrorObject>[0],
      );
      message = parsed.message || parsed.error || '';
    } catch {
      message = '';
    }
  }
  return new ChatApiError(
    message || fallback,
    typeof response?.status === 'number' ? response.status : 0,
    body,
  );
}

export interface SendMessagePayload {
  message: string;
  conversation_id?: string;
  database_id?: number;
  schema?: string;
  reference_message_id?: string;
}

export interface SendMessageResult {
  job_id: string;
  conversation_id: string;
  user_message: ChatMessage;
}

export async function sendMessage(
  payload: SendMessagePayload,
): Promise<SendMessageResult> {
  try {
    const { json } = await SupersetClient.post({
      endpoint: `${BASE}/message`,
      jsonPayload: payload,
    });
    return json as SendMessageResult;
  } catch (err) {
    throw await toApiError(err, t('Failed to send the message.'));
  }
}

export async function getJob(jobId: string): Promise<JobStatus> {
  const { json } = await SupersetClient.get({
    endpoint: `${BASE}/job/${jobId}`,
  });
  return json as JobStatus;
}

export async function getCatalog(
  databaseId: number,
  schema: string,
  refresh = false,
): Promise<CatalogSummary> {
  const params = new URLSearchParams({
    database_id: String(databaseId),
    schema,
    refresh: refresh ? 'true' : 'false',
  });
  try {
    const { json } = await SupersetClient.get({
      endpoint: `${BASE}/catalog?${params.toString()}`,
    });
    return json as CatalogSummary;
  } catch (err) {
    throw await toApiError(err, t('Could not read this schema.'));
  }
}

export async function listConversations(): Promise<Conversation[]> {
  const { json } = await SupersetClient.get({
    endpoint: `${BASE}/conversations`,
  });
  return (json.result || []) as Conversation[];
}

export async function getConversation(id: string): Promise<Conversation> {
  try {
    const { json } = await SupersetClient.get({
      endpoint: `${BASE}/conversations/${id}`,
    });
    return json.result as Conversation;
  } catch (err) {
    throw await toApiError(err, t('Could not load the conversation.'));
  }
}

export async function deleteConversation(id: string): Promise<void> {
  try {
    await SupersetClient.delete({ endpoint: `${BASE}/conversations/${id}` });
  } catch (err) {
    throw await toApiError(err, t('Could not delete the conversation.'));
  }
}

/** The query context Explore would save for ``formData`` (best effort). */
async function buildQueryContext(
  formData: QueryFormData,
): Promise<string | undefined> {
  try {
    const payload = await buildV1ChartDataPayload({
      formData,
      force: false,
      resultFormat: 'json',
      resultType: 'full',
      setDataMask: () => {},
      ownState: {},
    });
    return JSON.stringify(payload);
  } catch {
    return undefined;
  }
}

export interface SaveChartPayload {
  conversationId: string;
  messageId: string;
  formData: QueryFormData;
  sliceName: string;
  dashboardId?: number;
  newDashboardTitle?: string;
}

export async function saveChart({
  conversationId,
  messageId,
  formData,
  sliceName,
  dashboardId,
  newDashboardTitle,
}: SaveChartPayload): Promise<SavedChart> {
  const queryContext = await buildQueryContext(formData);
  try {
    const { json } = await SupersetClient.post({
      endpoint: `${BASE}/save_chart`,
      jsonPayload: {
        conversation_id: conversationId,
        message_id: messageId,
        slice_name: sliceName,
        dashboard_id: dashboardId,
        new_dashboard_title: newDashboardTitle,
        query_context: queryContext,
      },
    });
    return json as SavedChart;
  } catch (err) {
    throw await toApiError(err, t('Failed to save the chart.'));
  }
}

/** Open unsaved form data in Explore through the form-data cache. */
export async function openInExplore(
  formData: QueryFormData,
  datasetId: number,
): Promise<void> {
  // Open the tab synchronously so pop-up blockers allow it
  const tab = window.open('', '_blank');
  try {
    const { json } = await SupersetClient.post({
      endpoint: '/api/v1/explore/form_data',
      jsonPayload: {
        datasource_id: datasetId,
        datasource_type: 'table',
        form_data: JSON.stringify(formData),
      },
    });
    const url = `/explore/?form_data_key=${json.key}`;
    if (tab) tab.location.href = url;
    else window.location.href = url;
  } catch (err) {
    tab?.close();
    throw await toApiError(err, t('Could not open the chart in Explore.'));
  }
}
