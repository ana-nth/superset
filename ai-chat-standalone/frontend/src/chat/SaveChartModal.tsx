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
import { useEffect, useState } from 'react';
import { Alert, Form, Input, Modal, Select } from 'antd';
import { api } from '../api';
import { ChatChart, DashboardSummary, SaveResult } from '../types';

interface SaveChartModalProps {
  chart: ChatChart;
  conversationId: string;
  messageId: string;
  onClose: () => void;
  onSaved: (result: SaveResult) => void;
}

export default function SaveChartModal({
  chart,
  conversationId,
  messageId,
  onClose,
  onSaved,
}: SaveChartModalProps) {
  const [title, setTitle] = useState(chart.saved_title || chart.title);
  const [dashboards, setDashboards] = useState<DashboardSummary[]>([]);
  const [dashboardId, setDashboardId] = useState<string | undefined>();
  const [newDashboard, setNewDashboard] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .dashboards()
      .then(setDashboards)
      .catch(() => setDashboards([]));
  }, []);

  const save = async () => {
    if (!title.trim()) {
      setError('Please enter a chart name.');
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const result = await api.saveChart({
        conversation_id: conversationId,
        message_id: messageId,
        title: title.trim(),
        dashboard_id: dashboardId,
        new_dashboard_title: dashboardId ? undefined : newDashboard.trim() || undefined,
      });
      onSaved(result);
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open
      title={chart.saved_chart_id ? 'Add to dashboard' : 'Save chart'}
      okText="Save"
      onOk={save}
      confirmLoading={saving}
      onCancel={onClose}
      destroyOnClose
    >
      <Form layout="vertical">
        <Form.Item label="Chart name" required>
          <Input
            value={title}
            maxLength={250}
            disabled={Boolean(chart.saved_chart_id)}
            onChange={e => setTitle(e.target.value)}
          />
        </Form.Item>
        <Form.Item label="Add to an existing dashboard (optional)">
          <Select
            allowClear
            showSearch
            optionFilterProp="label"
            placeholder="Select a dashboard"
            value={dashboardId}
            onChange={value => setDashboardId(value)}
            options={dashboards.map(d => ({ value: d.id, label: d.title }))}
          />
        </Form.Item>
        <Form.Item label="…or create a new dashboard">
          <Input
            placeholder="New dashboard name"
            value={newDashboard}
            maxLength={500}
            disabled={Boolean(dashboardId)}
            onChange={e => setNewDashboard(e.target.value)}
          />
        </Form.Item>
        {error && <Alert type="error" showIcon message={error} />}
      </Form>
    </Modal>
  );
}
