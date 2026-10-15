-- =============================================================================
-- Data-release page document (P6b-3, D-P6-3), on DR-seed.
-- =============================================================================
--   1. dr_page_set is admin-only; refused before the freeze landed; validates
--      the assets (name, content-addressed key dr/<tag>/page/<sha16>/<name>,
--      sha256, size, count, duplicates); the happy path sets page_md, writes
--      manifest rows of component 'page' that enter as copied (archive_key =
--      storage_key), logs page_dr; a second call replaces the rows and
--      returns the retired keys (removed_keys), the CLI's deletion authority;
--   2. the page is invisible to the export and byte machinery: no 'page'
--      export partition, no pending bytes, the derived state unchanged;
--   3. a re-freeze keeps the page rows and the markdown; a purge deletes them;
--   4. once published the page is frozen (dr_page_set refused, also after a
--      withdraw); a reader sees the page rows through RLS, a link account
--      does not.
-- Run: psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_dr_page.sql
\set ON_ERROR_STOP on
BEGIN;

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

CREATE FUNCTION pg_temp.zzz_try(p_sql text) RETURNS text
LANGUAGE plpgsql AS $$
BEGIN
  EXECUTE p_sql;
  RETURN 'ok';
EXCEPTION WHEN OTHERS THEN
  RETURN 'ERR: ' || SQLERRM;
END $$;

-- two valid assets for DR-seed
CREATE FUNCTION pg_temp.zzz_assets(p_tag text) RETURNS jsonb
LANGUAGE sql AS $$
  SELECT jsonb_build_array(
    jsonb_build_object('name', 'nz.png', 'storage_key', 'dr/' || p_tag || '/page/' || repeat('1', 16) || '/nz.png',
                       'content_hash', 'sha256:' || repeat('1', 64), 'size_bytes', 1234, 'backend', 'osn', 'bucket', 'data'),
    jsonb_build_object('name', 'depth_f444w.pdf', 'storage_key', 'dr/' || p_tag || '/page/' || repeat('2', 16) || '/depth_f444w.pdf',
                       'content_hash', 'sha256:' || repeat('2', 64), 'size_bytes', 56789, 'backend', 'osn', 'bucket', 'data'))
$$;
GRANT EXECUTE ON FUNCTION pg_temp.zzz_assets(text) TO authenticated;

CREATE FUNCTION pg_temp.zzz_make_ready(p_tag text) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
  c uuid;
BEGIN
  PERFORM dr_export_begin(p_tag);
  c := dr_archive_claim(p_tag);
  PERFORM dr_bytes_mark(p_tag, (
    SELECT jsonb_agg(jsonb_build_object('storage_key', m.storage_key, 'content_hash', m.content_hash, 'state', 'copied',
                                        'archive_key', 'archive/' || md5(m.storage_key) || md5(m.storage_key) || '/' || substr(m.storage_key, 6),
                                        'registry', jsonb_build_object('content_type', 'application/octet-stream')))
    FROM dr_manifest m WHERE m.tag = p_tag AND m.byte_state NOT IN ('copied', 'stamped')), c);
  PERFORM dr_archive_release(p_tag, c);
  PERFORM dr_export_mark(p_tag, e.stream, e.partition, 'building') FROM dr_exports e
   WHERE e.tag = p_tag AND e.state <> 'ready';
  PERFORM dr_export_mark(p_tag, e.stream, e.partition, 'ready', 'sha256:' || repeat('a', 64), 1, 1)
  FROM dr_exports e WHERE e.tag = p_tag AND e.state = 'building';
  RETURN (SELECT state FROM dr_releases WHERE tag = p_tag);
END $$;

-- ------------------------------------------------------------------ 1. dr_page_set
DO $$
DECLARE
  got text; res jsonb; n int;
  usr uuid := '22222222-2222-2222-2222-222222222222';
  adm uuid := '11111111-1111-1111-1111-111111111111';
  n_pending_before int; n_events int;
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  SELECT count(*) INTO n_pending_before FROM dr_manifest WHERE tag = 'DR-seed' AND byte_state IN ('pending', 'failed');

  got := pg_temp.zzz_as('authenticated', usr, $q$SELECT dr_page_set('DR-seed', '# x', '[]'::jsonb)::text$q$);
  IF got NOT LIKE 'ERR:%Admin%' THEN RAISE EXCEPTION 'non-admin should be refused, got %', got; END IF;
  got := pg_temp.zzz_as('anon', NULL, $q$SELECT dr_page_set('DR-seed', '# x', '[]'::jsonb)::text$q$);
  IF got NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'anon should be refused, got %', got; END IF;

  got := pg_temp.zzz_try($q$SELECT dr_page_set('DR-nope', '# x', '[]'::jsonb)$q$);
  IF got NOT LIKE 'ERR:%no release%' THEN RAISE EXCEPTION 'unknown tag: %', got; END IF;
  UPDATE dr_releases SET state = 'freezing' WHERE tag = 'DR-seed';
  got := pg_temp.zzz_try($q$SELECT dr_page_set('DR-seed', '# x', '[]'::jsonb)$q$);
  IF got NOT LIKE 'ERR:%freezing%' THEN RAISE EXCEPTION 'freezing should be refused, got %', got; END IF;
  UPDATE dr_releases SET state = 'frozen' WHERE tag = 'DR-seed';

  -- validation
  got := pg_temp.zzz_try($q$SELECT dr_page_set('DR-seed', '', '[]'::jsonb)$q$);
  IF got NOT LIKE 'ERR:%empty%' THEN RAISE EXCEPTION 'empty markdown: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_page_set('DR-seed', '# x', '{}'::jsonb)$q$);
  IF got NOT LIKE 'ERR:%array%' THEN RAISE EXCEPTION 'non-array assets: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_page_set('DR-seed', '# x', jsonb_build_array(jsonb_build_object('name', '../x.png', 'storage_key', 'dr/DR-seed/page/../x.png', 'content_hash', 'sha256:' || repeat('1', 64), 'size_bytes', 10)))$q$);
  IF got NOT LIKE 'ERR:%bad page asset name%' THEN RAISE EXCEPTION 'bad name: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_page_set('DR-seed', '# x', jsonb_build_array(jsonb_build_object('name', 'README.md', 'storage_key', 'dr/DR-seed/page/' || repeat('1', 16) || '/README.md', 'content_hash', 'sha256:' || repeat('1', 64), 'size_bytes', 10)))$q$);
  IF got NOT LIKE 'ERR:%README.md is the page markdown%' THEN RAISE EXCEPTION 'README.md asset: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_page_set('DR-seed', '# x', jsonb_build_array('x.png'))$q$);
  IF got NOT LIKE 'ERR:%must be an object%' THEN RAISE EXCEPTION 'non-object element: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_page_set('DR-seed', '# x', jsonb_build_array(jsonb_build_object('name', 'x.png', 'storage_key', 'dr/DR-seed/page/' || repeat('1', 16) || '/x.png', 'content_hash', 'sha256:' || repeat('1', 64))))$q$);
  IF got NOT LIKE 'ERR:%numeric size_bytes%' THEN RAISE EXCEPTION 'missing size: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_page_set('DR-seed', repeat('x', 1048577), '[]'::jsonb)$q$);
  IF got NOT LIKE 'ERR:%exceeds 1 MB%' THEN RAISE EXCEPTION 'big markdown: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_page_set('DR-seed', '# x', jsonb_build_array(jsonb_build_object('name', 'x.png', 'storage_key', 'dr/DR-other/page/' || repeat('1', 16) || '/x.png', 'content_hash', 'sha256:' || repeat('1', 64), 'size_bytes', 10)))$q$);
  IF got NOT LIKE 'ERR:%must live at%' THEN RAISE EXCEPTION 'wrong prefix: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_page_set('DR-seed', '# x', jsonb_build_array(jsonb_build_object('name', 'x.png', 'storage_key', 'dr/DR-seed/page/' || repeat('9', 16) || '/x.png', 'content_hash', 'sha256:' || repeat('1', 64), 'size_bytes', 10)))$q$);
  IF got NOT LIKE 'ERR:%must live at dr/DR-seed/page/1111111111111111/x.png%' THEN RAISE EXCEPTION 'wrong hash segment: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_page_set('DR-seed', '# x', jsonb_build_array(jsonb_build_object('name', 'x.png', 'storage_key', 'dr/DR-seed/page/x.png', 'content_hash', 'sha256:' || repeat('1', 64), 'size_bytes', 10)))$q$);
  IF got NOT LIKE 'ERR:%must live at%' THEN RAISE EXCEPTION 'key without the hash segment: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_page_set('DR-seed', '# x', jsonb_build_array(jsonb_build_object('name', 'x.png', 'storage_key', 'dr/DR-seed/page/abc/x.png', 'content_hash', 'etag:abc', 'size_bytes', 10)))$q$);
  IF got NOT LIKE 'ERR:%sha256%' THEN RAISE EXCEPTION 'bad hash: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_page_set('DR-seed', '# x', jsonb_build_array(jsonb_build_object('name', 'x.png', 'storage_key', 'dr/DR-seed/page/' || repeat('1', 16) || '/x.png', 'content_hash', 'sha256:' || repeat('1', 64), 'size_bytes', 30000000)))$q$);
  IF got NOT LIKE 'ERR:%25 MB%' THEN RAISE EXCEPTION 'too big: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_page_set('DR-seed', '# x', jsonb_build_array(jsonb_build_object('name', 'x.png', 'storage_key', 'dr/DR-seed/page/' || repeat('1', 16) || '/x.png', 'content_hash', 'sha256:' || repeat('1', 64), 'size_bytes', 10, 'bucket', 'tiles')))$q$);
  IF got NOT LIKE 'ERR:%osn/data%' THEN RAISE EXCEPTION 'wrong bucket: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_page_set('DR-seed', '# x', pg_temp.zzz_assets('DR-seed') || pg_temp.zzz_assets('DR-seed'))$q$);
  IF got NOT LIKE 'ERR:%duplicate%' THEN RAISE EXCEPTION 'duplicates: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_page_set('DR-seed', '# x', (SELECT jsonb_agg(jsonb_build_object('name', 'f' || i || '.png', 'storage_key', 'dr/DR-seed/page/' || repeat('1', 16) || '/f' || i || '.png', 'content_hash', 'sha256:' || repeat('1', 64), 'size_bytes', 10)) FROM generate_series(1, 51) i))$q$);
  IF got NOT LIKE 'ERR:%at most 50%' THEN RAISE EXCEPTION 'too many: %', got; END IF;
  IF EXISTS (SELECT 1 FROM dr_manifest WHERE tag = 'DR-seed' AND component = 'page') THEN
    RAISE EXCEPTION 'a refused call must leave no page rows';
  END IF;

  -- a failed release accepts a page (the page is independent of the catalog copy)
  UPDATE dr_releases SET state = 'failed' WHERE tag = 'DR-seed';
  got := pg_temp.zzz_try($q$SELECT dr_page_set('DR-seed', '# on failed', '[]'::jsonb)$q$);
  IF got <> 'ok' THEN RAISE EXCEPTION 'failed release should accept a page: %', got; END IF;
  UPDATE dr_releases SET state = 'frozen' WHERE tag = 'DR-seed';

  -- happy path, as the admin
  got := pg_temp.zzz_as('authenticated', adm, $q$SELECT (dr_page_set('DR-seed', E'# DR-seed\n\nTwo figures.', pg_temp.zzz_assets('DR-seed')))::text$q$);
  IF got LIKE 'ERR:%' THEN RAISE EXCEPTION 'happy path failed: %', got; END IF;
  res := got::jsonb;
  IF (res->>'assets')::int <> 2 OR res->>'state' <> 'frozen' OR res->'removed_keys' <> '[]'::jsonb THEN RAISE EXCEPTION 'summary: %', res; END IF;
  IF (SELECT page_md FROM dr_releases WHERE tag = 'DR-seed') NOT LIKE '# DR-seed%' THEN RAISE EXCEPTION 'page_md not set'; END IF;
  SELECT count(*) INTO n FROM dr_manifest
   WHERE tag = 'DR-seed' AND component = 'page' AND product_type = 'dr_page_asset' AND program_slug IS NULL
     AND field IS NULL AND spectrum_id IS NULL AND byte_state = 'copied' AND archive_key = storage_key
     AND backend = 'osn' AND bucket = 'data' AND storage_key ~ '^dr/DR-seed/page/[0-9a-f]{16}/[^/]+$';
  IF n <> 2 THEN RAISE EXCEPTION 'expected 2 copied content-addressed page rows, got %', n; END IF;
  SELECT count(*) INTO n_events FROM deploy_events WHERE action = 'page_dr' AND metadata->>'dr_tag' = 'DR-seed';
  IF n_events <> 2 THEN RAISE EXCEPTION 'expected two page_dr events (failed-state set + this one), got %', n_events; END IF;

  -- a second call replaces the rows and returns exactly the retired keys
  res := dr_page_set('DR-seed', '# v2', jsonb_build_array(pg_temp.zzz_assets('DR-seed')->0));
  SELECT count(*) INTO n FROM dr_manifest WHERE tag = 'DR-seed' AND component = 'page';
  IF n <> 1 OR (SELECT page_md FROM dr_releases WHERE tag = 'DR-seed') <> '# v2' THEN
    RAISE EXCEPTION 're-set should replace the page rows (got % rows)', n;
  END IF;
  IF res->'removed_keys' <> jsonb_build_array('dr/DR-seed/page/' || repeat('2', 16) || '/depth_f444w.pdf') THEN
    RAISE EXCEPTION 'removed_keys should name the retired pdf only: %', res->'removed_keys';
  END IF;
  -- the same set again retires nothing
  res := dr_page_set('DR-seed', '# v2b', jsonb_build_array(pg_temp.zzz_assets('DR-seed')->0));
  IF res->'removed_keys' <> '[]'::jsonb THEN RAISE EXCEPTION 'an unchanged set must retire nothing: %', res; END IF;
  res := dr_page_set('DR-seed', '# v3', '[]'::jsonb);
  IF EXISTS (SELECT 1 FROM dr_manifest WHERE tag = 'DR-seed' AND component = 'page') THEN
    RAISE EXCEPTION 'an empty asset list should clear the page rows';
  END IF;
  IF res->'removed_keys' <> jsonb_build_array('dr/DR-seed/page/' || repeat('1', 16) || '/nz.png') THEN
    RAISE EXCEPTION 'clearing should retire the remaining key: %', res->'removed_keys';
  END IF;
  PERFORM dr_page_set('DR-seed', E'# DR-seed\n\nTwo figures.', pg_temp.zzz_assets('DR-seed'));

  -- ------------------------------------------------------------------ 2. invisible to exports and bytes
  IF EXISTS (SELECT 1 FROM dr_export_partitions('DR-seed') p WHERE p.partition = 'page' OR p.storage_key LIKE '%/page%') THEN
    RAISE EXCEPTION 'the page must not be an export partition';
  END IF;
  SELECT count(*) INTO n FROM dr_manifest WHERE tag = 'DR-seed' AND byte_state IN ('pending', 'failed');
  IF n <> n_pending_before THEN RAISE EXCEPTION 'page rows must not add pending bytes (% vs %)', n, n_pending_before; END IF;
  IF EXISTS (SELECT 1 FROM dr_bytes_pending('DR-seed', NULL, 2000, true) b WHERE b.storage_key LIKE 'dr/%') THEN
    RAISE EXCEPTION 'the byte pass must never see page rows';
  END IF;
  IF dr_derive_state('DR-seed') <> 'frozen' THEN RAISE EXCEPTION 'derived state changed: %', dr_derive_state('DR-seed'); END IF;
  IF (dr_bytes_summary('DR-seed')->>'manifest')::int <> (SELECT count(*) FROM dr_manifest WHERE tag = 'DR-seed' AND component <> 'page')
     OR COALESCE((dr_bytes_summary('DR-seed')->'by_state'->'copied'->>'count')::int, 0) <> 0 THEN
    RAISE EXCEPTION 'the byte summary must leave the page rows out: %', dr_bytes_summary('DR-seed');
  END IF;
  RAISE NOTICE 'OK 1-2: dr_page_set (gates, validation, set, replace, event); exports and bytes ignore the page';
END $$;

-- ------------------------------------------------------------------ 3. re-freeze keeps the page; purge deletes it
DO $$
DECLARE n int; n_members int;
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  SELECT count(*) INTO n_members FROM dr_manifest WHERE tag = 'DR-seed';
  UPDATE dr_releases SET state = 'freezing', freeze_started_at = now() WHERE tag = 'DR-seed';
  PERFORM dr_freeze_job('DR-seed');
  IF (SELECT state FROM dr_releases WHERE tag = 'DR-seed') <> 'frozen' THEN
    RAISE EXCEPTION 're-freeze failed: %', (SELECT error FROM dr_releases WHERE tag = 'DR-seed');
  END IF;
  SELECT count(*) INTO n FROM dr_manifest WHERE tag = 'DR-seed' AND component = 'page' AND byte_state = 'copied';
  IF n <> 2 THEN RAISE EXCEPTION 're-freeze should keep the 2 page rows, got %', n; END IF;
  IF (SELECT page_md FROM dr_releases WHERE tag = 'DR-seed') NOT LIKE '# DR-seed%' THEN RAISE EXCEPTION 're-freeze lost page_md'; END IF;
  IF (SELECT count(*) FROM dr_manifest WHERE tag = 'DR-seed') <> n_members THEN
    RAISE EXCEPTION 're-freeze changed the member count (% -> %)', n_members, (SELECT count(*) FROM dr_manifest WHERE tag = 'DR-seed');
  END IF;
  -- the page assets are not catalog members: every manifest statistic leaves them out
  IF (SELECT counts->'manifest_by_type' ? 'dr_page_asset' FROM dr_releases WHERE tag = 'DR-seed')
     OR (SELECT counts->'by_component' ? 'page' FROM dr_releases WHERE tag = 'DR-seed')
     OR (SELECT (counts->>'manifest')::int FROM dr_releases WHERE tag = 'DR-seed') <> n_members - 2 THEN
    RAISE EXCEPTION 'counts must leave the page rows out: %', (SELECT counts FROM dr_releases WHERE tag = 'DR-seed');
  END IF;
  RAISE NOTICE 'OK 3a: re-freeze keeps the page document';
END $$;

SAVEPOINT before_purge;
DO $$
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  PERFORM dr_purge_release('DR-seed');
  IF EXISTS (SELECT 1 FROM dr_manifest WHERE tag = 'DR-seed') OR EXISTS (SELECT 1 FROM dr_releases WHERE tag = 'DR-seed') THEN
    RAISE EXCEPTION 'purge left page rows or the release';
  END IF;
  RAISE NOTICE 'OK 3b: purge deletes the page rows with the tag';
END $$;
ROLLBACK TO SAVEPOINT before_purge;

-- ------------------------------------------------------------------ 4. published: frozen page, reader RLS
DO $$
DECLARE
  got text; n int;
  usr uuid := '22222222-2222-2222-2222-222222222222';
  adm uuid := '11111111-1111-1111-1111-111111111111';
  link uuid := '44444444-4444-4444-4444-444444444444';
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  -- the reader must not see the page rows of an unpublished tag
  got := pg_temp.zzz_as('authenticated', usr, $q$SELECT count(*)::text FROM dr_manifest WHERE tag = 'DR-seed' AND component = 'page'$q$);
  IF got <> '0' THEN RAISE EXCEPTION 'reader saw page rows of an unpublished tag: %', got; END IF;

  IF pg_temp.zzz_make_ready('DR-seed') <> 'ready' THEN RAISE EXCEPTION 'could not make DR-seed ready'; END IF;
  got := pg_temp.zzz_as('authenticated', adm, $q$SELECT (dr_publish('DR-seed', '10.5281/zenodo.424242'))->>'state'$q$);
  IF got <> 'published' THEN RAISE EXCEPTION 'publish failed: %', got; END IF;

  got := pg_temp.zzz_try($q$SELECT dr_page_set('DR-seed', '# late', '[]'::jsonb)$q$);
  IF got NOT LIKE 'ERR:%published%' THEN RAISE EXCEPTION 'published page should be frozen, got %', got; END IF;
  IF (SELECT page_md FROM dr_releases WHERE tag = 'DR-seed') NOT LIKE '# DR-seed%' THEN RAISE EXCEPTION 'page_md changed after publish'; END IF;

  got := pg_temp.zzz_as('authenticated', usr, $q$SELECT count(*)::text FROM dr_manifest WHERE tag = 'DR-seed' AND component = 'page'$q$);
  IF got <> '2' THEN RAISE EXCEPTION 'reader should see the 2 page rows of a published tag, got %', got; END IF;
  got := pg_temp.zzz_as('authenticated', usr, $q$SELECT page_md FROM dr_releases WHERE tag = 'DR-seed'$q$);
  IF got NOT LIKE '# DR-seed%' THEN RAISE EXCEPTION 'reader should read page_md, got %', got; END IF;
  got := pg_temp.zzz_as('authenticated', link, $q$SELECT count(*)::text FROM dr_manifest WHERE tag = 'DR-seed' AND component = 'page'$q$);
  IF got <> '0' THEN RAISE EXCEPTION 'link account must not see page rows, got %', got; END IF;
  got := pg_temp.zzz_as('anon', NULL, $q$SELECT count(*)::text FROM dr_manifest WHERE tag = 'DR-seed' AND component = 'page'$q$);
  IF got NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'anon must be denied, got %', got; END IF;

  got := pg_temp.zzz_as('authenticated', adm, $q$SELECT (dr_withdraw('DR-seed', 'test'))->>'state'$q$);
  IF got <> 'withdrawn' THEN RAISE EXCEPTION 'withdraw failed: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_page_set('DR-seed', '# late', '[]'::jsonb)$q$);
  IF got NOT LIKE 'ERR:%withdrawn%' THEN RAISE EXCEPTION 'withdrawn page should stay frozen, got %', got; END IF;
  RAISE NOTICE 'OK 4: published page frozen (also after withdraw); reader RLS on page rows; link and anon denied';
END $$;

ROLLBACK;
