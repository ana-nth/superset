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
import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Alert, App, Button, Flex, Input, List, Popconfirm, Typography } from 'antd';
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons';
import { api } from '../api';
import { DashboardSummary } from '../types';

export default function DashboardsPage() {
  const { message: toast } = App.useApp();
  const [dashboards, setDashboards] = useState<DashboardSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [title, setTitle] = useState('');

  const reload = useCallback(() => {
    api
      .dashboards()
      .then(setDashboards)
      .catch((err: Error) => setError(err.message));
  }, []);

  useEffect(reload, [reload]);

  const create = async () => {
    if (!title.trim()) return;
    try {
      await api.createDashboard(title.trim());
      setTitle('');
      reload();
    } catch (err) {
      toast.error((err as Error).message);
    }
  };

  const remove = async (id: string) => {
    try {
      await api.deleteDashboard(id);
      reload();
    } catch (err) {
      toast.error((err as Error).message);
    }
  };

  return (
    <div style={{ padding: 24, maxWidth: 960, margin: '0 auto' }}>
      <Typography.Title level={3}>Dashboards</Typography.Title>
      {error && <Alert type="error" showIcon message={error} />}
      <Flex gap={8} style={{ marginBottom: 16 }}>
        <Input
          placeholder="New dashboard name"
          value={title}
          maxLength={500}
          onChange={e => setTitle(e.target.value)}
          onPressEnter={create}
        />
        <Button type="primary" icon={<PlusOutlined />} onClick={create} disabled={!title.trim()}>
          Create
        </Button>
      </Flex>
      <List
        bordered
        loading={dashboards === null && !error}
        dataSource={dashboards ?? []}
        locale={{ emptyText: 'No dashboards yet. Save a chart from the chat to start one.' }}
        renderItem={d => (
          <List.Item
            actions={[
              <Popconfirm
                key="delete"
                title="Delete this dashboard? Its charts stay saved."
                onConfirm={() => remove(d.id)}
              >
                <Button size="small" type="text" danger icon={<DeleteOutlined />} />
              </Popconfirm>,
            ]}
          >
            <List.Item.Meta
              title={<Link to={`/dashboards/${d.id}`}>{d.title}</Link>}
              description={`${d.chart_count} chart${d.chart_count === 1 ? '' : 's'}`}
            />
          </List.Item>
        )}
      />
    </div>
  );
}
