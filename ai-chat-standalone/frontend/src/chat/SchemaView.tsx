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
import { Collapse, Descriptions, Flex, Table, Tag, Typography } from 'antd';
import { SchemaSource } from '../types';

const AGG_WORDS: Record<string, string> = { sum: 'summed', avg: 'averaged' };

function SourceDetails({ source }: { source: SchemaSource }) {
  const [fact, ...joined] = source.tables;
  return (
    <Flex vertical gap={12}>
      {source.description && <Typography.Text>{source.description}</Typography.Text>}
      <Descriptions size="small" column={1} bordered labelStyle={{ width: 140 }}>
        {source.grain && (
          <Descriptions.Item label="One row is">{source.grain}</Descriptions.Item>
        )}
        <Descriptions.Item label="Rows">{source.rows.toLocaleString()}</Descriptions.Item>
        <Descriptions.Item label="Tables">
          <Flex wrap gap={4} align="center">
            <Tag color="blue">{fact}</Tag>
            {joined.length > 0 && <Typography.Text type="secondary">joined to</Typography.Text>}
            {joined.map(t => (
              <Tag key={t}>{t}</Tag>
            ))}
          </Flex>
        </Descriptions.Item>
        {source.time_column && (
          <Descriptions.Item label="Dates">
            {source.time_column}: {source.time_min} → {source.time_max}
            {source.time_columns.length > 1 &&
              ` (also ${source.time_columns.filter(c => c !== source.time_column).join(', ')})`}
          </Descriptions.Item>
        )}
      </Descriptions>

      {source.measures.length > 0 && (
        <Table
          size="small"
          pagination={false}
          rowKey="name"
          title={() => <Typography.Text strong>Measures (numbers you can total or average)</Typography.Text>}
          dataSource={source.measures}
          columns={[
            {
              title: 'Measure',
              key: 'name',
              render: (_, m) => (
                <>
                  {m.label} <Typography.Text type="secondary">({m.name})</Typography.Text>
                </>
              ),
            },
            { title: 'Meaning', dataIndex: 'description', render: (d: string) => d || '–' },
            { title: 'Unit', dataIndex: 'unit', render: (u: string) => u || '–' },
            { title: 'Default', dataIndex: 'agg', render: (a: string) => AGG_WORDS[a] ?? a },
          ]}
        />
      )}

      {source.dimensions.length > 0 && (
        <Table
          size="small"
          pagination={false}
          rowKey="name"
          title={() => <Typography.Text strong>Breakdowns (ways to slice and filter)</Typography.Text>}
          dataSource={source.dimensions}
          columns={[
            {
              title: 'Breakdown',
              key: 'name',
              render: (_, d) => (
                <>
                  {d.label} <Typography.Text type="secondary">({d.name})</Typography.Text>
                  {d.description && (
                    <div>
                      <Typography.Text type="secondary">{d.description}</Typography.Text>
                    </div>
                  )}
                </>
              ),
            },
            { title: 'Distinct values', dataIndex: 'cardinality', width: 120 },
            {
              title: 'Examples',
              key: 'values',
              render: (_, d) => (
                <Flex wrap gap={4}>
                  {d.values.map(v => (
                    <Tag key={v}>{v}</Tag>
                  ))}
                  {d.cardinality > d.values.length && d.values.length > 0 && '…'}
                </Flex>
              ),
            },
          ]}
        />
      )}

      {source.metrics.length > 0 && (
        <Table
          size="small"
          pagination={false}
          rowKey="name"
          title={() => <Typography.Text strong>Business metrics</Typography.Text>}
          dataSource={source.metrics}
          columns={[
            { title: 'Metric', dataIndex: 'label' },
            { title: 'Meaning', dataIndex: 'meaning', render: (d: string) => d || '–' },
            { title: 'Unit', dataIndex: 'unit', render: (u: string) => u || '–' },
          ]}
        />
      )}
    </Flex>
  );
}

/** The data sources of a schema with their tables, columns and metrics. */
export default function SchemaView({ sources }: { sources: SchemaSource[] }) {
  return (
    <Collapse
      size="small"
      style={{ marginTop: 12 }}
      data-testid="chat-schema"
      items={sources.map(source => ({
        key: source.name,
        label: (
          <Flex wrap gap={8} align="center">
            <Typography.Text strong>{source.label}</Typography.Text>
            <Typography.Text type="secondary">
              {source.name} · {source.rows.toLocaleString()} rows
              {source.time_min && ` · ${source.time_min} → ${source.time_max}`}
            </Typography.Text>
          </Flex>
        ),
        children: <SourceDetails source={source} />,
      }))}
    />
  );
}
