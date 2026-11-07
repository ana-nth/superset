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
import { Link, useParams } from 'react-router-dom';
import { Alert, App, Button, Col, Empty, Popconfirm, Row, Spin, Tooltip, Typography } from 'antd';
import { MinusCircleOutlined } from '@ant-design/icons';
import { api } from '../api';
import { DashboardDetail } from '../types';
import SavedChartCard from './SavedChartCard';

export default function DashboardPage() {
  const { id = '' } = useParams();
  const { message: toast } = App.useApp();
  const [dashboard, setDashboard] = useState<DashboardDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(() => {
    api
      .dashboard(id)
      .then(setDashboard)
      .catch((err: Error) => setError(err.message));
  }, [id]);

  useEffect(reload, [reload]);

  const rename = async (title: string) => {
    if (!title.trim() || !dashboard || title === dashboard.title) return;
    try {
      await api.renameDashboard(id, title.trim());
      setDashboard({ ...dashboard, title: title.trim() });
    } catch (err) {
      toast.error((err as Error).message);
    }
  };

  const removeChart = async (chartId: string) => {
    try {
      await api.removeFromDashboard(id, chartId);
      reload();
    } catch (err) {
      toast.error((err as Error).message);
    }
  };

  if (error) return <Alert style={{ margin: 24 }} type="error" showIcon message={error} />;
  if (!dashboard) return <Spin style={{ margin: 24 }} />;

  return (
    <div style={{ padding: 24 }}>
      <Typography.Title level={3} editable={{ onChange: rename, tooltip: 'Rename' }}>
        {dashboard.title}
      </Typography.Title>
      {dashboard.charts.length === 0 && (
        <Empty description="This dashboard has no charts yet">
          <Link to="/">
            <Button type="primary">Ask for a chart</Button>
          </Link>
        </Empty>
      )}
      <Row gutter={[16, 16]}>
        {dashboard.charts.map(chart => (
          <Col key={chart.id} xs={24} xl={12}>
            <SavedChartCard
              chart={chart}
              actions={
                <Popconfirm
                  title="Remove from this dashboard?"
                  onConfirm={() => removeChart(chart.id)}
                >
                  <Tooltip title="Remove from dashboard">
                    <Button size="small" type="text" icon={<MinusCircleOutlined />} />
                  </Tooltip>
                </Popconfirm>
              }
            />
          </Col>
        ))}
      </Row>
    </div>
  );
}
