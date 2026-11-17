-- check_field_coverage.sql
--
-- The field products (exposure maps, the layout plot, the sky coverage) were
-- NIRCam's alone until 2026-11. Every imaging instrument has them now, one
-- set per instrument of a field. This checks:
--
--   1. field_coverage() gives one map for every instrument: NIRCam's entry
--      from the typed columns of fields, the others from
--      fields.instrument_coverage, and a 'nircam' key smuggled into the jsonb
--      never overrides the typed columns.
--   2. set_field_coverage() merges one instrument's entry and leaves the
--      other's alone, refuses NIRCam and unknown instruments, and does nothing
--      for a caller the fields write policy does not admit.
--   3. The registry admits the HST exposure-map and layout kinds and still
--      refuses an unknown one.
--   4. The field summaries return the coverage map, and their layout plot is
--      NIRCam's when the field has one and the first other instrument's when
--      it does not.
--   5. A release component `acswfc/<field>` freezes that instrument's exposure
--      maps and layout plot (none of NIRCam's), reports its coverage, and the
--      frozen field row keeps the map; browsing the release, the field's
--      layout plot is the frozen one.
--
-- Run: psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_field_coverage.sql
-- Everything inside one rolled-back transaction.
\set ON_ERROR_STOP on
BEGIN;

-- ---------------------------------------------------------------------------
-- Fixture: zzz_fc has NIRCam and ACS/WFC, zzz_fh has ACS/WFC only.
-- ---------------------------------------------------------------------------
INSERT INTO fields (name, coverage_area_arcmin2, coverage_area_deg2, expmap_pixel_scale_arcsec)
VALUES ('zzz_fc', 120.5, 0.03347222, 0.5);
INSERT INTO fields (name) VALUES ('zzz_fh');

INSERT INTO auth.users (id, email) VALUES
  ('00000000-0000-0000-0000-0000000000f1', 'zzz-fc-admin@test.invalid'),
  ('00000000-0000-0000-0000-0000000000f2', 'zzz-fc-user@test.invalid');
INSERT INTO user_profiles (user_id, username, full_name, is_admin, is_link_account, can_comment, can_inspect) VALUES
  ('00000000-0000-0000-0000-0000000000f1', 'zzz-fc-admin', 'ZZZ FC Admin', true,  false, true, true),
  ('00000000-0000-0000-0000-0000000000f2', 'zzz-fc-user',  'ZZZ FC User',  false, false, true, false);

INSERT INTO deployments (id, field, status, published_at, instrument) VALUES
  (990301, 'zzz_fc', 'published', now(), 'nircam'),
  (990302, 'zzz_fc', 'published', now(), 'acswfc'),
  (990303, 'zzz_fh', 'published', now(), 'acswfc'),
  (990304, 'zzz_fh', 'published', now(), 'wfc3ir');

INSERT INTO mosaics (instrument, field, tile, filter, pixel_scale, extension, epoch, file_path, deploy_status, deployment_id) VALUES
  ('nircam', 'zzz_fc', 'main', 'f444w', '30mas', 'sci', '', 'data/products/nircam/zzz_fc/f444w/mosaic_nircam_f444w_zzz_fc_30mas_main_sci.fits.gz', 'published', 990301),
  ('acswfc', 'zzz_fc', 'main', 'f814w', '30mas', 'sci', '', 'data/products/acswfc/zzz_fc/f814w/mosaic_acswfc_f814w_zzz_fc_30mas_main_sci.fits.gz', 'published', 990302),
  ('acswfc', 'zzz_fh', 'main', 'f814w', '30mas', 'sci', '', 'data/products/acswfc/zzz_fh/f814w/mosaic_acswfc_f814w_zzz_fh_30mas_main_sci.fits.gz', 'published', 990303);

INSERT INTO storage_objects (backend, bucket, storage_key, content_hash, size_bytes, content_type, product_type, instrument, status, field, filter, deployment_id)
SELECT 'osn', 'data', m.file_path, 'sha256:' || md5(m.file_path) || md5(m.instrument), 5000, 'application/gzip',
       m.instrument || '_mosaic', m.instrument, 'active', m.field, m.filter, m.deployment_id
FROM mosaics m WHERE m.field IN ('zzz_fc', 'zzz_fh');

-- The field products of each instrument. Inserting them is check 3's first half.
INSERT INTO storage_objects (backend, bucket, storage_key, content_hash, size_bytes, content_type, product_type, instrument, status, field, filter, deployment_id) VALUES
  ('osn', 'data', 'data/products/nircam/zzz_fc/f444w/expmap_zzz_fc_f444w.fits', 'sha256:' || repeat('b1', 32), 700, 'application/fits', 'nircam_expmap',      'nircam', 'active', 'zzz_fc', 'f444w', 990301),
  ('osn', 'data', 'data/products/nircam/zzz_fc/f444w/expmap_zzz_fc_f444w.png',  'sha256:' || repeat('b2', 32), 300, 'image/png',        'nircam_expmap_plot', 'nircam', 'active', 'zzz_fc', 'f444w', 990301),
  ('osn', 'data', 'data/products/nircam/zzz_fc/zzz_fc_layout.png',              'sha256:' || repeat('b3', 32), 300, 'image/png',        'nircam_layout',      'nircam', 'active', 'zzz_fc', NULL,    990301),
  ('osn', 'data', 'data/products/acswfc/zzz_fc/f814w/expmap_zzz_fc_f814w.fits', 'sha256:' || repeat('b4', 32), 700, 'application/fits', 'acswfc_expmap',      'acswfc', 'active', 'zzz_fc', 'f814w', 990302),
  ('osn', 'data', 'data/products/acswfc/zzz_fc/f814w/expmap_zzz_fc_f814w.png',  'sha256:' || repeat('b5', 32), 300, 'image/png',        'acswfc_expmap_plot', 'acswfc', 'active', 'zzz_fc', 'f814w', 990302),
  ('osn', 'data', 'data/products/acswfc/zzz_fc/zzz_fc_layout.png',              'sha256:' || repeat('b6', 32), 300, 'image/png',        'acswfc_layout',      'acswfc', 'active', 'zzz_fc', NULL,    990302),
  ('osn', 'data', 'data/products/acswfc/zzz_fh/zzz_fh_layout.png',              'sha256:' || repeat('b7', 32), 300, 'image/png',        'acswfc_layout',      'acswfc', 'active', 'zzz_fh', NULL,    990303),
  ('osn', 'data', 'data/products/wfc3ir/zzz_fh/f160w/expmap_zzz_fh_f160w.fits', 'sha256:' || repeat('b8', 32), 700, 'application/fits', 'wfc3ir_expmap',      'wfc3ir', 'active', 'zzz_fh', 'f160w', 990304),
  ('osn', 'data', 'data/products/wfc3ir/zzz_fh/f160w/expmap_zzz_fh_f160w.png',  'sha256:' || repeat('b9', 32), 300, 'image/png',        'wfc3ir_expmap_plot', 'wfc3ir', 'active', 'zzz_fh', 'f160w', 990304),
  ('osn', 'data', 'data/products/wfc3ir/zzz_fh/zzz_fh_layout.png',              'sha256:' || repeat('ba', 32), 300, 'image/png',        'wfc3ir_layout',      'wfc3ir', 'active', 'zzz_fh', NULL,    990304);

-- A helper that runs SQL as a principal (claims + SET LOCAL ROLE).
CREATE FUNCTION pg_temp.zzz_fc_as(p_role text, p_uid uuid, p_sql text) RETURNS text
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
GRANT EXECUTE ON FUNCTION pg_temp.zzz_fc_as(text, uuid, text) TO authenticated, anon;

-- ---------------------------------------------------------------------------
-- 1. One coverage map for every instrument
-- ---------------------------------------------------------------------------
DO $$
DECLARE c jsonb;
BEGIN
  c := public.field_coverage('{}'::jsonb, NULL, NULL, NULL);
  IF c <> '{}'::jsonb THEN RAISE EXCEPTION 'no coverage anywhere should be an empty map, got %', c; END IF;

  c := public.field_coverage('{}'::jsonb, 120.5, 0.03347222, 0.5);
  IF c <> '{"nircam": {"area_arcmin2": 120.5, "area_deg2": 0.03347222, "pixel_scale_arcsec": 0.5}}'::jsonb THEN
    RAISE EXCEPTION 'the typed columns should become the nircam entry, got %', c;
  END IF;

  -- the grid scale alone (a field whose layout JSON carried no area)
  c := public.field_coverage(NULL, NULL, NULL, 0.5);
  IF c <> '{"nircam": {"pixel_scale_arcsec": 0.5}}'::jsonb THEN
    RAISE EXCEPTION 'a scale without an area should still give a nircam entry, got %', c;
  END IF;

  c := public.field_coverage('{"acswfc": {"area_arcmin2": 58.2}}'::jsonb, 120.5, 0.03347222, 0.5);
  IF (c->'acswfc'->>'area_arcmin2')::float8 <> 58.2 OR (c->'nircam'->>'area_arcmin2')::float8 <> 120.5 THEN
    RAISE EXCEPTION 'both instruments should be in the map, got %', c;
  END IF;

  -- NIRCam has one source: the typed columns
  c := public.field_coverage('{"nircam": {"area_arcmin2": 1}}'::jsonb, 120.5, NULL, NULL);
  IF (c->'nircam'->>'area_arcmin2')::float8 <> 120.5 THEN
    RAISE EXCEPTION 'a nircam key in the jsonb must not override the typed columns, got %', c;
  END IF;
  c := public.field_coverage('{"nircam": {"area_arcmin2": 1}}'::jsonb, NULL, NULL, NULL);
  IF c ? 'nircam' THEN RAISE EXCEPTION 'a nircam key in the jsonb must not stand in for the typed columns, got %', c; END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Writing one instrument's coverage
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  admin uuid := '00000000-0000-0000-0000-0000000000f1';
  usr uuid := '00000000-0000-0000-0000-0000000000f2';
  got text; c jsonb;
BEGIN
  got := pg_temp.zzz_fc_as('authenticated', admin,
    $q$SELECT public.set_field_coverage('zzz_fc', 'acswfc', '{"area_arcmin2": 58.2, "area_deg2": 0.01616667, "pixel_scale_arcsec": 0.5, "deployment_id": 990302}')::text$q$);
  IF got LIKE 'ERR:%' THEN RAISE EXCEPTION 'an admin should record acswfc coverage: %', got; END IF;
  got := pg_temp.zzz_fc_as('authenticated', admin,
    $q$SELECT public.set_field_coverage('zzz_fc', 'wfc3ir', '{"area_arcmin2": 5.7, "pixel_scale_arcsec": 0.5}')::text$q$);
  IF got LIKE 'ERR:%' THEN RAISE EXCEPTION 'an admin should record wfc3ir coverage: %', got; END IF;
  SELECT instrument_coverage INTO c FROM fields WHERE name = 'zzz_fc';
  IF (c->'acswfc'->>'area_arcmin2')::float8 <> 58.2 OR (c->'wfc3ir'->>'area_arcmin2')::float8 <> 5.7 THEN
    RAISE EXCEPTION 'the second instrument overwrote the first: %', c;
  END IF;

  -- a redeploy replaces that instrument's entry and nothing else
  got := pg_temp.zzz_fc_as('authenticated', admin,
    $q$SELECT public.set_field_coverage('zzz_fc', 'wfc3ir', '{"area_arcmin2": 6.1, "pixel_scale_arcsec": 0.5}')::text$q$);
  SELECT instrument_coverage INTO c FROM fields WHERE name = 'zzz_fc';
  IF (c->'wfc3ir'->>'area_arcmin2')::float8 <> 6.1 OR (c->'acswfc'->>'area_arcmin2')::float8 <> 58.2 THEN
    RAISE EXCEPTION 'a redeploy should replace its own entry only: %', c;
  END IF;
  -- the typed NIRCam columns are untouched
  IF (SELECT coverage_area_arcmin2 FROM fields WHERE name = 'zzz_fc') <> 120.5 THEN
    RAISE EXCEPTION 'recording another instrument changed the NIRCam coverage';
  END IF;

  got := pg_temp.zzz_fc_as('authenticated', admin, $q$SELECT public.set_field_coverage('zzz_fc', 'nircam', '{"area_arcmin2": 1}')::text$q$);
  IF got NOT LIKE 'ERR: set_field_coverage: instrument nircam%' THEN RAISE EXCEPTION 'nircam should be refused, got %', got; END IF;
  got := pg_temp.zzz_fc_as('authenticated', admin, $q$SELECT public.set_field_coverage('zzz_fc', 'miri', '{"area_arcmin2": 1}')::text$q$);
  IF got NOT LIKE 'ERR: set_field_coverage: instrument miri%' THEN RAISE EXCEPTION 'an unknown instrument should be refused, got %', got; END IF;
  got := pg_temp.zzz_fc_as('authenticated', admin, $q$SELECT public.set_field_coverage('zzz_fc', 'acswfc', '[1, 2]')::text$q$);
  IF got NOT LIKE 'ERR: set_field_coverage: coverage must be a JSON object%' THEN RAISE EXCEPTION 'a non-object should be refused, got %', got; END IF;
  got := pg_temp.zzz_fc_as('authenticated', admin, $q$SELECT public.set_field_coverage('zzz_no_such_field', 'acswfc', '{"area_arcmin2": 1}')::text$q$);
  IF got NOT LIKE 'ERR: set_field_coverage: no field%' THEN RAISE EXCEPTION 'an unknown field should be an error, got %', got; END IF;

  -- a signed-in non-admin cannot write a field row, so nothing changes
  got := pg_temp.zzz_fc_as('authenticated', usr, $q$SELECT public.set_field_coverage('zzz_fc', 'acswfc', '{"area_arcmin2": 999}')::text$q$);
  IF got NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'a non-admin write should be an error, got %', got; END IF;
  IF (SELECT (instrument_coverage->'acswfc'->>'area_arcmin2')::float8 FROM fields WHERE name = 'zzz_fc') <> 58.2 THEN
    RAISE EXCEPTION 'a non-admin changed the coverage';
  END IF;
  got := pg_temp.zzz_fc_as('anon', NULL, $q$SELECT public.set_field_coverage('zzz_fc', 'acswfc', '{"area_arcmin2": 999}')::text$q$);
  IF got NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'anon should not be able to call set_field_coverage, got %', got; END IF;

  -- the HST-only field
  got := pg_temp.zzz_fc_as('authenticated', admin,
    $q$SELECT public.set_field_coverage('zzz_fh', 'acswfc', '{"area_arcmin2": 30.25, "area_deg2": 0.00840278, "pixel_scale_arcsec": 0.5, "deployment_id": 990303}')::text$q$);
  IF got LIKE 'ERR:%' THEN RAISE EXCEPTION 'coverage of the HST-only field failed: %', got; END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 3. The registry refuses an unknown field product
-- ---------------------------------------------------------------------------
DO $$
DECLARE ok boolean := false;
BEGIN
  BEGIN
    INSERT INTO storage_objects (backend, bucket, storage_key, content_hash, size_bytes, content_type, product_type, instrument, status, field)
    VALUES ('osn', 'data', 'data/products/acswfc/zzz_fc/zzz_fc_footprints.png', 'sha256:' || repeat('bb', 32), 1, 'image/png', 'acswfc_footprints', 'acswfc', 'active', 'zzz_fc');
  EXCEPTION WHEN check_violation THEN ok := true;
  END;
  IF NOT ok THEN RAISE EXCEPTION 'an unknown product type should be refused'; END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 4. The field summaries
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  usr uuid := '00000000-0000-0000-0000-0000000000f2';
  got text; c jsonb;
BEGIN
  -- NIRCam and ACS/WFC: the layout plot is NIRCam's; the typed columns stay NIRCam's
  got := pg_temp.zzz_fc_as('authenticated', usr, $q$SELECT layout_key || '|' || coverage_area_arcmin2::text FROM public.get_nircam_field_summary('zzz_fc')$q$);
  IF got <> 'data/products/nircam/zzz_fc/zzz_fc_layout.png|120.5' THEN
    RAISE EXCEPTION 'a field with NIRCam should show NIRCam''s layout and area, got %', got;
  END IF;
  got := pg_temp.zzz_fc_as('authenticated', usr, $q$SELECT coverage::text FROM public.get_nircam_field_summary('zzz_fc')$q$);
  c := got::jsonb;
  IF (c->'nircam'->>'area_arcmin2')::float8 <> 120.5 OR (c->'acswfc'->>'area_arcmin2')::float8 <> 58.2
     OR (c->'wfc3ir'->>'area_arcmin2')::float8 <> 6.1 OR (c->'acswfc'->>'pixel_scale_arcsec')::float8 <> 0.5 THEN
    RAISE EXCEPTION 'the summary should carry every instrument''s coverage, got %', got;
  END IF;

  -- ACS/WFC only: no NIRCam area, the layout plot is ACS/WFC's (before WFC3/IR's)
  got := pg_temp.zzz_fc_as('authenticated', usr, $q$SELECT layout_key || '|' || COALESCE(coverage_area_arcmin2::text, 'null') || '|' || coverage::text FROM public.get_nircam_field_summary('zzz_fh')$q$);
  IF got NOT LIKE 'data/products/acswfc/zzz_fh/zzz_fh_layout.png|null|%' THEN
    RAISE EXCEPTION 'an HST-only field should show the ACS/WFC layout and no NIRCam area, got %', got;
  END IF;
  c := split_part(got, '|', 3)::jsonb;
  IF c ? 'nircam' OR (c->'acswfc'->>'area_arcmin2')::float8 <> 30.25 THEN
    RAISE EXCEPTION 'an HST-only field should carry ACS/WFC coverage alone, got %', c;
  END IF;

  -- the landing grid agrees
  got := pg_temp.zzz_fc_as('authenticated', usr, $q$SELECT string_agg(field || '=' || layout_key || '=' || (coverage ? 'acswfc')::text, ';' ORDER BY field) FROM public.get_nircam_fields() WHERE field IN ('zzz_fc', 'zzz_fh')$q$);
  IF got <> 'zzz_fc=data/products/nircam/zzz_fc/zzz_fc_layout.png=true;zzz_fh=data/products/acswfc/zzz_fh/zzz_fh_layout.png=true' THEN
    RAISE EXCEPTION 'the landing grid is off: %', got;
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 5. A release component freezes its instrument's field products
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  admin uuid := '00000000-0000-0000-0000-0000000000f1';
  got text; r record; types text;
BEGIN
  got := pg_temp.zzz_fc_as('authenticated', admin, $q$SELECT (public.dr_enqueue_freeze('DR-zzzfc-a', '["acswfc/zzz_fc"]', NULL, true))->>'state'$q$);
  IF got <> 'frozen' THEN RAISE EXCEPTION 'the acswfc freeze should succeed, got % (%)', got, (SELECT error FROM dr_releases WHERE tag = 'DR-zzzfc-a'); END IF;

  SELECT string_agg(product_type, ',' ORDER BY product_type) INTO types FROM dr_manifest WHERE tag = 'DR-zzzfc-a';
  IF types <> 'acswfc_expmap,acswfc_expmap_plot,acswfc_layout,acswfc_mosaic' THEN
    RAISE EXCEPTION 'acswfc/<field> should hold its mosaic, exposure map, plot and layout, got %', types;
  END IF;
  IF EXISTS (SELECT 1 FROM dr_manifest WHERE tag = 'DR-zzzfc-a' AND component <> 'acswfc/zzz_fc') THEN
    RAISE EXCEPTION 'acswfc/<field> made another component';
  END IF;

  SELECT * INTO r FROM dr_releases WHERE tag = 'DR-zzzfc-a';
  IF (r.counts->'by_component'->'acswfc/zzz_fc'->>'area_arcmin2')::float8 <> 58.2 THEN
    RAISE EXCEPTION 'the acswfc component should report its own coverage: %', r.counts->'by_component';
  END IF;
  IF (SELECT (instrument_coverage->'acswfc'->>'area_arcmin2')::float8 FROM dr_fields WHERE tag = 'DR-zzzfc-a' AND name = 'zzz_fc') <> 58.2 THEN
    RAISE EXCEPTION 'the frozen field row lost the coverage map';
  END IF;

  -- NIRCam's component reports NIRCam's area, as before
  got := pg_temp.zzz_fc_as('authenticated', admin, $q$SELECT (public.dr_enqueue_freeze('DR-zzzfc-n', '["nircam/zzz_fc"]', NULL, true))->>'state'$q$);
  IF got <> 'frozen' THEN RAISE EXCEPTION 'the nircam freeze should succeed, got % (%)', got, (SELECT error FROM dr_releases WHERE tag = 'DR-zzzfc-n'); END IF;
  SELECT * INTO r FROM dr_releases WHERE tag = 'DR-zzzfc-n';
  IF (r.counts->'by_component'->'nircam/zzz_fc'->>'area_arcmin2')::float8 <> 120.5 THEN
    RAISE EXCEPTION 'the nircam component should report the NIRCam coverage: %', r.counts->'by_component';
  END IF;
  IF EXISTS (SELECT 1 FROM dr_manifest WHERE tag = 'DR-zzzfc-n' AND product_type LIKE 'acswfc%') THEN
    RAISE EXCEPTION 'nircam/<field> took an acswfc field product';
  END IF;

  -- browsing the acswfc release: the field's layout plot is the frozen acswfc one
  PERFORM set_config('campfire.dr_tag', 'DR-zzzfc-a', true);
  SELECT layout_key INTO got FROM public.get_nircam_field_summary('zzz_fc');
  PERFORM set_config('campfire.dr_tag', '', true);
  IF got IS DISTINCT FROM 'data/products/acswfc/zzz_fc/zzz_fc_layout.png' THEN
    RAISE EXCEPTION 'under the acswfc release the layout should be the frozen acswfc one, got %', got;
  END IF;
END $$;

ROLLBACK;
\echo 'check_field_coverage: OK'
