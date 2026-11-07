-- Idempotent read-only role for the AI Auto Dashboard agent.
-- Run once with:
--   docker exec -i superset_warehouse psql -U warehouse -d warehouse < warehouse/ro_role.sql
-- Password defaults to 'ai_dash_ro_secret'; override with AI_DASH_DB_PASSWORD in docker/.env.

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ai_dash_ro') THEN
    -- Uses a static default; change password after first run if needed.
    CREATE ROLE ai_dash_ro WITH LOGIN PASSWORD 'ai_dash_ro_secret';
  END IF;
END
$$;

-- Grant schema usage
GRANT USAGE ON SCHEMA wh TO ai_dash_ro;

-- Grant SELECT on all current tables
GRANT SELECT ON ALL TABLES IN SCHEMA wh TO ai_dash_ro;

-- Ensure future tables are also accessible
ALTER DEFAULT PRIVILEGES IN SCHEMA wh GRANT SELECT ON TABLES TO ai_dash_ro;
