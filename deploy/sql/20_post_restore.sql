-- Run against `zava` after pg_restore.
--
-- The restore runs with --no-privileges, so grants in the dump are skipped and
-- store_manager has to be granted access again here.
--
-- RLS itself is NOT recreated: the backup carries the real policies (keyed on
-- app.current_rls_user_id against retail.stores, with the super-manager UUID as
-- the regional bypass) and pg_restore brings them back with RLS enabled on all
-- ten retail tables. scripts/init-db.sh also has a block that recreates policies
-- with an ::integer cast, but it is guarded on a customers.rls_user_id column
-- that this schema does not have, so it never runs - verified on the live
-- database - and it is not carried over.
GRANT USAGE ON SCHEMA retail TO store_manager;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA retail TO store_manager;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA retail TO store_manager;
