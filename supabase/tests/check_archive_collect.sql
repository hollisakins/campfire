-- =============================================================================
-- The archive is exactly the releases (P4e, decisions D-P4-6 / D-P4-7)
-- =============================================================================
-- An archive object is referenced by a release when its manifest names it as
-- the byte pass's copy (archive_key) or as a retained copy (storage_key +
-- content_hash against archived_from_key + content_hash). On the seed's
-- DR-seed this guard pins:
--   1. archive_object_references: both ways of referencing, several tags,
--      none; archive_collectable lists exactly the unreferenced objects and
--      pages on id;
--   2. dr_purge_archive_plan: the objects only the tag references (a copy
--      shared with another tag is excluded, a retained copy for its member is
--      included); refuses a published tag; waits for a live archive claim;
--   3. the three phases: dr_archive_collect_begin claims (archived ->
--      collecting; refuses a key another tag references, a published p_tag,
--      a live key; counts an already claimed key), a collecting row is offered
--      to nobody (dr_bytes_pending source, archive_register_retained,
--      dr_bytes_mark) but is listed by the plan and by archive_collectable so
--      an interrupted run resumes, dr_archive_collect_finish deletes the rows
--      (one `collect` event; a non-collecting key is skipped and counted);
--   4. a withdrawn release keeps its references (never collectable);
--   5. non-admins are refused; anon has no EXECUTE; the references helper is
--      service_role-only; archive_register_retained inserts, accepts an
--      archived twin, refuses a non-archive key.
-- Run: psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_archive_collect.sql
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

-- The seed's DR-seed is published; the fixtures below need it unpublished.
UPDATE dr_releases SET state = 'frozen', published_at = NULL, archive_claim = NULL, archive_claimed_at = NULL
 WHERE tag = 'DR-seed';

-- Three DR-seed members (distinct hashes) and the archive objects of the story:
--   A: DR-seed's own byte-pass copy (archive_key)            -> only DR-seed
--   B: a copy DR-seed AND DR-zzz-other both name (archive_key) -> shared
--   C: a retained copy for DR-seed's member (archived_from_key + hash) -> only DR-seed
--   D: an orphan nobody names                                   -> collectable
CREATE TEMP TABLE zzz_m AS
SELECT storage_key, content_hash, backend, bucket, product_type, program_slug, size_bytes,
       row_number() OVER (ORDER BY storage_key) AS n
FROM (
  SELECT DISTINCT ON (content_hash) storage_key, content_hash, backend, bucket, product_type, program_slug, size_bytes
  FROM dr_manifest WHERE tag = 'DR-seed' AND product_type IN ('spectrum_json', 'zfit', 'spectrum_1d_json', 'nirspec_lines_json')
  ORDER BY content_hash, storage_key
) d
LIMIT 3;

CREATE TEMP TABLE zzz_keys (name text PRIMARY KEY, key text);
INSERT INTO zzz_keys VALUES
  ('A', 'archive/' || repeat('a', 64) || '/products/zzz/a.json'),
  ('B', 'archive/' || repeat('b', 64) || '/products/zzz/b.json'),
  ('C', NULL),
  ('D', 'archive/' || repeat('e', 64) || '/products/zzz/d.json');

DO $$
DECLARE
  m1 zzz_m%ROWTYPE; m2 zzz_m%ROWTYPE; m3 zzz_m%ROWTYPE;
  ka text; kb text; kc text; kd text;
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  IF (SELECT count(*) FROM zzz_m) <> 3 THEN RAISE EXCEPTION 'seed: need 3 DR-seed sidecar members with distinct hashes'; END IF;
  SELECT * INTO m1 FROM zzz_m WHERE n = 1;
  SELECT * INTO m2 FROM zzz_m WHERE n = 2;
  SELECT * INTO m3 FROM zzz_m WHERE n = 3;
  SELECT key INTO ka FROM zzz_keys WHERE name = 'A';
  SELECT key INTO kb FROM zzz_keys WHERE name = 'B';
  SELECT key INTO kd FROM zzz_keys WHERE name = 'D';
  kc := 'archive/' || substr(m3.content_hash, 8) || '/' || substr(m3.storage_key, 6);
  UPDATE zzz_keys SET key = kc WHERE name = 'C';

  -- A: DR-seed copied m1
  UPDATE dr_manifest SET byte_state = 'copied', archive_key = ka WHERE tag = 'DR-seed' AND storage_key = m1.storage_key;
  INSERT INTO storage_objects (backend, bucket, storage_key, root, status, content_hash, size_bytes, content_type,
                               product_type, program_slug, archived_from_key, archived_at)
  VALUES (m1.backend, m1.bucket, ka, 'archive', 'archived', m1.content_hash, 10, 'application/json',
          m1.product_type, m1.program_slug, m1.storage_key, now() - interval '3 days');
  -- B: DR-seed and DR-zzz-other both copied m2 to the same object
  UPDATE dr_manifest SET byte_state = 'copied', archive_key = kb WHERE tag = 'DR-seed' AND storage_key = m2.storage_key;
  INSERT INTO dr_releases (tag, state, components, frozen_at) VALUES ('DR-zzz-other', 'frozen', '["nirspec"]', now());
  INSERT INTO dr_manifest (tag, storage_key, component, program_slug, product_type, backend, bucket, content_hash, size_bytes, byte_state, archive_key)
  VALUES ('DR-zzz-other', m2.storage_key, 'nirspec', m2.program_slug, m2.product_type, m2.backend, m2.bucket, m2.content_hash, m2.size_bytes, 'copied', kb);
  INSERT INTO storage_objects (backend, bucket, storage_key, root, status, content_hash, size_bytes, content_type,
                               product_type, program_slug, archived_from_key, archived_at)
  VALUES (m2.backend, m2.bucket, kb, 'archive', 'archived', m2.content_hash, 20, 'application/json',
          m2.product_type, m2.program_slug, m2.storage_key, now() - interval '2 days');
  -- C: the deploy gate retained m3's bytes before an overwrite; DR-seed's pass has not run on it
  UPDATE dr_manifest SET byte_state = 'pending', archive_key = NULL WHERE tag = 'DR-seed' AND storage_key = m3.storage_key;
  INSERT INTO storage_objects (backend, bucket, storage_key, root, status, content_hash, size_bytes, content_type,
                               product_type, program_slug, archived_from_key, archived_at)
  VALUES (m3.backend, m3.bucket, kc, 'archive', 'archived', m3.content_hash, 30, 'application/json',
          m3.product_type, m3.program_slug, m3.storage_key, now() - interval '1 day');
  -- D: nobody's
  INSERT INTO storage_objects (backend, bucket, storage_key, root, status, content_hash, size_bytes, content_type,
                               product_type, program_slug, archived_from_key, archived_at)
  VALUES ('osn', 'data', kd, 'archive', 'archived', 'sha256:' || repeat('e', 64), 40, 'application/json',
          'zfit', m1.program_slug, 'data/products/zzz/d.json', now() - interval '4 days');
END $$;

-- ------------------------------------------------------------------ 1.
DO $$
DECLARE
  ka text; kb text; kc text; kd text; refs text[]; got text[]; cnt int; cnt_seed int;
  m3 zzz_m%ROWTYPE;
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  SELECT key INTO ka FROM zzz_keys WHERE name = 'A';
  SELECT key INTO kb FROM zzz_keys WHERE name = 'B';
  SELECT key INTO kc FROM zzz_keys WHERE name = 'C';
  SELECT key INTO kd FROM zzz_keys WHERE name = 'D';
  SELECT * INTO m3 FROM zzz_m WHERE n = 3;
  refs := archive_object_references(ka, NULL, NULL);
  IF refs <> ARRAY['DR-seed'] THEN RAISE EXCEPTION 'A should be referenced by DR-seed only: %', refs; END IF;
  refs := archive_object_references(kb, NULL, NULL);
  IF refs <> ARRAY['DR-seed', 'DR-zzz-other'] THEN RAISE EXCEPTION 'B should be referenced by both: %', refs; END IF;
  refs := archive_object_references(kc, m3.storage_key, m3.content_hash);
  IF refs <> ARRAY['DR-seed'] THEN RAISE EXCEPTION 'C (retained copy) should be referenced by DR-seed: %', refs; END IF;
  refs := archive_object_references(kc, m3.storage_key, 'sha256:' || repeat('f', 64));
  IF refs <> '{}' THEN RAISE EXCEPTION 'a retained copy of other bytes is not a reference: %', refs; END IF;
  refs := archive_object_references(kd, 'data/products/zzz/d.json', 'sha256:' || repeat('e', 64));
  IF refs <> '{}' THEN RAISE EXCEPTION 'D should be unreferenced: %', refs; END IF;

  -- collectable: D, and only D among the fixtures (the seed itself may carry
  -- unreferenced archive rows; those are collectable too and must not be any of A/B/C)
  SELECT array_agg(c.storage_key ORDER BY c.storage_key) INTO got FROM archive_collectable(0, 2000) c
   WHERE c.storage_key IN (ka, kb, kc, kd);
  IF got <> ARRAY[kd] THEN RAISE EXCEPTION 'collectable among the fixtures should be D only: %', got; END IF;
  -- paging on id covers every collectable row once
  WITH RECURSIVE pages(after, depth) AS (
    SELECT 0::bigint, 0
    UNION ALL
    SELECT q.last, pages.depth + 1 FROM pages
    CROSS JOIN LATERAL (SELECT max(c.id) AS last FROM archive_collectable(pages.after, 1) c) q
    WHERE q.last IS NOT NULL AND pages.depth < 500
  ) SELECT count(*) - 1 INTO cnt FROM pages;
  SELECT count(*) INTO cnt_seed FROM archive_collectable(0, 2000);
  IF cnt <> cnt_seed THEN RAISE EXCEPTION 'paging by 1 took % pages for % rows', cnt, cnt_seed; END IF;
  IF pg_temp.zzz_try('SELECT count(*)::text FROM archive_collectable(0, 0)') NOT LIKE 'ERR: archive_collectable: p_limit%' THEN
    RAISE EXCEPTION 'p_limit 0 accepted';
  END IF;
  RAISE NOTICE 'OK 1: references both ways; collectable = unreferenced; paging';
END $$;

-- ------------------------------------------------------------------ 2.
DO $$
DECLARE
  ka text; kb text; kc text; kd text; got text[]; err text;
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  SELECT key INTO ka FROM zzz_keys WHERE name = 'A';
  SELECT key INTO kb FROM zzz_keys WHERE name = 'B';
  SELECT key INTO kc FROM zzz_keys WHERE name = 'C';
  SELECT key INTO kd FROM zzz_keys WHERE name = 'D';
  SELECT array_agg(p.storage_key ORDER BY p.storage_key) INTO got FROM dr_purge_archive_plan('DR-seed') p;
  IF got IS DISTINCT FROM (SELECT array_agg(x ORDER BY x) FROM unnest(ARRAY[ka, kc]) x) THEN
    RAISE EXCEPTION 'the plan for DR-seed should be A (own copy) and C (retained for its member), not B (shared) or D (nobody''s): %', got;
  END IF;
  IF (SELECT sum(p.size_bytes) FROM dr_purge_archive_plan('DR-seed') p) <> 40 THEN
    RAISE EXCEPTION 'plan sizes wrong';
  END IF;
  -- the other tag's plan: nothing (B is shared with DR-seed)
  IF EXISTS (SELECT 1 FROM dr_purge_archive_plan('DR-zzz-other')) THEN
    RAISE EXCEPTION 'DR-zzz-other references only the shared object; its plan must be empty';
  END IF;
  -- published: refused
  UPDATE dr_releases SET state = 'published', published_at = now() WHERE tag = 'DR-seed';
  err := pg_temp.zzz_try('SELECT count(*)::text FROM dr_purge_archive_plan(''DR-seed'')');
  IF err NOT LIKE 'ERR: release DR-seed is published%' THEN RAISE EXCEPTION 'published plan accepted: %', err; END IF;
  UPDATE dr_releases SET state = 'frozen', published_at = NULL WHERE tag = 'DR-seed';
  -- a live archive claim: wait
  UPDATE dr_releases SET archive_claim = gen_random_uuid(), archive_claimed_at = now() WHERE tag = 'DR-seed';
  err := pg_temp.zzz_try('SELECT count(*)::text FROM dr_purge_archive_plan(''DR-seed'')');
  IF err NOT LIKE 'ERR: release DR-seed is being archived%' THEN RAISE EXCEPTION 'live claim ignored: %', err; END IF;
  UPDATE dr_releases SET archive_claim = NULL, archive_claimed_at = NULL WHERE tag = 'DR-seed';
  err := pg_temp.zzz_try('SELECT count(*)::text FROM dr_purge_archive_plan(''DR-nope'')');
  IF err NOT LIKE 'ERR: no release%' THEN RAISE EXCEPTION 'unknown tag accepted: %', err; END IF;
  RAISE NOTICE 'OK 2: purge plan = only the tag''s objects; published refused; claim waited for';
END $$;

-- ------------------------------------------------------------------ 3.
DO $$
DECLARE
  ka text; kb text; kc text; kd text; res jsonb; err text; cnt int; before_events int;
  m3 zzz_m%ROWTYPE;
  claim_a uuid := '00000000-0000-4000-8000-00000000000a';
  claim_b uuid := '00000000-0000-4000-8000-00000000000b';
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  SELECT key INTO ka FROM zzz_keys WHERE name = 'A';
  SELECT key INTO kb FROM zzz_keys WHERE name = 'B';
  SELECT key INTO kc FROM zzz_keys WHERE name = 'C';
  SELECT key INTO kd FROM zzz_keys WHERE name = 'D';
  SELECT * INTO m3 FROM zzz_m WHERE n = 3;
  SELECT count(*) INTO before_events FROM deploy_events WHERE action = 'collect';

  -- phase 1 refusals: B is still referenced by DR-zzz-other; A without the
  -- tag is referenced by DR-seed; a live key is never an archive row
  err := pg_temp.zzz_try(format('SELECT dr_archive_collect_begin(ARRAY[%L], %L)::text', kb, 'DR-seed'));
  IF err NOT LIKE 'ERR: dr_archive_collect_begin: % is still referenced by release(s) DR-zzz-other%' THEN
    RAISE EXCEPTION 'shared object claimed: %', err;
  END IF;
  err := pg_temp.zzz_try(format('SELECT dr_archive_collect_begin(ARRAY[%L])::text', ka));
  IF err NOT LIKE 'ERR: dr_archive_collect_begin: % is still referenced by release(s) DR-seed%' THEN
    RAISE EXCEPTION 'referenced object claimed by gc: %', err;
  END IF;
  err := pg_temp.zzz_try(format('SELECT dr_archive_collect_begin(ARRAY[%L], %L)::text', m3.storage_key, 'DR-seed'));
  IF err NOT LIKE 'ERR: dr_archive_collect_begin: % is not an archive registry row%' THEN
    RAISE EXCEPTION 'live key accepted: %', err;
  END IF;
  -- a refused batch claims nothing (A was in the batch with B)
  err := pg_temp.zzz_try(format('SELECT dr_archive_collect_begin(ARRAY[%L, %L], %L)::text', ka, kb, 'DR-seed'));
  IF err NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'mixed batch accepted: %', err; END IF;
  IF EXISTS (SELECT 1 FROM storage_objects WHERE storage_key = ka AND status <> 'archived') THEN
    RAISE EXCEPTION 'a refused batch must claim nothing';
  END IF;
  IF (SELECT count(*) FROM storage_objects WHERE storage_key = ka) <> 1 THEN
    RAISE EXCEPTION 'fixture A lost';
  END IF;

  -- phase 1: the purge sweep claims A and C (run A's token), and the tag goes to purging
  UPDATE dr_releases SET state = 'ready' WHERE tag = 'DR-seed';
  res := dr_archive_collect_begin(ARRAY[ka, kc], 'DR-seed', claim_a);
  IF (res->>'claimed')::int <> 2 OR (res->>'already_collecting')::int <> 0 OR (res->>'bytes')::int <> 40
     OR (res->>'claim')::uuid <> claim_a THEN
    RAISE EXCEPTION 'begin result wrong: %', res;
  END IF;
  SELECT count(*) INTO cnt FROM storage_objects WHERE storage_key IN (ka, kc) AND status = 'collecting' AND collect_claim = claim_a;
  IF cnt <> 2 THEN RAISE EXCEPTION 'rows not claimed with the token'; END IF;
  IF (dr_archive_collect_begin(ARRAY[kd])->>'claim') IS NULL THEN RAISE EXCEPTION 'begin must mint a claim when given none'; END IF;
  PERFORM dr_archive_collect_finish(ARRAY[kd], NULL, (SELECT collect_claim FROM storage_objects WHERE storage_key = kd));
  INSERT INTO storage_objects (backend, bucket, storage_key, root, status, content_hash, size_bytes, content_type,
                               product_type, program_slug, archived_from_key, archived_at)
  VALUES ('osn', 'data', kd, 'archive', 'archived', 'sha256:' || repeat('e', 64), 40, 'application/json',
          'zfit', m3.program_slug, 'data/products/zzz/d.json', now() - interval '4 days');
  IF (SELECT state FROM dr_releases WHERE tag = 'DR-seed') <> 'purging' THEN
    RAISE EXCEPTION 'begin with a tag must leave it purging';
  END IF;
  -- purging: publish, archive, export and re-freeze refuse; the plan and the purge accept
  err := pg_temp.zzz_try('SELECT dr_publish(''DR-seed'')::text');
  IF err NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'a purging release was published: %', err; END IF;
  err := pg_temp.zzz_try('SELECT dr_archive_claim(''DR-seed'')::text');
  IF err NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'a purging release took an archive claim: %', err; END IF;
  err := pg_temp.zzz_try('SELECT dr_export_begin(''DR-seed'')::text');
  IF err NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'a purging release began an export: %', err; END IF;
  err := pg_temp.zzz_try('SELECT dr_enqueue_freeze(''DR-seed'', ''["nirspec"]''::jsonb, NULL, true)::text');
  IF err NOT LIKE 'ERR: release DR-seed is being purged%' THEN RAISE EXCEPTION 'a purging release was re-frozen: %', err; END IF;
  IF (SELECT count(*) FROM dr_purge_archive_plan('DR-seed')) < 2 THEN RAISE EXCEPTION 'the plan must accept a purging tag'; END IF;
  -- one collector per object: a live claim (refreshed within 10 minutes) refuses another run (token B)...
  err := pg_temp.zzz_try(format('SELECT dr_archive_collect_begin(ARRAY[%L], NULL, %L)::text', ka, claim_b));
  IF err NOT LIKE 'ERR: dr_archive_collect_begin: % is being collected by another run%' THEN
    RAISE EXCEPTION 'a live claim was taken over: %', err;
  END IF;
  -- ...the owning run re-running (same token) is fine, a heartbeat
  res := dr_archive_collect_begin(ARRAY[ka, kc], 'DR-seed', claim_a);
  IF (res->>'claimed')::int <> 0 OR (res->>'already_collecting')::int <> 2 THEN
    RAISE EXCEPTION 'the owner should be able to re-run: %', res;
  END IF;
  -- ...and a dead run's claim (older than 10 minutes) is taken over by run B, which then owns the rows
  UPDATE storage_objects SET updated_at = now() - interval '11 minutes' WHERE storage_key IN (ka, kc);
  res := dr_archive_collect_begin(ARRAY[ka, kc], 'DR-seed', claim_b);
  IF (res->>'claimed')::int <> 0 OR (res->>'already_collecting')::int <> 2 THEN
    RAISE EXCEPTION 'a re-run should take over dead claims: %', res;
  END IF;
  IF (SELECT count(*) FROM storage_objects WHERE storage_key IN (ka, kc) AND collect_claim = claim_b AND updated_at > now() - interval '1 minute') <> 2 THEN
    RAISE EXCEPTION 'the takeover must transfer and refresh the claim';
  END IF;
  -- the fence: run A, resuming, owns nothing any more; verify refreshes nothing, finish deletes nothing
  IF dr_archive_collect_verify(ARRAY[ka, kc], claim_a) <> '{}'::text[] THEN
    RAISE EXCEPTION 'a run taken over still verified its keys';
  END IF;
  res := dr_archive_collect_finish(ARRAY[ka, kc], 'DR-seed', claim_a);
  IF (res->>'collected')::int <> 0 OR (res->>'not_collecting')::int <> 2 THEN
    RAISE EXCEPTION 'a run taken over deleted rows: %', res;
  END IF;
  IF (SELECT count(*) FROM storage_objects WHERE storage_key IN (ka, kc) AND status = 'collecting') <> 2 THEN
    RAISE EXCEPTION 'the rows must survive the dead run''s finish';
  END IF;
  -- run B verifies its keys (claim refreshed) and only those: a key it never claimed is not returned
  IF dr_archive_collect_verify(ARRAY[ka, kc, kb], claim_b) <> (SELECT array_agg(x ORDER BY x) FROM unnest(ARRAY[ka, kc]) x) THEN
    RAISE EXCEPTION 'verify must return exactly the keys the run owns';
  END IF;
  IF (SELECT count(*) FROM storage_objects WHERE storage_key IN (ka, kc) AND updated_at > now() - interval '1 second') <> 2 THEN
    RAISE EXCEPTION 'verify must refresh the claim';
  END IF;
  err := pg_temp.zzz_try(format('SELECT dr_archive_collect_finish(ARRAY[%L], NULL, NULL)::text', ka));
  IF err NOT LIKE 'ERR: dr_archive_collect_finish: the claim%' THEN RAISE EXCEPTION 'finish without a claim accepted: %', err; END IF;

  -- a collecting row is offered to nobody...
  IF EXISTS (SELECT 1 FROM dr_bytes_pending('DR-seed', NULL, 2000) b
             WHERE b.storage_key = m3.storage_key AND b.source_key = kc) THEN
    RAISE EXCEPTION 'a collecting copy was offered as a byte-pass source';
  END IF;
  err := pg_temp.zzz_try(format($q$SELECT archive_register_retained(jsonb_build_object(
      'storage_key', %L, 'content_hash', %L, 'archived_from_key', %L, 'product_type', %L, 'size_bytes', 30))::text$q$,
      kc, m3.content_hash, m3.storage_key, m3.product_type));
  IF err NOT LIKE 'ERR: archive object % is being collected (status collecting)%' THEN
    RAISE EXCEPTION 'the gate registered on a collecting row: %', err;
  END IF;
  UPDATE dr_releases SET state = 'copying', archive_claim = '00000000-0000-4000-8000-0000000000aa', archive_claimed_at = now() WHERE tag = 'DR-seed';
  res := dr_bytes_mark('DR-seed', jsonb_build_array(jsonb_build_object(
      'storage_key', m3.storage_key, 'content_hash', m3.content_hash, 'state', 'copied', 'archive_key', kc,
      'registry', jsonb_build_object('size_bytes', 30, 'content_type', 'application/json'))),
      '00000000-0000-4000-8000-0000000000aa');
  IF (SELECT byte_state FROM dr_manifest WHERE tag = 'DR-seed' AND storage_key = m3.storage_key) <> 'failed'
     OR (SELECT byte_error FROM dr_manifest WHERE tag = 'DR-seed' AND storage_key = m3.storage_key) NOT LIKE '%is being collected; copy it again' THEN
    RAISE EXCEPTION 'the byte pass recorded a member on a collecting row: %', res;
  END IF;
  UPDATE dr_manifest SET byte_state = 'pending', byte_error = NULL WHERE tag = 'DR-seed' AND storage_key = m3.storage_key;
  UPDATE dr_releases SET state = 'purging', archive_claim = NULL, archive_claimed_at = NULL WHERE tag = 'DR-seed';
  -- ...but still listed, so an interrupted run resumes
  IF (SELECT count(*) FROM dr_purge_archive_plan('DR-seed') p WHERE p.storage_key IN (ka, kc) AND p.status = 'collecting') <> 2 THEN
    RAISE EXCEPTION 'the plan must list the claimed rows';
  END IF;
  IF (SELECT count(*) FROM archive_collectable(0, 2000) c WHERE c.storage_key IN (ka, kc) AND c.status = 'collecting') <> 2 THEN
    RAISE EXCEPTION 'archive_collectable must list the claimed rows';
  END IF;

  -- phase 3: the rows go; one event; a non-collecting key is skipped
  SELECT count(*) INTO before_events FROM deploy_events WHERE action = 'collect';
  res := dr_archive_collect_finish(ARRAY[ka, kc, kb], 'DR-seed', claim_b);
  IF (res->>'collected')::int <> 2 OR (res->>'not_collecting')::int <> 1 OR (res->>'bytes')::int <> 40 THEN
    RAISE EXCEPTION 'finish result wrong: %', res;
  END IF;
  IF EXISTS (SELECT 1 FROM storage_objects WHERE storage_key IN (ka, kc)) THEN
    RAISE EXCEPTION 'collecting rows not deleted';
  END IF;
  IF (SELECT status FROM storage_objects WHERE storage_key = kb) <> 'archived' THEN
    RAISE EXCEPTION 'B must be untouched';
  END IF;
  SELECT count(*) INTO cnt FROM deploy_events WHERE action = 'collect';
  IF cnt <> before_events + 1 THEN RAISE EXCEPTION 'expected exactly one collect event, got % new', cnt - before_events; END IF;
  IF NOT EXISTS (SELECT 1 FROM deploy_events WHERE action = 'collect' AND affected_count = 2
                 AND metadata->>'dr_tag' = 'DR-seed' AND (metadata->>'bytes')::int = 40) THEN
    RAISE EXCEPTION 'collect event payload wrong';
  END IF;
  res := dr_archive_collect_finish(ARRAY[ka, kc], 'DR-seed', claim_b);
  IF (res->>'collected')::int <> 0 OR (res->>'not_collecting')::int <> 2 THEN
    RAISE EXCEPTION 'a re-run of finish should skip gone rows: %', res;
  END IF;
  SELECT count(*) INTO cnt FROM deploy_events WHERE action = 'collect';
  IF cnt <> before_events + 1 THEN RAISE EXCEPTION 'an empty finish logged an event'; END IF;

  -- the tombstone: the deleted keys, with the time, and the byte pass sees it
  IF (SELECT count(*) FROM archive_collections WHERE storage_key IN (ka, kc) AND collected_at > now() - interval '1 minute') <> 2 THEN
    RAISE EXCEPTION 'finish must write a tombstone per deleted key';
  END IF;
  IF (SELECT b.target_collected_at FROM dr_bytes_pending('DR-seed', NULL, 2000) b WHERE b.storage_key = m3.storage_key) IS NULL
     OR (SELECT b.page_at FROM dr_bytes_pending('DR-seed', NULL, 2000) b WHERE b.storage_key = m3.storage_key) IS NULL THEN
    RAISE EXCEPTION 'dr_bytes_pending must expose the tombstone of the member''s copy target and the page time';
  END IF;
  -- an outcome from a page fetched before the collection fails the member (not the batch)...
  UPDATE dr_releases SET state = 'copying', archive_claim = '00000000-0000-4000-8000-0000000000aa', archive_claimed_at = now() WHERE tag = 'DR-seed';
  res := dr_bytes_mark('DR-seed', jsonb_build_array(jsonb_build_object(
      'storage_key', m3.storage_key, 'content_hash', m3.content_hash, 'state', 'copied', 'archive_key', kc,
      'page_at', (now() - interval '1 hour')::text,
      'registry', jsonb_build_object('size_bytes', 30, 'content_type', 'application/json'))),
      '00000000-0000-4000-8000-0000000000aa');
  IF (SELECT byte_state FROM dr_manifest WHERE tag = 'DR-seed' AND storage_key = m3.storage_key) <> 'failed'
     OR (SELECT byte_error FROM dr_manifest WHERE tag = 'DR-seed' AND storage_key = m3.storage_key) NOT LIKE '%was collected at%copy it again' THEN
    RAISE EXCEPTION 'a stale outcome was recorded: %', res;
  END IF;
  IF EXISTS (SELECT 1 FROM storage_objects WHERE storage_key = kc) THEN RAISE EXCEPTION 'a stale outcome registered a row'; END IF;
  -- ...and one from a page fetched after the collection (copied again) is recorded
  res := dr_bytes_mark('DR-seed', jsonb_build_array(jsonb_build_object(
      'storage_key', m3.storage_key, 'content_hash', m3.content_hash, 'state', 'copied', 'archive_key', kc,
      'page_at', (now() + interval '1 second')::text,
      'registry', jsonb_build_object('size_bytes', 30, 'content_type', 'application/json'))),
      '00000000-0000-4000-8000-0000000000aa');
  IF (SELECT byte_state FROM dr_manifest WHERE tag = 'DR-seed' AND storage_key = m3.storage_key) <> 'copied'
     OR (SELECT status FROM storage_objects WHERE storage_key = kc) <> 'archived' THEN
    RAISE EXCEPTION 'a fresh copy after the collection was refused: %', res;
  END IF;
  -- put the fixture back: C collected again (its row deleted) for the gate's turn
  UPDATE dr_manifest SET byte_state = 'pending', archive_key = NULL WHERE tag = 'DR-seed' AND storage_key = m3.storage_key;
  PERFORM dr_archive_collect_begin(ARRAY[kc], 'DR-seed', claim_b);
  PERFORM dr_archive_collect_finish(ARRAY[kc], 'DR-seed', claim_b);
  UPDATE dr_releases SET state = 'frozen', archive_claim = NULL, archive_claimed_at = NULL WHERE tag = 'DR-seed';

  -- the same content can be archived again by the gate: the key is free for a new row
  res := archive_register_retained(jsonb_build_object(
      'storage_key', kc, 'content_hash', m3.content_hash, 'archived_from_key', m3.storage_key,
      'product_type', m3.product_type, 'size_bytes', 30, 'program_slug', m3.program_slug));
  IF (res->>'inserted')::boolean IS NOT TRUE OR (SELECT status FROM storage_objects WHERE storage_key = kc) <> 'archived' THEN
    RAISE EXCEPTION 're-registration after collection failed: %', res;
  END IF;
  -- ...and that row is referenced (by m3's pending member), so no collection can claim it
  err := pg_temp.zzz_try(format('SELECT dr_archive_collect_begin(ARRAY[%L])::text', kc));
  IF err NOT LIKE 'ERR: dr_archive_collect_begin: % is still referenced%' THEN
    RAISE EXCEPTION 'a registered, referenced copy was claimed by gc: %', err;
  END IF;

  -- gc collects D (no tag), all phases
  res := dr_archive_collect_begin(ARRAY[kd]);
  IF (res->>'claimed')::int <> 1 THEN RAISE EXCEPTION 'D not claimed: %', res; END IF;
  IF dr_archive_collect_verify(ARRAY[kd], (res->>'claim')::uuid) <> ARRAY[kd] THEN RAISE EXCEPTION 'D not verified'; END IF;
  res := dr_archive_collect_finish(ARRAY[kd], NULL, (res->>'claim')::uuid);
  IF (res->>'collected')::int <> 1 THEN RAISE EXCEPTION 'D not collected: %', res; END IF;
  IF EXISTS (SELECT 1 FROM archive_collectable(0, 2000) c WHERE c.storage_key = kd) THEN
    RAISE EXCEPTION 'a collected row is still listed as collectable';
  END IF;

  -- empty / oversized / published
  IF (dr_archive_collect_begin('{}'::text[])->>'claimed')::int <> 0 OR (dr_archive_collect_finish('{}'::text[], NULL, claim_b)->>'collected')::int <> 0
     OR dr_archive_collect_verify('{}'::text[], claim_b) <> '{}'::text[] THEN
    RAISE EXCEPTION 'empty call';
  END IF;
  err := pg_temp.zzz_try('SELECT dr_archive_collect_begin((SELECT array_agg(''archive/'' || i::text) FROM generate_series(1, 2001) i))::text');
  IF err NOT LIKE 'ERR: dr_archive_collect_begin: at most 2000%' THEN RAISE EXCEPTION 'oversized begin accepted: %', err; END IF;
  err := pg_temp.zzz_try(format('SELECT dr_archive_collect_finish((SELECT array_agg(''archive/'' || i::text) FROM generate_series(1, 2001) i), NULL, %L)::text', claim_b));
  IF err NOT LIKE 'ERR: dr_archive_collect_finish: at most 2000%' THEN RAISE EXCEPTION 'oversized finish accepted: %', err; END IF;
  err := pg_temp.zzz_try(format('SELECT dr_archive_collect_verify((SELECT array_agg(''archive/'' || i::text) FROM generate_series(1, 2001) i), %L)::text', claim_b));
  IF err NOT LIKE 'ERR: dr_archive_collect_verify: at most 2000%' THEN RAISE EXCEPTION 'oversized verify accepted: %', err; END IF;
  UPDATE dr_releases SET state = 'published', published_at = now() WHERE tag = 'DR-seed';
  err := pg_temp.zzz_try(format('SELECT dr_archive_collect_begin(ARRAY[%L], %L)::text', kb, 'DR-seed'));
  IF err NOT LIKE 'ERR: release DR-seed is published%' THEN RAISE EXCEPTION 'published tag accepted: %', err; END IF;
  UPDATE dr_releases SET state = 'frozen', published_at = NULL WHERE tag = 'DR-seed';
  err := pg_temp.zzz_try(format('SELECT dr_archive_collect_begin(ARRAY[%L], %L)::text', kb, 'DR-nope'));
  IF err NOT LIKE 'ERR: no release%' THEN RAISE EXCEPTION 'unknown tag accepted: %', err; END IF;
  RAISE NOTICE 'OK 3: begin claims under the locks (tag -> purging; one collector per object with an ownership token, dead claims taken over and the dead run fenced) and refuses shared / referenced / live keys; a claimed row is offered to nobody but listed; finish deletes and writes the tombstone, one event, heals on re-run; an outcome from a page older than the tombstone fails the member; the key is free again and a new row is protected';
END $$;

-- ------------------------------------------------------------------ 4.
DO $$
DECLARE kb text; got text[];
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  SELECT key INTO kb FROM zzz_keys WHERE name = 'B';
  -- DR-seed purged (its manifest rows gone), DR-zzz-other withdrawn: B stays referenced
  DELETE FROM dr_manifest WHERE tag = 'DR-seed';
  UPDATE dr_releases SET state = 'withdrawn', published_at = now() - interval '1 day' WHERE tag = 'DR-zzz-other';
  IF archive_object_references(kb, NULL, NULL) <> ARRAY['DR-zzz-other'] THEN
    RAISE EXCEPTION 'a withdrawn release must keep its references';
  END IF;
  IF EXISTS (SELECT 1 FROM archive_collectable(0, 2000) c WHERE c.storage_key = kb) THEN
    RAISE EXCEPTION 'a withdrawn release''s object listed as collectable';
  END IF;
  IF pg_temp.zzz_try('SELECT count(*)::text FROM dr_purge_archive_plan(''DR-zzz-other'')') NOT LIKE 'ERR: release DR-zzz-other is withdrawn%' THEN
    RAISE EXCEPTION 'withdrawn plan accepted';
  END IF;
  RAISE NOTICE 'OK 4: a withdrawn release keeps its archive objects';
END $$;

-- ------------------------------------------------------------------ 5.
DO $$
DECLARE
  got text;
  usr uuid := '22222222-2222-2222-2222-222222222222';
  adm uuid := '11111111-1111-1111-1111-111111111111';
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  got := pg_temp.zzz_as('authenticated', usr, 'SELECT count(*)::text FROM archive_collectable(0, 10)');
  IF got NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'non-admin listed collectable: %', got; END IF;
  got := pg_temp.zzz_as('authenticated', usr, 'SELECT count(*)::text FROM dr_purge_archive_plan(''DR-seed'')');
  IF got NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'non-admin planned a purge: %', got; END IF;
  got := pg_temp.zzz_as('authenticated', usr, 'SELECT dr_archive_collect_begin(ARRAY[''archive/x''])::text');
  IF got NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'non-admin claimed: %', got; END IF;
  got := pg_temp.zzz_as('authenticated', usr, 'SELECT dr_archive_collect_finish(ARRAY[''archive/x''], NULL, gen_random_uuid())::text');
  IF got NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'non-admin finished: %', got; END IF;
  got := pg_temp.zzz_as('authenticated', usr, 'SELECT dr_archive_collect_verify(ARRAY[''archive/x''], gen_random_uuid())::text');
  IF got NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'non-admin verified: %', got; END IF;
  got := pg_temp.zzz_as('authenticated', usr, $q$SELECT archive_register_retained('{"storage_key": "archive/x"}'::jsonb)::text$q$);
  IF got NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'non-admin registered: %', got; END IF;
  -- archive_register_retained: a non-archive key and a missing field are refused; an archived twin is accepted
  got := pg_temp.zzz_try($q$SELECT archive_register_retained('{"storage_key": "data/x", "content_hash": "h", "archived_from_key": "data/x", "product_type": "zfit"}'::jsonb)::text$q$);
  IF got NOT LIKE 'ERR: archive_register_retained: storage_key must be an archive key%' THEN RAISE EXCEPTION 'live key registered as archive: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT archive_register_retained('{"storage_key": "archive/x"}'::jsonb)::text$q$);
  IF got NOT LIKE 'ERR: archive_register_retained: content_hash%' THEN RAISE EXCEPTION 'incomplete row registered: %', got; END IF;
  got := pg_temp.zzz_try(format($q$SELECT archive_register_retained(jsonb_build_object('storage_key', %L, 'content_hash', 'sha256:x', 'archived_from_key', 'data/x', 'product_type', 'zfit', 'size_bytes', 1))->>'inserted'$q$,
                                (SELECT key FROM zzz_keys WHERE name = 'B')));
  IF got <> 'false' THEN RAISE EXCEPTION 'an existing archived row should be accepted, not inserted: %', got; END IF;
  got := pg_temp.zzz_as('authenticated', usr, 'SELECT archive_object_references(''archive/x'', NULL, NULL)::text');
  IF got NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'authenticated may call the references helper: %', got; END IF;
  got := pg_temp.zzz_as('authenticated', adm, 'SELECT count(*)::text FROM archive_collectable(0, 10)');
  IF got LIKE 'ERR:%' THEN RAISE EXCEPTION 'admin refused: %', got; END IF;
  IF has_function_privilege('anon', 'public.archive_collectable(bigint, integer)', 'EXECUTE')
     OR has_function_privilege('anon', 'public.dr_purge_archive_plan(text, bigint, integer)', 'EXECUTE')
     OR has_function_privilege('anon', 'public.dr_archive_collect_begin(text[], text, uuid)', 'EXECUTE')
     OR has_function_privilege('anon', 'public.dr_archive_collect_verify(text[], uuid)', 'EXECUTE')
     OR has_function_privilege('anon', 'public.dr_archive_collect_finish(text[], text, uuid)', 'EXECUTE')
     OR has_function_privilege('anon', 'public.archive_register_retained(jsonb)', 'EXECUTE')
     OR has_function_privilege('anon', 'public.archive_object_references(text, text, text)', 'EXECUTE')
     OR has_function_privilege('authenticated', 'public.archive_object_references(text, text, text)', 'EXECUTE') THEN
    RAISE EXCEPTION 'EXECUTE too wide';
  END IF;
  RAISE NOTICE 'OK 5: non-admins refused; anon has no EXECUTE; references helper is service_role-only; registration validates';
END $$;

ROLLBACK;
