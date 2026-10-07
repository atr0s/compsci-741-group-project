-- Run against `zava` as the Entra admin, with -v store_manager_password=...
-- Idempotent: safe to re-run, and a re-run re-syncs the password with the
-- environment file (which is also what the MCP app's POSTGRES_URL was built from).

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_diskann CASCADE;

-- The MCP server's login. It owns nothing and bypasses nothing: Row Level
-- Security is enforced against it by the policies the data restore brings in.
SELECT format('CREATE ROLE store_manager LOGIN PASSWORD %L', :'store_manager_password')
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'store_manager')\gexec
ALTER ROLE store_manager WITH LOGIN PASSWORD :'store_manager_password';
GRANT CONNECT ON DATABASE zava TO store_manager;

CREATE SCHEMA IF NOT EXISTS retail;
