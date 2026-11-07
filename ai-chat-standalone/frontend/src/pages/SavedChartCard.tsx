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
import { ReactNode, useCallback } from 'react';
import { Link } from 'react-router-dom';
import { App, Button, Card, Tooltip } from 'antd';
import { DownloadOutlined, ExpandOutlined } from '@ant-design/icons';
import { api } from '../api';
import ChartView from '../charts/ChartView';
import { SavedChartRecord } from '../types';

interface SavedChartCardProps {
  chart: SavedChartRecord;
  height?: number;
  /** Extra header actions (delete, remove from dashboard…) */
  actions?: ReactNode;
}

/** A saved chart, re-queried for fresh data every time it is shown. */
export default function SavedChartCard({ chart, height = 300, actions }: SavedChartCardProps) {
  const { message: toast } = App.useApp();
  const load = useCallback(() => api.chartData(chart.id), [chart.id]);
  return (
    <Card
      size="small"
      title={chart.title}
      extra={
        <>
          <Tooltip title="Open">
            <Link to={`/charts/${chart.id}`}>
              <Button size="small" type="text" icon={<ExpandOutlined />} />
            </Link>
          </Tooltip>
          <Tooltip title="Download CSV">
            <Button
              size="small"
              type="text"
              icon={<DownloadOutlined />}
              onClick={() => api.chartCsv(chart.id).catch((err: Error) => toast.error(err.message))}
            />
          </Tooltip>
          {actions}
        </>
      }
    >
      <ChartView config={chart.config} load={load} height={height} />
    </Card>
  );
}
