-- Run against `zava`. One line per fact, "name|value", for deploy.sh to check.
SELECT 'stores|'   || count(*) FROM retail.stores;
SELECT 'products|' || count(*) FROM retail.products;
SELECT 'customers|'|| count(*) FROM retail.customers;
SELECT 'embeddings|' || count(*) FROM retail.product_description_embeddings;
SELECT 'rls_tables|' || count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'retail' AND c.relrowsecurity;
SELECT 'policies|' || count(*) FROM pg_policies WHERE schemaname = 'retail';
SELECT 'store_manager_can_read|' || has_table_privilege('store_manager', 'retail.products', 'SELECT');
