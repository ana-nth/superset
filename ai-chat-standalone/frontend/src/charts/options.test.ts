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
import { describe, expect, it } from 'vitest';
import { ChartConfig } from '../types';
import { formatNumber, formatPeriod } from './format';
import { buildOption, trendSummary } from './options';

const revenue = { key: 'Total Revenue', label: 'Total Revenue', unit: 'USD' };

function config(overrides: Partial<ChartConfig>): ChartConfig {
  return {
    viz_type: 'bar',
    metrics: [revenue],
    dimensions: [],
    time_column: null,
    time_grain: null,
    subheader: '',
    sort_desc: true,
    ...overrides,
  };
}

interface Series {
  name: string;
  type: string;
  data: unknown[];
  stack?: string;
}

describe('formatting', () => {
  it('formats numbers with units', () => {
    expect(formatNumber(1234567, 'USD')).toBe('$1.2M');
    expect(formatNumber(-2500, 'USD')).toBe('-$2.5K');
    expect(formatNumber(12.345, '%')).toBe('12.3%');
    expect(formatNumber(42, 'units')).toBe('42 units');
    expect(formatNumber(null)).toBe('–');
  });

  it('formats periods in UTC for their grain', () => {
    expect(formatPeriod('2025-07-01', 'P3M')).toBe('Q3 2025');
    expect(formatPeriod('2025-03-01T00:00:00', 'P1M')).toBe('Mar 2025');
    expect(formatPeriod('2025-03-01 00:00:00+00:00', 'P1Y')).toBe('2025');
  });
});

describe('buildOption', () => {
  it('draws one bar series per metric in query order', () => {
    const option = buildOption(
      config({ x: 'region', dimensions: ['region'], show_values: true }),
      {
        rows: [
          { region: 'West', 'Total Revenue': 300 },
          { region: 'East', 'Total Revenue': 100 },
        ],
        row_count: 2,
      },
    ) as { xAxis: { data: string[] }; series: Series[] };
    expect(option.xAxis.data).toEqual(['West', 'East']);
    expect(option.series).toHaveLength(1);
    expect(option.series[0].data).toEqual([300, 100]);
  });

  it('pivots a time series by its breakdown and sorts periods', () => {
    const option = buildOption(
      config({
        viz_type: 'area',
        x: 'order_date',
        x_is_time: true,
        series: 'channel',
        stack: true,
        time_column: 'order_date',
        time_grain: 'P1M',
        dimensions: ['channel'],
      }),
      {
        rows: [
          { order_date: '2025-02-01', channel: 'Online', 'Total Revenue': 5 },
          { order_date: '2025-01-01', channel: 'Online', 'Total Revenue': 3 },
          { order_date: '2025-01-01', channel: 'Retail', 'Total Revenue': 7 },
        ],
        row_count: 3,
      },
    ) as { xAxis: { data: string[] }; series: Series[] };
    expect(option.xAxis.data).toEqual(['Jan 2025', 'Feb 2025']);
    const byName = Object.fromEntries(option.series.map(s => [s.name, s]));
    expect(byName.Online.data).toEqual([3, 5]);
    expect(byName.Retail.data).toEqual([7, null]); // no Retail row in February
    expect(byName.Retail.stack).toBe('total');
  });

  it('nests a two-level treemap', () => {
    const option = buildOption(
      config({ viz_type: 'treemap', categories: ['cat', 'sub'], dimensions: ['cat', 'sub'] }),
      {
        rows: [
          { cat: 'A', sub: 'a1', 'Total Revenue': 1 },
          { cat: 'A', sub: 'a2', 'Total Revenue': 2 },
          { cat: 'B', sub: 'b1', 'Total Revenue': 4 },
        ],
        row_count: 3,
      },
    ) as { series: { data: { name: string; value: number; children: unknown[] }[] }[] };
    const [a, b] = option.series[0].data;
    expect(a).toMatchObject({ name: 'A', value: 3 });
    expect(a.children).toHaveLength(2);
    expect(b).toMatchObject({ name: 'B', value: 4 });
  });

  it('stacks histogram series over the server bins', () => {
    const option = buildOption(config({ viz_type: 'histogram', column_label: 'Price' }), {
      histogram: {
        bins: [
          { start: 0, end: 5 },
          { start: 5, end: 10 },
        ],
        series: [
          { name: 'Online', counts: [1, 2] },
          { name: 'Retail', counts: [3, 0] },
        ],
      },
      row_count: 6,
    }) as { xAxis: { data: string[] }; series: Series[] };
    expect(option.xAxis.data).toEqual(['0–5', '5–10']);
    expect(option.series.map(s => s.data)).toEqual([
      [1, 2],
      [3, 0],
    ]);
  });

  it('leaves tables and big numbers to React', () => {
    expect(buildOption(config({ viz_type: 'table' }), { rows: [], row_count: 0 })).toBeNull();
    expect(buildOption(config({ viz_type: 'big_number' }), { rows: [], row_count: 0 })).toBeNull();
  });
});

describe('trendSummary', () => {
  it('compares the latest period with the previous one', () => {
    const summary = trendSummary(
      config({ viz_type: 'big_number_trend', x: 'd', time_grain: 'P1M' }),
      [
        { d: '2025-02-01', 'Total Revenue': 150 },
        { d: '2025-01-01', 'Total Revenue': 100 },
      ],
    );
    expect(summary.latest).toBe(150);
    expect(summary.change).toBeCloseTo(50);
    expect(summary.period).toBe('Feb 2025');
  });
});
