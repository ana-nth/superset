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
import AutoDashboardModal from './AutoDashboardModal';

const databasesEndpoint = 'glob:*/api/v1/database/?q=*';
const schemasEndpoint = 'glob:*/api/v1/database/7/schemas/';
const generateEndpoint = 'glob:*/api/v1/ai_dashboard/generate';
const statusEndpoint = 'glob:*/api/v1/ai_dashboard/status/task-1';

beforeEach(() => {
  fetchMock.reset();
  fetchMock.get(databasesEndpoint, {
    result: [
      { id: 3, database_name: 'examples' },
      { id: 7, database_name: 'warehouse' },
    ],
  });
  fetchMock.get(schemasEndpoint, { result: ['information_schema', 'wh'] });
});

afterAll(() => fetchMock.reset());

test('preselects the warehouse database and wh schema', async () => {
  render(<AutoDashboardModal show onHide={jest.fn()} />, { useRedux: true });
  await waitFor(() => expect(fetchMock.called(schemasEndpoint)).toBe(true));
  expect(await screen.findByText('warehouse')).toBeInTheDocument();
  expect(await screen.findByText('wh')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Generate' })).toBeEnabled();
});

test('generates a dashboard and shows insights on success', async () => {
  fetchMock.post(generateEndpoint, { task_id: 'task-1' });
  fetchMock.get(statusEndpoint, {
    state: 'SUCCESS',
    progress: 100,
    dashboard_id: 42,
    charts_created: 12,
    summary: '- Revenue is concentrated',
    insights: [{ title: 'Pareto', text: 'Top 20% = 78%', severity: 'warning' }],
  });

  render(<AutoDashboardModal show onHide={jest.fn()} />, { useRedux: true });
  await waitFor(() => expect(fetchMock.called(schemasEndpoint)).toBe(true));
  await screen.findByText('wh');
  userEvent.click(screen.getByRole('button', { name: 'Generate' }));

  await waitFor(() => expect(fetchMock.called(generateEndpoint)).toBe(true));
  expect(
    JSON.parse(fetchMock.lastOptions(generateEndpoint)?.body as string),
  ).toEqual({ database_id: 7, schema: 'wh' });
  expect(
    await screen.findByText(/Dashboard created/, {}, { timeout: 5000 }),
  ).toBeInTheDocument();
  expect(screen.getByText('Top 20% = 78%')).toBeInTheDocument();
  expect(
    screen.getByRole('button', { name: 'Open dashboard' }),
  ).toBeInTheDocument();
});

test('shows the server error when generation fails', async () => {
  fetchMock.post(generateEndpoint, { task_id: 'task-1' });
  fetchMock.get(statusEndpoint, {
    state: 'FAILURE',
    error: 'Schema has no tables',
  });

  render(<AutoDashboardModal show onHide={jest.fn()} />, { useRedux: true });
  await waitFor(() => expect(fetchMock.called(schemasEndpoint)).toBe(true));
  await screen.findByText('wh');
  userEvent.click(screen.getByRole('button', { name: 'Generate' }));

  expect(
    await screen.findByText('Schema has no tables', {}, { timeout: 5000 }),
  ).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
});
