-- =============================================================================
-- Data-release publish / withdraw / diff / removal references (P4d), on DR-seed.
-- =============================================================================
--   1. dr_publish refuses an unfinished release (the state is re-derived), a bad
--      DOI and non-admins; once every export file is built and every member
--      archived it publishes (state, DOI, published_by, publish_dr event) and a
--      reader sees the frozen rows of their programs; a second publish is refused;
--   2. dr_withdraw needs a reason and a published release; withdrawn hides the
--      rows from readers (admins keep them), records the reason, logs
--      withdraw_dr, cannot be purged, and can be published again;
--   3. dr_diff reports each kind of live change since the freeze, relative to
--      the seed's baseline, with samples capped at the limit;
--   4. dr_observation_references blocks a removal for a published release and
--      for an unpublished one with unarchived members, not otherwise.
-- Run: psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_dr_publish.sql
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

-- Every member archived (synthetic archive keys) and every export file ready.
CREATE FUNCTION pg_temp.zzz_make_ready(p_tag text) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
  c uuid;
BEGIN
  PERFORM dr_export_begin(p_tag);
  c := dr_archive_claim(p_tag);          -- as one archive run
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

DO $$
DECLARE
  got text; res jsonb; n int; n_admin int;
  usr uuid := '22222222-2222-2222-2222-222222222222';
  adm uuid := '11111111-1111-1111-1111-111111111111';
  obs text;
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  SELECT observation INTO obs FROM dr_spectra WHERE tag = 'DR-seed' ORDER BY spectrum_id LIMIT 1;

  -- ------------------------------------------------------------------ 4a. unpublished, unarchived: blocks
  IF NOT (SELECT blocks FROM dr_observation_references(obs) WHERE tag = 'DR-seed') THEN
    RAISE EXCEPTION 'unarchived members of an unpublished release should block a removal';
  END IF;
  IF (SELECT n_unarchived FROM dr_observation_references(obs) WHERE tag = 'DR-seed') = 0 THEN
    RAISE EXCEPTION 'expected unarchived members';
  END IF;

  -- ------------------------------------------------------------------ 1.
  got := pg_temp.zzz_try($q$SELECT dr_publish('DR-seed')$q$);
  IF got NOT LIKE 'ERR: release DR-seed is frozen (publish needs ready%' THEN RAISE EXCEPTION 'unfinished release published: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_publish('DR-seed', 'doi:10.5281/zenodo.1')$q$);
  IF got NOT LIKE 'ERR: not a DOI%' THEN RAISE EXCEPTION 'bad DOI accepted: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_publish('DR-nope')$q$);
  IF got NOT LIKE 'ERR: no release DR-nope%' THEN RAISE EXCEPTION 'unknown tag: %', got; END IF;

  IF pg_temp.zzz_make_ready('DR-seed') <> 'ready' THEN RAISE EXCEPTION 'fixture: DR-seed not ready'; END IF;
  -- ready but unarchived nothing: no longer blocks a removal
  IF (SELECT blocks FROM dr_observation_references(obs) WHERE tag = 'DR-seed') THEN
    RAISE EXCEPTION 'a fully archived unpublished release should not block a removal';
  END IF;

  got := pg_temp.zzz_as('authenticated', usr, $q$SELECT dr_publish('DR-seed')::text$q$);
  IF got NOT LIKE 'ERR: Access denied%' THEN RAISE EXCEPTION 'non-admin published: %', got; END IF;
  -- readers see nothing before publish
  got := pg_temp.zzz_as('authenticated', usr, $q$SELECT count(*)::text FROM dr_spectra WHERE tag = 'DR-seed'$q$);
  IF got <> '0' THEN RAISE EXCEPTION 'reader saw % unpublished rows', got; END IF;

  -- a live deploy / removal lease on one of the release's observations blocks
  -- the publish; one on an unrelated scope does not (it stays live through the
  -- publish below) (Codex on #602)
  INSERT INTO deploy_scope_state (scope_type, scope_key, in_progress_since, heartbeat_at, in_progress_host)
  VALUES ('observation', obs, now(), now(), 'zzz-host'),
         ('observation', 'zzz-unrelated-live', now(), now(), 'zzz-host')
  ON CONFLICT (scope_type, scope_key) DO UPDATE SET in_progress_since = now(), heartbeat_at = now();
  got := pg_temp.zzz_try($q$SELECT dr_publish('DR-seed')$q$);
  IF got NOT LIKE 'ERR: a deploy or removal is in progress on a scope DR-seed covers%' THEN
    RAISE EXCEPTION 'publish under a live lease on its own observation: %', got;
  END IF;
  DELETE FROM deploy_scope_state WHERE scope_type = 'observation' AND scope_key = obs;
  -- a STALE lease on one of the release's observations is invalidated by the
  -- publish (version bumped, lease cleared), so its holder cannot resume;
  -- a stale lease on an unrelated scope is left alone (Codex on #602)
  INSERT INTO deploy_scope_state (scope_type, scope_key, version, in_progress_since, heartbeat_at, in_progress_host)
  VALUES ('observation', obs, 7, now() - interval '2 days', now() - interval '2 days', 'zzz-host'),
         ('observation', 'zzz-unrelated-obs', 7, now() - interval '2 days', now() - interval '2 days', 'zzz-host')
  ON CONFLICT (scope_type, scope_key) DO UPDATE
    SET version = 7, in_progress_since = now() - interval '2 days', heartbeat_at = now() - interval '2 days',
        in_progress_host = 'zzz-host';

  got := pg_temp.zzz_as('authenticated', adm, $q$SELECT dr_publish('DR-seed', '10.5281/zenodo.123456', 'none known')->>'state'$q$);
  IF (SELECT version FROM deploy_scope_state WHERE scope_type = 'observation' AND scope_key = obs) <> 8
     OR (SELECT in_progress_since FROM deploy_scope_state WHERE scope_type = 'observation' AND scope_key = obs) IS NOT NULL THEN
    RAISE EXCEPTION 'publish did not invalidate a stale lease on its observation';
  END IF;
  IF (SELECT version FROM deploy_scope_state WHERE scope_key = 'zzz-unrelated-obs') <> 7 THEN
    RAISE EXCEPTION 'publish touched an unrelated stale lease';
  END IF;
  IF got <> 'published' THEN RAISE EXCEPTION 'admin publish (with an unrelated live lease): %', got; END IF;
  DELETE FROM deploy_scope_state WHERE scope_key IN ('zzz-unrelated-live', 'zzz-unrelated-obs');
  IF NOT EXISTS (SELECT 1 FROM dr_releases WHERE tag = 'DR-seed' AND state = 'published' AND doi = '10.5281/zenodo.123456'
                 AND known_issues = 'none known' AND published_by = adm AND published_at IS NOT NULL) THEN
    RAISE EXCEPTION 'published row incomplete: %', (SELECT to_jsonb(r) FROM dr_releases r WHERE tag = 'DR-seed');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM deploy_events WHERE action = 'publish_dr' AND metadata->>'dr_tag' = 'DR-seed' AND actor = adm) THEN
    RAISE EXCEPTION 'no publish_dr event';
  END IF;
  got := pg_temp.zzz_as('authenticated', usr, $q$SELECT count(*)::text FROM dr_spectra WHERE tag = 'DR-seed'$q$);
  n := got::int;
  SELECT count(*) INTO n_admin FROM dr_spectra WHERE tag = 'DR-seed';
  IF n = 0 OR n > n_admin THEN RAISE EXCEPTION 'reader should see some published rows within their programs: % of %', n, n_admin; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_publish('DR-seed')$q$);
  IF got NOT LIKE 'ERR: release DR-seed is already published%' THEN RAISE EXCEPTION 'double publish: %', got; END IF;
  -- a published release blocks a removal
  IF NOT (SELECT blocks FROM dr_observation_references(obs) WHERE tag = 'DR-seed') THEN
    RAISE EXCEPTION 'a published release should block a removal';
  END IF;
  RAISE NOTICE 'OK 1: publish (gates, ready re-derived, visibility, event)';

  -- ------------------------------------------------------------------ 2.
  got := pg_temp.zzz_try($q$SELECT dr_withdraw('DR-seed', '  ')$q$);
  IF got NOT LIKE 'ERR: a withdrawal needs a reason%' THEN RAISE EXCEPTION 'reasonless withdraw: %', got; END IF;
  got := pg_temp.zzz_as('authenticated', usr, $q$SELECT dr_withdraw('DR-seed', 'x')::text$q$);
  IF got NOT LIKE 'ERR: Access denied%' THEN RAISE EXCEPTION 'non-admin withdrew: %', got; END IF;
  got := pg_temp.zzz_as('authenticated', adm, $q$SELECT dr_withdraw('DR-seed', 'wrong calibration in G395M')->>'state'$q$);
  IF got <> 'withdrawn' THEN RAISE EXCEPTION 'withdraw: %', got; END IF;
  IF NOT EXISTS (SELECT 1 FROM dr_releases WHERE tag = 'DR-seed' AND withdrawn_by = adm AND withdrawn_at IS NOT NULL
                 AND known_issues LIKE 'none known' || E'\n' || 'Withdrawn %: wrong calibration in G395M') THEN
    RAISE EXCEPTION 'withdrawn row incomplete: %', (SELECT to_jsonb(r) FROM dr_releases r WHERE tag = 'DR-seed');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM deploy_events WHERE action = 'withdraw_dr' AND metadata->>'dr_tag' = 'DR-seed') THEN
    RAISE EXCEPTION 'no withdraw_dr event';
  END IF;
  got := pg_temp.zzz_as('authenticated', usr, $q$SELECT count(*)::text FROM dr_spectra WHERE tag = 'DR-seed'$q$);
  IF got <> '0' THEN RAISE EXCEPTION 'reader still sees % withdrawn rows', got; END IF;
  got := pg_temp.zzz_as('authenticated', adm, $q$SELECT count(*)::text FROM dr_spectra WHERE tag = 'DR-seed'$q$);
  IF got::int <> n_admin THEN RAISE EXCEPTION 'admin lost rows of the withdrawn release: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_purge_release('DR-seed')$q$);
  IF got NOT LIKE 'ERR: release DR-seed is withdrawn%' THEN RAISE EXCEPTION 'withdrawn release purged: %', got; END IF;
  got := pg_temp.zzz_try($q$SELECT dr_withdraw('DR-seed', 'again')$q$);
  IF got NOT LIKE 'ERR: release DR-seed is withdrawn (only a published release is withdrawn%' THEN RAISE EXCEPTION 'double withdraw: %', got; END IF;
  -- withdrawn does not block a removal (bytes are archived, nobody reads it)
  IF (SELECT blocks FROM dr_observation_references(obs) WHERE tag = 'DR-seed') THEN
    RAISE EXCEPTION 'a withdrawn release should not block a removal';
  END IF;
  -- and it can be published again, without a re-derive
  res := dr_publish('DR-seed');
  IF res->>'state' <> 'published' OR res->>'withdrawn_at' IS NOT NULL OR res->>'doi' <> '10.5281/zenodo.123456' THEN
    RAISE EXCEPTION 'republish: %', res;
  END IF;
  IF res ? 'snapshot_xmin' THEN RAISE EXCEPTION 'snapshot_xmin leaked into the answer'; END IF;
  RAISE NOTICE 'OK 2: withdraw (reason, visibility, no purge, republish)';
END $$;

-- ------------------------------------------------------------------ 3.
DO $$
DECLARE
  base jsonb; after jsonb; s record; o record; o2 record; o3 record; o4 record; drop_sid text;
  k text; n int;
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  base := dr_diff('DR-seed', 3);
  IF base->>'tag' <> 'DR-seed' OR NOT (base ? 'counts') THEN RAISE EXCEPTION 'diff shape: %', base; END IF;

  -- one change of each kind
  SELECT d.spectrum_id INTO s FROM dr_spectra d JOIN spectra x ON x.spectrum_id = d.spectrum_id
   WHERE d.tag = 'DR-seed' ORDER BY d.spectrum_id LIMIT 1;
  UPDATE spectra SET science_hash = 'sha256:' || repeat('9', 64), file_hash = 'sha256:' || repeat('8', 64)
   WHERE spectrum_id = s.spectrum_id;
  UPDATE spectra SET deploy_status = 'revoked'
   WHERE spectrum_id = (SELECT d.spectrum_id FROM dr_spectra d WHERE d.tag = 'DR-seed' ORDER BY d.spectrum_id OFFSET 1 LIMIT 1);
  SELECT d.spectrum_id INTO drop_sid FROM dr_spectra d WHERE d.tag = 'DR-seed' ORDER BY d.spectrum_id OFFSET 2 LIMIT 1;
  DELETE FROM dr_spectra WHERE tag = 'DR-seed' AND spectrum_id = drop_sid;     -- live now has one the release lacks

  -- (d.id is the live objects.id at the freeze; d.object_id the name)
  SELECT d.id AS object_id INTO o FROM dr_objects d JOIN objects x ON x.id = d.id AND x.is_active
   WHERE d.tag = 'DR-seed' AND x.redshift IS NOT DISTINCT FROM d.redshift ORDER BY d.id LIMIT 1;
  SELECT d.id AS object_id INTO o2 FROM dr_objects d JOIN objects x ON x.id = d.id AND x.is_active
   WHERE d.tag = 'DR-seed' AND x.redshift IS NOT DISTINCT FROM d.redshift ORDER BY d.id OFFSET 1 LIMIT 1;
  SELECT d.id AS object_id INTO o3 FROM dr_objects d JOIN objects x ON x.id = d.id AND x.is_active
   WHERE d.tag = 'DR-seed' AND NOT COALESCE(x.needs_review, false) AND NOT d.needs_review
   ORDER BY d.id OFFSET 2 LIMIT 1;
  SELECT d.id AS object_id INTO o4 FROM dr_objects d JOIN objects x ON x.id = d.id AND x.is_active
   WHERE d.tag = 'DR-seed' ORDER BY d.id DESC LIMIT 1;
  -- the triggers on objects are not what is under test here
  SET LOCAL session_replication_role = replica;
  UPDATE objects SET object_id = object_id || '-renamed', redshift_quality = COALESCE(redshift_quality, 0) + 1 WHERE id = o.object_id;
  UPDATE objects SET redshift_inspected = COALESCE(redshift, 0) + 0.5 WHERE id = o2.object_id;   -- redshift is generated
  UPDATE objects SET needs_review = true WHERE id = o3.object_id;
  UPDATE objects SET is_active = false WHERE id = o4.object_id;
  SET LOCAL session_replication_role = origin;

  after := dr_diff('DR-seed', 3);
  FOREACH k IN ARRAY ARRAY['spectrum_science_changed', 'spectrum_unpublished', 'spectrum_new',
                           'object_renamed', 'object_quality_changed', 'object_redshift_changed',
                           'review_raised', 'object_gone'] LOOP
    IF COALESCE((after->'counts'->>k)::int, 0) < COALESCE((base->'counts'->>k)::int, 0) + 1 THEN
      RAISE EXCEPTION 'diff missed a % change: base %, after %', k, base->'counts', after->'counts';
    END IF;
    IF jsonb_array_length(after->'samples'->k) > 3 THEN RAISE EXCEPTION 'samples of % exceed the limit', k; END IF;
  END LOOP;
  IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements(after->'samples'->'object_renamed') e
                 WHERE (e->>'object_id')::int = o.object_id AND e->>'live' LIKE '%-renamed') THEN
    RAISE EXCEPTION 'renamed object not sampled: %', after->'samples'->'object_renamed';
  END IF;
  -- a gone object does not also count as renamed / changed
  IF EXISTS (SELECT 1 FROM jsonb_array_elements(after->'samples'->'object_renamed') e WHERE (e->>'object_id')::int = o4.object_id) THEN
    RAISE EXCEPTION 'a deactivated object was reported as renamed';
  END IF;
  -- a legacy row without a science hash is compared, and shown, by file hash (Codex on #602)
  SELECT d.spectrum_id, d.file_hash INTO s FROM dr_spectra d JOIN spectra x ON x.spectrum_id = d.spectrum_id
   WHERE d.tag = 'DR-seed' AND x.deploy_status = 'published' ORDER BY d.spectrum_id OFFSET 3 LIMIT 1;
  UPDATE dr_spectra SET science_hash = NULL WHERE tag = 'DR-seed' AND spectrum_id = s.spectrum_id;
  UPDATE spectra SET file_hash = 'sha256:' || repeat('7', 64) WHERE spectrum_id = s.spectrum_id;
  after := dr_diff('DR-seed', 500);
  IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements(after->'samples'->'spectrum_science_changed') e
                 WHERE e->>'spectrum_id' = s.spectrum_id AND e->>'compared' = 'file_hash'
                   AND e->>'frozen' IS NOT DISTINCT FROM s.file_hash AND e->>'live' = 'sha256:' || repeat('7', 64)) THEN
    RAISE EXCEPTION 'file-hash comparison not shown: %', after->'samples'->'spectrum_science_changed';
  END IF;
  RAISE NOTICE 'OK 3: diff (every kind, capped samples)';
END $$;

-- ------------------------------------------------------------------ 4b.
DO $$
DECLARE
  obs text;
  got text;
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  SELECT observation INTO obs FROM dr_spectra WHERE tag = 'DR-seed' ORDER BY spectrum_id DESC LIMIT 1;
  UPDATE dr_releases SET state = 'failed' WHERE tag = 'DR-seed';
  IF (SELECT blocks FROM dr_observation_references(obs) WHERE tag = 'DR-seed') THEN
    RAISE EXCEPTION 'a failed release should not block a removal';
  END IF;
  IF EXISTS (SELECT 1 FROM dr_observation_references('no-such-observation')) THEN
    RAISE EXCEPTION 'an unreferenced observation has references';
  END IF;
  got := pg_temp.zzz_as('authenticated', '22222222-2222-2222-2222-222222222222',
                        format($q$SELECT count(*)::text FROM dr_observation_references(%L)$q$, obs));
  IF got NOT LIKE 'ERR: Access denied%' THEN RAISE EXCEPTION 'non-admin read references: %', got; END IF;
  got := pg_temp.zzz_as('authenticated', '22222222-2222-2222-2222-222222222222', $q$SELECT dr_diff('DR-seed')::text$q$);
  IF got NOT LIKE 'ERR: Access denied%' THEN RAISE EXCEPTION 'non-admin read the diff: %', got; END IF;
  IF has_function_privilege('anon', 'public.dr_publish(text, text, text)', 'EXECUTE')
     OR has_function_privilege('anon', 'public.dr_withdraw(text, text)', 'EXECUTE')
     OR has_function_privilege('anon', 'public.dr_diff(text, integer)', 'EXECUTE')
     OR has_function_privilege('anon', 'public.dr_observation_references(text)', 'EXECUTE') THEN
    RAISE EXCEPTION 'anon can execute a P4d RPC';
  END IF;
  RAISE NOTICE 'OK 4: removal references (published / unarchived block; archived / withdrawn / failed do not); gates';
END $$;

ROLLBACK;
