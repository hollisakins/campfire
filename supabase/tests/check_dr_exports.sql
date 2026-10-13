-- =============================================================================
-- Data-release exports (P4b, docs/dr-format-v1.md) on the seed's DR-seed.
-- =============================================================================
-- DR-seed is frozen by the real freeze from the seeded catalog (seed.sql §13)
-- and nothing changes it afterwards, so for a seeded program P the export
-- partition must equal the live sync RPC's answer for a caller holding P
-- alone -- the design's "built with the scoped sync RPC per program" promise:
--   1. dr_export_partitions lists every (stream, partition) with rows, with a
--      key-safe storage key; nothing for streams/partitions without rows;
--   2. spectra partition == get_spectra_for_sync([P]) (ids); line_fits
--      partition == get_line_fits_for_sync([P]) (ids + staleness); objects
--      partition == get_objects_for_sync([P]) on id, n_spectra, gratings
--      (the scoped aggregates); members partition == the NIRSpec members of
--      P; the nircam/<field> partition == the published mosaics;
--   3. keyset paging with a small limit covers each partition exactly once;
--   4. dr_export_begin seeds the ledger idempotently and moves the state;
--      dr_export_mark records files and the last ready one -> exported;
--      dr_export_invalidate sends files back to pending; dr_derive_state
--      gives copying / ready from the member byte states; a published
--      release refuses begin; non-admin refused everywhere.
-- Run: psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_dr_exports.sql
\set ON_ERROR_STOP on
\set ADMIN '11111111-1111-1111-1111-111111111111'
\set USER '22222222-2222-2222-2222-222222222222'
BEGIN;

-- The seed publishes DR-seed; exports are built before publish, so rewind it
-- for this transaction (rolled back at the end).
UPDATE dr_releases SET state = 'frozen', published_at = NULL WHERE tag = 'DR-seed';

CREATE FUNCTION pg_temp.zzz_as(p_role text, p_uid uuid, p_sql text) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE v text;
BEGIN
  PERFORM set_config('request.jwt.claims', json_build_object('sub', p_uid, 'role', p_role)::text, true);
  EXECUTE format('SET LOCAL ROLE %I', p_role);
  EXECUTE p_sql INTO v;
  RESET ROLE;
  PERFORM set_config('request.jwt.claims', '', true);
  RETURN v;
EXCEPTION WHEN OTHERS THEN
  RESET ROLE;
  PERFORM set_config('request.jwt.claims', '', true);
  RETURN 'ERR: ' || SQLERRM;
END $$;
GRANT EXECUTE ON FUNCTION pg_temp.zzz_as(text, uuid, text) TO authenticated;

DO $$
DECLARE
  prog text; fld text; got text; n int; n2 int; bad int;
  k text;
BEGIN
  -- the direct calls below run as the service role (the builder's identity)
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  -- the seeded program with the most frozen spectra, and the seeded NIRCam field
  SELECT program_slug INTO prog FROM dr_spectra WHERE tag = 'DR-seed' GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1;
  SELECT substr(component, 8) INTO fld FROM dr_members WHERE tag = 'DR-seed' AND component LIKE 'nircam/%' LIMIT 1;
  IF prog IS NULL OR fld IS NULL THEN RAISE EXCEPTION 'seed: DR-seed lacks spectra (%) or a nircam component (%)', prog, fld; END IF;

  -- ------------------------------------------------------------------ 1.
  SELECT count(*) INTO n FROM dr_export_partitions('DR-seed') p WHERE p.n_rows = 0;
  IF n <> 0 THEN RAISE EXCEPTION 'partitions with zero rows listed: %', n; END IF;
  SELECT count(*) INTO n FROM dr_export_partitions('DR-seed') p WHERE p.stream = 'spectra' AND p.partition = prog;
  IF n <> 1 THEN RAISE EXCEPTION 'spectra partition for % missing', prog; END IF;
  SELECT p.storage_key INTO k FROM dr_export_partitions('DR-seed') p WHERE p.stream = 'members' AND p.partition = 'nircam/' || fld;
  IF k <> dr_export_key('DR-seed', 'members', 'nircam/' || fld) OR k !~ ('^dr/DR-seed/exports/members/nircam_[A-Za-z0-9._-]+-[0-9a-f]{8}\.jsonl\.gz$') THEN
    RAISE EXCEPTION 'unexpected member key %', k;
  END IF;
  -- key-safe names are used as is; others are sanitised + md5-suffixed, so
  -- names that sanitise alike stay distinct (Codex on #600)
  IF dr_export_key('T', 'spectra', prog) <> 'dr/T/exports/spectra/' || prog || '.jsonl.gz' THEN
    RAISE EXCEPTION 'plain slug key altered: %', dr_export_key('T', 'spectra', prog);
  END IF;
  IF dr_export_key('T', 'members', 'photometry/goods-n/CAT v1') = dr_export_key('T', 'members', 'photometry/goods-n/CAT_v1') THEN
    RAISE EXCEPTION 'export keys collide for names that sanitise alike';
  END IF;
  IF (SELECT p.n_rows FROM dr_export_partitions('DR-seed') p WHERE p.stream = 'spectra' AND p.partition = prog)
     <> (SELECT count(*) FROM dr_spectra WHERE tag = 'DR-seed' AND program_slug = prog) THEN
    RAISE EXCEPTION 'spectra partition row count wrong';
  END IF;
  -- streams the seed has no rows for produce no partition (photometry has no component in DR-seed)
  SELECT count(*) INTO n FROM dr_export_partitions('DR-seed') p WHERE p.stream = 'photometry';
  IF n <> 0 THEN RAISE EXCEPTION 'photometry partitions without a photometry component: %', n; END IF;
  RAISE NOTICE 'OK 1: partitions (program %, field %)', prog, fld;

  -- ------------------------------------------------------------------ 2.
  -- spectra: ids equal the live scoped sync for [prog]
  SELECT count(*) INTO bad FROM (
    SELECT (r.row_json->>'id')::int AS id FROM dr_export_rows('DR-seed', 'spectra', prog, NULL, 10000) r
    EXCEPT
    SELECT (e->>'id')::int FROM get_spectra_for_sync(ARRAY[prog], NULL, NULL, 100000, false, false, NULL) s,
           jsonb_array_elements(s.spectra) e
  ) x;
  SELECT count(*) INTO n2 FROM (
    SELECT (e->>'id')::int FROM get_spectra_for_sync(ARRAY[prog], NULL, NULL, 100000, false, false, NULL) s,
           jsonb_array_elements(s.spectra) e
    EXCEPT
    SELECT (r.row_json->>'id')::int FROM dr_export_rows('DR-seed', 'spectra', prog, NULL, 10000) r
  ) x;
  IF bad <> 0 OR n2 <> 0 THEN RAISE EXCEPTION 'spectra partition differs from the live sync for %: % extra, % missing', prog, bad, n2; END IF;

  -- line fits: ids and staleness
  SELECT count(*) INTO bad FROM (
    SELECT (r.row_json->>'spectrum_id')::int, (r.row_json->>'stale_redshift')::boolean, (r.row_json->>'stale_spectrum')::boolean
    FROM dr_export_rows('DR-seed', 'line_fits', prog, NULL, 10000) r
    EXCEPT
    SELECT (e->>'spectrum_id')::int, (e->>'stale_redshift')::boolean, (e->>'stale_spectrum')::boolean
    FROM get_line_fits_for_sync(ARRAY[prog], NULL, 100000, false, false, NULL) s, jsonb_array_elements(s.line_fit_records) e
  ) x;
  SELECT count(*) INTO n2 FROM (
    SELECT (e->>'spectrum_id')::int FROM get_line_fits_for_sync(ARRAY[prog], NULL, 100000, false, false, NULL) s, jsonb_array_elements(s.line_fit_records) e
    EXCEPT
    SELECT (r.row_json->>'spectrum_id')::int FROM dr_export_rows('DR-seed', 'line_fits', prog, NULL, 10000) r
  ) x;
  IF bad <> 0 OR n2 <> 0 THEN RAISE EXCEPTION 'line_fits partition differs from the live sync for %: % extra, % missing', prog, bad, n2; END IF;

  -- objects: id set and the scoped aggregates (n_spectra, gratings)
  SELECT count(*) INTO bad FROM (
    SELECT (r.row_json->>'id')::int, (r.row_json->>'n_spectra')::int,
           (SELECT array_agg(x ORDER BY x) FROM jsonb_array_elements_text(r.row_json->'gratings') x)
    FROM dr_export_rows('DR-seed', 'objects', prog, NULL, 10000) r
    EXCEPT
    SELECT (e->>'id')::int, (e->>'n_spectra')::int,
           (SELECT array_agg(x ORDER BY x) FROM jsonb_array_elements_text(e->'gratings') x)
    FROM get_objects_for_sync(ARRAY[prog], NULL, NULL, 100000, false, false, NULL, NULL) s, jsonb_array_elements(s.objects) e
  ) x;
  SELECT count(*) INTO n2 FROM (
    SELECT (e->>'id')::int FROM get_objects_for_sync(ARRAY[prog], NULL, NULL, 100000, false, false, NULL, NULL) s, jsonb_array_elements(s.objects) e
    EXCEPT
    SELECT (r.row_json->>'id')::int FROM dr_export_rows('DR-seed', 'objects', prog, NULL, 10000) r
  ) x;
  IF bad <> 0 OR n2 <> 0 THEN RAISE EXCEPTION 'objects partition differs from the live scoped sync for %: % rows differ, % missing', prog, bad, n2; END IF;

  -- members
  SELECT count(*) INTO n FROM dr_export_rows('DR-seed', 'members', prog, NULL, 10000) r;
  IF n <> (SELECT count(*) FROM dr_members WHERE tag = 'DR-seed' AND program_slug = prog) OR n = 0 THEN
    RAISE EXCEPTION 'members partition for % has % rows', prog, n;
  END IF;
  SELECT count(*) INTO n FROM dr_export_rows('DR-seed', 'members', 'nircam/' || fld, NULL, 10000) r;
  IF n <> (SELECT count(*) FROM nircam_images WHERE field = fld AND deploy_status = 'published') OR n = 0 THEN
    RAISE EXCEPTION 'nircam member partition has % rows', n;
  END IF;
  RAISE NOTICE 'OK 2: partitions equal the live scoped sync (spectra, line fits, objects) and the frozen members';

  -- ------------------------------------------------------------------ 3.
  -- keyset paging with limit 7 covers the objects partition exactly once
  DECLARE
    after text := NULL; total int := 0; seen text[] := '{}'; page_n int; last text;
  BEGIN
    LOOP
      SELECT count(*), max(r.next_cursor) INTO page_n, last FROM dr_export_rows('DR-seed', 'objects', prog, after, 7) r;
      SELECT array_cat(seen, array_agg(r.next_cursor)) INTO seen FROM dr_export_rows('DR-seed', 'objects', prog, after, 7) r;
      total := total + page_n;
      EXIT WHEN page_n < 7;
      after := last;
    END LOOP;
    IF total <> (SELECT count(*) FROM dr_export_rows('DR-seed', 'objects', prog, NULL, 10000)) THEN
      RAISE EXCEPTION 'paging covered % rows, expected %', total, (SELECT count(*) FROM dr_export_rows('DR-seed', 'objects', prog, NULL, 10000));
    END IF;
    IF (SELECT count(DISTINCT x) FROM unnest(seen) x) <> total THEN RAISE EXCEPTION 'paging produced duplicates'; END IF;
  END;
  BEGIN
    PERFORM dr_export_rows('DR-seed', 'nope', prog, NULL, 10);
    RAISE EXCEPTION 'unknown stream should raise';
  EXCEPTION WHEN OTHERS THEN
    IF SQLERRM NOT LIKE 'unknown export stream%' THEN RAISE; END IF;
  END;
  RAISE NOTICE 'OK 3: keyset paging';

  -- ------------------------------------------------------------------ 4.
  got := pg_temp.zzz_as('authenticated', '22222222-2222-2222-2222-222222222222', $q$SELECT (dr_export_begin('DR-seed'))::text$q$);
  IF got NOT LIKE 'ERR: Access denied%' THEN RAISE EXCEPTION 'non-admin begin should be refused, got %', got; END IF;
  got := pg_temp.zzz_as('authenticated', '22222222-2222-2222-2222-222222222222', $q$SELECT count(*)::text FROM dr_export_partitions('DR-seed')$q$);
  IF got NOT LIKE 'ERR: Access denied%' THEN RAISE EXCEPTION 'non-admin partitions should be refused, got %', got; END IF;
  got := pg_temp.zzz_as('authenticated', '22222222-2222-2222-2222-222222222222', $q$SELECT count(*)::text FROM dr_export_rows('DR-seed', 'objects', 'x', NULL, 10)$q$);
  IF got NOT LIKE 'ERR: Access denied%' THEN RAISE EXCEPTION 'non-admin rows should be refused, got %', got; END IF;

  got := pg_temp.zzz_as('authenticated', '11111111-1111-1111-1111-111111111111', $q$SELECT (dr_export_begin('DR-seed'))->>'state'$q$);
  IF got <> 'exporting' THEN RAISE EXCEPTION 'begin should move to exporting, got %', got; END IF;
  -- zzz_as clears the claims on its way out; the direct calls below need them again
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  SELECT count(*) INTO n FROM dr_exports WHERE tag = 'DR-seed';
  IF n <> (SELECT count(*) FROM dr_export_partitions('DR-seed')) OR n = 0 THEN RAISE EXCEPTION 'ledger not seeded (%)', n; END IF;
  -- idempotent: a second begin adds nothing
  got := pg_temp.zzz_as('authenticated', '11111111-1111-1111-1111-111111111111', $q$SELECT (dr_export_begin('DR-seed'))->>'new'$q$);
  IF got <> '0' THEN RAISE EXCEPTION 'second begin inserted % rows', got; END IF;

  -- mark every file ready but one -> still exporting; the last -> exported
  UPDATE dr_exports SET state = 'ready', sha256 = 'sha256:' || repeat('0', 64), size_bytes = 1, rows = 1, built_at = now()
   WHERE tag = 'DR-seed' AND NOT (stream = 'spectra' AND partition = prog);
  got := pg_temp.zzz_as('authenticated', '11111111-1111-1111-1111-111111111111',
    format($q$SELECT (dr_export_mark('DR-seed', 'spectra', %L, 'building'))->>'state'$q$, prog));
  IF got <> 'exporting' THEN RAISE EXCEPTION 'building mark should keep exporting, got %', got; END IF;
  got := pg_temp.zzz_as('authenticated', '11111111-1111-1111-1111-111111111111',
    format($q$SELECT (dr_export_mark('DR-seed', 'spectra', %L, 'failed', NULL, NULL, NULL, 'boom'))->>'state'$q$, prog));
  IF got <> 'exporting' OR (SELECT error FROM dr_exports WHERE tag = 'DR-seed' AND stream = 'spectra' AND partition = prog) <> 'boom' THEN
    RAISE EXCEPTION 'failed mark not recorded (%)', got;
  END IF;
  got := pg_temp.zzz_as('authenticated', '11111111-1111-1111-1111-111111111111',
    format($q$SELECT (dr_export_mark('DR-seed', 'spectra', %L, 'ready', 'sha256:%s', 123, 45))->>'state'$q$, prog, repeat('a', 64)));
  IF got <> 'exported' THEN RAISE EXCEPTION 'last ready mark should move to exported, got %', got; END IF;
  IF (SELECT state FROM dr_releases WHERE tag = 'DR-seed') <> 'exported' THEN RAISE EXCEPTION 'release not exported'; END IF;

  -- invalidate: the members files of one partition go back to pending, the
  -- state follows (exporting); a rebuild brings it back to exported; with every
  -- member copied the derived state is ready, and copying while members are
  -- half done (the byte pass in flight)
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  got := (dr_export_invalidate('DR-seed', 'members', prog))->>'invalidated';
  IF got <> '1' OR (SELECT state FROM dr_exports WHERE tag = 'DR-seed' AND stream = 'members' AND partition = prog) <> 'pending'
     OR (SELECT state FROM dr_releases WHERE tag = 'DR-seed') <> 'exporting' THEN
    RAISE EXCEPTION 'invalidate did not reset the partition (got %, state %)', got, (SELECT state FROM dr_releases WHERE tag = 'DR-seed');
  END IF;
  IF (dr_export_mark('DR-seed', 'members', prog, 'ready', 'sha256:' || repeat('b', 64), 1, 1))->>'state' <> 'exported' THEN
    RAISE EXCEPTION 'rebuild should return to exported';
  END IF;
  UPDATE dr_members SET byte_state = 'copied', archive_key = 'archive/x/' || storage_key WHERE tag = 'DR-seed' AND program_slug = prog;
  IF dr_derive_state('DR-seed') <> 'copying' THEN RAISE EXCEPTION 'half-copied members should derive copying, got %', dr_derive_state('DR-seed'); END IF;
  UPDATE dr_members SET byte_state = 'copied', archive_key = 'archive/x/' || storage_key WHERE tag = 'DR-seed';
  IF dr_derive_state('DR-seed') <> 'ready' THEN RAISE EXCEPTION 'all exported + all copied should derive ready, got %', dr_derive_state('DR-seed'); END IF;
  -- a re-run of begin on a ready release is allowed and keeps it ready
  IF (dr_export_begin('DR-seed'))->>'state' <> 'ready' THEN RAISE EXCEPTION 'begin on ready should keep ready'; END IF;
  UPDATE dr_members SET byte_state = 'pending', archive_key = NULL WHERE tag = 'DR-seed';
  IF dr_derive_state('DR-seed') <> 'exported' THEN RAISE EXCEPTION 'reverting members should derive exported'; END IF;
  IF (SELECT rows FROM dr_exports WHERE tag = 'DR-seed' AND stream = 'spectra' AND partition = prog) <> 45
     OR (SELECT error FROM dr_exports WHERE tag = 'DR-seed' AND stream = 'spectra' AND partition = prog) IS NOT NULL THEN
    RAISE EXCEPTION 'ready mark did not record rows / clear the error';
  END IF;
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);
  BEGIN
    PERFORM dr_export_mark('DR-seed', 'spectra', 'no_such_program', 'ready');
    RAISE EXCEPTION 'mark of a missing ledger row should raise';
  EXCEPTION WHEN OTHERS THEN
    IF SQLERRM NOT LIKE 'no ledger row%' THEN RAISE; END IF;
  END;

  -- an empty release cannot be exported (it could never become ready)
  INSERT INTO dr_releases (tag, state, components) VALUES ('DR-zzz-empty', 'frozen', '["nirspec"]');
  got := pg_temp.zzz_as('authenticated', '11111111-1111-1111-1111-111111111111', $q$SELECT (dr_export_begin('DR-zzz-empty'))::text$q$);
  IF got NOT LIKE 'ERR: release DR-zzz-empty has nothing to export%' THEN RAISE EXCEPTION 'empty release export: %', got; END IF;
  IF EXISTS (SELECT 1 FROM dr_exports WHERE tag = 'DR-zzz-empty') OR (SELECT state FROM dr_releases WHERE tag = 'DR-zzz-empty') <> 'frozen' THEN
    RAISE EXCEPTION 'refused empty export wrote something';
  END IF;

  -- published: begin refused
  UPDATE dr_releases SET state = 'published', published_at = now() WHERE tag = 'DR-seed';
  got := pg_temp.zzz_as('authenticated', '11111111-1111-1111-1111-111111111111', $q$SELECT (dr_export_begin('DR-seed'))::text$q$);
  IF got NOT LIKE 'ERR: release DR-seed is published%' THEN RAISE EXCEPTION 'begin on a published release should be refused, got %', got; END IF;

  -- readers never see the ledger; admins do
  got := pg_temp.zzz_as('authenticated', '22222222-2222-2222-2222-222222222222', $q$SELECT count(*)::text FROM dr_exports WHERE tag = 'DR-seed'$q$);
  IF got <> '0' THEN RAISE EXCEPTION 'reader sees the ledger: %', got; END IF;
  got := pg_temp.zzz_as('authenticated', '11111111-1111-1111-1111-111111111111', $q$SELECT count(*)::text FROM dr_exports WHERE tag = 'DR-seed'$q$);
  IF got = '0' THEN RAISE EXCEPTION 'admin cannot see the ledger'; END IF;
  RAISE NOTICE 'OK 4: ledger state machine + gates';
END $$;

ROLLBACK;
