-- Run against the `postgres` database as the Entra admin. Idempotent.
-- Kept out of Bicep on purpose: a database created through ARM is owned by the
-- password admin, and the Entra admin (who restores into it) then cannot create
-- the retail schema. Creating it here makes the Entra admin the owner, which is
-- how the running environment was built.
SELECT 'CREATE DATABASE zava'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'zava')\gexec
