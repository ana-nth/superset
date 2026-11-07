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

import { useState, useEffect, useRef } from 'react';
import {
  t,
  SupersetClient,
  styled,
  getClientErrorObject,
} from '@superset-ui/core';
import {
  Alert,
  Modal,
  Button,
  Select,
  Dropdown,
} from '@superset-ui/core/components';
import ProgressBar from '@superset-ui/core/components/ProgressBar';
import { Icons } from '@superset-ui/core/components/Icons';

// ─── Styled components ────────────────────────────────────────────────────────

const ModalContent = styled.div`
  ${({ theme }) => `
    padding: ${theme.sizeUnit * 2}px 0;
    .auto-dashboard-select {
      width: 100%;
      margin-bottom: ${theme.sizeUnit * 3}px;
    }
  `}
`;

const StatusRow = styled.div`
  ${({ theme }) => `
    display: flex;
    align-items: center;
    gap: ${theme.sizeUnit * 2}px;
    margin: ${theme.sizeUnit * 2}px 0;
  `}
`;

const InsightsList = styled.ul`
  ${({ theme }) => `
    margin: ${theme.sizeUnit * 3}px 0 0;
    padding-left: ${theme.sizeUnit * 5}px;
    max-height: ${theme.sizeUnit * 80}px;
    overflow-y: auto;
    li {
      margin-bottom: ${theme.sizeUnit * 1.5}px;
      font-size: ${theme.fontSizeSM}px;
      line-height: 1.4;
    }
  `}
`;

const SeverityBadge = styled.span<{ severity: Severity }>`
  ${({ theme, severity }) => {
    const color =
      severity === 'critical'
        ? theme.colorError
        : severity === 'warning'
          ? theme.colorWarning
          : theme.colorSuccess;
    return `
      display: inline-block;
      padding: 0 ${theme.sizeUnit * 1.5}px;
      border-radius: ${theme.borderRadiusSM}px;
      font-size: ${theme.fontSizeSM}px;
      font-weight: ${theme.fontWeightStrong};
      color: ${color};
      border: 1px solid ${color};
      margin-right: ${theme.sizeUnit * 1.5}px;
    `;
  }}
`;

const ActionRow = styled.div`
  ${({ theme }) => `
    display: flex;
    gap: ${theme.sizeUnit * 2}px;
    margin-top: ${theme.sizeUnit * 4}px;
  `}
`;

const Summary = styled.div`
  ${({ theme }) => `
    margin-top: ${theme.sizeUnit * 2}px;
    white-space: pre-line;
    font-size: ${theme.fontSizeSM}px;
  `}
`;

// ─── Types ────────────────────────────────────────────────────────────────────

interface Option<T> {
  value: T;
  label: string;
}

type Severity = 'info' | 'warning' | 'critical';

interface Insight {
  title: string;
  text: string;
  severity: Severity;
  check?: string;
  chart_id?: number;
}

interface DatabaseRecord {
  id: number;
  database_name: string;
}

interface TaskStatus {
  state: 'PENDING' | 'RUNNING' | 'SUCCESS' | 'FAILURE' | 'NOT_FOUND';
  progress?: number;
  progress_message?: string;
  dashboard_id?: number;
  insights?: Insight[];
  summary?: string;
  error?: string;
  mode?: string;
  llm_used?: boolean;
  fallback_reason?: string;
  charts_created?: number;
}

type TaskState = 'idle' | 'running' | 'success' | 'failure';

// ─── Component ────────────────────────────────────────────────────────────────

interface AutoDashboardModalProps {
  show: boolean;
  onHide: () => void;
}

const POLL_INTERVAL_MS = 2000;
// The task is written to the cache before it is queued, so NOT_FOUND for this
// long means the state expired or the cache was flushed.
const NOT_FOUND_GRACE_MS = 30000;
const MAX_RUNTIME_MS = 30 * 60 * 1000;

async function errorMessage(err: unknown, fallback: string): Promise<string> {
  try {
    const parsed = await getClientErrorObject(
      err as Parameters<typeof getClientErrorObject>[0],
    );
    return parsed.message || parsed.error || fallback;
  } catch {
    return fallback;
  }
}

export default function AutoDashboardModal({
  show,
  onHide,
}: AutoDashboardModalProps) {
  const [databases, setDatabases] = useState<Option<number>[]>([]);
  const [selectedDb, setSelectedDb] = useState<number | null>(null);
  const [schemas, setSchemas] = useState<Option<string>[]>([]);
  const [selectedSchema, setSelectedSchema] = useState<string | null>(null);
  const [taskId, setTaskId] = useState<string | null>(null);
  const [taskState, setTaskState] = useState<TaskState>('idle');
  const [progressMessage, setProgressMessage] = useState('');
  const [progressPct, setProgressPct] = useState(0);
  const [result, setResult] = useState<TaskStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const startedAt = useRef(0);

  // Load databases when the modal opens
  useEffect(() => {
    if (!show) return;
    SupersetClient.get({ endpoint: '/api/v1/database/?q=(page_size:100)' })
      .then(({ json }) => {
        const dbs = ((json.result || []) as DatabaseRecord[]).map(d => ({
          value: d.id,
          label: d.database_name,
        }));
        setDatabases(dbs);
        // Pre-select the "warehouse" database if present
        const wh = dbs.find(d => d.label.toLowerCase().includes('warehouse'));
        setSelectedDb(current => current ?? wh?.value ?? null);
      })
      .catch(() => setDatabases([]));
  }, [show]);

  // Load schemas when the database changes
  useEffect(() => {
    if (!selectedDb) return;
    SupersetClient.get({ endpoint: `/api/v1/database/${selectedDb}/schemas/` })
      .then(({ json }) => {
        const names = (json.result || []) as string[];
        setSchemas(names.map(s => ({ value: s, label: s })));
        setSelectedSchema(current =>
          current && names.includes(current)
            ? current
            : (names.find(s => s === 'wh') ??
              names.find(s => !/^(information_schema|pg_)/.test(s)) ??
              null),
        );
      })
      .catch(() => setSchemas([]));
  }, [selectedDb]);

  // Poll the task while it runs; the interval is cleared on close/unmount
  useEffect(() => {
    if (!taskId || taskState !== 'running') return undefined;
    let notFoundSince = 0;
    const interval = setInterval(async () => {
      if (Date.now() - startedAt.current > MAX_RUNTIME_MS) {
        setTaskState('failure');
        setError(t('Timed out waiting for the dashboard to be generated.'));
        return;
      }
      try {
        const { json } = await SupersetClient.get({
          endpoint: `/api/v1/ai_dashboard/status/${taskId}`,
        });
        const status = json as TaskStatus;
        if (status.progress_message)
          setProgressMessage(status.progress_message);
        if (typeof status.progress === 'number') {
          setProgressPct(pct => Math.max(pct, status.progress ?? 0));
        }
        if (status.state === 'NOT_FOUND') {
          notFoundSince = notFoundSince || Date.now();
          if (Date.now() - notFoundSince > NOT_FOUND_GRACE_MS) {
            setTaskState('failure');
            setError(t('The generation job was lost. Please try again.'));
          }
          return;
        }
        notFoundSince = 0;
        if (status.state === 'SUCCESS') {
          setProgressPct(100);
          setResult(status);
          setTaskState('success');
        } else if (status.state === 'FAILURE') {
          setTaskState('failure');
          setError(status.error || t('Unknown error'));
        }
      } catch {
        // transient network error — keep polling
      }
    }, POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [taskId, taskState]);

  const startPolling = (tid: string) => {
    startedAt.current = Date.now();
    setTaskId(tid);
    setTaskState('running');
  };

  const handleGenerate = async () => {
    if (!selectedDb || !selectedSchema) return;
    setTaskState('running');
    setProgressPct(1);
    setProgressMessage(t('Starting…'));
    setError(null);
    setResult(null);

    try {
      const { json } = await SupersetClient.post({
        endpoint: '/api/v1/ai_dashboard/generate',
        jsonPayload: { database_id: selectedDb, schema: selectedSchema },
      });
      startPolling(json.task_id as string);
    } catch (err) {
      // A job already running for this user: follow it instead of failing
      const response = err as Response;
      if (response?.status === 409 && typeof response.json === 'function') {
        try {
          const body = await response.clone().json();
          if (body?.task_id) {
            setProgressMessage(t('Resuming the job already in progress…'));
            startPolling(body.task_id);
            return;
          }
        } catch {
          // fall through to the generic error
        }
      }
      setTaskState('failure');
      setError(await errorMessage(err, t('Failed to start job')));
    }
  };

  const handleReset = () => {
    setTaskId(null);
    setTaskState('idle');
    setProgressPct(0);
    setProgressMessage('');
    setResult(null);
    setError(null);
  };

  const handleHide = () => {
    // A running job keeps going on the server; reopening starts fresh
    handleReset();
    onHide();
  };

  const dashboardId = result?.dashboard_id ?? null;
  const insights = result?.insights ?? [];

  const downloadMenuItems = dashboardId
    ? [
        {
          key: 'zip',
          label: t('Export ZIP'),
          onClick: () => {
            window.location.href = `/api/v1/dashboard/export/?q=!(${dashboardId})`;
          },
        },
        {
          key: 'json',
          label: t('Download JSON'),
          onClick: async () => {
            try {
              const { json: dashJson } = await SupersetClient.get({
                endpoint: `/api/v1/dashboard/${dashboardId}`,
              });
              const blob = new Blob(
                [JSON.stringify(dashJson.result, null, 2)],
                { type: 'application/json' },
              );
              const url = URL.createObjectURL(blob);
              const a = document.createElement('a');
              a.href = url;
              a.download = `dashboard_${dashboardId}.json`;
              a.click();
              URL.revokeObjectURL(url);
            } catch {
              // ignore
            }
          },
        },
      ]
    : [];

  return (
    <Modal
      show={show}
      onHide={handleHide}
      name="auto-dashboard"
      width="640px"
      title={
        <span>
          <Icons.StarOutlined iconSize="m" /> {t('Auto Dashboard')}
        </span>
      }
      footer={
        taskState === 'idle' ? (
          <Button
            id="auto-dashboard-generate-btn"
            buttonStyle="primary"
            onClick={handleGenerate}
            disabled={!selectedDb || !selectedSchema}
          >
            {t('Generate')}
          </Button>
        ) : null
      }
    >
      <ModalContent>
        {taskState === 'idle' && (
          <>
            <p>
              {t(
                'The AI agent runs 5 analytics checks (data quality, trends, segmentation, anomalies, KPIs) on the selected schema and builds a dashboard automatically.',
              )}
            </p>
            <Select
              className="auto-dashboard-select"
              ariaLabel={t('Database')}
              placeholder={t('Select database')}
              options={databases}
              value={selectedDb ?? undefined}
              onChange={value => {
                setSelectedDb(value as number);
                setSelectedSchema(null);
              }}
            />
            <Select
              className="auto-dashboard-select"
              ariaLabel={t('Schema')}
              placeholder={t('Select schema')}
              options={schemas}
              value={selectedSchema ?? undefined}
              onChange={value => setSelectedSchema(value as string)}
            />
          </>
        )}

        {taskState === 'running' && (
          <>
            <StatusRow>
              <Icons.LoadingOutlined iconSize="m" />
              <span>{progressMessage || t('Running…')}</span>
            </StatusRow>
            <ProgressBar percent={progressPct} striped />
            <p>
              {t(
                'You can close this window; the dashboard will still be created and appear in the dashboard list.',
              )}
            </p>
          </>
        )}

        {taskState === 'success' && (
          <>
            <StatusRow>
              <Icons.CheckCircleOutlined iconSize="m" />
              <strong>
                {t(
                  'Dashboard created with %s charts!',
                  result?.charts_created ?? '',
                )}
              </strong>
            </StatusRow>
            {result?.fallback_reason && (
              <Alert
                type="info"
                showIcon
                message={t(
                  'The AI agent could not finish, so the rule-based engine built this dashboard.',
                )}
                description={result.fallback_reason}
              />
            )}
            {result?.summary && <Summary>{result.summary}</Summary>}
            {insights.length > 0 && (
              <InsightsList>
                {insights.map((ins, idx) => (
                  <li key={`${ins.title}-${idx}`}>
                    <SeverityBadge severity={ins.severity}>
                      {ins.severity.toUpperCase()}
                    </SeverityBadge>
                    <strong>{ins.title}:</strong> {ins.text}
                  </li>
                ))}
              </InsightsList>
            )}
            <ActionRow>
              <Button
                id="auto-dashboard-open-btn"
                buttonStyle="primary"
                onClick={() => {
                  if (dashboardId) {
                    window.open(
                      `/superset/dashboard/${dashboardId}/`,
                      '_blank',
                    );
                  }
                }}
              >
                {t('Open dashboard')}
              </Button>
              <Dropdown menu={{ items: downloadMenuItems }}>
                <Button
                  id="auto-dashboard-download-btn"
                  buttonStyle="secondary"
                >
                  {t('Download')} <Icons.DownOutlined iconSize="s" />
                </Button>
              </Dropdown>
              <Button
                id="auto-dashboard-rerun-btn"
                buttonStyle="link"
                onClick={handleReset}
              >
                {t('Run again')}
              </Button>
            </ActionRow>
          </>
        )}

        {taskState === 'failure' && (
          <>
            <Alert
              type="error"
              showIcon
              message={t('Generation failed')}
              description={error || t('Unknown error')}
            />
            <ActionRow>
              <Button
                id="auto-dashboard-retry-btn"
                buttonStyle="secondary"
                onClick={handleReset}
              >
                {t('Try again')}
              </Button>
            </ActionRow>
          </>
        )}
      </ModalContent>
    </Modal>
  );
}
