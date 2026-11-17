-- check_mosaics_instrument.sql
--
-- The mosaic index was `nircam_images` until 2026-11. It is `mosaics` now and
-- holds every imaging instrument (NIRCam, and the HST instruments ACS/WFC and
-- WFC3/IR on the same tiles). This checks what the rename added:
--
--   1. `instrument` defaults to nircam, is part of the row's identity (two
--      instruments may hold the same field / tile / filter / scale / extension
--      / epoch) and takes no unknown value.
--   2. A field deployment carries its instrument: a writer that gives none
--      gets nircam (the trigger), an unknown one is refused, and an
--      observation deployment has none.
--   3. set_deployment_status on one instrument's deployment flips that
--      instrument's mosaics and leaves the other instrument's alone.
--   4. The field summaries list the instruments, and the admin deployments
--      list returns and filters on the instrument.
--   5. A release component is one instrument of one field. On a field that
--      has NIRCam and ACS/WFC mosaics: `nircam/<field>` freezes nothing of
--      ACS/WFC's, `acswfc/<field>` nothing of NIRCam's (mosaics and field
--      products alike), both together freeze both, and an instrument with no
--      published mosaic of the field is refused.
--
-- Run: psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_mosaics_instrument.sql
-- Everything inside one rolled-back transaction.
\set ON_ERROR_STOP on
BEGIN;

-- ---------------------------------------------------------------------------
-- Fixture
-- ---------------------------------------------------------------------------
INSERT INTO programs (slug, program_name, is_public) VALUES ('zzz_mi_prog', 'Mosaics Instrument Program', true);
INSERT INTO fields (name) VALUES ('zzz_mi');
INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES ('zzz_mi_obs', 'zzz_mi_prog', 9501, 'zzz_mi');

INSERT INTO auth.users (id, email) VALUES
  ('00000000-0000-0000-0000-0000000000e1', 'zzz-mi-admin@test.invalid'),
  ('00000000-0000-0000-0000-0000000000e2', 'zzz-mi-user@test.invalid');
INSERT INTO user_profiles (user_id, username, full_name, is_admin, is_link_account, can_comment, can_inspect) VALUES
  ('00000000-0000-0000-0000-0000000000e1', 'zzz-mi-admin', 'ZZZ MI Admin', true,  false, true, true),
  ('00000000-0000-0000-0000-0000000000e2', 'zzz-mi-user',  'ZZZ MI User',  false, false, true, false);

-- One published deployment per instrument. The NIRCam one is written the way
-- a writer that predates the column writes it: with no instrument.
INSERT INTO deployments (id, field, status, published_at) VALUES (990201, 'zzz_mi', 'published', now());
INSERT INTO deployments (id, field, status, published_at, instrument) VALUES (990202, 'zzz_mi', 'published', now(), 'acswfc');

-- The same tile, scale, extension and epoch for both instruments, and one
-- filter name the two share on purpose (f814w is ACS/WFC's; giving NIRCam a
-- row of that name is the collision the identity has to allow).
INSERT INTO mosaics (field, tile, filter, pixel_scale, extension, epoch, file_path, deploy_status, deployment_id) VALUES
  ('zzz_mi', 'main', 'f444w', '30mas', 'sci', '', 'data/products/nircam/zzz_mi/f444w/mosaic_nircam_f444w_zzz_mi_30mas_main_sci.fits.gz', 'published', 990201),
  ('zzz_mi', 'main', 'f814w', '30mas', 'sci', '', 'data/products/nircam/zzz_mi/f814w/mosaic_nircam_f814w_zzz_mi_30mas_main_sci.fits.gz', 'published', 990201);
INSERT INTO mosaics (instrument, field, tile, filter, pixel_scale, extension, epoch, file_path, deploy_status, deployment_id) VALUES
  ('acswfc', 'zzz_mi', 'main', 'f814w', '30mas', 'sci', '', 'data/products/acswfc/zzz_mi/f814w/mosaic_acswfc_f814w_zzz_mi_30mas_main_sci.fits.gz', 'published', 990202);

INSERT INTO storage_objects (backend, bucket, storage_key, content_hash, size_bytes, content_type, product_type, instrument, status, field, filter, deployment_id)
SELECT 'osn', 'data', m.file_path, 'sha256:' || md5(m.file_path) || md5(m.instrument), 5000, 'application/gzip',
       m.instrument || '_mosaic', m.instrument, 'active', m.field, m.filter, m.deployment_id
FROM mosaics m WHERE m.field = 'zzz_mi';
-- Field products: NIRCam's exposure map and thumbnail, ACS/WFC's thumbnail and quick-look.
INSERT INTO storage_objects (backend, bucket, storage_key, content_hash, size_bytes, content_type, product_type, instrument, status, field, filter, deployment_id) VALUES
  ('osn', 'data', 'data/products/nircam/zzz_mi/f444w/expmap_zzz_mi_f444w.fits', 'sha256:' || repeat('a1', 32), 700, 'application/fits', 'nircam_expmap', 'nircam', 'active', 'zzz_mi', 'f444w', 990201),
  ('osn', 'data', 'data/products/nircam/zzz_mi/f444w/mosaic_nircam_f444w_zzz_mi_30mas_main_thumb.png', 'sha256:' || repeat('a2', 32), 100, 'image/png', 'nircam_mosaic_thumbnail', 'nircam', 'active', 'zzz_mi', 'f444w', 990201),
  ('osn', 'data', 'data/products/acswfc/zzz_mi/f814w/mosaic_acswfc_f814w_zzz_mi_30mas_main_thumb.png', 'sha256:' || repeat('a3', 32), 100, 'image/png', 'acswfc_mosaic_thumbnail', 'acswfc', 'active', 'zzz_mi', 'f814w', 990202),
  ('osn', 'data', 'data/products/acswfc/zzz_mi/f814w/mosaic_acswfc_f814w_zzz_mi_30mas_main_quicklook.png', 'sha256:' || repeat('a4', 32), 400, 'image/png', 'acswfc_mosaic_quicklook', 'acswfc', 'active', 'zzz_mi', 'f814w', 990202);

-- A helper that runs SQL as a principal (claims + SET LOCAL ROLE).
CREATE FUNCTION pg_temp.zzz_mi_as(p_role text, p_uid uuid, p_sql text) RETURNS text
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
GRANT EXECUTE ON FUNCTION pg_temp.zzz_mi_as(text, uuid, text) TO authenticated, anon;

-- ---------------------------------------------------------------------------
-- 1. The row identity
-- ---------------------------------------------------------------------------
DO $$
DECLARE n int; ok boolean;
BEGIN
  SELECT count(*) INTO n FROM mosaics WHERE field = 'zzz_mi' AND instrument = 'nircam';
  IF n <> 2 THEN RAISE EXCEPTION 'a mosaic written without an instrument should be nircam, got % nircam rows', n; END IF;

  -- same field / tile / filter / scale / extension / epoch, two instruments
  SELECT count(*) INTO n FROM mosaics
   WHERE field = 'zzz_mi' AND tile = 'main' AND filter = 'f814w' AND pixel_scale = '30mas' AND extension = 'sci' AND epoch = '';
  IF n <> 2 THEN RAISE EXCEPTION 'two instruments should each hold the f814w slot, got %', n; END IF;

  ok := false;
  BEGIN
    INSERT INTO mosaics (instrument, field, tile, filter, pixel_scale, extension, epoch, file_path)
    VALUES ('acswfc', 'zzz_mi', 'main', 'f814w', '30mas', 'sci', '', 'data/products/acswfc/zzz_mi/f814w/dup.fits.gz');
  EXCEPTION WHEN unique_violation THEN ok := true;
  END;
  IF NOT ok THEN RAISE EXCEPTION 'a second acswfc row of the same slot should be refused'; END IF;

  ok := false;
  BEGIN
    INSERT INTO mosaics (instrument, field, tile, filter, pixel_scale, extension, epoch, file_path)
    VALUES ('miri', 'zzz_mi', 'main', 'f770w', '30mas', 'sci', '', 'data/products/miri/zzz_mi/f770w/x.fits.gz');
  EXCEPTION WHEN check_violation THEN ok := true;
  END;
  IF NOT ok THEN RAISE EXCEPTION 'an unknown instrument should be refused'; END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Deployments carry their instrument
-- ---------------------------------------------------------------------------
DO $$
DECLARE v text; ok boolean; v_id int;
BEGIN
  SELECT instrument INTO v FROM deployments WHERE id = 990201;
  IF v IS DISTINCT FROM 'nircam' THEN RAISE EXCEPTION 'a field deployment written without an instrument should be nircam, got %', v; END IF;
  SELECT instrument INTO v FROM deployments WHERE id = 990202;
  IF v IS DISTINCT FROM 'acswfc' THEN RAISE EXCEPTION 'the acswfc deployment lost its instrument: %', v; END IF;

  ok := false;
  BEGIN
    INSERT INTO deployments (field, status, instrument) VALUES ('zzz_mi', 'draft', 'miri');
  EXCEPTION WHEN check_violation THEN ok := true;
  END;
  IF NOT ok THEN RAISE EXCEPTION 'a field deployment of an unknown instrument should be refused'; END IF;

  INSERT INTO deployments (observation, status) VALUES ('zzz_mi_obs', 'draft') RETURNING id, instrument INTO v_id, v;
  IF v IS NOT NULL THEN RAISE EXCEPTION 'an observation deployment should have no instrument, got %', v; END IF;
  ok := false;
  BEGIN
    INSERT INTO deployments (observation, status, instrument) VALUES ('zzz_mi_obs', 'draft', 'nircam');
  EXCEPTION WHEN check_violation THEN ok := true;
  END;
  IF NOT ok THEN RAISE EXCEPTION 'an observation deployment with an imaging instrument should be refused'; END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 3. The lifecycle acts on one instrument's deployment
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  admin uuid := '00000000-0000-0000-0000-0000000000e1';
  usr uuid := '00000000-0000-0000-0000-0000000000e2';
  got text; r json; n int;
BEGIN
  got := pg_temp.zzz_mi_as('authenticated', admin, $q$SELECT public.set_deployment_status(990202, 'draft')::text$q$);
  IF got LIKE 'ERR:%' THEN RAISE EXCEPTION 'set_deployment_status failed: %', got; END IF;
  r := got::json;
  IF r->>'instrument' <> 'acswfc' OR ((r->'mosaics')->>'updated')::int <> 1 THEN
    RAISE EXCEPTION 'drafting the acswfc deployment should flip its one mosaic and say acswfc: %', got;
  END IF;
  -- the key until 2026-11, answered for one release beside the new one
  IF (r->'nircam_images')::text IS DISTINCT FROM (r->'mosaics')::text THEN
    RAISE EXCEPTION 'the nircam_images alias should repeat the mosaics block: %', got;
  END IF;
  SELECT count(*) INTO n FROM mosaics WHERE field = 'zzz_mi' AND instrument = 'nircam' AND deploy_status = 'published';
  IF n <> 2 THEN RAISE EXCEPTION 'drafting acswfc touched the nircam mosaics (% still published)', n; END IF;
  IF (SELECT metadata->>'instrument' FROM deploy_events WHERE deployment_id = 990202 ORDER BY occurred_at DESC LIMIT 1) <> 'acswfc' THEN
    RAISE EXCEPTION 'the lifecycle event should record the deployment''s instrument';
  END IF;

  -- a signed-in non-admin sees the published instrument only
  got := pg_temp.zzz_mi_as('authenticated', usr, $q$SELECT string_agg(DISTINCT instrument, ',' ORDER BY instrument) FROM mosaics WHERE field = 'zzz_mi'$q$);
  IF got <> 'nircam' THEN RAISE EXCEPTION 'a non-admin should see nircam only while acswfc is draft, got %', got; END IF;

  got := pg_temp.zzz_mi_as('authenticated', admin, $q$SELECT public.set_deployment_status(990202, 'published')::text$q$);
  IF got LIKE 'ERR:%' THEN RAISE EXCEPTION 'republish failed: %', got; END IF;
  got := pg_temp.zzz_mi_as('authenticated', usr, $q$SELECT string_agg(DISTINCT instrument, ',' ORDER BY instrument) FROM mosaics WHERE field = 'zzz_mi'$q$);
  IF got <> 'acswfc,nircam' THEN RAISE EXCEPTION 'a non-admin should see both instruments once published, got %', got; END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 4. Field summaries and the admin deployments list
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  admin uuid := '00000000-0000-0000-0000-0000000000e1';
  usr uuid := '00000000-0000-0000-0000-0000000000e2';
  got text;
BEGIN
  got := pg_temp.zzz_mi_as('authenticated', usr, $q$SELECT array_to_string(instruments, ',') || '|' || array_to_string(filters, ',') FROM public.get_nircam_field_summary('zzz_mi')$q$);
  IF got <> 'acswfc,nircam|f444w,f814w' THEN RAISE EXCEPTION 'field summary should list both instruments and the filters, got %', got; END IF;

  -- three (instrument, filter) pairs: nircam f444w, nircam f814w, acswfc f814w
  got := pg_temp.zzz_mi_as('authenticated', usr, $q$SELECT n_filters::text || '|' || array_to_string(instruments, ',') FROM public.get_nircam_fields() WHERE field = 'zzz_mi'$q$);
  IF got <> '3|acswfc,nircam' THEN RAISE EXCEPTION 'field card should count 3 filters over both instruments, got %', got; END IF;

  got := pg_temp.zzz_mi_as('authenticated', admin, $q$SELECT string_agg(id::text, ',' ORDER BY id) FROM public.get_admin_deployments(NULL, 'acswfc') WHERE field = 'zzz_mi'$q$);
  IF got <> '990202' THEN RAISE EXCEPTION 'the acswfc filter should list the acswfc deployment only, got %', got; END IF;
  got := pg_temp.zzz_mi_as('authenticated', admin, $q$SELECT string_agg(id::text || ':' || instrument, ',' ORDER BY id) FROM public.get_admin_deployments(NULL, 'nircam') WHERE field = 'zzz_mi'$q$);
  IF got <> '990201:nircam' THEN RAISE EXCEPTION 'the nircam filter should list the nircam deployment only, got %', got; END IF;
  got := pg_temp.zzz_mi_as('authenticated', admin, $q$SELECT count(*)::text FROM public.get_admin_deployments(NULL, 'nirspec') WHERE field IS NOT NULL$q$);
  IF got <> '0' THEN RAISE EXCEPTION 'the nirspec filter should list no field deployment, got %', got; END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 5. A release component is one instrument of one field
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  admin uuid := '00000000-0000-0000-0000-0000000000e1';
  got text; n int; r record;
BEGIN
  -- an instrument with no published mosaic of the field is refused
  got := pg_temp.zzz_mi_as('authenticated', admin, $q$SELECT (public.dr_enqueue_freeze('DR-zzzmi-w', '["wfc3ir/zzz_mi"]', NULL, true))::text$q$);
  IF got NOT LIKE 'ERR: component wfc3ir/zzz_mi: no published wfc3ir mosaics%' THEN
    RAISE EXCEPTION 'wfc3ir/<field> with no wfc3ir mosaic should be refused, got %', got;
  END IF;
  got := pg_temp.zzz_mi_as('authenticated', admin, $q$SELECT (public.dr_enqueue_freeze('DR-zzzmi-m', '["miri/zzz_mi"]', NULL, true))::text$q$);
  IF got NOT LIKE 'ERR: unknown component%' THEN RAISE EXCEPTION 'an unknown instrument component should be refused, got %', got; END IF;

  -- NIRCam alone: nothing of ACS/WFC's
  got := pg_temp.zzz_mi_as('authenticated', admin, $q$SELECT (public.dr_enqueue_freeze('DR-zzzmi-n', '["nircam/zzz_mi"]', NULL, true))->>'state'$q$);
  IF got <> 'frozen' THEN RAISE EXCEPTION 'the nircam-only freeze should succeed, got % (%)', got, (SELECT error FROM dr_releases WHERE tag = 'DR-zzzmi-n'); END IF;
  IF EXISTS (SELECT 1 FROM dr_mosaics WHERE tag = 'DR-zzzmi-n' AND instrument <> 'nircam')
     OR (SELECT count(*) FROM dr_mosaics WHERE tag = 'DR-zzzmi-n') <> 2 THEN
    RAISE EXCEPTION 'nircam/<field> should freeze the 2 nircam mosaics and no other instrument''s';
  END IF;
  IF EXISTS (SELECT 1 FROM dr_manifest WHERE tag = 'DR-zzzmi-n' AND (component <> 'nircam/zzz_mi' OR storage_key LIKE 'data/products/acswfc/%')) THEN
    RAISE EXCEPTION 'nircam/<field> took an acswfc file or made another component';
  END IF;
  -- 2 mosaics + the exposure map + the thumbnail
  SELECT count(*) INTO n FROM dr_manifest WHERE tag = 'DR-zzzmi-n';
  IF n <> 4 THEN RAISE EXCEPTION 'nircam/<field> should hold 4 members, got %', n; END IF;
  SELECT * INTO r FROM dr_releases WHERE tag = 'DR-zzzmi-n';
  IF r.counts->'nircam_fields' <> '["zzz_mi"]'::jsonb OR r.counts->'imaging_components' <> '["nircam/zzz_mi"]'::jsonb
     OR (r.counts->>'mosaics')::int <> 2 THEN
    RAISE EXCEPTION 'nircam-only counts are off: %', r.counts;
  END IF;
  -- a release row is permanent, so the old key is not written beside the new
  IF r.counts ? 'nircam_images' THEN
    RAISE EXCEPTION 'a freeze still writes counts.nircam_images: %', r.counts;
  END IF;

  -- ACS/WFC alone: nothing of NIRCam's
  got := pg_temp.zzz_mi_as('authenticated', admin, $q$SELECT (public.dr_enqueue_freeze('DR-zzzmi-a', '["acswfc/zzz_mi"]', NULL, true))->>'state'$q$);
  IF got <> 'frozen' THEN RAISE EXCEPTION 'the acswfc-only freeze should succeed, got % (%)', got, (SELECT error FROM dr_releases WHERE tag = 'DR-zzzmi-a'); END IF;
  IF (SELECT string_agg(instrument || ':' || filter, ',') FROM dr_mosaics WHERE tag = 'DR-zzzmi-a') <> 'acswfc:f814w' THEN
    RAISE EXCEPTION 'acswfc/<field> should freeze the one acswfc mosaic only';
  END IF;
  IF EXISTS (SELECT 1 FROM dr_manifest WHERE tag = 'DR-zzzmi-a' AND (component <> 'acswfc/zzz_mi' OR storage_key LIKE 'data/products/nircam/%')) THEN
    RAISE EXCEPTION 'acswfc/<field> took a nircam file or made another component';
  END IF;
  -- the mosaic + its thumbnail + its quick-look
  SELECT count(*) INTO n FROM dr_manifest WHERE tag = 'DR-zzzmi-a';
  IF n <> 3 THEN RAISE EXCEPTION 'acswfc/<field> should hold 3 members, got %', n; END IF;
  SELECT * INTO r FROM dr_releases WHERE tag = 'DR-zzzmi-a';
  IF r.counts->'nircam_fields' <> '[]'::jsonb OR r.counts->'imaging_components' <> '["acswfc/zzz_mi"]'::jsonb
     OR (r.counts->'by_component'->'acswfc/zzz_mi'->>'mosaics')::int <> 1 THEN
    RAISE EXCEPTION 'acswfc-only counts are off: %', r.counts;
  END IF;
  -- the field row is frozen with either instrument (the field pages read it)
  IF NOT EXISTS (SELECT 1 FROM dr_fields WHERE tag = 'DR-zzzmi-a' AND name = 'zzz_mi') THEN
    RAISE EXCEPTION 'an acswfc component should freeze its field row';
  END IF;

  -- both: each file under its own instrument's component
  got := pg_temp.zzz_mi_as('authenticated', admin, $q$SELECT (public.dr_enqueue_freeze('DR-zzzmi-b', '["nircam/zzz_mi","acswfc/zzz_mi"]', NULL, true))->>'state'$q$);
  IF got <> 'frozen' THEN RAISE EXCEPTION 'the two-instrument freeze should succeed, got % (%)', got, (SELECT error FROM dr_releases WHERE tag = 'DR-zzzmi-b'); END IF;
  IF (SELECT count(*) FROM dr_manifest WHERE tag = 'DR-zzzmi-b' AND component = 'nircam/zzz_mi') <> 4
     OR (SELECT count(*) FROM dr_manifest WHERE tag = 'DR-zzzmi-b' AND component = 'acswfc/zzz_mi') <> 3
     OR (SELECT count(*) FROM dr_mosaics WHERE tag = 'DR-zzzmi-b') <> 3 THEN
    RAISE EXCEPTION 'the two-instrument freeze should hold 4 + 3 members and 3 mosaic rows';
  END IF;
  IF EXISTS (SELECT 1 FROM dr_manifest m WHERE m.tag = 'DR-zzzmi-b'
             AND split_part(m.component, '/', 1) <> split_part(m.product_type, '_', 1)) THEN
    RAISE EXCEPTION 'a member sits under another instrument''s component';
  END IF;

  -- the frozen twin is the live row, column for column
  IF EXISTS (SELECT 1 FROM dr_mosaics d JOIN mosaics m ON m.id = d.id
             WHERE d.tag = 'DR-zzzmi-b' AND to_jsonb(d) - 'tag' <> to_jsonb(m)) THEN
    RAISE EXCEPTION 'dr_mosaics is not a verbatim copy of mosaics';
  END IF;
END $$;

ROLLBACK;
