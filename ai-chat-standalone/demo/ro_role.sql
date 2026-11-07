-- Licensed to the Apache Software Foundation (ASF) under one
-- or more contributor license agreements.  See the NOTICE file
-- distributed with this work for additional information
-- regarding copyright ownership.  The ASF licenses this file
-- to you under the Apache License, Version 2.0 (the
-- "License"); you may not use this file except in compliance
-- with the License.  You may obtain a copy of the License at
--
--   http://www.apache.org/licenses/LICENSE-2.0
--
-- Unless required by applicable law or agreed to in writing,
-- software distributed under the License is distributed on an
-- "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
-- KIND, either express or implied.  See the License for the
-- specific language governing permissions and limitations
-- under the License.

-- Idempotent read-only role the chat connects with (load.py sets its password).
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ai_chat_ro') THEN
    CREATE ROLE ai_chat_ro WITH LOGIN;
  END IF;
END
$$;

GRANT CONNECT ON DATABASE warehouse TO ai_chat_ro;
GRANT USAGE ON SCHEMA wh TO ai_chat_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA wh TO ai_chat_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA wh GRANT SELECT ON TABLES TO ai_chat_ro;
ALTER ROLE ai_chat_ro SET default_transaction_read_only = on;
