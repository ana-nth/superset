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
import rison from 'rison';
import { SupersetClient, t } from '@superset-ui/core';
import {
  Alert,
  AsyncSelect,
  Button,
  Form,
  FormItem,
  Input,
  Modal,
} from '@superset-ui/core/components';
import { saveChart } from './api';
import { ChatChart, SavedChart } from './types';

interface DashboardOption {
  value: number | string;
  label: string;
}

interface SaveChartModalProps {
  show: boolean;
  chart: ChatChart;
  conversationId: string;
  messageId: string;
  userId?: number;
  onHide: () => void;
  onSaved: (result: SavedChart) => void;
}

export default function SaveChartModal({
  show,
  chart,
  conversationId,
  messageId,
  userId,
  onHide,
  onSaved,
}: SaveChartModalProps) {
  const [sliceName, setSliceName] = useState(chart.saved_title || chart.title);
  const [dashboard, setDashboard] = useState<DashboardOption | undefined>();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (show) {
      setSliceName(chart.saved_title || chart.title);
      setDashboard(undefined);
      setError(null);
    }
  }, [show, chart]);

  const loadDashboards = useCallback(
    async (search: string, page: number, pageSize: number) => {
      const filters: { col: string; opr: string; value: unknown }[] = [
        { col: 'dashboard_title', opr: 'ct', value: search },
      ];
      if (userId)
        filters.push({ col: 'owners', opr: 'rel_m_m', value: userId });
      const query = rison.encode({
        columns: ['id', 'dashboard_title'],
        filters,
        page,
        page_size: pageSize,
        order_column: 'dashboard_title',
      });
      const { json } = await SupersetClient.get({
        endpoint: `/api/v1/dashboard/?q=${query}`,
      });
      return {
        data: (
          (json.result || []) as { id: number; dashboard_title: string }[]
        ).map(d => ({ value: d.id, label: d.dashboard_title })),
        totalCount: json.count as number,
      };
    },
    [userId],
  );

  const handleSave = async () => {
    const name = sliceName.trim();
    if (!name) {
      setError(t('Please enter a chart name.'));
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const result = await saveChart({
        conversationId,
        messageId,
        formData: chart.form_data,
        sliceName: name,
        dashboardId:
          typeof dashboard?.value === 'number' ? dashboard.value : undefined,
        newDashboardTitle:
          typeof dashboard?.value === 'string' ? dashboard.value : undefined,
      });
      onSaved(result);
      onHide();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      show={show}
      onHide={onHide}
      name="chat-save-chart"
      title={chart.saved_chart_id ? t('Add to dashboard') : t('Save chart')}
      footer={
        <>
          <Button buttonStyle="secondary" onClick={onHide}>
            {t('Cancel')}
          </Button>
          <Button
            buttonStyle="primary"
            onClick={handleSave}
            loading={saving}
            data-test="chat-save-confirm"
          >
            {t('Save')}
          </Button>
        </>
      }
    >
      <Form layout="vertical">
        <FormItem label={t('Chart name')} required>
          <Input
            aria-label={t('Chart name')}
            value={sliceName}
            maxLength={250}
            disabled={Boolean(chart.saved_chart_id)}
            onChange={e => setSliceName(e.target.value)}
          />
        </FormItem>
        <FormItem label={t('Add to dashboard (optional)')}>
          <AsyncSelect
            allowClear
            allowNewOptions
            ariaLabel={t('Select a dashboard')}
            options={loadDashboards}
            value={dashboard}
            onChange={(value: unknown) =>
              setDashboard(value as DashboardOption | undefined)
            }
            labelInValue
            placeholder={t('Select a dashboard or type a new name')}
          />
        </FormItem>
        {error && <Alert type="error" showIcon message={error} />}
      </Form>
    </Modal>
  );
}
