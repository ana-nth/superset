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
import { useState } from 'react';
import {
  Alert,
  App,
  Button,
  Divider,
  Flex,
  Form,
  Input,
  List,
  Modal,
  Popconfirm,
  Select,
  Tag,
  Typography,
} from 'antd';
import { DeleteOutlined } from '@ant-design/icons';
import { api } from '../api';
import { DatabaseRecord } from '../types';

interface DatabaseModalProps {
  databases: DatabaseRecord[];
  allowEdits: boolean;
  onClose: () => void;
  onChanged: (selectId?: string) => void;
}

const EXAMPLES = [
  'postgresql+psycopg2://user:password@host:5432/dbname',
  'mysql+pymysql://user:password@host:3306/dbname',
  'mssql+pymssql://user:password@host:1433/dbname',
];

/** Lists connections and adds new ones (SQLAlchemy URIs). */
export default function DatabaseModal({
  databases,
  allowEdits,
  onClose,
  onChanged,
}: DatabaseModalProps) {
  const { message: toast } = App.useApp();
  const [name, setName] = useState('');
  const [uri, setUri] = useState('');
  const [schemas, setSchemas] = useState<string[]>([]);
  const [found, setFound] = useState<string[] | null>(null);
  const [busy, setBusy] = useState<'test' | 'save' | null>(null);
  const [error, setError] = useState<string | null>(null);

  const test = async () => {
    setBusy('test');
    setError(null);
    try {
      const result = await api.testDatabase(uri.trim());
      setFound(result.schemas);
    } catch (err) {
      setFound(null);
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const save = async () => {
    setBusy('save');
    setError(null);
    try {
      const created = await api.addDatabase(name.trim(), uri.trim(), schemas);
      toast.success(`Connected ${created.name}`);
      setName('');
      setUri('');
      setSchemas([]);
      setFound(null);
      onChanged(created.id);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const remove = async (id: string) => {
    try {
      await api.deleteDatabase(id);
      onChanged();
    } catch (err) {
      toast.error((err as Error).message);
    }
  };

  return (
    <Modal open title="Databases" footer={null} onCancel={onClose} width={640}>
      <List
        size="small"
        dataSource={databases}
        locale={{ emptyText: 'No databases yet' }}
        renderItem={db => (
          <List.Item
            actions={
              allowEdits && !db.managed
                ? [
                    <Popconfirm
                      key="delete"
                      title="Delete this connection and its saved charts?"
                      onConfirm={() => remove(db.id)}
                    >
                      <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                    </Popconfirm>,
                  ]
                : []
            }
          >
            <List.Item.Meta
              title={
                <Flex gap={8} align="center">
                  {db.name}
                  <Tag>{db.backend || 'unknown'}</Tag>
                  {db.managed && <Tag color="blue">server config</Tag>}
                </Flex>
              }
              description={
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {db.uri}
                  {db.schemas.length > 0 && ` · schemas: ${db.schemas.join(', ')}`}
                </Typography.Text>
              }
            />
          </List.Item>
        )}
      />
      {allowEdits ? (
        <>
          <Divider orientation="left" plain>
            Connect a database
          </Divider>
          <Form layout="vertical">
            <Form.Item label="Name" required>
              <Input value={name} maxLength={250} onChange={e => setName(e.target.value)} />
            </Form.Item>
            <Form.Item
              label="SQLAlchemy URI"
              required
              extra={
                <span>
                  e.g. <code>{EXAMPLES[0]}</code>. Use a read-only database user.
                </span>
              }
            >
              <Input.Password
                visibilityToggle
                value={uri}
                placeholder={EXAMPLES[0]}
                onChange={e => {
                  setUri(e.target.value);
                  setFound(null);
                }}
              />
            </Form.Item>
            <Form.Item
              label="Schemas users may chat about (optional)"
              extra="Leave empty to allow every schema the user can read."
            >
              <Select
                mode="tags"
                value={schemas}
                onChange={setSchemas}
                options={(found ?? []).map(s => ({ value: s, label: s }))}
                placeholder={found ? 'Pick schemas' : 'Test the connection to list schemas'}
              />
            </Form.Item>
            {found && (
              <Alert
                type="success"
                showIcon
                style={{ marginBottom: 12 }}
                message={`Connected. ${found.length} schema(s): ${found.slice(0, 10).join(', ')}`}
              />
            )}
            {error && <Alert type="error" showIcon style={{ marginBottom: 12 }} message={error} />}
            <Flex gap={8} justify="flex-end">
              <Button onClick={test} loading={busy === 'test'} disabled={!uri.trim()}>
                Test connection
              </Button>
              <Button
                type="primary"
                onClick={save}
                loading={busy === 'save'}
                disabled={!uri.trim() || !name.trim()}
              >
                Connect
              </Button>
            </Flex>
          </Form>
        </>
      ) : (
        <Alert
          type="info"
          showIcon
          message="Connections are managed by the server configuration (AI_CHAT_DATABASES)."
        />
      )}
    </Modal>
  );
}
