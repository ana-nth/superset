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
import { Alert, App, Button, Card, Flex, Spin, Tag, theme, Typography } from 'antd';
import { ConsoleSqlOutlined, DownloadOutlined } from '@ant-design/icons';
import { api } from '../api';
import ChartView from '../charts/ChartView';
import { SavedChartRecord } from '../types';

export default function ChartPage() {
  const { id = '' } = useParams();
  const { token } = theme.useToken();
  const { message: toast } = App.useApp();
  const [chart, setChart] = useState<SavedChartRecord | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sql, setSql] = useState<string | null>(null);

  useEffect(() => {
    setChart(null);
    setError(null);
    setSql(null);
    api
      .chart(id)
      .then(setChart)
      .catch((err: Error) => setError(err.message));
  }, [id]);

  const load = useCallback(() => api.chartData(id), [id]);

  if (error) return <Alert style={{ margin: 24 }} type="error" showIcon message={error} />;
  if (!chart) return <Spin style={{ margin: 24 }} />;

  return (
    <div style={{ padding: 24, maxWidth: 1200, margin: '0 auto' }}>
      <Card
        title={chart.title}
        extra={
          <Flex gap={8}>
            <Button
              icon={<ConsoleSqlOutlined />}
              onClick={() =>
                api
                  .chartSql(id)
                  .then(setSql)
                  .catch((err: Error) => toast.error(err.message))
              }
            >
              SQL
            </Button>
            <Button
              icon={<DownloadOutlined />}
              onClick={() => api.chartCsv(id).catch((err: Error) => toast.error(err.message))}
            >
              CSV
            </Button>
          </Flex>
        }
      >
        <Flex wrap gap={4} style={{ marginBottom: 12 }}>
          <Tag>{chart.schema}</Tag>
          {chart.dashboards.map(d => (
            <Link key={d.id} to={`/dashboards/${d.id}`}>
              <Tag color="blue">{d.title}</Tag>
            </Link>
          ))}
        </Flex>
        <ChartView config={chart.config} load={load} height={480} />
        {sql && (
          <pre
            style={{
              marginTop: 16,
              padding: 12,
              background: token.colorFillTertiary,
              borderRadius: token.borderRadius,
              whiteSpace: 'pre-wrap',
              fontSize: token.fontSizeSM,
            }}
          >
            {sql}
          </pre>
        )}
      </Card>
      <Typography.Paragraph type="secondary" style={{ marginTop: 12 }}>
        Saved charts are re-queried every time they are opened.
      </Typography.Paragraph>
    </div>
  );
}
