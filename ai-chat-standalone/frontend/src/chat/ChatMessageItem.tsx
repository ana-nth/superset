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
import { memo, useCallback } from 'react';
import { Link } from 'react-router-dom';
import { Alert, App, Button, Card, Flex, Tag, theme, Tooltip, Typography } from 'antd';
import {
  ConsoleSqlOutlined,
  CopyOutlined,
  DownloadOutlined,
  SaveOutlined,
} from '@ant-design/icons';
import { api } from '../api';
import ChartView from '../charts/ChartView';
import { ChatMessage } from '../types';
import SchemaView from './SchemaView';

interface ChatMessageItemProps {
  message: ChatMessage;
  conversationId: string | null;
  busy: boolean;
  onOption: (text: string, referenceId?: string) => void;
  onSave: (message: ChatMessage) => void;
  onRetry: (message: ChatMessage) => void;
}

function OptionButtons({
  options,
  busy,
  onOption,
}: {
  options: string[];
  busy: boolean;
  onOption: (text: string) => void;
}) {
  return (
    <Flex wrap gap={8} style={{ marginTop: 12 }}>
      {options.map(option => (
        <Button key={option} size="small" disabled={busy} onClick={() => onOption(option)}>
          {option}
        </Button>
      ))}
    </Flex>
  );
}

function ChatMessageItem({
  message,
  conversationId,
  busy,
  onOption,
  onSave,
  onRetry,
}: ChatMessageItemProps) {
  const { token } = theme.useToken();
  const { message: toast } = App.useApp();
  const isUser = message.role === 'user';
  const { chart } = message;
  const notes = message.notes?.filter(Boolean) ?? [];
  const wide = Boolean(chart) || message.type === 'sql' || Boolean(message.schema?.length);

  const loadData = useCallback(
    () => api.messageData(conversationId ?? '', message.id),
    [conversationId, message.id],
  );

  const bubble = {
    maxWidth: wide ? '100%' : '75%',
    width: wide ? '100%' : 'auto',
    padding: '12px 16px',
    borderRadius: token.borderRadiusLG,
    background: isUser ? token.colorPrimaryBg : token.colorBgContainer,
    border: `1px solid ${isUser ? token.colorPrimaryBorder : token.colorBorderSecondary}`,
    whiteSpace: 'pre-line' as const,
    wordBreak: 'break-word' as const,
    opacity: message.pending ? 0.6 : 1,
  };

  if (isUser) {
    return (
      <Flex justify="flex-end" style={{ marginBottom: 16 }} data-testid="chat-user-message">
        <div style={bubble}>{message.text}</div>
      </Flex>
    );
  }

  if (message.type === 'error') {
    return (
      <Flex style={{ marginBottom: 16 }} data-testid="chat-error-message">
        <Alert
          type="error"
          showIcon
          message={message.text}
          style={{ maxWidth: '75%' }}
          action={
            <Button size="small" disabled={busy} onClick={() => onRetry(message)}>
              Try again
            </Button>
          }
        />
      </Flex>
    );
  }

  return (
    <Flex style={{ marginBottom: 16 }} data-testid="chat-assistant-message">
      <div style={bubble}>
        {message.type === 'no_data' ? (
          <Alert type="warning" showIcon message={message.text} />
        ) : message.type === 'saved' ? (
          <Alert type="success" showIcon message={message.text} />
        ) : (
          <div>{message.text}</div>
        )}

        {message.insight && (
          <Typography.Paragraph strong style={{ margin: '8px 0 0' }}>
            {message.insight}
          </Typography.Paragraph>
        )}

        {message.schema && message.schema.length > 0 && (
          <SchemaView sources={message.schema} />
        )}

        {message.saved && (
          <Flex wrap gap={8} style={{ marginTop: 12 }}>
            <Link to={message.saved.chart_url}>
              <Button size="small">Open chart</Button>
            </Link>
            {message.saved.dashboard_url && (
              <Link to={message.saved.dashboard_url}>
                <Button size="small">Open dashboard</Button>
              </Link>
            )}
          </Flex>
        )}

        {message.sql && (
          <>
            <pre
              data-testid="chat-sql"
              style={{
                marginTop: 8,
                maxHeight: 320,
                overflow: 'auto',
                padding: 12,
                background: token.colorFillTertiary,
                borderRadius: token.borderRadius,
                fontSize: token.fontSizeSM,
                whiteSpace: 'pre-wrap',
              }}
            >
              {message.sql}
            </pre>
            <Button
              size="small"
              icon={<CopyOutlined />}
              onClick={() =>
                navigator.clipboard
                  ?.writeText(message.sql ?? '')
                  .then(() => toast.success('SQL copied'))
              }
            >
              Copy SQL
            </Button>
          </>
        )}

        {chart && (
          <Card
            size="small"
            style={{ marginTop: 12 }}
            title={chart.saved_title || chart.title}
            extra={chart.saved_chart_id && <Tag color="success">Saved</Tag>}
          >
            <Flex wrap gap={4} style={{ marginBottom: 8 }}>
              {chart.interpretation.map(chip => (
                <Tag key={chip}>{chip}</Tag>
              ))}
            </Flex>
            {conversationId && <ChartView config={chart.config} load={loadData} />}
            <Flex wrap gap={8} style={{ marginTop: 12 }}>
              <Button
                size="small"
                type="primary"
                icon={<SaveOutlined />}
                onClick={() => onSave(message)}
                data-testid="chat-save-chart"
              >
                {chart.saved_chart_id ? 'Add to dashboard' : 'Save'}
              </Button>
              <Tooltip title="Show the SQL behind this chart">
                <Button
                  size="small"
                  icon={<ConsoleSqlOutlined />}
                  disabled={busy}
                  onClick={() => onOption('Show the SQL', message.id)}
                >
                  SQL
                </Button>
              </Tooltip>
              <Button
                size="small"
                icon={<DownloadOutlined />}
                onClick={() =>
                  api
                    .messageCsv(conversationId ?? '', message.id)
                    .catch((err: Error) => toast.error(err.message))
                }
              >
                CSV
              </Button>
            </Flex>
          </Card>
        )}

        {notes.length > 0 && (
          <ul
            data-testid="chat-notes"
            style={{
              margin: '8px 0 0',
              paddingLeft: 16,
              color: token.colorTextSecondary,
              fontSize: token.fontSizeSM,
            }}
          >
            {notes.map(note => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        )}

        {message.options && message.options.length > 0 && (
          <OptionButtons options={message.options} busy={busy} onOption={text => onOption(text)} />
        )}
      </div>
    </Flex>
  );
}

export default memo(ChatMessageItem);
