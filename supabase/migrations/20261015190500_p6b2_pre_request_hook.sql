SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- P6b-2 (docs/design-data-releases-impl-plan.md §8 P6b-2, D-P6-2): register
-- public.dr_request_context() as PostgREST's pre-request hook, so the
-- X-Campfire-DR header selects the data release a request browses (the
-- cat_* source views read it through dr_browse_tag()).
--
-- Hand-authored: a role setting is not declarative (pg-delta does not model
-- it, so it never shows as drift). This is the plain PostgREST
-- `db-pre-request` option set the Supabase way, on the authenticator role;
-- a self-hosted PostgREST honours the same role setting (or the equivalent
-- PGRST_DB_PRE_REQUEST environment variable). The function itself is
-- declared in supabase/schemas/functions.sql and created by the preceding
-- migration. Only the Data API runs it (not Storage or Realtime).
ALTER ROLE authenticator SET pgrst.db_pre_request = 'public.dr_request_context';
NOTIFY pgrst, 'reload config';
