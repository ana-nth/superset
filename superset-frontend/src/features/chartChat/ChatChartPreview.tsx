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
import { useCallback, useEffect, useState } from 'react';
import {
  getClientErrorObject,
  QueryData,
  QueryFormData,
  styled,
  SuperChart,
  t,
} from '@superset-ui/core';
import { Alert, Button, Loading } from '@superset-ui/core/components';
import {
  getChartDataRequest,
  handleChartDataResponse,
} from 'src/components/Chart/chartAction';
import { getQuerySettings } from 'src/explore/exploreUtils';

const PREVIEW_HEIGHT = 340;

const Frame = styled.div`
  ${({ theme }) => `
    position: relative;
    width: 100%;
    height: ${PREVIEW_HEIGHT}px;
    padding: ${theme.sizeUnit * 2}px 0;
  `}
`;

const Centered = styled.div`
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
`;

interface ChatChartPreviewProps {
  formData: QueryFormData;
}

/** Renders a chat chart the way Explore would, from its form data. */
export default function ChatChartPreview({ formData }: ChatChartPreviewProps) {
  const [data, setData] = useState<QueryData[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError(null);
    const [useLegacyApi] = getQuerySettings(formData);
    getChartDataRequest({ formData })
      .then(({ response, json }: { response: Response; json: unknown }) =>
        handleChartDataResponse(response, json, useLegacyApi),
      )
      .then((result: QueryData[]) => {
        if (!cancelled) setData(result);
      })
      .catch(async (err: unknown) => {
        if (cancelled) return;
        try {
          const parsed = await getClientErrorObject(
            err as Parameters<typeof getClientErrorObject>[0],
          );
          setError(
            parsed.message || parsed.error || t('Failed to load chart data.'),
          );
        } catch {
          setError(t('Failed to load chart data.'));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [formData, attempt]);

  const retry = useCallback(() => setAttempt(n => n + 1), []);

  if (error) {
    return (
      <Alert
        type="error"
        showIcon
        message={t('The chart could not be displayed')}
        description={error}
        action={
          <Button buttonSize="small" buttonStyle="secondary" onClick={retry}>
            {t('Retry')}
          </Button>
        }
      />
    );
  }
  return (
    <Frame data-test="chat-chart-preview">
      {data === null ? (
        <Centered>
          <Loading position="inline-centered" />
        </Centered>
      ) : (
        <SuperChart
          chartType={formData.viz_type}
          formData={formData}
          queriesData={data}
          enableNoResults
          height="100%"
          width="100%"
        />
      )}
    </Frame>
  );
}
