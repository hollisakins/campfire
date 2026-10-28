-- =============================================================================
-- Retain-on-supersede gate: retain_required_hashes (P4e, decision D-P2-4)
-- =============================================================================
-- The archive holds exactly the files data releases reference, so a deploy
-- copies a live object before overwriting it only when a release has frozen
-- its hash and not archived it yet. This guard pins the one function that
-- decides that, on the seed's DR-seed manifest:
--   1. a member whose byte pass has not run (pending) or failed is required;
--      a copied or stamped member is not; an unknown hash is not; the answer
--      is distinct and sorted; NULL and empty input answer an empty array;
--   2. the same hash named by two releases is required while either has not
--      archived it;
--   3. more than 2000 hashes per call is refused;
--   4. a non-admin is refused, anon has no EXECUTE, service_role is admitted.
-- Run: psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_retain_gate.sql
\set ON_ERROR_STOP on
BEGIN;

CREATE FUNCTION pg_temp.zzz_as(p_role text, p_uid uuid, p_sql text) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE v text;
BEGIN
  PERFORM set_config('request.jwt.claims', json_build_object('sub', p_uid, 'role', p_role)::text, true);
  EXECUTE format('SET LOCAL ROLE %I', p_role);
  EXECUTE p_sql INTO v;
  RESET ROLE;
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  RETURN v;
EXCEPTION WHEN OTHERS THEN
  RESET ROLE;
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  RETURN 'ERR: ' || SQLERRM;
END $$;
GRANT EXECUTE ON FUNCTION pg_temp.zzz_as(text, uuid, text) TO authenticated;

CREATE FUNCTION pg_temp.zzz_try(p_sql text) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE v text;
BEGIN
  EXECUTE p_sql INTO v;
  RETURN COALESCE(v, 'ok');
EXCEPTION WHEN OTHERS THEN
  RETURN 'ERR: ' || SQLERRM;
END $$;

-- Four DR-seed members with distinct hashes, one per byte state.
CREATE TEMP TABLE zzz_pick AS
SELECT storage_key, content_hash, row_number() OVER (ORDER BY storage_key) AS n
FROM (
  SELECT DISTINCT ON (content_hash) storage_key, content_hash
  FROM dr_manifest WHERE tag = 'DR-seed'
  ORDER BY content_hash, storage_key
) d
LIMIT 4;

DO $$
DECLARE
  n int;
BEGIN
  SELECT count(*) INTO n FROM zzz_pick;
  IF n <> 4 THEN RAISE EXCEPTION 'seed: need 4 DR-seed members with distinct hashes, got %', n; END IF;
END $$;

-- ------------------------------------------------------------------ 1.
DO $$
DECLARE
  h1 text; h2 text; h3 text; h4 text;
  got text[];
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  SELECT content_hash INTO h1 FROM zzz_pick WHERE n = 1;
  SELECT content_hash INTO h2 FROM zzz_pick WHERE n = 2;
  SELECT content_hash INTO h3 FROM zzz_pick WHERE n = 3;
  SELECT content_hash INTO h4 FROM zzz_pick WHERE n = 4;
  UPDATE dr_manifest SET byte_state = 'pending', archive_key = NULL, stamped_hash = NULL WHERE tag = 'DR-seed' AND content_hash = h1;
  UPDATE dr_manifest SET byte_state = 'failed',  archive_key = NULL, stamped_hash = NULL, byte_error = 'gone' WHERE tag = 'DR-seed' AND content_hash = h2;
  UPDATE dr_manifest SET byte_state = 'copied',  archive_key = 'archive/' || repeat('c', 64) || '/x' WHERE tag = 'DR-seed' AND content_hash = h3;
  UPDATE dr_manifest SET byte_state = 'stamped', archive_key = 'archive/' || repeat('d', 64) || '/y', stamped_hash = 'sha256:' || repeat('d', 64) WHERE tag = 'DR-seed' AND content_hash = h4;

  got := retain_required_hashes(ARRAY[h4, h3, h2, h1, h1, 'sha256:' || repeat('0', 64)]);
  IF got IS DISTINCT FROM (SELECT array_agg(x ORDER BY x) FROM unnest(ARRAY[h1, h2]) x) THEN
    RAISE EXCEPTION 'required set wrong: % (wanted the pending and failed hashes, sorted, once each)', got;
  END IF;
  IF retain_required_hashes(NULL) <> '{}'::text[] OR retain_required_hashes('{}'::text[]) <> '{}'::text[] THEN
    RAISE EXCEPTION 'NULL / empty input must answer an empty array';
  END IF;
  IF retain_required_hashes(ARRAY['sha256:' || repeat('0', 64)]) <> '{}'::text[] THEN
    RAISE EXCEPTION 'an unknown hash must not be required';
  END IF;
  RAISE NOTICE 'OK 1: pending and failed members are required; copied, stamped and unknown are not';
END $$;

-- ------------------------------------------------------------------ 2.
DO $$
DECLARE
  h3 text; k3 text; got text[];
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  SELECT content_hash, storage_key INTO h3, k3 FROM zzz_pick WHERE n = 3;   -- copied in DR-seed
  INSERT INTO dr_releases (tag, state, components, notes)
  VALUES ('DR-zzz', 'frozen', '["nirspec"]'::jsonb, 'guard');
  INSERT INTO dr_manifest (tag, storage_key, component, product_type, backend, bucket, content_hash, byte_state)
  SELECT 'DR-zzz', m.storage_key, m.component, m.product_type, m.backend, m.bucket, m.content_hash, 'pending'
  FROM dr_manifest m WHERE m.tag = 'DR-seed' AND m.storage_key = k3;
  got := retain_required_hashes(ARRAY[h3]);
  IF got <> ARRAY[h3] THEN
    RAISE EXCEPTION 'a hash archived by one release but frozen and not archived by another must be required: %', got;
  END IF;
  UPDATE dr_manifest SET byte_state = 'copied', archive_key = 'archive/' || repeat('c', 64) || '/x' WHERE tag = 'DR-zzz';
  IF retain_required_hashes(ARRAY[h3]) <> '{}'::text[] THEN
    RAISE EXCEPTION 'once every release has archived it, the hash is not required';
  END IF;
  RAISE NOTICE 'OK 2: a hash is required while any release still needs it';
END $$;

-- ------------------------------------------------------------------ 3.
DO $$
DECLARE got text;
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  got := pg_temp.zzz_try($q$SELECT retain_required_hashes((SELECT array_agg('sha256:' || lpad(i::text, 64, '0')) FROM generate_series(1, 2001) i))::text$q$);
  IF got NOT LIKE 'ERR: retain_required_hashes: at most 2000%' THEN RAISE EXCEPTION 'oversized call accepted: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT retain_required_hashes((SELECT array_agg('sha256:' || lpad(i::text, 64, '0')) FROM generate_series(1, 2000) i))::text$q$);
  IF got <> '{}' THEN RAISE EXCEPTION '2000 hashes should be accepted: %', got; END IF;
  RAISE NOTICE 'OK 3: at most 2000 hashes per call';
END $$;

-- ------------------------------------------------------------------ 4.
DO $$
DECLARE
  got text;
  usr uuid := '22222222-2222-2222-2222-222222222222';
  adm uuid := '11111111-1111-1111-1111-111111111111';
  h1 text;
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  SELECT content_hash INTO h1 FROM zzz_pick WHERE n = 1;
  got := pg_temp.zzz_as('authenticated', usr, format($q$SELECT retain_required_hashes(ARRAY[%L])::text$q$, h1));
  IF got NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'non-admin was answered: %', got; END IF;
  got := pg_temp.zzz_as('authenticated', adm, format($q$SELECT retain_required_hashes(ARRAY[%L])::text$q$, h1));
  IF got <> format('{%s}', h1) THEN RAISE EXCEPTION 'admin should be answered: %', got; END IF;
  got := pg_temp.zzz_as('service_role', NULL, format($q$SELECT retain_required_hashes(ARRAY[%L])::text$q$, h1));
  IF got <> format('{%s}', h1) THEN RAISE EXCEPTION 'service_role should be answered: %', got; END IF;
  IF has_function_privilege('anon', 'public.retain_required_hashes(text[])', 'EXECUTE') THEN
    RAISE EXCEPTION 'anon can execute retain_required_hashes';
  END IF;
  RAISE NOTICE 'OK 4: admin and service_role answered; non-admin refused; anon has no EXECUTE';
END $$;

ROLLBACK;
