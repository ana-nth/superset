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
import { useSearchParams } from 'react-router-dom';
import {
  Alert,
  App,
  Button,
  Flex,
  Input,
  Layout,
  Select,
  Spin,
  theme,
  Tooltip,
  Typography,
} from 'antd';
import {
  CommentOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  LoadingOutlined,
  PlusOutlined,
  SyncOutlined,
} from '@ant-design/icons';
import { api, ApiError } from '../api';
import DatabaseModal from '../databases/DatabaseModal';
import {
  AppConfig,
  CatalogSummary,
  ChatMessage,
  Conversation,
  DatabaseRecord,
  SaveResult,
} from '../types';
import ChatMessageItem from './ChatMessageItem';
import SaveChartModal from './SaveChartModal';

export const MAX_MESSAGE_CHARS = 2000;
const POLL_INTERVAL_MS = 1000;
// The job is written before it is queued, so NOT_FOUND for this long means it
// expired or the cache was flushed
const NOT_FOUND_GRACE_MS = 15000;
const MAX_JOB_MS = 5 * 60 * 1000;
const MAX_POLL_FAILURES = 10;

function errorMessage(code: string, text: string): ChatMessage {
  return {
    id: `local-error-${Date.now()}`,
    role: 'assistant',
    type: 'error',
    text,
    error_code: code,
  };
}

export default function ChatPage({ appConfig }: { appConfig: AppConfig | null }) {
  const { token } = theme.useToken();
  const { message: toast } = App.useApp();
  const [searchParams, setSearchParams] = useSearchParams();

  const [databases, setDatabases] = useState<DatabaseRecord[]>([]);
  const [databaseId, setDatabaseId] = useState<string | null>(null);
  const [schemas, setSchemas] = useState<string[]>([]);
  const [schema, setSchema] = useState<string | null>(null);
  const [catalog, setCatalog] = useState<CatalogSummary | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [showDatabases, setShowDatabases] = useState(false);

  const [conversationId, setConversationId] = useState<string | null>(() =>
    searchParams.get('conversation'),
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
    api
      .conversations()
      .then(setConversations)
      .catch(() => setConversations([]));
  }, []);

  const setUrlConversation = useCallback(
    (id: string | null) => {
      setSearchParams(
        current => {
          const next = new URLSearchParams(current);
          if (id) next.set('conversation', id);
          else next.delete('conversation');
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  // ── data scope ─────────────────────────────────────────────────────────────

  const loadDatabases = useCallback(
    (selectId?: string) => {
      api
        .databases()
        .then(list => {
          setDatabases(list);
          const preferred =
            list.find(d => d.id === selectId) ??
            list.find(d => d.name.toLowerCase().includes('warehouse')) ??
            list[0];
          setDatabaseId(current =>
            selectId || !current || !list.some(d => d.id === current)
              ? (preferred?.id ?? null)
              : current,
          );
        })
        .catch((err: Error) => {
          setDatabases([]);
          toast.error(err.message || 'Could not load databases.');
        });
    },
    [toast],
  );

  useEffect(() => {
    loadDatabases();
    refreshConversations();
  }, [loadDatabases, refreshConversations]);

  useEffect(() => {
    if (!databaseId) {
      setSchemas([]);
      return;
    }
    api
      .schemas(databaseId)
      .then(names => {
        setSchemas(names);
        setSchema(current =>
          current && names.includes(current)
            ? current
            : (names.find(s => s === 'wh') ?? names.find(s => s === 'public') ?? names[0] ?? null),
        );
      })
      .catch((err: Error) => {
        setSchemas([]);
        setSchema(null);
        setCatalogError(err.message);
      });
  }, [databaseId]);

  const loadCatalog = useCallback(
    (refresh = false) => {
      if (!databaseId || !schema) return;
      setCatalogLoading(true);
      setCatalogError(null);
      api
        .catalog(databaseId, schema, refresh)
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
    const fromUrl = searchParams.get('conversation');
    if (fromUrl !== activeConversation.current) setConversationId(fromUrl);
  }, [searchParams]);

  useEffect(() => {
    if (!conversationId || createdHere.current === conversationId) {
      return undefined;
    }
    let cancelled = false;
    setLoadingConversation(true);
    api
      .conversation(conversationId)
      .then(conv => {
        if (cancelled) return;
        setMessages(conv.messages ?? []);
        setDatabaseId(conv.database_id);
        setSchema(conv.schema);
        if (conv.pending_job_id) setJob({ id: conv.pending_job_id, stage: 'Still working…' });
      })
      .catch((err: ApiError) => {
        if (cancelled) return;
        setConversationId(null);
        setMessages([]);
        setUrlConversation(null);
        toast.error(err.status === 404 ? 'That conversation no longer exists.' : err.message);
      })
      .finally(() => {
        if (!cancelled) setLoadingConversation(false);
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId, toast, setUrlConversation]);

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
          current.some(m => m.id === message.id) ? current : [...current, message],
        );
      }
      refreshConversations();
    };

    const interval = setInterval(async () => {
      if (stopped) return;
      if (Date.now() - startedAt > MAX_JOB_MS) {
        finish(errorMessage('timeout', 'This is taking too long. Please try again or rephrase.'));
        return;
      }
      try {
        const status = await api.job(jobId);
        failures = 0;
        if (stopped) return;
        if (status.state === 'NOT_FOUND') {
          notFoundSince = notFoundSince || Date.now();
          if (Date.now() - notFoundSince > NOT_FOUND_GRACE_MS) {
            finish(errorMessage('lost', 'The request was lost. Please try again.'));
          }
          return;
        }
        notFoundSince = 0;
        const stage = status.stage;
        if (stage && activeConversation.current === owner) {
          setJob(current => (current?.id === jobId ? { id: jobId, stage } : current));
        }
        if (status.state === 'SUCCESS') {
          finish(status.message ?? null);
        } else if (status.state === 'FAILURE') {
          finish(errorMessage('failed', status.error || 'Something went wrong.'));
        }
      } catch {
        failures += 1;
        if (failures >= MAX_POLL_FAILURES) {
          finish(
            errorMessage(
              'network',
              'Lost connection to the server. Reload the page to see the answer.',
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
        setSendError(`Messages are limited to ${MAX_MESSAGE_CHARS} characters.`);
        return;
      }
      if (!conversationId && (!databaseId || !schema)) {
        setSendError('Choose a database and schema first.');
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
        const result = await api.sendMessage({
          message: text,
          ...(conversationId
            ? { conversation_id: conversationId }
            : { database_id: databaseId!, schema: schema! }),
          ...(referenceMessageId ? { reference_message_id: referenceMessageId } : {}),
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
        setJob({ id: result.job_id, stage: 'Thinking…' });
      } catch (err) {
        const apiError = err as ApiError;
        setMessages(current => current.filter(m => m.id !== optimistic.id));
        setInput(text);
        if (apiError.status === 409 && apiError.body?.job_id) {
          // A previous message is still being answered: follow it
          setJob({ id: String(apiError.body.job_id), stage: 'Finishing your previous message…' });
        }
        if (apiError.status === 404) {
          setConversationId(null);
          setUrlConversation(null);
          setSendError('That conversation expired. Your message will start a new chat.');
          return;
        }
        setSendError(apiError.message);
      }
    },
    [busy, conversationId, databaseId, schema, setUrlConversation],
  );

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      send(input);
    }
  };

  const startNewChat = () => {
    activeConversation.current = null;
    createdHere.current = null;
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
      await api.deleteConversation(id);
      if (id === conversationId) startNewChat();
      refreshConversations();
    } catch (err) {
      toast.error((err as Error).message);
    }
  };

  const handleRetry = useCallback(
    (message: ChatMessage) => {
      const original = messages.find(m => m.role === 'user' && m.id === message.reply_to);
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

  const handleSaved = (result: SaveResult) => {
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
    setMessages(current => [
      ...current,
      {
        id: `local-saved-${result.chart_id}-${Date.now()}`,
        role: 'assistant',
        type: 'saved',
        text: result.dashboard_title
          ? `Saved “${result.chart_title}” and added it to “${result.dashboard_title}”.`
          : `Saved “${result.chart_title}”.`,
        saved: result,
      },
    ]);
  };

  const scopeLocked = hasMessages || busy;
  const coverage = useMemo(() => {
    if (!catalog?.sources.length) return '';
    const dated = catalog.sources.filter(s => s.time_min);
    const tables = `${catalog.sources.length} data sources`;
    if (!dated.length) return tables;
    return `${tables} · ${dated[0].time_min} → ${dated[0].time_max}`;
  }, [catalog]);

  const lockedHint = scopeLocked ? 'Start a new chat to change the data' : '';

  // ── render ─────────────────────────────────────────────────────────────────

  return (
    <Layout style={{ height: 'calc(100vh - 56px)' }} data-testid="chart-chat">
      <Layout.Sider
        width={260}
        breakpoint="lg"
        collapsedWidth={0}
        theme="light"
        style={{
          borderRight: `1px solid ${token.colorBorderSecondary}`,
          overflowY: 'auto',
          padding: 12,
          background: token.colorBgContainer,
        }}
      >
        <Button type="primary" block icon={<PlusOutlined />} onClick={startNewChat}>
          New chat
        </Button>
        <Flex vertical gap={4} style={{ marginTop: 12 }}>
          {conversations.map(conv => {
            const active = conv.id === conversationId;
            return (
              <Flex
                key={conv.id}
                align="center"
                gap={4}
                role="button"
                aria-label={conv.title}
                onClick={() => openConversation(conv.id)}
                style={{
                  padding: 8,
                  borderRadius: token.borderRadius,
                  cursor: 'pointer',
                  background: active ? token.colorPrimaryBg : 'transparent',
                }}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <Typography.Text ellipsis style={{ display: 'block' }}>
                    {conv.title || 'New chat'}
                  </Typography.Text>
                  <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
                    {conv.schema}
                  </Typography.Text>
                </div>
                <Tooltip title="Delete chat">
                  <Button
                    size="small"
                    type="text"
                    aria-label="Delete chat"
                    icon={<DeleteOutlined />}
                    onClick={event => {
                      event.stopPropagation();
                      removeConversation(conv.id);
                    }}
                  />
                </Tooltip>
              </Flex>
            );
          })}
        </Flex>
      </Layout.Sider>

      <Layout style={{ minWidth: 0 }}>
        <Flex
          align="center"
          wrap
          gap={12}
          style={{
            padding: '12px 24px',
            borderBottom: `1px solid ${token.colorBorderSecondary}`,
            background: token.colorBgContainer,
          }}
        >
          <Typography.Title level={5} style={{ margin: '0 auto 0 0' }}>
            <CommentOutlined /> AI Chart Chat
          </Typography.Title>
          <Tooltip title={lockedHint}>
            <Select
              style={{ width: 200 }}
              aria-label="Database"
              placeholder="Database"
              value={databaseId ?? undefined}
              disabled={scopeLocked}
              options={databases.map(d => ({ value: d.id, label: d.name }))}
              onChange={value => {
                setDatabaseId(value);
                setSchema(null);
                setCatalog(null);
              }}
            />
          </Tooltip>
          <Tooltip title={lockedHint}>
            <Select
              style={{ width: 160 }}
              aria-label="Schema"
              placeholder="Schema"
              value={schema ?? undefined}
              disabled={scopeLocked}
              options={schemas.map(s => ({ value: s, label: s }))}
              onChange={value => setSchema(value)}
            />
          </Tooltip>
          {coverage && <Typography.Text type="secondary">{coverage}</Typography.Text>}
          <Tooltip title="Re-read the schema (after data or table changes)">
            <Button
              type="text"
              aria-label="Refresh data catalog"
              icon={<SyncOutlined spin={catalogLoading} />}
              disabled={!databaseId || !schema || catalogLoading}
              onClick={() => loadCatalog(true)}
            />
          </Tooltip>
          <Tooltip title="Manage database connections">
            <Button
              aria-label="Databases"
              icon={<DatabaseOutlined />}
              onClick={() => setShowDatabases(true)}
            />
          </Tooltip>
        </Flex>

        <div ref={scrollRef} style={{ flex: 1, overflowY: 'auto', padding: 24 }}>
          <div style={{ maxWidth: 960, margin: '0 auto' }}>
            {loadingConversation && (
              <Flex justify="center">
                <Spin />
              </Flex>
            )}

            {!loadingConversation && !hasMessages && (
              <Flex
                vertical
                align="center"
                gap={12}
                style={{ padding: '40px 16px', textAlign: 'center' }}
                data-testid="chat-welcome"
              >
                <Typography.Title level={3}>Ask your data anything</Typography.Title>
                <Typography.Text type="secondary">
                  Ask a question about your data (“which region sold the most last
                  quarter?”) or about the data itself (“how are the tables related?”), and refine
                  answers with follow-ups such as “make it a pie” or “only for 2025”.
                </Typography.Text>
                {databases.length === 0 && (
                  <Alert
                    type="info"
                    showIcon
                    message="No database is connected yet."
                    action={
                      <Button size="small" onClick={() => setShowDatabases(true)}>
                        Connect one
                      </Button>
                    }
                  />
                )}
                {catalogLoading && <Spin tip="Reading the schema…" />}
                {catalogError && (
                  <Alert
                    type="warning"
                    showIcon
                    message={catalogError}
                    action={
                      <Button size="small" onClick={() => loadCatalog(true)}>
                        Retry
                      </Button>
                    }
                  />
                )}
                {catalog && (
                  <Flex wrap gap={8} justify="center" style={{ marginTop: 8 }}>
                    {catalog.suggestions.map(suggestion => (
                      <Button key={suggestion} disabled={busy} onClick={() => send(suggestion)}>
                        {suggestion}
                      </Button>
                    ))}
                    <Button
                      type="link"
                      disabled={busy}
                      onClick={() =>
                        send('Explain this database: the tables, how they relate and what I can ask')
                      }
                    >
                      Explain this database
                    </Button>
                  </Flex>
                )}
                {appConfig && (
                  <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
                    Model: {appConfig.model} ({appConfig.provider})
                  </Typography.Text>
                )}
              </Flex>
            )}

            {messages.map(message => (
              <ChatMessageItem
                key={message.id}
                message={message}
                conversationId={conversationId}
                busy={busy}
                onOption={handleOption}
                onSave={setSaveTarget}
                onRetry={handleRetry}
              />
            ))}

            {job && (
              <Flex
                align="center"
                gap={8}
                style={{ color: token.colorTextSecondary, marginBottom: 16 }}
                data-testid="chat-thinking"
              >
                <LoadingOutlined />
                <span>{job.stage}</span>
              </Flex>
            )}
          </div>
        </div>

        <div
          style={{
            padding: '12px 24px 16px',
            borderTop: `1px solid ${token.colorBorderSecondary}`,
            background: token.colorBgContainer,
          }}
        >
          <div style={{ maxWidth: 960, margin: '0 auto' }}>
            {sendError && (
              <Alert
                type="error"
                showIcon
                closable
                message={sendError}
                onClose={() => setSendError(null)}
                style={{ marginBottom: 8 }}
              />
            )}
            <Flex gap={8} align="flex-end">
              <Input.TextArea
                aria-label="Message"
                value={input}
                placeholder="e.g. Monthly revenue by sales channel for 2025 as a line chart"
                autoSize={{ minRows: 1, maxRows: 6 }}
                maxLength={MAX_MESSAGE_CHARS}
                onChange={event => setInput(event.target.value)}
                onKeyDown={handleKeyDown}
              />
              <Button
                type="primary"
                onClick={() => send(input)}
                disabled={!input.trim() || busy}
                loading={busy}
                data-testid="chat-send"
              >
                Send
              </Button>
            </Flex>
            <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
              Enter to send, Shift+Enter for a new line. Charts are previews until you save
              them.
            </Typography.Text>
          </div>
        </div>
      </Layout>

      {saveTarget?.chart && conversationId && (
        <SaveChartModal
          chart={saveTarget.chart}
          conversationId={conversationId}
          messageId={saveTarget.id}
          onClose={() => setSaveTarget(null)}
          onSaved={handleSaved}
        />
      )}
      {showDatabases && (
        <DatabaseModal
          databases={databases}
          allowEdits={appConfig?.allow_ui_connections ?? false}
          onClose={() => setShowDatabases(false)}
          onChanged={selectId => {
            if (selectId) {
              // A new connection starts a new chat about it
              startNewChat();
              setShowDatabases(false);
            }
            loadDatabases(selectId);
          }}
        />
      )}
    </Layout>
  );
}
