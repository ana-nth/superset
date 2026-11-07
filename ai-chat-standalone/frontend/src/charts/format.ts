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
import { Cell } from '../types';

const MONTHS = [
  'Jan',
  'Feb',
  'Mar',
  'Apr',
  'May',
  'Jun',
  'Jul',
  'Aug',
  'Sep',
  'Oct',
  'Nov',
  'Dec',
];

export function toNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  const number = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(number) ? number : null;
}

/** 1234567 → "1.2M", with "$" for USD and "%" for percentages. */
export function formatNumber(value: unknown, unit = ''): string {
  const number = toNumber(value);
  if (number === null) return value === null || value === undefined ? '–' : String(value);
  if (unit === '%') return `${number.toFixed(1)}%`;
  const abs = Math.abs(number);
  let text: string;
  if (abs >= 1e9) text = `${(number / 1e9).toFixed(1)}B`;
  else if (abs >= 1e6) text = `${(number / 1e6).toFixed(1)}M`;
  else if (abs >= 1e3) text = `${(number / 1e3).toFixed(1)}K`;
  else if (Number.isInteger(number)) text = String(number);
  else text = number.toFixed(2);
  if (unit.toUpperCase() === 'USD') {
    return number < 0 ? `-$${text.slice(1)}` : `$${text}`;
  }
  if (unit && unit !== 'date') return `${text} ${unit}`;
  return text;
}

/** Parse ISO dates from the API as UTC so periods never shift by time zone. */
export function parseDate(value: Cell): Date | null {
  if (value === null || typeof value === 'boolean') return null;
  if (typeof value === 'number') return new Date(value);
  const text = value.trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return new Date(`${text}T00:00:00Z`);
  const normalised = text.replace(' ', 'T');
  const zoned = /(Z|[+-]\d{2}:?\d{2})$/.test(normalised);
  const date = new Date(zoned ? normalised : `${normalised}Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** A period start formatted for its grain ("Q3 2025", "Mar 2025"). */
export function formatPeriod(value: Cell, grain: string | null | undefined): string {
  const date = parseDate(value);
  if (!date) return value === null ? '–' : String(value);
  const year = date.getUTCFullYear();
  const month = MONTHS[date.getUTCMonth()];
  const day = String(date.getUTCDate()).padStart(2, '0');
  switch (grain) {
    case 'P1Y':
      return String(year);
    case 'P3M':
      return `Q${Math.floor(date.getUTCMonth() / 3) + 1} ${year}`;
    case 'P1M':
      return `${month} ${year}`;
    case 'PT1H':
      return `${day} ${month} ${String(date.getUTCHours()).padStart(2, '0')}:00`;
    default:
      return `${day} ${month} ${year}`;
  }
}

export function formatCell(value: Cell): string {
  if (value === null) return '–';
  return String(value);
}
