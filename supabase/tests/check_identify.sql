-- =============================================================================
-- identify_files (P5-2, `campfire identify`) on the seed's DR-seed.
-- =============================================================================
-- For a frozen spectrum of DR-seed, by its frozen file_hash and science hash:
--   1. a caller holding the program gets the live spectrum (match file) and the
--      DR-seed member, current = true (the live science hash is the frozen one);
--   2. a caller without the program gets nothing for that file; a field
--      product (mosaic, no program) answers for every caller;
--   3. the stamped archive bytes identify the member by stamped_hash; the
--      live spectrum re-reduced since (science hash moved) makes current false
--      and carries the live bytes' date;
--   4. a withdrawn release is hidden from a reader and shown to an admin
--      (p_include_unpublished); a draft spectrum likewise;
--   5. a tombstone keyed by the sha256 answers in `collected`; a malformed
--      hash matches nothing and raises nothing; more than 200 files is refused;
--   6. only the service role may call it (the scope is the caller's word).
-- Run: psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_identify.sql
\set ON_ERROR_STOP on
BEGIN;

DO $$
DECLARE
  prog text; sid int; sname text; fhash text; shash text; mkey text; mos_key text; mos_hash text;
  r jsonb; got jsonb; n int;
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"service_role"}', true);

  -- a DR-seed member whose live spectrum is published; the seed carries no
  -- science hashes (the sample predates them), so give this one the same
  -- science hash on both sides and on its manifest row, as deploy would
  SELECT ds.program_slug, ds.id, ds.spectrum_id, ds.file_hash
    INTO prog, sid, sname, fhash
    FROM dr_spectra ds JOIN spectra s ON s.id = ds.id
   WHERE ds.tag = 'DR-seed' AND ds.file_hash ~ '^sha256:[0-9a-f]{64}$' AND s.deploy_status = 'published'
     -- no dependents, so the row's id can move below (a removed-and-redeployed observation)
     AND NOT EXISTS (SELECT 1 FROM line_fits lf WHERE lf.spectrum_id = s.id)
     AND NOT EXISTS (SELECT 1 FROM lines l WHERE l.spectrum_id = s.id)
   ORDER BY ds.id LIMIT 1;
  IF sid IS NULL THEN RAISE EXCEPTION 'seed: no DR-seed member with a file hash and a published live spectrum'; END IF;
  shash := 'sha256:' || repeat('5', 64);
  UPDATE dr_spectra SET science_hash = shash WHERE tag = 'DR-seed' AND id = sid;
  UPDATE spectra SET science_hash = shash WHERE id = sid;
  UPDATE dr_manifest SET science_hash = shash WHERE tag = 'DR-seed' AND spectrum_id = sname AND product_type = 'nirspec_spec';
  -- the manifest's key (canonical) for the member's final
  SELECT m.storage_key INTO mkey FROM dr_manifest m
   WHERE m.tag = 'DR-seed' AND m.spectrum_id = sname AND m.product_type = 'nirspec_spec' LIMIT 1;
  IF mkey IS NULL THEN RAISE EXCEPTION 'seed: DR-seed has no manifest row for %', sname; END IF;
  SELECT m.storage_key, m.content_hash INTO mos_key, mos_hash
    FROM dr_manifest m WHERE m.tag = 'DR-seed' AND m.program_slug IS NULL AND m.product_type = 'nircam_mosaic'
     AND m.content_hash ~ '^sha256:[0-9a-f]{64}$' LIMIT 1;
  IF mos_key IS NULL THEN RAISE EXCEPTION 'seed: DR-seed has no mosaic manifest row with a hash'; END IF;

  -- ------------------------------------------------------------------ 1.
  got := identify_files(jsonb_build_array(jsonb_build_object('sha256', fhash, 'science_hash', shash)), ARRAY[prog], false);
  IF jsonb_array_length(got) <> 1 THEN RAISE EXCEPTION '1: one answer per input, got %', got; END IF;
  r := got->0;
  IF r->>'sha256' <> fhash THEN RAISE EXCEPTION '1: echoes the sha256'; END IF;
  IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements(r->'live') l WHERE l->>'spectrum_id' = sname AND l->>'match' = 'file') THEN
    RAISE EXCEPTION '1: live spectrum % expected (match file), got %', sname, r->'live';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements(r->'releases') x
                  WHERE x->>'tag' = 'DR-seed' AND x->>'spectrum_id' = sname AND (x->>'current')::boolean) THEN
    RAISE EXCEPTION '1: DR-seed member current = true expected, got %', r->'releases';
  END IF;
  -- by the science hash alone (different bytes, same science)
  r := identify_files(jsonb_build_array(jsonb_build_object('science_hash', shash)), ARRAY[prog], false)->0;
  IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements(r->'live') l WHERE l->>'match' = 'science') THEN
    RAISE EXCEPTION '1: a science-hash match is reported as such, got %', r->'live';
  END IF;
  IF jsonb_array_length(r->'releases') < 1 THEN RAISE EXCEPTION '1: science hash finds the member'; END IF;

  -- ------------------------------------------------------------------ 2.
  r := identify_files(jsonb_build_array(jsonb_build_object('sha256', fhash, 'science_hash', shash)), ARRAY['no-such-program'], false)->0;
  IF jsonb_array_length(r->'live') <> 0 OR jsonb_array_length(r->'releases') <> 0 THEN
    RAISE EXCEPTION '2: a caller without the program learns nothing, got %', r;
  END IF;
  r := identify_files(jsonb_build_array(jsonb_build_object('sha256', mos_hash)), ARRAY[]::text[], false)->0;
  IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements(r->'releases') x WHERE x->>'storage_key' = mos_key AND x->'current' = 'null'::jsonb) THEN
    RAISE EXCEPTION '2: a field product answers for every caller (current null), got %', r;
  END IF;

  -- a live product that is not a spectrum (the seeded mosaic, by its
  -- registry hash): answered for every caller, as the presign route would
  r := identify_files(jsonb_build_array(jsonb_build_object('sha256', mos_hash)), ARRAY[]::text[], false)->0;
  IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements(r->'live') l WHERE l->>'product_type' = 'nircam_mosaic' AND l->>'match' = 'file') THEN
    RAISE EXCEPTION '2: a live mosaic is found by its registry hash, got %', r->'live';
  END IF;
  -- a spectrum's final is listed once, through its spectrum, not again as a product
  r := identify_files(jsonb_build_array(jsonb_build_object('sha256', fhash)), ARRAY[prog], false)->0;
  SELECT count(*) INTO n FROM jsonb_array_elements(r->'live');
  IF n <> 1 THEN RAISE EXCEPTION '2: the final is listed once, got %', r->'live'; END IF;

  -- ------------------------------------------------------------------ 3.
  UPDATE dr_manifest SET byte_state = 'stamped', stamped_hash = 'sha256:' || repeat('a', 64),
                         archive_key = 'archive/' || repeat('a', 64) || '/' || storage_key
   WHERE tag = 'DR-seed' AND storage_key = mkey;
  r := identify_files(jsonb_build_array(jsonb_build_object('sha256', 'sha256:' || repeat('a', 64))), ARRAY[prog], false)->0;
  IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements(r->'releases') x WHERE x->>'match' = 'stamped' AND x->>'byte_state' = 'stamped') THEN
    RAISE EXCEPTION '3: the stamped bytes identify the member, got %', r;
  END IF;
  IF jsonb_array_length(r->'live') <> 0 THEN RAISE EXCEPTION '3: stamped bytes are no live file'; END IF;
  -- the live spectrum re-reduced since the freeze
  UPDATE spectra SET science_hash = 'sha256:' || repeat('b', 64), file_hash = 'sha256:' || repeat('c', 64) WHERE id = sid;
  -- a redeploy rewrites the registry row in place with it
  UPDATE storage_objects SET content_hash = 'sha256:' || repeat('c', 64), updated_at = '2030-01-02T03:04:05Z'
   WHERE storage_key = mkey AND root = 'live';
  r := identify_files(jsonb_build_array(jsonb_build_object('sha256', fhash, 'science_hash', shash)), ARRAY[prog], false)->0;
  IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements(r->'releases') x
                  WHERE x->>'tag' = 'DR-seed' AND NOT (x->>'current')::boolean AND (x->>'live_deployed_at') LIKE '2030-01-02%') THEN
    RAISE EXCEPTION '3: superseded member: current false with the live bytes'' date, got %', r->'releases';
  END IF;
  IF jsonb_array_length(r->'live') <> 0 THEN RAISE EXCEPTION '3: the old bytes match no live spectrum now'; END IF;

  -- the member's sidecars share its spectrum_id but are other bytes: the
  -- spectrum's hashes must not match them
  IF EXISTS (SELECT 1 FROM dr_manifest WHERE tag = 'DR-seed' AND spectrum_id = sname AND product_type <> 'nirspec_spec') THEN
    r := identify_files(jsonb_build_array(jsonb_build_object('science_hash', shash)), ARRAY[prog], false)->0;
    IF EXISTS (SELECT 1 FROM jsonb_array_elements(r->'releases') x WHERE x->>'product_type' <> 'nirspec_spec') THEN
      RAISE EXCEPTION '3: the spectrum''s hashes matched a sidecar row, got %', r->'releases';
    END IF;
  END IF;
  -- rows that predate the science hash: the whole-file hash decides currentness
  UPDATE spectra SET science_hash = NULL, file_hash = fhash WHERE id = sid;
  UPDATE dr_spectra SET science_hash = NULL WHERE tag = 'DR-seed' AND id = sid;
  r := identify_files(jsonb_build_array(jsonb_build_object('sha256', fhash)), ARRAY[prog], false)->0;
  IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements(r->'releases') x WHERE x->>'tag' = 'DR-seed' AND (x->>'current')::boolean) THEN
    RAISE EXCEPTION '3: same file hash, no science hashes: current, got %', r->'releases';
  END IF;
  UPDATE spectra SET file_hash = 'sha256:' || repeat('c', 64) WHERE id = sid;
  r := identify_files(jsonb_build_array(jsonb_build_object('sha256', fhash)), ARRAY[prog], false)->0;
  IF EXISTS (SELECT 1 FROM jsonb_array_elements(r->'releases') x WHERE x->>'tag' = 'DR-seed' AND (x->>'current')::boolean) THEN
    RAISE EXCEPTION '3: file hash moved, no science hashes: not current, got %', r->'releases';
  END IF;
  UPDATE spectra SET science_hash = shash, file_hash = fhash WHERE id = sid;
  UPDATE dr_spectra SET science_hash = shash WHERE tag = 'DR-seed' AND id = sid;
  -- the observation removed and redeployed with the same science: a new
  -- numeric id, the same spectrum name, still current
  UPDATE spectra SET id = 900000000 WHERE id = sid;
  r := identify_files(jsonb_build_array(jsonb_build_object('science_hash', shash)), ARRAY[prog], false)->0;
  IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements(r->'releases') x WHERE x->>'tag' = 'DR-seed' AND (x->>'current')::boolean) THEN
    RAISE EXCEPTION '3: a redeployed spectrum (new id, same name and science) is current, got %', r->'releases';
  END IF;
  UPDATE spectra SET id = sid WHERE id = 900000000;

  -- ------------------------------------------------------------------ 4.
  UPDATE dr_releases SET state = 'withdrawn', withdrawn_at = now() WHERE tag = 'DR-seed';
  r := identify_files(jsonb_build_array(jsonb_build_object('science_hash', shash)), ARRAY[prog], false)->0;
  IF jsonb_array_length(r->'releases') <> 0 THEN RAISE EXCEPTION '4: a withdrawn release is hidden from a reader'; END IF;
  r := identify_files(jsonb_build_array(jsonb_build_object('science_hash', shash)), ARRAY[prog], true)->0;
  IF jsonb_array_length(r->'releases') < 1 THEN RAISE EXCEPTION '4: an admin sees the withdrawn release'; END IF;
  UPDATE dr_releases SET state = 'published', withdrawn_at = NULL WHERE tag = 'DR-seed';
  UPDATE spectra SET deploy_status = 'draft', science_hash = shash WHERE id = sid;
  r := identify_files(jsonb_build_array(jsonb_build_object('science_hash', shash)), ARRAY[prog], false)->0;
  IF jsonb_array_length(r->'live') <> 0 THEN RAISE EXCEPTION '4: a draft spectrum is hidden from a reader'; END IF;
  r := identify_files(jsonb_build_array(jsonb_build_object('science_hash', shash)), ARRAY[prog], true)->0;
  IF jsonb_array_length(r->'live') <> 1 THEN RAISE EXCEPTION '4: an admin sees the draft'; END IF;

  -- ------------------------------------------------------------------ 5.
  INSERT INTO archive_collections (storage_key, collected_at)
  VALUES ('archive/' || repeat('d', 64) || '/data/products/x.fits', '2029-05-06T00:00:00Z');
  r := identify_files(jsonb_build_array(jsonb_build_object('sha256', 'sha256:' || repeat('d', 64))), ARRAY[]::text[], false)->0;
  IF NOT ((r->'collected'->0->>'collected_at') LIKE '2029-05-06%') THEN RAISE EXCEPTION '5: tombstone expected, got %', r; END IF;
  r := identify_files(jsonb_build_array(jsonb_build_object('sha256', 'md5:zzz', 'science_hash', 'nope')), ARRAY[prog], false)->0;
  IF r->'sha256' <> 'null'::jsonb OR jsonb_array_length(r->'live') <> 0 THEN RAISE EXCEPTION '5: a malformed hash matches nothing, got %', r; END IF;
  BEGIN
    PERFORM identify_files((SELECT jsonb_agg(jsonb_build_object('sha256', fhash)) FROM generate_series(1, 201)), ARRAY[prog], false);
    RAISE EXCEPTION '5: 201 files must be refused';
  EXCEPTION WHEN invalid_parameter_value THEN NULL;
  END;

  -- ------------------------------------------------------------------ 6.
  IF has_function_privilege('authenticated', 'public.identify_files(jsonb, text[], boolean)', 'EXECUTE') THEN
    RAISE EXCEPTION '6: authenticated must not execute identify_files (explicit scope)';
  END IF;
  IF has_function_privilege('anon', 'public.identify_files(jsonb, text[], boolean)', 'EXECUTE') THEN
    RAISE EXCEPTION '6: anon must not execute identify_files';
  END IF;
  IF NOT has_function_privilege('service_role', 'public.identify_files(jsonb, text[], boolean)', 'EXECUTE') THEN
    RAISE EXCEPTION '6: the service role runs the bearer route';
  END IF;

  RAISE NOTICE 'PASS: identify_files';
END $$;

ROLLBACK;
