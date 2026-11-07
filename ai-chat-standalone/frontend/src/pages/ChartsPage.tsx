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
import { Alert, App, Button, Col, Empty, Popconfirm, Row, Spin, Typography } from 'antd';
import { DeleteOutlined } from '@ant-design/icons';
import { api } from '../api';
import { SavedChartRecord } from '../types';
import SavedChartCard from './SavedChartCard';

export default function ChartsPage() {
  const { message: toast } = App.useApp();
  const [charts, setCharts] = useState<SavedChartRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(() => {
    api
      .charts()
      .then(setCharts)
      .catch((err: Error) => setError(err.message));
  }, []);

  useEffect(reload, [reload]);

  const remove = async (id: string) => {
    try {
      await api.deleteChart(id);
      reload();
    } catch (err) {
      toast.error((err as Error).message);
    }
  };

  return (
    <div style={{ padding: 24 }}>
      <Typography.Title level={3}>Saved charts</Typography.Title>
      {error && <Alert type="error" showIcon message={error} />}
      {charts === null && !error && <Spin />}
      {charts?.length === 0 && (
        <Empty description="No saved charts yet">
          <Link to="/">
            <Button type="primary">Ask for a chart</Button>
          </Link>
        </Empty>
      )}
      <Row gutter={[16, 16]}>
        {charts?.map(chart => (
          <Col key={chart.id} xs={24} lg={12}>
            <SavedChartCard
              chart={chart}
              actions={
                <Popconfirm title="Delete this chart?" onConfirm={() => remove(chart.id)}>
                  <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                </Popconfirm>
              }
            />
          </Col>
        ))}
      </Row>
    </div>
  );
}
