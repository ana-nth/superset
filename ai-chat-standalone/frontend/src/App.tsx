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
import { useEffect, useState } from 'react';
import { Link, Route, Routes, useLocation } from 'react-router-dom';
import {
  App as AntApp,
  ConfigProvider,
  Flex,
  Input,
  Layout,
  Menu,
  Modal,
  theme,
  Typography,
} from 'antd';
import { BarChartOutlined, CommentOutlined, DashboardOutlined } from '@ant-design/icons';
import { api, AUTH_REQUIRED_EVENT, getApiKey, setApiKey } from './api';
import ChatPage from './chat/ChatPage';
import ChartPage from './pages/ChartPage';
import ChartsPage from './pages/ChartsPage';
import DashboardPage from './pages/DashboardPage';
import DashboardsPage from './pages/DashboardsPage';
import { DarkModeContext, useSystemDarkMode } from './theme';
import { AppConfig } from './types';

function ApiKeyPrompt() {
  const [open, setOpen] = useState(false);
  const [key, setKey] = useState(getApiKey());
  useEffect(() => {
    const show = () => setOpen(true);
    window.addEventListener(AUTH_REQUIRED_EVENT, show);
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, show);
  }, []);
  return (
    <Modal
      open={open}
      title="API key required"
      okText="Save"
      onOk={() => {
        setApiKey(key.trim());
        window.location.reload();
      }}
      onCancel={() => setOpen(false)}
    >
      <Typography.Paragraph>
        This server requires an API key. Ask your administrator for one.
      </Typography.Paragraph>
      <Input.Password value={key} onChange={e => setKey(e.target.value)} />
    </Modal>
  );
}

function Shell() {
  const location = useLocation();
  const { token } = theme.useToken();
  const [appConfig, setAppConfig] = useState<AppConfig | null>(null);

  useEffect(() => {
    api
      .config()
      .then(setAppConfig)
      .catch(() => setAppConfig(null));
  }, []);

  const section = location.pathname.startsWith('/dashboards')
    ? 'dashboards'
    : location.pathname.startsWith('/charts')
      ? 'charts'
      : 'chat';

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Layout.Header
        style={{
          display: 'flex',
          alignItems: 'center',
          height: 56,
          padding: '0 24px',
          background: token.colorBgContainer,
          borderBottom: `1px solid ${token.colorBorderSecondary}`,
        }}
      >
        <Flex align="center" gap={8} style={{ marginRight: 24 }}>
          <BarChartOutlined style={{ fontSize: 20, color: token.colorPrimary }} />
          <Typography.Text strong>AI Chart Chat</Typography.Text>
        </Flex>
        <Menu
          mode="horizontal"
          selectedKeys={[section]}
          style={{ flex: 1, borderBottom: 'none', lineHeight: '54px' }}
          items={[
            { key: 'chat', icon: <CommentOutlined />, label: <Link to="/">Chat</Link> },
            { key: 'charts', icon: <BarChartOutlined />, label: <Link to="/charts">Charts</Link> },
            {
              key: 'dashboards',
              icon: <DashboardOutlined />,
              label: <Link to="/dashboards">Dashboards</Link>,
            },
          ]}
        />
      </Layout.Header>
      <Layout.Content>
        <Routes>
          <Route path="/" element={<ChatPage appConfig={appConfig} />} />
          <Route path="/charts" element={<ChartsPage />} />
          <Route path="/charts/:id" element={<ChartPage />} />
          <Route path="/dashboards" element={<DashboardsPage />} />
          <Route path="/dashboards/:id" element={<DashboardPage />} />
          <Route path="*" element={<ChatPage appConfig={appConfig} />} />
        </Routes>
      </Layout.Content>
      <ApiKeyPrompt />
    </Layout>
  );
}

export default function Root() {
  const dark = useSystemDarkMode();
  return (
    <DarkModeContext.Provider value={dark}>
      <ConfigProvider
        theme={{ algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm }}
      >
        <AntApp>
          <Shell />
        </AntApp>
      </ConfigProvider>
    </DarkModeContext.Provider>
  );
}
