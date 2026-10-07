-- Run against `zava` before pg_restore. Only matters on a RE-load, where the
-- policies already in place would collide with the ones the dump recreates, so
-- they are dropped and RLS disabled first. A no-op on a fresh database. Carried
-- over from scripts/init-db.sh, which does the same before every restore.
DO $$
DECLARE
    r RECORD;
    p RECORD;
BEGIN
    FOR r IN SELECT schemaname, tablename FROM pg_tables WHERE schemaname = 'retail' LOOP
        EXECUTE format('ALTER TABLE %I.%I DISABLE ROW LEVEL SECURITY', r.schemaname, r.tablename);
        FOR p IN SELECT policyname FROM pg_policies
                  WHERE schemaname = r.schemaname AND tablename = r.tablename LOOP
            EXECUTE format('DROP POLICY IF EXISTS %I ON %I.%I', p.policyname, r.schemaname, r.tablename);
        END LOOP;
    END LOOP;
END
$$;
