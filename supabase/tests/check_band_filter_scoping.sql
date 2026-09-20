-- Guard: the photometry band filter must stay VIEWER-SCOPED now that its
-- helper bypasses RLS.
--
-- object_band_values() (functions.sql) is SECURITY DEFINER: read under RLS,
-- every object_photometry_bands row re-ran the derived table's two-hop policy
-- chain (parent cross-match -> object) and the band filter timed out in
-- production, so the function enforces the select_objects_by_access predicate
-- itself with one join to objects. That makes the predicate copy load-bearing:
-- if it drifts from the policy, a band filter could surface proprietary or
-- draft objects to a viewer who cannot read them, or hide objects an admin
-- can. This asserts, for the helper and the catalog RPCs that join it, that a
-- public-only viewer, an admin and a viewer holding a private-program grant
-- each get exactly the objects the objects policy gives them, and that the
-- window is tested on the per-object aggregates (min mag / max S/N over an
-- object's cross-matches) the rows display.
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_band_filter_scoping.sql
BEGIN;

DO $$
DECLARE
  v_pub   INTEGER;   -- public program, published, one cross-match: mag 25.0, S/N ~20
  v_dual  INTEGER;   -- public, published, TWO cross-matches: (26.0, S/N ~4) + (27.0, S/N ~8)
  v_nodet INTEGER;   -- public, published, negative flux: no magnitude, S/N -1
  v_draft INTEGER;   -- public program but no published spectrum: mag 23.0
  v_prop  INTEGER;   -- proprietary program, published: mag 24.0
  v_ids   INTEGER[];
  v_mag   DOUBLE PRECISION;
  v_snr   DOUBLE PRECISION;
  v_json  JSONB;
  v_row   JSONB;
  v_count BIGINT;
  v_text  TEXT[];
BEGIN
  -- Fixtures ------------------------------------------------------------------
  INSERT INTO programs (slug, program_name, is_public) VALUES
    ('zzz_bf_pub',  'Band Filter Test Public',  true),
    ('zzz_bf_prop', 'Band Filter Test Private', false);

  INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES
    ('bf_obs_pub',  'zzz_bf_pub',  9981, 'zzz_bf_field'),
    ('bf_obs_prop', 'zzz_bf_prop', 9982, 'zzz_bf_field');

  INSERT INTO auth.users (id, email) VALUES
    ('00000000-0000-0000-0000-0000000000b1', 'zzz-bf-admin@test.invalid'),
    ('00000000-0000-0000-0000-0000000000b2', 'zzz-bf-user@test.invalid');
  INSERT INTO user_profiles (user_id, username, full_name, is_admin, can_comment, can_inspect) VALUES
    ('00000000-0000-0000-0000-0000000000b1', 'zzz-bf-admin', 'ZZZ BF Admin', true,  true, true),
    ('00000000-0000-0000-0000-0000000000b2', 'zzz-bf-user',  'ZZZ BF User',  false, true, true);
  -- The ordinary user holds a grant on the private program (and is not admin).
  INSERT INTO user_program_access (user_id, program_slug) VALUES
    ('00000000-0000-0000-0000-0000000000b2', 'zzz_bf_prop');

  INSERT INTO objects (object_id, field, ra, dec, programs, observations, has_published_spectrum) VALUES
    ('TEST-BF-PUB',   'zzz_bf_field', 150.00, 2.00, '{zzz_bf_pub}',  '{bf_obs_pub}',  true),
    ('TEST-BF-DUAL',  'zzz_bf_field', 150.01, 2.00, '{zzz_bf_pub}',  '{bf_obs_pub}',  true),
    ('TEST-BF-NODET', 'zzz_bf_field', 150.02, 2.00, '{zzz_bf_pub}',  '{bf_obs_pub}',  true),
    ('TEST-BF-DRAFT', 'zzz_bf_field', 150.03, 2.00, '{zzz_bf_pub}',  '{bf_obs_pub}',  false),
    ('TEST-BF-PROP',  'zzz_bf_field', 150.04, 2.00, '{zzz_bf_prop}', '{bf_obs_prop}', true);

  SELECT id INTO v_pub   FROM objects WHERE object_id = 'TEST-BF-PUB';
  SELECT id INTO v_dual  FROM objects WHERE object_id = 'TEST-BF-DUAL';
  SELECT id INTO v_nodet FROM objects WHERE object_id = 'TEST-BF-NODET';
  SELECT id INTO v_draft FROM objects WHERE object_id = 'TEST-BF-DRAFT';
  SELECT id INTO v_prop  FROM objects WHERE object_id = 'TEST-BF-PROP';

  -- One published spectrum on the public and the proprietary object (for the
  -- spectra RPC), a draft one on the draft object.
  INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, object_id) VALUES
    ('test-bf-pub',   'zzz_bf_field', 150.00, 2.00, 'zzz_bf_pub',  'bf_obs_pub',  v_pub),
    ('test-bf-draft', 'zzz_bf_field', 150.03, 2.00, 'zzz_bf_pub',  'bf_obs_pub',  v_draft),
    ('test-bf-prop',  'zzz_bf_field', 150.04, 2.00, 'zzz_bf_prop', 'bf_obs_prop', v_prop);
  INSERT INTO spectra (target_id, grating, fits_path, signal_to_noise, exposure_time, deploy_status) VALUES
    ('test-bf-pub',   'PRISM', '/tmp/bf_pub_prism.fits',   10.0, 1000.0, 'published'),
    ('test-bf-draft', 'PRISM', '/tmp/bf_draft_prism.fits', 10.0, 1000.0, 'draft'),
    ('test-bf-prop',  'PRISM', '/tmp/bf_prop_prism.fits',  10.0, 1000.0, 'published');

  -- Fluxes in uJy: mag = 23.9 - 2.5 log10(flux). The sync_object_photometry_bands
  -- trigger unnests these into object_photometry_bands.
  INSERT INTO object_photometry (object_id, field, ra, dec, catalog_name, photometry)
  SELECT o.id, o.field, o.ra, o.dec, c.catalog,
         jsonb_build_object('flux_unit', 'uJy', 'bands', jsonb_build_object(
           'f444w', jsonb_build_object('flux', c.flux, 'flux_err', c.err, 'wav', 4.4)))
  FROM (VALUES
    ('TEST-BF-PUB',   'zzz_bf_cat_a',  0.36308, 0.018),   -- mag 25.00, S/N ~20
    ('TEST-BF-DUAL',  'zzz_bf_cat_a',  0.14454, 0.036),   -- mag 26.00, S/N ~4
    ('TEST-BF-DUAL',  'zzz_bf_cat_b',  0.05754, 0.0072),  -- mag 27.00, S/N ~8
    ('TEST-BF-NODET', 'zzz_bf_cat_a', -0.5,     0.5),     -- no mag,    S/N -1
    ('TEST-BF-DRAFT', 'zzz_bf_cat_a',  2.29087, 0.02),    -- mag 23.00, S/N ~115
    ('TEST-BF-PROP',  'zzz_bf_cat_a',  0.91201, 0.03)     -- mag 24.00, S/N ~30
  ) AS c(object_id, catalog, flux, err)
  JOIN objects o ON o.object_id = c.object_id;

  IF (SELECT count(*) FROM object_photometry_bands b JOIN object_photometry p ON p.id = b.photometry_id
      WHERE p.catalog_name LIKE 'zzz\_bf\_%') <> 6 THEN
    RAISE EXCEPTION 'fixture: trigger did not unnest 6 band rows';
  END IF;

  -- 1) Helper semantics, no JWT (public programs only, not admin) --------------
  v_ids := ARRAY(SELECT v.object_id FROM public.object_band_values('f444w') v);
  IF NOT (v_pub = ANY(v_ids) AND v_dual = ANY(v_ids) AND v_nodet = ANY(v_ids)) THEN
    RAISE EXCEPTION 'helper: dropped a visible public object: %', v_ids;
  END IF;
  IF v_prop = ANY(v_ids) OR v_draft = ANY(v_ids) THEN
    RAISE EXCEPTION 'helper: leaked a proprietary or draft object to a public-only caller: %', v_ids;
  END IF;

  -- Per-object aggregates across two cross-matches: min(mag), max(snr).
  SELECT v.mag, v.snr INTO v_mag, v_snr FROM public.object_band_values('f444w') v WHERE v.object_id = v_dual;
  IF abs(v_mag - 26.0) > 0.01 OR abs(v_snr - 7.99) > 0.05 THEN
    RAISE EXCEPTION 'helper: dual-catalog aggregates wrong: mag=%, snr=% (want 26.0, ~8)', v_mag, v_snr;
  END IF;
  SELECT v.mag, v.snr INTO v_mag, v_snr FROM public.object_band_values('f444w') v WHERE v.object_id = v_nodet;
  IF v_mag IS NOT NULL OR abs(v_snr + 1.0) > 0.01 THEN
    RAISE EXCEPTION 'helper: non-detection must carry NULL mag and its signed S/N: mag=%, snr=%', v_mag, v_snr;
  END IF;

  -- Windows are tested on those aggregates.
  v_ids := ARRAY(SELECT v.object_id FROM public.object_band_values('f444w', NULL, 26.5, NULL, NULL) v ORDER BY 1);
  IF v_ids <> ARRAY[LEAST(v_pub, v_dual), GREATEST(v_pub, v_dual)] THEN
    RAISE EXCEPTION 'helper: mag <= 26.5 should be exactly {pub, dual} (no-mag row excluded): %', v_ids;
  END IF;
  v_ids := ARRAY(SELECT v.object_id FROM public.object_band_values('f444w', 25.5, NULL, NULL, NULL) v);
  IF v_ids <> ARRAY[v_dual] THEN
    RAISE EXCEPTION 'helper: mag >= 25.5 should be exactly {dual}: %', v_ids;
  END IF;
  v_ids := ARRAY(SELECT v.object_id FROM public.object_band_values('f444w', NULL, NULL, 5, NULL) v ORDER BY 1);
  IF v_ids <> ARRAY[LEAST(v_pub, v_dual), GREATEST(v_pub, v_dual)] THEN
    RAISE EXCEPTION 'helper: snr >= 5 should be {pub, dual} (dual via its BEST cross-match): %', v_ids;
  END IF;
  v_ids := ARRAY(SELECT v.object_id FROM public.object_band_values('f444w', NULL, NULL, NULL, 5) v);
  IF v_ids <> ARRAY[v_nodet] THEN
    RAISE EXCEPTION 'helper: snr <= 5 should be exactly {nodet}: %', v_ids;
  END IF;
  v_ids := ARRAY(SELECT v.object_id FROM public.object_band_values('f444w', NULL, 25.5, 5, NULL) v);
  IF v_ids <> ARRAY[v_pub] THEN
    RAISE EXCEPTION 'helper: mag <= 25.5 AND snr >= 5 should be exactly {pub}: %', v_ids;
  END IF;

  -- The id projection is the same set; NULL / unknown band yield nothing.
  IF ARRAY(SELECT public.objects_matching_band_filter('f444w', NULL, 26.5, NULL, NULL) ORDER BY 1)
     <> ARRAY(SELECT v.object_id FROM public.object_band_values('f444w', NULL, 26.5, NULL, NULL) v ORDER BY 1) THEN
    RAISE EXCEPTION 'objects_matching_band_filter disagrees with object_band_values';
  END IF;
  IF EXISTS (SELECT 1 FROM public.object_band_values(NULL)) OR EXISTS (SELECT 1 FROM public.object_band_values('zzz_no_such_band')) THEN
    RAISE EXCEPTION 'helper: NULL or unknown band must yield no rows';
  END IF;

  -- 2) Public-only authenticated viewer, through RLS ---------------------------
  -- zzz_bf_pub is public, so it is accessible with no JWT claims set.
  PERFORM set_config('role', 'authenticated', true);

  -- Direct call of the SECURITY DEFINER helper must not widen the viewer's set.
  v_ids := ARRAY(SELECT v.object_id FROM public.object_band_values('f444w') v);
  IF v_prop = ANY(v_ids) OR v_draft = ANY(v_ids) OR NOT (v_pub = ANY(v_ids)) THEN
    RAISE EXCEPTION 'authenticated: direct helper call leaked or dropped rows: %', v_ids;
  END IF;

  -- Objects list: window + sort on the band; displayed values are the tested ones.
  SELECT targets, total_count INTO v_json, v_count
    FROM public.get_filtered_objects_paginated(
      p_program_slugs => ARRAY['zzz_bf_pub'],
      p_fields        => ARRAY['zzz_bf_field'],
      p_band          => 'f444w',
      p_band_mag_max  => 26.5,
      p_sort_column   => 'band_mag',
      p_sort_direction => 'asc'
    );
  IF v_count <> 2 OR jsonb_array_length(v_json) <> 2 THEN
    RAISE EXCEPTION 'paginated objects mag <= 26.5: want 2 rows, got count=% rows=%', v_count, v_json;
  END IF;
  IF v_json -> 0 ->> 'object_id' <> 'TEST-BF-PUB' OR v_json -> 1 ->> 'object_id' <> 'TEST-BF-DUAL' THEN
    RAISE EXCEPTION 'paginated objects sorted by band_mag asc: wrong order: %', v_json;
  END IF;
  IF EXISTS (SELECT 1 FROM jsonb_array_elements(v_json) r(row) WHERE (r.row ->> 'band_mag')::float8 > 26.5) THEN
    RAISE EXCEPTION 'paginated objects: a row displays a magnitude outside the window that admitted it: %', v_json;
  END IF;
  SELECT r.row INTO v_row FROM jsonb_array_elements(v_json) r(row) WHERE r.row ->> 'object_id' = 'TEST-BF-DUAL';
  IF abs((v_row ->> 'band_mag')::float8 - 26.0) > 0.01 OR abs((v_row ->> 'band_snr')::float8 - 7.99) > 0.05 THEN
    RAISE EXCEPTION 'paginated objects: dual-catalog row displays wrong aggregates: %', v_row;
  END IF;

  -- No window: the no-magnitude row is admitted and sorts last.
  SELECT targets, total_count INTO v_json, v_count
    FROM public.get_filtered_objects_paginated(
      p_program_slugs => ARRAY['zzz_bf_pub'],
      p_fields        => ARRAY['zzz_bf_field'],
      p_band          => 'f444w',
      p_sort_column   => 'band_mag',
      p_sort_direction => 'asc'
    );
  IF v_count <> 3 OR v_json -> 2 ->> 'object_id' <> 'TEST-BF-NODET' OR (v_json -> 2 -> 'band_mag') <> 'null'::jsonb THEN
    RAISE EXCEPTION 'paginated objects unwindowed: want 3 rows with the no-mag row last: count=% rows=%', v_count, v_json;
  END IF;

  -- get_filtered_object_ids agrees (it sorts through the same join).
  v_text := ARRAY(SELECT object_id FROM public.get_filtered_object_ids(
      p_program_slugs => ARRAY['zzz_bf_pub'],
      p_fields        => ARRAY['zzz_bf_field'],
      p_band          => 'f444w',
      p_band_mag_max  => 26.5,
      p_sort_column   => 'band_mag',
      p_sort_direction => 'asc'));
  IF v_text <> ARRAY['TEST-BF-PUB', 'TEST-BF-DUAL'] THEN
    RAISE EXCEPTION 'get_filtered_object_ids disagrees with the paginated RPC: %', v_text;
  END IF;

  -- Spectra list: the band columns come from the parent object.
  SELECT targets, total_count INTO v_json, v_count
    FROM public.get_filtered_spectra_paginated(
      p_program_slugs => ARRAY['zzz_bf_pub'],
      p_fields        => ARRAY['zzz_bf_field'],
      p_band          => 'f444w'
    );
  IF v_count <> 1 OR v_json -> 0 ->> 'target_id' <> 'test-bf-pub'
     OR abs((v_json -> 0 ->> 'band_mag')::float8 - 25.0) > 0.01 THEN
    RAISE EXCEPTION 'paginated spectra: want the one published public spectrum at mag 25: count=% rows=%', v_count, v_json;
  END IF;

  -- 3) Admin: every program, drafts included ----------------------------------
  PERFORM set_config('request.jwt.claims',
    json_build_object('sub', '00000000-0000-0000-0000-0000000000b1', 'role', 'authenticated')::text, true);
  v_ids := ARRAY(SELECT v.object_id FROM public.object_band_values('f444w') v);
  IF NOT (v_prop = ANY(v_ids) AND v_draft = ANY(v_ids) AND v_pub = ANY(v_ids)) THEN
    RAISE EXCEPTION 'admin: helper hid proprietary or draft objects: %', v_ids;
  END IF;
  SELECT total_count INTO v_count
    FROM public.get_filtered_objects_paginated(
      p_program_slugs => ARRAY['zzz_bf_pub', 'zzz_bf_prop'],
      p_fields        => ARRAY['zzz_bf_field'],
      p_band          => 'f444w',
      p_include_unpublished => true
    );
  IF v_count <> 5 THEN
    RAISE EXCEPTION 'admin: paginated objects with band filter should see all 5 fixtures, got %', v_count;
  END IF;

  -- 4) Ordinary viewer with a grant on the private program --------------------
  PERFORM set_config('request.jwt.claims',
    json_build_object('sub', '00000000-0000-0000-0000-0000000000b2', 'role', 'authenticated')::text, true);
  v_ids := ARRAY(SELECT v.object_id FROM public.object_band_values('f444w') v);
  IF NOT (v_prop = ANY(v_ids)) OR v_draft = ANY(v_ids) THEN
    RAISE EXCEPTION 'granted viewer: helper should include the proprietary object and not the draft one: %', v_ids;
  END IF;
  SELECT total_count INTO v_count
    FROM public.get_filtered_objects_paginated(
      p_program_slugs => ARRAY['zzz_bf_pub', 'zzz_bf_prop'],
      p_fields        => ARRAY['zzz_bf_field'],
      p_band          => 'f444w'
    );
  IF v_count <> 4 THEN
    RAISE EXCEPTION 'granted viewer: paginated objects with band filter should see 4 (pub, dual, nodet, prop), got %', v_count;
  END IF;

  PERFORM set_config('request.jwt.claims', '', true);
  PERFORM set_config('role', 'none', true);

  RAISE NOTICE 'OK: band filter scoping holds (helper + objects / object_ids / spectra RPCs; public, admin and granted viewers).';
END $$;

ROLLBACK;
