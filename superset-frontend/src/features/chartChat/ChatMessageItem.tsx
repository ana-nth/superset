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
import { memo } from 'react';
import { styled, t } from '@superset-ui/core';
import { Alert, Button, Tag, Tooltip } from '@superset-ui/core/components';
import { Icons } from '@superset-ui/core/components/Icons';
import ChatChartPreview from './ChatChartPreview';
import { ChatMessage } from './types';

const Row = styled.div<{ isUser: boolean }>`
  ${({ theme, isUser }) => `
    display: flex;
    justify-content: ${isUser ? 'flex-end' : 'flex-start'};
    margin-bottom: ${theme.sizeUnit * 4}px;
  `}
`;

const Bubble = styled.div<{ isUser: boolean; wide: boolean }>`
  ${({ theme, isUser, wide }) => `
    max-width: ${wide ? '100%' : '75%'};
    width: ${wide ? '100%' : 'auto'};
    padding: ${theme.sizeUnit * 3}px ${theme.sizeUnit * 4}px;
    border-radius: ${theme.borderRadiusLG}px;
    background: ${isUser ? theme.colorPrimaryBg : theme.colorBgContainer};
    border: 1px solid ${isUser ? theme.colorPrimaryBorder : theme.colorBorderSecondary};
    white-space: pre-line;
    word-break: break-word;
  `}
`;

const Insight = styled.p`
  ${({ theme }) => `
    margin: ${theme.sizeUnit * 2}px 0 0;
    color: ${theme.colorText};
    font-weight: ${theme.fontWeightStrong};
  `}
`;

const Chips = styled.div`
  ${({ theme }) => `
    display: flex;
    flex-wrap: wrap;
    gap: ${theme.sizeUnit}px;
    margin-top: ${theme.sizeUnit * 2}px;
  `}
`;

const ChartCard = styled.div`
  ${({ theme }) => `
    margin-top: ${theme.sizeUnit * 3}px;
    border: 1px solid ${theme.colorBorderSecondary};
    border-radius: ${theme.borderRadius}px;
    padding: ${theme.sizeUnit * 3}px;
  `}
`;

const ChartHeader = styled.div`
  ${({ theme }) => `
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: ${theme.sizeUnit * 2}px;
    flex-wrap: wrap;
    font-weight: ${theme.fontWeightStrong};
  `}
`;

const Actions = styled.div`
  ${({ theme }) => `
    display: flex;
    flex-wrap: wrap;
    gap: ${theme.sizeUnit * 2}px;
    margin-top: ${theme.sizeUnit * 3}px;
  `}
`;

const Notes = styled.ul`
  ${({ theme }) => `
    margin: ${theme.sizeUnit * 2}px 0 0;
    padding-left: ${theme.sizeUnit * 4}px;
    color: ${theme.colorTextSecondary};
    font-size: ${theme.fontSizeSM}px;
  `}
`;

const SqlBlock = styled.pre`
  ${({ theme }) => `
    margin-top: ${theme.sizeUnit * 2}px;
    max-height: ${theme.sizeUnit * 80}px;
    overflow: auto;
    padding: ${theme.sizeUnit * 3}px;
    background: ${theme.colorFillTertiary};
    border-radius: ${theme.borderRadius}px;
    font-size: ${theme.fontSizeSM}px;
    white-space: pre-wrap;
  `}
`;

interface ChatMessageItemProps {
  message: ChatMessage;
  busy: boolean;
  onOption: (text: string, referenceId?: string) => void;
  onOpenExplore: (message: ChatMessage) => void;
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
    <Actions>
      {options.map(option => (
        <Button
          key={option}
          buttonSize="small"
          buttonStyle="secondary"
          disabled={busy}
          onClick={() => onOption(option)}
        >
          {option}
        </Button>
      ))}
    </Actions>
  );
}

function ChatMessageItem({
  message,
  busy,
  onOption,
  onOpenExplore,
  onSave,
  onRetry,
}: ChatMessageItemProps) {
  const isUser = message.role === 'user';
  const { chart } = message;
  const notes = message.notes?.filter(Boolean) ?? [];

  if (isUser) {
    return (
      <Row isUser data-test="chat-user-message">
        <Bubble isUser wide={false}>
          {message.text}
        </Bubble>
      </Row>
    );
  }

  if (message.type === 'error') {
    return (
      <Row isUser={false} data-test="chat-error-message">
        <Bubble isUser={false} wide={false}>
          <Alert
            type="error"
            showIcon
            message={message.text}
            action={
              <Button
                buttonSize="small"
                buttonStyle="secondary"
                disabled={busy}
                onClick={() => onRetry(message)}
              >
                {t('Try again')}
              </Button>
            }
          />
        </Bubble>
      </Row>
    );
  }

  return (
    <Row isUser={false} data-test="chat-assistant-message">
      <Bubble isUser={false} wide={Boolean(chart) || message.type === 'sql'}>
        {message.type === 'no_data' ? (
          <Alert type="warning" showIcon message={message.text} />
        ) : message.type === 'saved' ? (
          <Alert type="success" showIcon message={message.text} />
        ) : (
          <div>{message.text}</div>
        )}

        {message.insight && <Insight>{message.insight}</Insight>}

        {message.saved && (
          <Actions>
            <Button
              buttonSize="small"
              buttonStyle="secondary"
              href={message.saved.chart_url}
              target="_blank"
            >
              {t('Open chart')}
            </Button>
            {message.saved.dashboard_url && (
              <Button
                buttonSize="small"
                buttonStyle="secondary"
                href={message.saved.dashboard_url}
                target="_blank"
              >
                {t('Open dashboard')}
              </Button>
            )}
          </Actions>
        )}

        {message.sql && (
          <>
            <SqlBlock data-test="chat-sql">{message.sql}</SqlBlock>
            <Actions>
              <Button
                buttonSize="small"
                buttonStyle="secondary"
                onClick={() =>
                  navigator.clipboard?.writeText(message.sql ?? '')
                }
              >
                <Icons.CopyOutlined iconSize="s" /> {t('Copy SQL')}
              </Button>
            </Actions>
          </>
        )}

        {chart && (
          <ChartCard>
            <ChartHeader>
              <span>{chart.saved_title || chart.title}</span>
              {chart.saved_chart_id && <Tag color="success">{t('Saved')}</Tag>}
            </ChartHeader>
            <Chips>
              {chart.interpretation.map(chip => (
                <Tag key={chip}>{chip}</Tag>
              ))}
            </Chips>
            <ChatChartPreview formData={chart.form_data} />
            <Actions>
              <Button
                buttonSize="small"
                buttonStyle="primary"
                onClick={() => onSave(message)}
                data-test="chat-save-chart"
              >
                <Icons.SaveOutlined iconSize="s" />{' '}
                {chart.saved_chart_id ? t('Add to dashboard') : t('Save')}
              </Button>
              <Button
                buttonSize="small"
                buttonStyle="secondary"
                onClick={() => onOpenExplore(message)}
              >
                <Icons.EditOutlined iconSize="s" /> {t('Edit in Explore')}
              </Button>
              <Tooltip title={t('Show the SQL behind this chart')}>
                <Button
                  buttonSize="small"
                  buttonStyle="secondary"
                  disabled={busy}
                  onClick={() => onOption(t('Show the SQL'), message.id)}
                >
                  <Icons.ConsoleSqlOutlined iconSize="s" /> {t('SQL')}
                </Button>
              </Tooltip>
            </Actions>
          </ChartCard>
        )}

        {notes.length > 0 && (
          <Notes data-test="chat-notes">
            {notes.map(note => (
              <li key={note}>{note}</li>
            ))}
          </Notes>
        )}

        {message.options && message.options.length > 0 && (
          <OptionButtons
            options={message.options}
            busy={busy}
            onOption={text => onOption(text)}
          />
        )}
      </Bubble>
    </Row>
  );
}

export default memo(ChatMessageItem);
