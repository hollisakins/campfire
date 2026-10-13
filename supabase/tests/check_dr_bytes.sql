-- =============================================================================
-- Data-release byte pass (P4c) on the seed's DR-seed.
-- =============================================================================
-- The bytes themselves move in Python (campfire.deploy.dr_bytes, unit-tested
-- against a fake object store); this guard covers the three RPCs that hand the
-- pass its work and record what it did:
--   1. dr_bytes_pending: every pending member, keyset paging, the key prefix
--      (one observation), the source of the frozen bytes -- the live key while
--      the live row still holds the frozen hash, the retained archive copy when
--      it does not, NULL when the bytes are gone -- and the stamped object of
--      another release with the same science to reuse (D-P4-2);
--   2. dr_bytes_mark refuses malformed batches (state, archive key, stamp hash,
--      stamping a non-FITS, an unregistered archive object, unknown members,
--      oversized batches) without writing anything;
--   3. a mixed batch: member states, archive registry rows (root archive,
--      status archived, the stamped hash for a stamp, archived_from_key,
--      program_slug), the error kept for a failure, one archive deploy event,
--      the touched member partition's export file back to pending (others
--      untouched), state copying; marking the same object again inserts nothing;
--   4. every member done + exports rebuilt -> ready; dr_bytes_summary;
--   5. a published release refuses marks; non-admins are refused everywhere.
-- Run: psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_dr_bytes.sql
\set ON_ERROR_STOP on
BEGIN;

-- The seed publishes DR-seed; the byte pass runs before publish.
UPDATE dr_releases SET state = 'frozen', published_at = NULL WHERE tag = 'DR-seed';

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

-- One call, its error text (or 'ok').
CREATE FUNCTION pg_temp.zzz_try(p_sql text) RETURNS text
LANGUAGE plpgsql AS $$
BEGIN
  EXECUTE p_sql;
  RETURN 'ok';
EXCEPTION WHEN OTHERS THEN
  RETURN 'ERR: ' || SQLERRM;
END $$;

-- The members the checks use.
CREATE TEMP TABLE zzz_pick AS
WITH spec AS (
  SELECT storage_key, row_number() OVER (ORDER BY storage_key) AS i FROM dr_members
  WHERE tag = 'DR-seed' AND product_type = 'nirspec_spec'
), js AS (
  SELECT storage_key, row_number() OVER (ORDER BY storage_key) AS i FROM dr_members
  WHERE tag = 'DR-seed' AND product_type = 'spectrum_json'
)
SELECT (SELECT storage_key FROM spec WHERE i = 1) AS s1,
       (SELECT storage_key FROM spec WHERE i = 2) AS s2,
       (SELECT storage_key FROM spec WHERE i = 3) AS s3,
       (SELECT storage_key FROM js WHERE i = 1) AS j1,
       (SELECT storage_key FROM js WHERE i = 2) AS j2,
       (SELECT storage_key FROM js WHERE i = 3) AS j3;
-- the seed's registry rows carry no science hash; give the two FITS one each
UPDATE dr_members m SET science_hash = 'sha256:' || repeat(CASE WHEN m.storage_key = p.s1 THEN '1' ELSE '2' END, 64)
FROM zzz_pick p WHERE m.tag = 'DR-seed' AND m.storage_key IN (p.s1, p.s2);

DO $$
DECLARE
  p zzz_pick%ROWTYPE;
  n int; n2 int; got text; r record; res jsonb;
  obs_prefix text;
  prev_key text := 'archive/' || repeat('c', 64) || '/products/nirspec/prev/prev_spec.fits';
  ret_key text;
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  SELECT * INTO p FROM zzz_pick;
  IF p.s1 IS NULL OR p.s2 IS NULL OR p.j3 IS NULL THEN RAISE EXCEPTION 'seed: DR-seed lacks the members this guard needs: %', to_jsonb(p); END IF;

  -- ------------------------------------------------------------------ 1.
  SELECT count(*) INTO n FROM dr_bytes_pending('DR-seed', NULL, 2000);
  IF n <> (SELECT count(*) FROM dr_members WHERE tag = 'DR-seed') THEN
    RAISE EXCEPTION 'pending should list every member: % vs %', n, (SELECT count(*) FROM dr_members WHERE tag = 'DR-seed');
  END IF;
  IF EXISTS (SELECT 1 FROM dr_bytes_pending('DR-seed', NULL, 2000) b
             WHERE b.source_root IS DISTINCT FROM 'live' OR b.source_key <> b.storage_key
                OR b.reuse_archive_key IS NOT NULL OR b.registry->>'content_type' IS NULL) THEN
    RAISE EXCEPTION 'untouched seed members should all come from their live keys';
  END IF;
  -- keyset paging covers each member once
  WITH RECURSIVE pages(after, k, depth) AS (
    SELECT NULL::text, NULL::text, 0
    UNION ALL
    SELECT q.last, q.last, pages.depth + 1 FROM pages
    CROSS JOIN LATERAL (SELECT max(b.storage_key) AS last FROM dr_bytes_pending('DR-seed', pages.after, 50) b) q
    WHERE q.last IS NOT NULL AND pages.depth < 100
  ) SELECT count(*) - 1 INTO n FROM pages;
  IF n <> ceil((SELECT count(*) FROM dr_members WHERE tag = 'DR-seed') / 50.0) THEN
    RAISE EXCEPTION 'paging by 50 took % pages', n;
  END IF;
  -- one observation through the key prefix
  obs_prefix := regexp_replace(p.s1, '[^/]+$', '');
  SELECT count(*) INTO n FROM dr_bytes_pending('DR-seed', NULL, 2000, false, obs_prefix);
  SELECT count(*) INTO n2 FROM dr_members WHERE tag = 'DR-seed' AND storage_key LIKE obs_prefix || '%';
  IF n <> n2 OR n = 0 THEN RAISE EXCEPTION 'prefix % gave % members, expected %', obs_prefix, n, n2; END IF;

  -- the live object of j2 moved on and nothing retained it: bytes gone
  UPDATE storage_objects SET content_hash = 'sha256:' || repeat('b', 64)
   WHERE storage_key = p.j2 AND root = 'live';
  -- j3 moved on too, but retain-on-supersede kept the frozen bytes
  UPDATE storage_objects SET content_hash = 'sha256:' || repeat('d', 64)
   WHERE storage_key = p.j3 AND root = 'live';
  ret_key := 'archive/' || substr((SELECT content_hash FROM dr_members WHERE tag = 'DR-seed' AND storage_key = p.j3), 8)
             || '/' || substr(p.j3, 6);
  INSERT INTO storage_objects (backend, bucket, storage_key, root, status, content_hash, size_bytes, content_type,
                               product_type, program_slug, archived_from_key, archived_at)
  SELECT m.backend, m.bucket, ret_key, 'archive', 'archived', m.content_hash, m.size_bytes, 'application/json',
         m.product_type, m.program_slug, m.storage_key, now()
  FROM dr_members m WHERE m.tag = 'DR-seed' AND m.storage_key = p.j3;
  -- an earlier release stamped s2 from the same science
  INSERT INTO dr_releases (tag, state, components) VALUES ('DR-zzz-prev', 'frozen', '["nirspec"]');
  INSERT INTO dr_members (tag, storage_key, component, program_slug, product_type, spectrum_id, backend, bucket,
                          content_hash, science_hash, size_bytes, byte_state, archive_key, stamped_hash)
  SELECT 'DR-zzz-prev', m.storage_key, m.component, m.program_slug, m.product_type, m.spectrum_id, m.backend, m.bucket,
         m.content_hash, m.science_hash, m.size_bytes, 'stamped', prev_key, 'sha256:' || repeat('c', 64)
  FROM dr_members m WHERE m.tag = 'DR-seed' AND m.storage_key = p.s2;
  INSERT INTO storage_objects (backend, bucket, storage_key, root, status, content_hash, science_hash, size_bytes,
                               content_type, product_type, program_slug, archived_from_key, archived_at)
  SELECT m.backend, m.bucket, prev_key, 'archive', 'archived', 'sha256:' || repeat('c', 64), m.science_hash, m.size_bytes,
         'application/fits', m.product_type, m.program_slug, m.storage_key, now()
  FROM dr_members m WHERE m.tag = 'DR-seed' AND m.storage_key = p.s2;

  SELECT * INTO r FROM dr_bytes_pending('DR-seed', NULL, 2000) b WHERE b.storage_key = p.j2;
  IF r.source_key IS NOT NULL OR r.source_root IS NOT NULL THEN RAISE EXCEPTION 'gone bytes should have no source: %', to_jsonb(r); END IF;
  SELECT * INTO r FROM dr_bytes_pending('DR-seed', NULL, 2000) b WHERE b.storage_key = p.j3;
  IF r.source_root <> 'archive' OR r.source_key <> ret_key OR r.registry->>'retained_key' <> ret_key THEN
    RAISE EXCEPTION 'retained copy should be the source: %', to_jsonb(r);
  END IF;
  SELECT * INTO r FROM dr_bytes_pending('DR-seed', NULL, 2000) b WHERE b.storage_key = p.s2;
  IF r.reuse_archive_key IS DISTINCT FROM prev_key OR r.reuse_stamped_hash <> 'sha256:' || repeat('c', 64)
     OR r.reuse_size_bytes IS DISTINCT FROM (SELECT size_bytes FROM dr_members WHERE tag = 'DR-seed' AND storage_key = p.s2) THEN
    RAISE EXCEPTION 'stamped object of the earlier release should be offered for reuse: %', to_jsonb(r);
  END IF;
  SELECT * INTO r FROM dr_bytes_pending('DR-seed', NULL, 2000) b WHERE b.storage_key = p.s1;
  IF r.reuse_archive_key IS NOT NULL THEN RAISE EXCEPTION 'nothing to reuse for s1'; END IF;
  RAISE NOTICE 'OK 1: pending (paging, prefix, live / retained / gone sources, cross-release reuse)';

  -- ------------------------------------------------------------------ 2.
  got := pg_temp.zzz_try(format($q$SELECT dr_bytes_mark('DR-seed', '[{"storage_key": %s, "state": "done"}]')$q$, to_jsonb(p.j1)));
  IF got NOT LIKE 'ERR: invalid byte state%' THEN RAISE EXCEPTION 'bad state accepted: %', got; END IF;
  got := pg_temp.zzz_try(format($q$SELECT dr_bytes_mark('DR-seed', '[{"storage_key": %s, "state": "copied", "archive_key": "data/x"}]')$q$, to_jsonb(p.j1)));
  IF got NOT LIKE 'ERR: a copied member needs an archive key under archive/%' THEN RAISE EXCEPTION 'live key accepted as archive key: %', got; END IF;
  got := pg_temp.zzz_try(format($q$SELECT dr_bytes_mark('DR-seed', '[{"storage_key": %s, "state": "stamped", "archive_key": "archive/x"}]')$q$, to_jsonb(p.s1)));
  IF got NOT LIKE 'ERR: a stamped member needs the sha256%' THEN RAISE EXCEPTION 'stamp without hash accepted: %', got; END IF;
  got := pg_temp.zzz_try(format($q$SELECT dr_bytes_mark('DR-seed', '[{"storage_key": %s, "state": "stamped", "archive_key": "archive/x", "stamped_hash": "sha256:%s", "registry": {}}]')$q$, to_jsonb(p.j1), repeat('e', 64)));
  IF got NOT LIKE 'ERR: only NIRSpec FITS finals are stamped%' THEN RAISE EXCEPTION 'json stamp accepted: %', got; END IF;
  got := pg_temp.zzz_try(format($q$SELECT dr_bytes_mark('DR-seed', '[{"storage_key": %s, "state": "copied", "archive_key": "archive/nowhere"}]')$q$, to_jsonb(p.j1)));
  IF got NOT LIKE 'ERR: archive object archive/nowhere is not registered%' THEN RAISE EXCEPTION 'unregistered archive object accepted: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_bytes_mark('DR-seed', '[{"storage_key": "data/no/such", "state": "failed"}]')$q$);
  IF got NOT LIKE 'ERR: no member data/no/such%' THEN RAISE EXCEPTION 'unknown member accepted: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_bytes_mark('DR-seed', (SELECT jsonb_agg(jsonb_build_object('storage_key', 'k' || g, 'state', 'failed')) FROM generate_series(1, 2001) g))$q$);
  IF got NOT LIKE 'ERR: at most 2000 results%' THEN RAISE EXCEPTION 'oversized batch accepted: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_bytes_mark('DR-seed', '{"storage_key": "x"}')$q$);
  IF got NOT LIKE 'ERR: results must be a json array%' THEN RAISE EXCEPTION 'non-array accepted: %', got; END IF;
  IF EXISTS (SELECT 1 FROM dr_members WHERE tag = 'DR-seed' AND byte_state <> 'pending') THEN
    RAISE EXCEPTION 'a refused batch wrote member states';
  END IF;
  RAISE NOTICE 'OK 2: malformed batches refused, nothing written';
END $$;

DO $$
DECLARE
  p zzz_pick%ROWTYPE;
  res jsonb; n int; part text;
  s1_key text := 'archive/' || repeat('f', 64) || '/stamped/s1_spec.fits';
  j1_key text;
  prev_key text := 'archive/' || repeat('c', 64) || '/products/nirspec/prev/prev_spec.fits';
  events_before int;
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  SELECT * INTO p FROM zzz_pick;
  j1_key := 'archive/' || substr((SELECT content_hash FROM dr_members WHERE tag = 'DR-seed' AND storage_key = p.j1), 8) || '/' || substr(p.j1, 6);

  -- every export file built and ready first
  PERFORM dr_export_begin('DR-seed');
  PERFORM dr_export_mark('DR-seed', e.stream, e.partition, 'building') FROM dr_exports e WHERE e.tag = 'DR-seed';
  PERFORM dr_export_mark('DR-seed', e.stream, e.partition, 'ready', 'sha256:' || repeat('a', 64), 1, 1)
  FROM dr_exports e WHERE e.tag = 'DR-seed';
  IF (SELECT state FROM dr_releases WHERE tag = 'DR-seed') <> 'exported' THEN RAISE EXCEPTION 'fixture: release not exported'; END IF;
  SELECT COALESCE(program_slug, component) INTO part FROM dr_members WHERE tag = 'DR-seed' AND storage_key = p.s1;
  SELECT count(*) INTO events_before FROM deploy_events WHERE action = 'archive' AND metadata->>'dr_tag' = 'DR-seed';

  -- ------------------------------------------------------------------ 3.
  res := dr_bytes_mark('DR-seed', jsonb_build_array(
    jsonb_build_object('storage_key', p.s1, 'state', 'stamped', 'archive_key', s1_key,
                       'stamped_hash', 'sha256:' || repeat('f', 64),
                       'registry', jsonb_build_object('size_bytes', 12345, 'content_type', 'application/fits',
                                                      'instrument', 'nirspec', 'observation', 'no-such-obs', 'deployment_id', -1, 'cfpipe_version', '9.9.9')),
    jsonb_build_object('storage_key', p.j1, 'state', 'copied', 'archive_key', j1_key,
                       'registry', jsonb_build_object('content_type', 'application/json')),
    jsonb_build_object('storage_key', p.j2, 'state', 'failed', 'error', 'frozen bytes are gone'),
    jsonb_build_object('storage_key', p.s2, 'state', 'stamped', 'archive_key', prev_key,
                       'stamped_hash', 'sha256:' || repeat('c', 64))));
  IF (res->>'marked')::int <> 4 OR (res->>'stamped')::int <> 2 OR (res->>'copied')::int <> 1
     OR (res->>'failed')::int <> 1 OR (res->>'registered')::int <> 2 OR res->>'state' <> 'copying' THEN
    RAISE EXCEPTION 'mixed batch result wrong: %', res;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM storage_objects so JOIN dr_members m ON m.tag = 'DR-seed' AND m.storage_key = p.s1
                 WHERE so.storage_key = s1_key AND so.root = 'archive' AND so.status = 'archived'
                   AND so.content_hash = 'sha256:' || repeat('f', 64) AND so.science_hash = m.science_hash
                   AND so.size_bytes = 12345 AND so.archived_from_key = p.s1 AND so.observation IS NULL AND so.deployment_id IS NULL AND so.program_slug IS NOT DISTINCT FROM m.program_slug
                   AND so.spectrum_id IS NOT DISTINCT FROM m.spectrum_id AND so.cfpipe_version = '9.9.9'
                   AND so.product_type = 'nirspec_spec') THEN
    RAISE EXCEPTION 'stamped archive row wrong: %', (SELECT to_jsonb(so) FROM storage_objects so WHERE so.storage_key = s1_key);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM storage_objects so JOIN dr_members m ON m.tag = 'DR-seed' AND m.storage_key = p.j1
                 WHERE so.storage_key = j1_key AND so.content_hash = m.content_hash AND so.size_bytes = m.size_bytes
                   AND so.archived_from_key = p.j1) THEN
    RAISE EXCEPTION 'copied archive row wrong';
  END IF;
  IF (SELECT byte_state || '|' || archive_key || '|' || stamped_hash FROM dr_members WHERE tag = 'DR-seed' AND storage_key = p.s1)
     <> 'stamped|' || s1_key || '|sha256:' || repeat('f', 64) THEN
    RAISE EXCEPTION 's1 member not stamped';
  END IF;
  IF (SELECT byte_state || '|' || byte_error FROM dr_members WHERE tag = 'DR-seed' AND storage_key = p.j2) <> 'failed|frozen bytes are gone' THEN
    RAISE EXCEPTION 'failure not recorded';
  END IF;
  IF (SELECT archive_key FROM dr_members WHERE tag = 'DR-seed' AND storage_key = p.s2) <> prev_key THEN
    RAISE EXCEPTION 'reused stamp not recorded';
  END IF;
  SELECT count(*) INTO n FROM deploy_events WHERE action = 'archive' AND metadata->>'dr_tag' = 'DR-seed';
  IF n <> events_before + 1 THEN RAISE EXCEPTION 'expected one archive event per batch, got %', n - events_before; END IF;
  -- the touched member partition is back to pending, the rest stay ready
  IF (SELECT state FROM dr_exports WHERE tag = 'DR-seed' AND stream = 'members' AND partition = part) <> 'pending' THEN
    RAISE EXCEPTION 'member partition % not invalidated', part;
  END IF;
  IF EXISTS (SELECT 1 FROM dr_exports WHERE tag = 'DR-seed' AND state <> 'ready'
             AND NOT (stream = 'members' AND partition IN (SELECT COALESCE(m.program_slug, m.component) FROM dr_members m
                                                           WHERE m.tag = 'DR-seed' AND m.storage_key IN (p.s1, p.s2, p.j1, p.j2)))) THEN
    RAISE EXCEPTION 'unrelated export files were invalidated';
  END IF;
  -- the earlier release is untouched
  IF (SELECT byte_state FROM dr_members WHERE tag = 'DR-zzz-prev' AND storage_key = p.s2) <> 'stamped' THEN
    RAISE EXCEPTION 'the other release changed';
  END IF;
  -- the same object again: no second registry row, no error
  res := dr_bytes_mark('DR-seed', jsonb_build_array(
    jsonb_build_object('storage_key', p.j1, 'state', 'copied', 'archive_key', j1_key,
                       'registry', jsonb_build_object('content_type', 'application/json'))));
  IF (res->>'registered')::int <> 0 OR (SELECT count(*) FROM storage_objects WHERE storage_key = j1_key) <> 1 THEN
    RAISE EXCEPTION 're-marking registered again: %', res;
  END IF;
  -- a member file being BUILT when the byte pass changes its rows goes back
  -- to pending, and the builder's ready mark is then refused (Codex on #601)
  PERFORM dr_export_mark('DR-seed', 'members',
    (SELECT COALESCE(program_slug, component) FROM dr_members WHERE tag = 'DR-seed' AND storage_key = p.j1), 'building');
  PERFORM dr_bytes_mark('DR-seed', jsonb_build_array(
    jsonb_build_object('storage_key', p.j1, 'state', 'copied', 'archive_key', j1_key)));
  IF (SELECT state FROM dr_exports WHERE tag = 'DR-seed' AND stream = 'members' AND partition =
        (SELECT COALESCE(program_slug, component) FROM dr_members WHERE tag = 'DR-seed' AND storage_key = p.j1)) <> 'pending' THEN
    RAISE EXCEPTION 'a building member file was not invalidated';
  END IF;
  -- a science hash the pass computed for a member frozen without one is kept
  -- on the member and its archive row; one that disagrees is refused (Codex on #601)
  IF (SELECT science_hash FROM dr_members WHERE tag = 'DR-seed' AND storage_key = p.s3) IS NOT NULL THEN
    RAISE EXCEPTION 'fixture: s3 should have no science hash';
  END IF;
  res := dr_bytes_mark('DR-seed', jsonb_build_array(
    jsonb_build_object('storage_key', p.s3, 'state', 'stamped', 'archive_key', 'archive/' || repeat('7', 64) || '/s3_spec.fits',
                       'stamped_hash', 'sha256:' || repeat('7', 64), 'science_hash', 'sha256:' || repeat('3', 64),
                       'registry', jsonb_build_object('size_bytes', 10))));
  IF (SELECT science_hash FROM dr_members WHERE tag = 'DR-seed' AND storage_key = p.s3) <> 'sha256:' || repeat('3', 64)
     OR (SELECT science_hash FROM storage_objects WHERE storage_key = 'archive/' || repeat('7', 64) || '/s3_spec.fits') <> 'sha256:' || repeat('3', 64) THEN
    RAISE EXCEPTION 'computed science hash not persisted';
  END IF;
  BEGIN
    PERFORM dr_bytes_mark('DR-seed', jsonb_build_array(
      jsonb_build_object('storage_key', p.s1, 'state', 'failed', 'science_hash', 'sha256:' || repeat('4', 64))));
    RAISE EXCEPTION 'a disagreeing science hash was accepted';
  EXCEPTION WHEN OTHERS THEN
    IF SQLERRM NOT LIKE 'science hash of % disagrees%' THEN RAISE; END IF;
  END;
  RAISE NOTICE 'OK 3: mixed batch (states, archive rows, event, export invalidation, idempotent, science hash kept)';

  -- ------------------------------------------------------------------ 4.
  res := dr_bytes_mark('DR-seed', (
    SELECT jsonb_agg(jsonb_build_object('storage_key', m.storage_key, 'state', 'copied',
                                        'archive_key', 'archive/' || md5(m.storage_key) || md5(m.storage_key) || '/' || substr(m.storage_key, 6),
                                        'registry', jsonb_build_object('content_type', 'application/octet-stream')))
    FROM dr_members m WHERE m.tag = 'DR-seed' AND m.byte_state IN ('pending', 'failed')));
  IF res->>'state' <> 'exporting' THEN RAISE EXCEPTION 'every member done but member files pending: expected exporting, got %', res; END IF;
  PERFORM dr_export_mark('DR-seed', e.stream, e.partition, 'building') FROM dr_exports e
   WHERE e.tag = 'DR-seed' AND e.state <> 'ready';
  PERFORM dr_export_mark('DR-seed', e.stream, e.partition, 'ready', 'sha256:' || repeat('a', 64), 1, 1)
  FROM dr_exports e WHERE e.tag = 'DR-seed' AND e.state = 'building';
  IF (SELECT state FROM dr_releases WHERE tag = 'DR-seed') <> 'ready' THEN
    RAISE EXCEPTION 'every member and file done: expected ready, got %', (SELECT state FROM dr_releases WHERE tag = 'DR-seed');
  END IF;
  SELECT count(*) INTO n FROM dr_bytes_pending('DR-seed', NULL, 2000, true);
  IF n <> 0 THEN RAISE EXCEPTION 'nothing should be pending, got %', n; END IF;
  res := dr_bytes_summary('DR-seed');
  IF (res->>'pending_bytes')::bigint <> 0 OR res->'by_state' ? 'pending' OR res->'by_state' ? 'failed'
     OR ((res->'by_state'->'copied'->>'count')::int + (res->'by_state'->'stamped'->>'count')::int) <> (res->>'members')::int
     OR (res->'by_state'->'stamped'->>'count')::int <> 3 THEN
    RAISE EXCEPTION 'summary wrong: %', res;
  END IF;
  RAISE NOTICE 'OK 4: all done -> ready; summary';
END $$;

-- ------------------------------------------------------------------ 5.
DO $$
DECLARE
  p zzz_pick%ROWTYPE;
  got text;
  usr uuid := '22222222-2222-2222-2222-222222222222';
  adm uuid := '11111111-1111-1111-1111-111111111111';
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  SELECT * INTO p FROM zzz_pick;
  got := pg_temp.zzz_as('authenticated', usr, $q$SELECT count(*)::text FROM dr_bytes_pending('DR-seed')$q$);
  IF got NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'non-admin read the pending list: %', got; END IF;
  got := pg_temp.zzz_as('authenticated', usr, format($q$SELECT dr_bytes_mark('DR-seed', '[{"storage_key": %s, "state": "pending"}]')::text$q$, to_jsonb(p.j1)));
  IF got NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'non-admin marked: %', got; END IF;
  got := pg_temp.zzz_as('authenticated', usr, $q$SELECT dr_bytes_summary('DR-seed')::text$q$);
  IF got NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'non-admin read the summary: %', got; END IF;
  got := pg_temp.zzz_as('authenticated', adm, $q$SELECT dr_bytes_summary('DR-seed')->>'state'$q$);
  IF got <> 'ready' THEN RAISE EXCEPTION 'admin summary: %', got; END IF;
  IF has_function_privilege('anon', 'public.dr_bytes_mark(text, jsonb)', 'EXECUTE')
     OR has_function_privilege('anon', 'public.dr_bytes_pending(text, text, integer, boolean, text)', 'EXECUTE')
     OR has_function_privilege('anon', 'public.dr_bytes_summary(text)', 'EXECUTE') THEN
    RAISE EXCEPTION 'anon can execute a byte-pass RPC';
  END IF;
  UPDATE dr_releases SET state = 'published', published_at = now() WHERE tag = 'DR-seed';
  got := pg_temp.zzz_try(format($q$SELECT dr_bytes_mark('DR-seed', '[{"storage_key": %s, "state": "pending"}]')$q$, to_jsonb(p.j1)));
  IF got NOT LIKE 'ERR: release DR-seed is published%' THEN RAISE EXCEPTION 'published release accepted a mark: %', got; END IF;
  RAISE NOTICE 'OK 5: published refuses marks; non-admins refused; anon has no EXECUTE';
END $$;

ROLLBACK;
