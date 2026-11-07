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
/**
 * ECharts options for each chart type, built from the render config the
 * backend sends and the rows of the chart query. Pure functions: no DOM.
 */
import type { EChartsCoreOption } from 'echarts/core';
import { Cell, ChartConfig, ChartData, MetricRef, Row } from '../types';
import { formatNumber, formatPeriod, toNumber } from './format';

interface TooltipParam {
  name: string;
  value: unknown;
  seriesName?: string;
}

const label = (value: Cell): string =>
  value === null || value === '' ? '(empty)' : String(value);

function unique(values: Cell[]): Cell[] {
  const seen = new Set<string>();
  const out: Cell[] = [];
  for (const value of values) {
    const key = String(value);
    if (!seen.has(key)) {
      seen.add(key);
      out.push(value);
    }
  }
  return out;
}

const byText = (a: Cell, b: Cell) => String(a).localeCompare(String(b));

function axisLabels(cfg: ChartConfig, values: Cell[], isTime: boolean) {
  return values.map(v => (isTime ? formatPeriod(v, cfg.time_grain) : label(v)));
}

const firstUnit = (cfg: ChartConfig) => cfg.metrics[0]?.unit ?? '';

function xyOption(cfg: ChartConfig, rows: Row[]): EChartsCoreOption {
  const x = cfg.x ?? cfg.dimensions[0];
  const isTime = Boolean(cfg.x_is_time);
  let xs = unique(rows.map(r => r[x]));
  // Time axes sort chronologically (ISO strings sort as text); categories keep
  // the query's order, which ranks them by the first metric
  if (isTime) xs = [...xs].sort(byText);
  const position = new Map(xs.map((v, i) => [String(v), i]));
  const kind = cfg.viz_type === 'bar' ? 'bar' : 'line';
  const unit = firstUnit(cfg);
  const base = (name: string) => ({
    name,
    type: kind,
    stack: cfg.stack ? 'total' : undefined,
    areaStyle: cfg.viz_type === 'area' ? { opacity: 0.25 } : undefined,
    showSymbol: kind === 'line' && xs.length <= 60,
    emphasis: { focus: 'series' },
  });

  const series: Record<string, unknown>[] = [];
  if (cfg.series) {
    const split = cfg.series;
    const metric = cfg.metrics[0];
    for (const name of unique(rows.map(r => label(r[split])))) {
      const data: (number | null)[] = new Array(xs.length).fill(null);
      for (const row of rows) {
        if (label(row[split]) === name) {
          data[position.get(String(row[x])) ?? 0] = toNumber(row[metric.key]);
        }
      }
      series.push({ ...base(String(name)), data });
    }
  } else {
    const byX = new Map(rows.map(r => [String(r[x]), r]));
    for (const metric of cfg.metrics) {
      series.push({
        ...base(metric.label),
        data: xs.map(v => toNumber(byX.get(String(v))?.[metric.key] ?? null)),
        label: cfg.show_values
          ? {
              show: true,
              position: 'top',
              formatter: (p: TooltipParam) => formatNumber(p.value, metric.unit),
            }
          : undefined,
      });
    }
  }
  const legend = series.length > 1;
  return {
    tooltip: {
      trigger: 'axis',
      valueFormatter: (v: unknown) => formatNumber(v, unit),
    },
    legend: legend ? { type: 'scroll', top: 0 } : undefined,
    grid: { left: 8, right: 24, top: legend ? 40 : 24, bottom: 8, containLabel: true },
    xAxis: {
      type: 'category',
      data: axisLabels(cfg, xs, isTime),
      boundaryGap: kind === 'bar',
      axisLabel: { rotate: cfg.label_rotate ?? 0, hideOverlap: true },
    },
    yAxis: {
      type: 'value',
      axisLabel: { formatter: (v: number) => formatNumber(v, unit) },
    },
    series,
  };
}

function sliceData(cfg: ChartConfig, rows: Row[]) {
  const category = cfg.categories?.[0] ?? cfg.dimensions[0];
  const metric = cfg.metrics[0];
  return rows.map(r => ({
    name: label(r[category]),
    value: toNumber(r[metric.key]) ?? 0,
  }));
}

function pieOption(cfg: ChartConfig, rows: Row[]): EChartsCoreOption {
  const unit = firstUnit(cfg);
  return {
    tooltip: {
      trigger: 'item',
      valueFormatter: (v: unknown) => formatNumber(v, unit),
    },
    legend: { type: 'scroll', orient: 'vertical', right: 0, top: 'middle' },
    series: [
      {
        type: 'pie',
        radius: ['40%', '70%'],
        center: ['40%', '50%'],
        avoidLabelOverlap: true,
        label: { formatter: '{b}: {d}%' },
        data: sliceData(cfg, rows),
      },
    ],
  };
}

function funnelOption(cfg: ChartConfig, rows: Row[]): EChartsCoreOption {
  const unit = firstUnit(cfg);
  return {
    tooltip: {
      trigger: 'item',
      valueFormatter: (v: unknown) => formatNumber(v, unit),
    },
    legend: { type: 'scroll', top: 0 },
    series: [
      {
        type: 'funnel',
        left: '10%',
        width: '80%',
        top: 40,
        bottom: 8,
        sort: 'descending',
        label: { position: 'inside', formatter: '{b}' },
        data: sliceData(cfg, rows),
      },
    ],
  };
}

interface TreeNode {
  name: string;
  value: number;
  children?: TreeNode[];
}

function treemapOption(cfg: ChartConfig, rows: Row[]): EChartsCoreOption {
  const categories = cfg.categories ?? cfg.dimensions;
  const metric = cfg.metrics[0];
  let data: TreeNode[];
  if (categories.length > 1) {
    const groups = new Map<string, TreeNode>();
    for (const row of rows) {
      const parent = label(row[categories[0]]);
      const value = toNumber(row[metric.key]) ?? 0;
      const node = groups.get(parent) ?? { name: parent, value: 0, children: [] };
      node.value += value;
      node.children!.push({ name: label(row[categories[1]]), value });
      groups.set(parent, node);
    }
    data = [...groups.values()];
  } else {
    data = sliceData(cfg, rows);
  }
  return {
    tooltip: {
      formatter: (p: TooltipParam) =>
        `${p.name}: ${formatNumber(p.value, metric.unit)}`,
    },
    series: [
      {
        type: 'treemap',
        roam: false,
        nodeClick: false,
        breadcrumb: { show: false },
        width: '100%',
        height: '100%',
        upperLabel: { show: categories.length > 1, height: 20 },
        label: { show: true, formatter: '{b}' },
        data,
      },
    ],
  };
}

function heatmapOption(cfg: ChartConfig, rows: Row[]): EChartsCoreOption {
  const x = cfg.x ?? cfg.dimensions[0];
  const y = cfg.y ?? cfg.dimensions[1];
  const metric = cfg.metrics[0];
  const isTime = Boolean(cfg.x_is_time);
  const xs = unique(rows.map(r => r[x])).sort(byText);
  const ys = unique(rows.map(r => r[y])).sort(byText);
  const xi = new Map(xs.map((v, i) => [String(v), i]));
  const yi = new Map(ys.map((v, i) => [String(v), i]));
  const data = rows.map(r => [
    xi.get(String(r[x])) ?? 0,
    yi.get(String(r[y])) ?? 0,
    toNumber(r[metric.key]),
  ]);
  const values = data
    .map(d => d[2])
    .filter((v): v is number => v !== null);
  const xLabels = axisLabels(cfg, xs, isTime);
  const yLabels = ys.map(label);
  return {
    tooltip: {
      position: 'top',
      formatter: (p: TooltipParam) => {
        const [cx, cy, value] = p.value as [number, number, number | null];
        return `${xLabels[cx]} × ${yLabels[cy]}: ${formatNumber(value, metric.unit)}`;
      },
    },
    grid: { left: 8, right: 24, top: 16, bottom: 64, containLabel: true },
    xAxis: {
      type: 'category',
      data: xLabels,
      splitArea: { show: true },
      axisLabel: { rotate: xs.length > 8 ? 45 : 0, hideOverlap: true },
    },
    yAxis: { type: 'category', data: yLabels, splitArea: { show: true } },
    visualMap: {
      min: values.length ? Math.min(...values) : 0,
      max: values.length ? Math.max(...values) : 1,
      calculable: true,
      orient: 'horizontal',
      left: 'center',
      bottom: 0,
      formatter: (v: number) => formatNumber(v, metric.unit),
    },
    series: [
      {
        type: 'heatmap',
        data,
        label: {
          show: data.length <= 150,
          formatter: (p: TooltipParam) =>
            formatNumber((p.value as unknown[])[2], metric.unit),
        },
      },
    ],
  };
}

function histogramOption(cfg: ChartConfig, data: ChartData): EChartsCoreOption {
  const histogram = data.histogram ?? { bins: [], series: [] };
  const categories = histogram.bins.map(
    b => `${formatNumber(b.start)}–${formatNumber(b.end)}`,
  );
  const legend = histogram.series.length > 1;
  return {
    tooltip: { trigger: 'axis' },
    legend: legend ? { type: 'scroll', top: 0 } : undefined,
    grid: { left: 8, right: 24, top: legend ? 40 : 24, bottom: 24, containLabel: true },
    xAxis: {
      type: 'category',
      data: categories,
      name: cfg.column_label,
      nameLocation: 'middle',
      nameGap: 48,
      axisLabel: { rotate: 45, hideOverlap: true },
    },
    yAxis: { type: 'value', name: 'Rows' },
    series: histogram.series.map(s => ({
      name: s.name,
      type: 'bar',
      stack: 'total',
      barCategoryGap: '4%',
      data: s.counts,
    })),
  };
}

function bubbleOption(cfg: ChartConfig, rows: Row[]): EChartsCoreOption {
  const entity = cfg.entity ?? cfg.dimensions[0];
  const xm = cfg.x_metric ?? cfg.metrics[0];
  const ym = cfg.y_metric ?? cfg.metrics[1] ?? cfg.metrics[0];
  const sm: MetricRef = cfg.size_metric ?? { key: '', label: '', unit: '' };
  const points = rows.map(r => [
    toNumber(r[xm.key]),
    toNumber(r[ym.key]),
    toNumber(r[sm.key]) ?? 0,
    label(r[entity]),
  ]);
  const maxSize = Math.max(1, ...points.map(p => Math.abs(p[2] as number)));
  const axis = (metric: MetricRef) => ({
    type: 'value',
    name: metric.label,
    nameLocation: 'middle',
    nameGap: 32,
    scale: true,
    axisLabel: { formatter: (v: number) => formatNumber(v, metric.unit) },
  });
  return {
    tooltip: {
      formatter: (p: TooltipParam) => {
        const [vx, vy, size, name] = p.value as [number, number, number, string];
        return [
          `<b>${name}</b>`,
          `${xm.label}: ${formatNumber(vx, xm.unit)}`,
          `${ym.label}: ${formatNumber(vy, ym.unit)}`,
          `${sm.label}: ${formatNumber(size, sm.unit)}`,
        ].join('<br/>');
      },
    },
    grid: { left: 16, right: 24, top: 24, bottom: 32, containLabel: true },
    xAxis: axis(xm),
    yAxis: axis(ym),
    series: [
      {
        type: 'scatter',
        data: points,
        symbolSize: (d: number[]) => 8 + 40 * Math.sqrt(Math.abs(d[2]) / maxSize),
        itemStyle: { opacity: 0.65 },
        label: {
          show: points.length <= 25,
          position: 'top',
          formatter: (p: TooltipParam) => String((p.value as unknown[])[3]),
        },
      },
    ],
  };
}

export interface TrendSummary {
  latest: number | null;
  previous: number | null;
  change: number | null;
  period: string;
  points: { label: string; value: number | null }[];
}

/** Latest value and change on the previous period of a big number trend. */
export function trendSummary(cfg: ChartConfig, rows: Row[]): TrendSummary {
  const x = cfg.x ?? cfg.time_column ?? '';
  const metric = cfg.metrics[0];
  const sorted = [...rows].sort((a, b) => byText(a[x], b[x]));
  const points = sorted.map(r => ({
    label: formatPeriod(r[x], cfg.time_grain),
    value: toNumber(r[metric.key]),
  }));
  const latest = points.at(-1)?.value ?? null;
  const previous = points.at(-2)?.value ?? null;
  const change =
    latest !== null && previous ? ((latest - previous) / Math.abs(previous)) * 100 : null;
  return { latest, previous, change, period: points.at(-1)?.label ?? '', points };
}

export function sparklineOption(cfg: ChartConfig, rows: Row[]): EChartsCoreOption {
  const { points } = trendSummary(cfg, rows);
  const unit = firstUnit(cfg);
  return {
    tooltip: { trigger: 'axis', valueFormatter: (v: unknown) => formatNumber(v, unit) },
    grid: { left: 0, right: 0, top: 8, bottom: 8 },
    xAxis: { type: 'category', show: false, data: points.map(p => p.label) },
    yAxis: { type: 'value', show: false, scale: true },
    series: [
      {
        type: 'line',
        data: points.map(p => p.value),
        smooth: true,
        showSymbol: false,
        areaStyle: { opacity: 0.15 },
      },
    ],
  };
}

/** The ECharts option for ``cfg``; null for types drawn without ECharts. */
export function buildOption(cfg: ChartConfig, data: ChartData): EChartsCoreOption | null {
  const rows = data.rows ?? [];
  switch (cfg.viz_type) {
    case 'line':
    case 'bar':
    case 'area':
      return xyOption(cfg, rows);
    case 'pie':
      return pieOption(cfg, rows);
    case 'funnel':
      return funnelOption(cfg, rows);
    case 'treemap':
      return treemapOption(cfg, rows);
    case 'heatmap':
      return heatmapOption(cfg, rows);
    case 'histogram':
      return histogramOption(cfg, data);
    case 'bubble':
      return bubbleOption(cfg, rows);
    case 'big_number_trend':
      return sparklineOption(cfg, rows);
    default:
      return null;
  }
}
