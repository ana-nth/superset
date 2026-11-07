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
import fetchMock from 'fetch-mock';
import {
  render,
  screen,
  userEvent,
  waitFor,
} from 'spec/helpers/testing-library';
import ChartChat from './ChartChat';

jest.mock('./ChatChartPreview', () => ({
  __esModule: true,
  default: () => <div data-test="chart-preview-mock" />,
}));

const databasesEndpoint = 'glob:*/api/v1/database/?q=*';
const schemasEndpoint = 'glob:*/api/v1/database/7/schemas/';
const catalogEndpoint = 'glob:*/api/v1/ai_chat/catalog?*';
const conversationsEndpoint = 'glob:*/api/v1/ai_chat/conversations';
const messageEndpoint = 'glob:*/api/v1/ai_chat/message';
const jobEndpoint = 'glob:*/api/v1/ai_chat/job/job-1';

const chartMessage = {
  id: 'a1',
  role: 'assistant',
  type: 'chart',
  text: 'Here is revenue by region.',
  insight: 'Top region by Total Line Amount: Northeast $245.0M (25%).',
  notes: ['No measure was named, so I used Line Amount.'],
  engine: 'llm',
  chart: {
    title: 'Revenue by Region',
    viz_type: 'echarts_timeseries_bar',
    viz_label: 'Bar Chart',
    dataset_id: 11,
    form_data: { viz_type: 'echarts_timeseries_bar', datasource: '11__table' },
    row_count: 4,
    interpretation: ['Data: Sales', 'Measure: Total Line Amount', 'By: Region'],
  },
};

beforeEach(() => {
  // The page keeps the conversation id in the URL; start each test without one
  window.history.replaceState({}, '', '/chart/chat/');
  fetchMock.reset();
  fetchMock.get(databasesEndpoint, {
    result: [
      { id: 3, database_name: 'examples' },
      { id: 7, database_name: 'warehouse' },
    ],
  });
  fetchMock.get(schemasEndpoint, { result: ['information_schema', 'wh'] });
  fetchMock.get(catalogEndpoint, {
    database_name: 'warehouse',
    schema: 'wh',
    sources: [
      {
        name: 'fact_sales',
        label: 'Sales',
        rows: 100,
        time_min: '2024-01-01',
        time_max: '2025-01-01',
        measures: ['line_amount'],
        dimensions: ['region'],
        metrics: [],
      },
    ],
    suggestions: ['Monthly line amount trend', 'Total line amount by region'],
  });
  fetchMock.get(conversationsEndpoint, { result: [] });
});

afterAll(() => fetchMock.reset());

const renderChat = () =>
  render(<ChartChat />, { useRedux: true, useRouter: true });

test('shows starter questions from the data catalog', async () => {
  renderChat();
  expect(
    await screen.findByRole('button', { name: 'Monthly line amount trend' }),
  ).toBeInTheDocument();
  expect(fetchMock.lastUrl(catalogEndpoint)).toContain('database_id=7');
  expect(fetchMock.lastUrl(catalogEndpoint)).toContain('schema=wh');
});

test('sends a message and renders the chart answer', async () => {
  fetchMock.post(messageEndpoint, {
    job_id: 'job-1',
    conversation_id: 'c1',
    user_message: {
      id: 'u1',
      role: 'user',
      type: 'text',
      text: 'revenue by region',
    },
  });
  fetchMock.get(jobEndpoint, { state: 'SUCCESS', message: chartMessage });

  renderChat();
  await screen.findByRole('button', { name: 'Monthly line amount trend' });
  userEvent.type(screen.getByLabelText('Message'), 'revenue by region');
  userEvent.click(screen.getByRole('button', { name: 'Send' }));

  await waitFor(() => expect(fetchMock.called(messageEndpoint)).toBe(true));
  expect(
    JSON.parse(fetchMock.lastOptions(messageEndpoint)?.body as string),
  ).toEqual({ message: 'revenue by region', database_id: 7, schema: 'wh' });
  expect(screen.getByText('revenue by region')).toBeInTheDocument();

  expect(
    await screen.findByText(chartMessage.insight, {}, { timeout: 4000 }),
  ).toBeInTheDocument();
  expect(screen.getByText('By: Region')).toBeInTheDocument();
  expect(screen.getByText(chartMessage.notes[0])).toBeInTheDocument();
  expect(screen.getByTestId('chart-preview-mock')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /Save/ })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /Edit in Explore/ })).toBeEnabled();
});

test('clarification options are sent as the next message', async () => {
  fetchMock.post(
    messageEndpoint,
    {
      job_id: 'job-1',
      conversation_id: 'c1',
      user_message: { id: 'u1', role: 'user', type: 'text', text: 'sales' },
    },
    { repeat: 1 },
  );
  fetchMock.get(jobEndpoint, {
    state: 'SUCCESS',
    message: {
      id: 'a1',
      role: 'assistant',
      type: 'clarification',
      text: 'Which measure did you mean?',
      options: ['Revenue by region', 'Quantity by region'],
    },
  });

  renderChat();
  await screen.findByRole('button', { name: 'Monthly line amount trend' });
  userEvent.type(screen.getByLabelText('Message'), 'sales{enter}');

  const option = await screen.findByRole(
    'button',
    { name: 'Quantity by region' },
    { timeout: 4000 },
  );
  fetchMock.post(
    messageEndpoint,
    { status: 409, body: { message: 'busy' } },
    { overwriteRoutes: false },
  );
  userEvent.click(option);
  await waitFor(() => expect(fetchMock.calls(messageEndpoint)).toHaveLength(2));
  expect(
    JSON.parse(fetchMock.lastOptions(messageEndpoint)?.body as string),
  ).toEqual({ message: 'Quantity by region', conversation_id: 'c1' });
});

test('keeps the text and shows the reason when sending is refused', async () => {
  fetchMock.post(messageEndpoint, {
    status: 429,
    body: { message: "You're sending messages too quickly. Try again in 30s." },
  });

  renderChat();
  await screen.findByRole('button', { name: 'Monthly line amount trend' });
  const box = screen.getByLabelText('Message');
  userEvent.type(box, 'revenue by region{enter}');

  expect(
    await screen.findByText(
      "You're sending messages too quickly. Try again in 30s.",
    ),
  ).toBeInTheDocument();
  expect(box).toHaveValue('revenue by region');
  expect(screen.queryByTestId('chat-user-message')).not.toBeInTheDocument();
});

test('a failed answer offers to try again', async () => {
  fetchMock.post(messageEndpoint, {
    job_id: 'job-1',
    conversation_id: 'c1',
    user_message: { id: 'u1', role: 'user', type: 'text', text: 'revenue' },
  });
  fetchMock.get(jobEndpoint, {
    state: 'SUCCESS',
    message: {
      id: 'e1',
      role: 'assistant',
      type: 'error',
      text: 'The chart query failed: timeout',
      reply_to: 'u1',
    },
  });

  renderChat();
  await screen.findByRole('button', { name: 'Monthly line amount trend' });
  userEvent.type(screen.getByLabelText('Message'), 'revenue{enter}');

  expect(
    await screen.findByText(
      'The chart query failed: timeout',
      {},
      { timeout: 4000 },
    ),
  ).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled();
});

test('shows the catalog error with a retry when the schema cannot be read', async () => {
  fetchMock.get(
    catalogEndpoint,
    { status: 400, body: { message: "Schema 'wh' has no tables" } },
    { overwriteRoutes: true },
  );
  renderChat();
  expect(
    await screen.findByText("Schema 'wh' has no tables"),
  ).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
});
