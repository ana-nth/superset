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
import {
  KeyboardEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useSelector } from 'react-redux';
import { useHistory, useLocation } from 'react-router-dom';
import { styled, SupersetClient, t } from '@superset-ui/core';
import {
  Alert,
  Button,
  Input,
  Loading,
  Select,
  Tooltip,
} from '@superset-ui/core/components';
import { Icons } from '@superset-ui/core/components/Icons';
import { useToasts } from 'src/components/MessageToasts/withToasts';
import {
  ChatApiError,
  deleteConversation,
  getCatalog,
  getConversation,
  getJob,
  listConversations,
  openInExplore,
  sendMessage,
} from './api';
import ChatMessageItem from './ChatMessageItem';
import SaveChartModal from './SaveChartModal';
import {
  CatalogSummary,
  ChatMessage,
  Conversation,
  Option,
  SavedChart,
} from './types';

export const MAX_MESSAGE_CHARS = 2000;
const POLL_INTERVAL_MS = 1000;
// The job is written before it is queued, so NOT_FOUND for this long means it
// expired or the cache was flushed
const NOT_FOUND_GRACE_MS = 15000;
const MAX_JOB_MS = 5 * 60 * 1000;
const MAX_POLL_FAILURES = 10;

// ─── Layout ───────────────────────────────────────────────────────────────────

const Page = styled.div`
  ${({ theme }) => `
    display: flex;
    height: calc(100vh - ${theme.sizeUnit * 16}px);
    min-height: ${theme.sizeUnit * 120}px;
    background: ${theme.colorBgLayout};
  `}
`;

const Sidebar = styled.aside`
  ${({ theme }) => `
    width: ${theme.sizeUnit * 65}px;
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    gap: ${theme.sizeUnit * 2}px;
    padding: ${theme.sizeUnit * 4}px ${theme.sizeUnit * 3}px;
    border-right: 1px solid ${theme.colorBorderSecondary};
    background: ${theme.colorBgContainer};
    overflow-y: auto;
    @media (max-width: 900px) {
      display: none;
    }
  `}
`;

const SidebarItem = styled.div<{ active: boolean }>`
  ${({ theme, active }) => `
    display: flex;
    align-items: center;
    gap: ${theme.sizeUnit}px;
    padding: ${theme.sizeUnit * 2}px;
    border-radius: ${theme.borderRadius}px;
    cursor: pointer;
    background: ${active ? theme.colorPrimaryBg : 'transparent'};
    &:hover {
      background: ${active ? theme.colorPrimaryBg : theme.colorFillTertiary};
    }
    .body {
      flex: 1;
      min-width: 0;
    }
    .title {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .meta {
      color: ${theme.colorTextTertiary};
      font-size: ${theme.fontSizeSM}px;
    }
  `}
`;

const Main = styled.main`
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
`;

const Header = styled.header`
  ${({ theme }) => `
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: ${theme.sizeUnit * 3}px;
    padding: ${theme.sizeUnit * 3}px ${theme.sizeUnit * 6}px;
    border-bottom: 1px solid ${theme.colorBorderSecondary};
    background: ${theme.colorBgContainer};
    h2 {
      margin: 0 auto 0 0;
      font-size: ${theme.fontSizeLG}px;
    }
    .scope-select {
      width: ${theme.sizeUnit * 50}px;
    }
  `}
`;

const Messages = styled.div`
  ${({ theme }) => `
    flex: 1;
    overflow-y: auto;
    padding: ${theme.sizeUnit * 6}px;
  `}
`;

const Column = styled.div`
  max-width: 960px;
  margin: 0 auto;
`;

const Welcome = styled.div`
  ${({ theme }) => `
    text-align: center;
    padding: ${theme.sizeUnit * 10}px ${theme.sizeUnit * 4}px;
    color: ${theme.colorTextSecondary};
    h3 {
      color: ${theme.colorText};
    }
  `}
`;

const Suggestions = styled.div`
  ${({ theme }) => `
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: ${theme.sizeUnit * 2}px;
    margin-top: ${theme.sizeUnit * 4}px;
  `}
`;

const Thinking = styled.div`
  ${({ theme }) => `
    display: flex;
    align-items: center;
    gap: ${theme.sizeUnit * 2}px;
    color: ${theme.colorTextSecondary};
    margin-bottom: ${theme.sizeUnit * 4}px;
  `}
`;

const Composer = styled.div`
  ${({ theme }) => `
    padding: ${theme.sizeUnit * 3}px ${theme.sizeUnit * 6}px ${theme.sizeUnit * 4}px;
    border-top: 1px solid ${theme.colorBorderSecondary};
    background: ${theme.colorBgContainer};
    .composer-row {
      display: flex;
      gap: ${theme.sizeUnit * 2}px;
      align-items: flex-end;
    }
    .hint {
      margin-top: ${theme.sizeUnit}px;
      color: ${theme.colorTextTertiary};
      font-size: ${theme.fontSizeSM}px;
    }
  `}
`;

// ─── Helpers ──────────────────────────────────────────────────────────────────

interface DatabaseRecord {
  id: number;
  database_name: string;
}

interface UserState {
  user?: { userId?: number };
}

function conversationIdFromUrl(search: string): string | null {
  return new URLSearchParams(search).get('conversation');
}

function errorMessage(code: string, text: string): ChatMessage {
  return {
    id: `local-error-${Date.now()}`,
    role: 'assistant',
    type: 'error',
    text,
    error_code: code,
  };
}

// ─── Component ────────────────────────────────────────────────────────────────

export default function ChartChat() {
  const history = useHistory();
  const location = useLocation();
  const { addDangerToast } = useToasts();
  const userId = useSelector<UserState, number | undefined>(
    state => state.user?.userId,
  );

  const [databases, setDatabases] = useState<Option<number>[]>([]);
  const [databaseId, setDatabaseId] = useState<number | null>(null);
  const [schemas, setSchemas] = useState<Option<string>[]>([]);
  const [schema, setSchema] = useState<string | null>(null);
  const [catalog, setCatalog] = useState<CatalogSummary | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);

  const [conversationId, setConversationId] = useState<string | null>(() =>
    conversationIdFromUrl(location.search),
  );
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loadingConversation, setLoadingConversation] = useState(false);
  const [job, setJob] = useState<{ id: string; stage: string } | null>(null);
  const [input, setInput] = useState('');
  const [sendError, setSendError] = useState<string | null>(null);
  const [saveTarget, setSaveTarget] = useState<ChatMessage | null>(null);

  const scrollRef = useRef<HTMLDivElement>(null);
  // The conversation being shown; async results for another one are dropped
  const activeConversation = useRef<string | null>(conversationId);
  activeConversation.current = conversationId;
  // A conversation this page just created is already in state: don't reload it
  const createdHere = useRef<string | null>(null);

  const busy = job !== null;
  const hasMessages = messages.length > 0;

  const refreshConversations = useCallback(() => {
    listConversations()
      .then(setConversations)
      .catch(() => setConversations([]));
  }, []);

  const setUrlConversation = useCallback(
    (id: string | null) => {
      const params = new URLSearchParams(location.search);
      if (id) params.set('conversation', id);
      else params.delete('conversation');
      const search = params.toString();
      history.replace({ search: search ? `?${search}` : '' });
    },
    [history, location.search],
  );

  // ── data scope ─────────────────────────────────────────────────────────────

  useEffect(() => {
    SupersetClient.get({ endpoint: '/api/v1/database/?q=(page_size:100)' })
      .then(({ json }) => {
        const options = ((json.result || []) as DatabaseRecord[]).map(d => ({
          value: d.id,
          label: d.database_name,
        }));
        setDatabases(options);
        const preferred =
          options.find(d => d.label.toLowerCase().includes('warehouse')) ??
          options[0];
        setDatabaseId(current => current ?? preferred?.value ?? null);
      })
      .catch(() => {
        setDatabases([]);
        addDangerToast(t('Could not load databases.'));
      });
    refreshConversations();
  }, [addDangerToast, refreshConversations]);

  useEffect(() => {
    if (!databaseId) return;
    SupersetClient.get({ endpoint: `/api/v1/database/${databaseId}/schemas/` })
      .then(({ json }) => {
        const names = (json.result || []) as string[];
        setSchemas(names.map(s => ({ value: s, label: s })));
        setSchema(current =>
          current && names.includes(current)
            ? current
            : (names.find(s => s === 'wh') ??
              names.find(s => !/^(information_schema|pg_)/.test(s)) ??
              null),
        );
      })
      .catch(() => {
        setSchemas([]);
        addDangerToast(t('Could not load schemas.'));
      });
  }, [databaseId, addDangerToast]);

  const loadCatalog = useCallback(
    (refresh = false) => {
      if (!databaseId || !schema) return;
      setCatalogLoading(true);
      setCatalogError(null);
      getCatalog(databaseId, schema, refresh)
        .then(setCatalog)
        .catch((err: Error) => {
          setCatalog(null);
          setCatalogError(err.message);
        })
        .finally(() => setCatalogLoading(false));
    },
    [databaseId, schema],
  );

  useEffect(() => {
    loadCatalog();
  }, [loadCatalog]);

  // ── conversation loading ───────────────────────────────────────────────────

  useEffect(() => {
    const fromUrl = conversationIdFromUrl(location.search);
    if (fromUrl !== activeConversation.current) setConversationId(fromUrl);
  }, [location.search]);

  useEffect(() => {
    if (!conversationId || createdHere.current === conversationId) {
      return undefined;
    }
    let cancelled = false;
    setLoadingConversation(true);
    getConversation(conversationId)
      .then(conv => {
        if (cancelled) return;
        setMessages(conv.messages ?? []);
        setDatabaseId(conv.database_id);
        setSchema(conv.schema);
        const pending = (conv as Conversation & { pending_job_id?: string })
          .pending_job_id;
        if (pending) setJob({ id: pending, stage: t('Still working…') });
      })
      .catch((err: ChatApiError) => {
        if (cancelled) return;
        setConversationId(null);
        setMessages([]);
        setUrlConversation(null);
        addDangerToast(
          err.status === 404
            ? t('That conversation no longer exists.')
            : err.message,
        );
      })
      .finally(() => {
        if (!cancelled) setLoadingConversation(false);
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId, addDangerToast, setUrlConversation]);

  // ── job polling ────────────────────────────────────────────────────────────

  const jobId = job?.id;
  useEffect(() => {
    if (!jobId) return undefined;
    const owner = activeConversation.current;
    const startedAt = Date.now();
    let notFoundSince = 0;
    let failures = 0;
    let stopped = false;

    const finish = (message: ChatMessage | null) => {
      stopped = true;
      if (activeConversation.current !== owner) return;
      setJob(null);
      if (message) {
        setMessages(current =>
          current.some(m => m.id === message.id)
            ? current
            : [...current, message],
        );
      }
      refreshConversations();
    };

    const interval = setInterval(async () => {
      if (stopped) return;
      if (Date.now() - startedAt > MAX_JOB_MS) {
        finish(
          errorMessage(
            'timeout',
            t('This is taking too long. Please try again or rephrase.'),
          ),
        );
        return;
      }
      try {
        const status = await getJob(jobId);
        failures = 0;
        if (stopped) return;
        if (status.state === 'NOT_FOUND') {
          notFoundSince = notFoundSince || Date.now();
          if (Date.now() - notFoundSince > NOT_FOUND_GRACE_MS) {
            finish(
              errorMessage(
                'lost',
                t('The request was lost. Please try again.'),
              ),
            );
          }
          return;
        }
        notFoundSince = 0;
        if (status.stage && activeConversation.current === owner) {
          setJob(current =>
            current?.id === jobId
              ? { id: jobId, stage: status.stage! }
              : current,
          );
        }
        if (status.state === 'SUCCESS') {
          finish(status.message ?? null);
        } else if (status.state === 'FAILURE') {
          finish(
            errorMessage('failed', status.error || t('Something went wrong.')),
          );
        }
      } catch {
        failures += 1;
        if (failures >= MAX_POLL_FAILURES) {
          finish(
            errorMessage(
              'network',
              t(
                'Lost connection to the server. Reload the page to see the answer.',
              ),
            ),
          );
        }
      }
    }, POLL_INTERVAL_MS);
    return () => {
      stopped = true;
      clearInterval(interval);
    };
  }, [jobId, refreshConversations]);

  // ── scrolling ──────────────────────────────────────────────────────────────

  useEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages.length, job?.stage]);

  // ── actions ────────────────────────────────────────────────────────────────

  const send = useCallback(
    async (raw: string, referenceMessageId?: string) => {
      const text = raw.trim();
      if (!text || busy) return;
      if (text.length > MAX_MESSAGE_CHARS) {
        setSendError(
          t('Messages are limited to %s characters.', MAX_MESSAGE_CHARS),
        );
        return;
      }
      if (!conversationId && (!databaseId || !schema)) {
        setSendError(t('Choose a database and schema first.'));
        return;
      }
      const optimistic: ChatMessage = {
        id: `local-${Date.now()}`,
        role: 'user',
        type: 'text',
        text,
        pending: true,
      };
      setMessages(current => [...current, optimistic]);
      setInput('');
      setSendError(null);
      try {
        const result = await sendMessage({
          message: text,
          ...(conversationId
            ? { conversation_id: conversationId }
            : { database_id: databaseId!, schema: schema! }),
          ...(referenceMessageId
            ? { reference_message_id: referenceMessageId }
            : {}),
        });
        setMessages(current =>
          current.map(m => (m.id === optimistic.id ? result.user_message : m)),
        );
        if (!conversationId) {
          createdHere.current = result.conversation_id;
          activeConversation.current = result.conversation_id;
          setConversationId(result.conversation_id);
          setUrlConversation(result.conversation_id);
        }
        setJob({ id: result.job_id, stage: t('Thinking…') });
      } catch (err) {
        const apiError = err as ChatApiError;
        setMessages(current => current.filter(m => m.id !== optimistic.id));
        setInput(text);
        if (apiError.status === 409 && apiError.body?.job_id) {
          // A previous message is still being answered: follow it
          setJob({
            id: String(apiError.body.job_id),
            stage: t('Finishing your previous message…'),
          });
        }
        if (apiError.status === 404) {
          setConversationId(null);
          setUrlConversation(null);
          setSendError(
            t('That conversation expired. Your message will start a new chat.'),
          );
          return;
        }
        setSendError(apiError.message);
      }
    },
    [busy, conversationId, databaseId, schema, setUrlConversation],
  );

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (
      event.key === 'Enter' &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      send(input);
    }
  };

  const startNewChat = () => {
    activeConversation.current = null;
    setConversationId(null);
    setMessages([]);
    setJob(null);
    setSendError(null);
    setUrlConversation(null);
  };

  const openConversation = (id: string) => {
    if (id === conversationId) return;
    activeConversation.current = id;
    setMessages([]);
    setJob(null);
    setSendError(null);
    setConversationId(id);
    setUrlConversation(id);
  };

  const removeConversation = async (id: string) => {
    try {
      await deleteConversation(id);
      if (id === conversationId) startNewChat();
      refreshConversations();
    } catch (err) {
      addDangerToast((err as Error).message);
    }
  };

  const handleOpenExplore = useCallback(
    (message: ChatMessage) => {
      const { chart } = message;
      if (!chart) return;
      if (chart.saved_chart_id) {
        window.open(`/explore/?slice_id=${chart.saved_chart_id}`, '_blank');
        return;
      }
      openInExplore(chart.form_data, chart.dataset_id).catch((err: Error) =>
        addDangerToast(err.message),
      );
    },
    [addDangerToast],
  );

  const handleRetry = useCallback(
    (message: ChatMessage) => {
      const original = messages.find(
        m => m.role === 'user' && m.id === message.reply_to,
      );
      const lastUser = [...messages].reverse().find(m => m.role === 'user');
      const text = (original ?? lastUser)?.text;
      if (text) send(text);
    },
    [messages, send],
  );

  const handleOption = useCallback(
    (text: string, referenceId?: string) => send(text, referenceId),
    [send],
  );

  const handleSaved = (result: SavedChart) => {
    if (!saveTarget) return;
    setMessages(current =>
      current.map(m =>
        m.id === saveTarget.id && m.chart
          ? {
              ...m,
              chart: {
                ...m.chart,
                saved_chart_id: result.chart_id,
                saved_title: result.chart_title,
              },
            }
          : m,
      ),
    );
    const savedMessage: ChatMessage = {
      id: `local-saved-${result.chart_id}-${Date.now()}`,
      role: 'assistant',
      type: 'saved',
      text: result.dashboard_title
        ? t(
            'Saved “%s” and added it to “%s”.',
            result.chart_title,
            result.dashboard_title,
          )
        : t('Saved “%s”.', result.chart_title),
      saved: result,
    };
    setMessages(current => [...current, savedMessage]);
  };

  const scopeLocked = hasMessages || busy;
  const coverage = useMemo(() => {
    if (!catalog?.sources.length) return '';
    const dated = catalog.sources.filter(s => s.time_min);
    const tables = t('%s data sources', catalog.sources.length);
    if (!dated.length) return tables;
    return `${tables} · ${dated[0].time_min} → ${dated[0].time_max}`;
  }, [catalog]);

  // ── render ─────────────────────────────────────────────────────────────────

  return (
    <Page data-test="chart-chat">
      <Sidebar>
        <Button buttonStyle="primary" onClick={startNewChat}>
          <Icons.PlusOutlined iconSize="m" /> {t('New chat')}
        </Button>
        {conversations.map(conv => (
          <SidebarItem
            key={conv.id}
            active={conv.id === conversationId}
            onClick={() => openConversation(conv.id)}
            role="button"
            aria-label={conv.title}
          >
            <div className="body">
              <div className="title">{conv.title || t('New chat')}</div>
              <div className="meta">{conv.schema}</div>
            </div>
            <Tooltip title={t('Delete chat')}>
              <Button
                buttonSize="xsmall"
                buttonStyle="link"
                aria-label={t('Delete chat')}
                onClick={event => {
                  event.stopPropagation();
                  removeConversation(conv.id);
                }}
              >
                <Icons.DeleteOutlined iconSize="s" />
              </Button>
            </Tooltip>
          </SidebarItem>
        ))}
      </Sidebar>

      <Main>
        <Header>
          <h2>
            <Icons.CommentOutlined iconSize="l" /> {t('AI Chart Chat')}
          </h2>
          <Tooltip
            title={scopeLocked ? t('Start a new chat to change the data') : ''}
          >
            <span>
              <Select
                className="scope-select"
                ariaLabel={t('Database')}
                placeholder={t('Database')}
                options={databases}
                value={databaseId ?? undefined}
                disabled={scopeLocked}
                onChange={value => {
                  setDatabaseId(value as number);
                  setSchema(null);
                }}
              />
            </span>
          </Tooltip>
          <Tooltip
            title={scopeLocked ? t('Start a new chat to change the data') : ''}
          >
            <span>
              <Select
                className="scope-select"
                ariaLabel={t('Schema')}
                placeholder={t('Schema')}
                options={schemas}
                value={schema ?? undefined}
                disabled={scopeLocked}
                onChange={value => setSchema(value as string)}
              />
            </span>
          </Tooltip>
          {coverage && <span>{coverage}</span>}
          <Tooltip
            title={t('Re-read the schema (after data or table changes)')}
          >
            <Button
              buttonSize="small"
              buttonStyle="link"
              aria-label={t('Refresh data catalog')}
              disabled={!databaseId || !schema || catalogLoading}
              onClick={() => loadCatalog(true)}
            >
              <Icons.SyncOutlined iconSize="m" />
            </Button>
          </Tooltip>
        </Header>

        <Messages ref={scrollRef}>
          <Column>
            {loadingConversation && <Loading position="inline-centered" />}

            {!loadingConversation && !hasMessages && (
              <Welcome data-test="chat-welcome">
                <h3>{t('Ask for a chart in plain English')}</h3>
                <p>
                  {t(
                    'Describe what you want to see — measures, breakdowns, periods, filters or a chart type — and refine it with follow-ups such as “make it a pie” or “only for 2025”.',
                  )}
                </p>
                {catalogLoading && <Loading position="inline-centered" />}
                {catalogError && (
                  <Alert
                    type="warning"
                    showIcon
                    message={catalogError}
                    action={
                      <Button
                        buttonSize="small"
                        buttonStyle="secondary"
                        onClick={() => loadCatalog(true)}
                      >
                        {t('Retry')}
                      </Button>
                    }
                  />
                )}
                {catalog && (
                  <Suggestions>
                    {catalog.suggestions.map(suggestion => (
                      <Button
                        key={suggestion}
                        buttonStyle="secondary"
                        disabled={busy}
                        onClick={() => send(suggestion)}
                      >
                        {suggestion}
                      </Button>
                    ))}
                    <Button
                      buttonStyle="link"
                      disabled={busy}
                      onClick={() => send(t('What data can I ask about?'))}
                    >
                      {t('What data can I ask about?')}
                    </Button>
                  </Suggestions>
                )}
              </Welcome>
            )}

            {messages.map(message => (
              <ChatMessageItem
                key={message.id}
                message={message}
                busy={busy}
                onOption={handleOption}
                onOpenExplore={handleOpenExplore}
                onSave={setSaveTarget}
                onRetry={handleRetry}
              />
            ))}

            {job && (
              <Thinking data-test="chat-thinking">
                <Icons.LoadingOutlined iconSize="m" />
                <span>{job.stage}</span>
              </Thinking>
            )}
          </Column>
        </Messages>

        <Composer>
          <Column>
            {sendError && (
              <Alert
                type="error"
                showIcon
                closable
                message={sendError}
                onClose={() => setSendError(null)}
              />
            )}
            <div className="composer-row">
              <Input.TextArea
                aria-label={t('Message')}
                value={input}
                placeholder={t(
                  'e.g. Monthly revenue by sales channel for 2025 as a line chart',
                )}
                autoSize={{ minRows: 1, maxRows: 6 }}
                maxLength={MAX_MESSAGE_CHARS}
                onChange={event => setInput(event.target.value)}
                onKeyDown={handleKeyDown}
              />
              <Button
                buttonStyle="primary"
                onClick={() => send(input)}
                disabled={!input.trim() || busy}
                loading={busy}
                data-test="chat-send"
              >
                {t('Send')}
              </Button>
            </div>
            <div className="hint">
              {t(
                'Enter to send, Shift+Enter for a new line. Charts are previews until you save them.',
              )}
            </div>
          </Column>
        </Composer>
      </Main>

      {saveTarget?.chart && conversationId && (
        <SaveChartModal
          show
          chart={saveTarget.chart}
          conversationId={conversationId}
          messageId={saveTarget.id}
          userId={userId}
          onHide={() => setSaveTarget(null)}
          onSaved={handleSaved}
        />
      )}
    </Page>
  );
}
