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
import { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Empty, Flex, Spin, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { ChartConfig, ChartData, Row } from '../types';
import EChart from './EChart';
import { formatCell, formatNumber, formatPeriod } from './format';
import { buildOption, trendSummary } from './options';

interface ChartViewProps {
  config: ChartConfig;
  /** Must be stable (useCallback): a new function reloads the data */
  load: () => Promise<ChartData>;
  height?: number;
}

/** Loads a chart's data and draws it. */
export default function ChartView({ config, load, height = 340 }: ChartViewProps) {
  const [data, setData] = useState<ChartData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError(null);
    load()
      .then(result => {
        if (!cancelled) setData(result);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message || 'Failed to load chart data.');
      });
    return () => {
      cancelled = true;
    };
  }, [load, attempt]);

  if (error) {
    return (
      <Alert
        type="error"
        showIcon
        message="The chart could not be displayed"
        description={error}
        action={
          <Button size="small" onClick={() => setAttempt(n => n + 1)}>
            Retry
          </Button>
        }
      />
    );
  }
  if (data === null) {
    return (
      <Flex align="center" justify="center" style={{ height }}>
        <Spin />
      </Flex>
    );
  }
  return <ChartBody config={config} data={data} height={height} />;
}

export function ChartBody({
  config,
  data,
  height,
}: {
  config: ChartConfig;
  data: ChartData;
  height: number;
}) {
  const option = useMemo(() => buildOption(config, data), [config, data]);
  const rows = data.rows ?? [];
  if (config.viz_type !== 'histogram' && rows.length === 0) {
    return <Empty description="No rows" style={{ padding: 24 }} />;
  }
  if (config.viz_type === 'big_number') {
    return <BigNumber config={config} rows={rows} height={height} />;
  }
  if (config.viz_type === 'big_number_trend') {
    return <BigNumberTrend config={config} rows={rows} height={height} />;
  }
  if (config.viz_type === 'table' || option === null) {
    return <DataTable config={config} rows={rows} />;
  }
  return <EChart option={option} height={height} />;
}

function BigNumber({
  config,
  rows,
  height,
}: {
  config: ChartConfig;
  rows: Row[];
  height: number;
}) {
  return (
    <Flex vertical align="center" justify="center" gap={8} style={{ minHeight: height / 2 }}>
      {config.metrics.map(metric => (
        <div key={metric.key} style={{ textAlign: 'center' }}>
          <Typography.Title level={1} style={{ margin: 0, fontSize: 56 }}>
            {formatNumber(rows[0]?.[metric.key] ?? null, metric.unit)}
          </Typography.Title>
          <Typography.Text type="secondary">
            {metric.label}
            {config.subheader ? ` · ${config.subheader}` : ''}
          </Typography.Text>
        </div>
      ))}
    </Flex>
  );
}

function BigNumberTrend({
  config,
  rows,
  height,
}: {
  config: ChartConfig;
  rows: Row[];
  height: number;
}) {
  const summary = useMemo(() => trendSummary(config, rows), [config, rows]);
  const option = useMemo(() => buildOption(config, { rows, row_count: rows.length }), [config, rows]);
  const metric = config.metrics[0];
  const change = summary.change;
  return (
    <Flex vertical gap={4}>
      <Typography.Title level={1} style={{ margin: 0, fontSize: 48 }}>
        {formatNumber(summary.latest, metric.unit)}
      </Typography.Title>
      <Typography.Text type="secondary">
        {metric.label} · {summary.period}
        {change !== null && (
          <Typography.Text type={change >= 0 ? 'success' : 'danger'}>
            {' '}
            {change >= 0 ? '▲' : '▼'} {Math.abs(change).toFixed(1)}% vs previous period
          </Typography.Text>
        )}
      </Typography.Text>
      {option && <EChart option={option} height={Math.max(height - 110, 120)} />}
    </Flex>
  );
}

export function DataTable({ config, rows }: { config: ChartConfig; rows: Row[] }) {
  const keys = config.columns ?? Object.keys(rows[0] ?? {});
  const units = new Map(config.metrics.map(m => [m.key, m.unit]));
  const columns: ColumnsType<Row> = keys.map(key => {
    const isMetric = units.has(key);
    const isTime = key === config.time_column && Boolean(config.time_grain);
    return {
      key,
      title: key,
      dataIndex: key,
      align: isMetric ? 'right' : 'left',
      sorter: (a: Row, b: Row) => {
        const x = a[key];
        const y = b[key];
        if (typeof x === 'number' && typeof y === 'number') return x - y;
        return String(x ?? '').localeCompare(String(y ?? ''));
      },
      render: (value: Row[string]) =>
        isMetric
          ? formatNumber(value, units.get(key))
          : isTime
            ? formatPeriod(value, config.time_grain)
            : formatCell(value),
    };
  });
  return (
    <Table<Row>
      size="small"
      columns={columns}
      dataSource={rows.map((row, index) => ({ ...row, __key: index }))}
      rowKey="__key"
      pagination={{ pageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
      scroll={{ x: true }}
    />
  );
}
