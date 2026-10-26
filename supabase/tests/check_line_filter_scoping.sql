-- Guard: the emission-line filter must stay CALLER-SCOPED now that its
-- helper bypasses RLS (#619).
--
-- object_line_values() (functions.sql) is SECURITY DEFINER: the filter's
-- five-way join timed out in production (37 s) and, read under RLS, every
-- lines / line_fits row re-ran its policy (an EXISTS on
-- the parent spectrum planned as a hashed sub-plan over all spectra). It
-- scopes its rows with two gates instead: the caller's explicit
-- p_program_slugs / p_include_unpublished (the only gate for a service-role
-- caller, which is how the /api/v1 bearer routes reach the RPCs after
-- authorizing the user in the web layer), and, for every other caller, the
-- select_spectra_by_access and select_objects_by_access predicates copied
-- from policies.sql. Both gates are load-bearing: if the explicit one is
-- dropped an authorized API user loses their private programs under a line
-- filter; if a policy copy drifts, a viewer calling the function directly
-- with slugs they do not hold could see proprietary or draft detections.
-- This asserts, for the helper and the catalog RPCs that join it, that a
-- service-role caller, a public-only viewer, an admin and a viewer holding a
-- private-program grant each get exactly the objects the policies give
-- them, that a detection on a spectrum the viewer cannot see never counts
-- (not even towards the S/N of an object they see through another
-- program), that the staleness rule holds, and that the window is tested on
-- the per-object aggregate (max S/N over an object's visible spectra) the
-- rows display.
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_line_filter_scoping.sql
BEGIN;

DO $$
DECLARE
  v_pub   INTEGER;   -- public program, published, one spectrum: CIII1908 S/N 10
  v_dual  INTEGER;   -- public, published, TWO spectra: S/N 4 and S/N 8 (object max 8)
  v_stale INTEGER;   -- public, published, S/N 20, but fitted at another redshift (stale)
  v_draft INTEGER;   -- public program but no published spectrum: draft spectrum S/N 50
  v_prop  INTEGER;   -- proprietary program, published: S/N 30
  v_mixed INTEGER;   -- seen through the public program (S/N 2) AND the proprietary one (S/N 12)
  v_ids   INTEGER[];
  v_snr   DOUBLE PRECISION;
  v_json  JSONB;
  v_row   JSONB;
  v_count BIGINT;
  v_text  TEXT[];
  c_pub   CONSTANT TEXT[] := ARRAY['zzz_lf_pub'];
  c_both  CONSTANT TEXT[] := ARRAY['zzz_lf_pub', 'zzz_lf_prop'];
  r       RECORD;
BEGIN
  -- Fixtures ------------------------------------------------------------------
  INSERT INTO programs (slug, program_name, is_public) VALUES
    ('zzz_lf_pub',  'Line Filter Test Public',  true),
    ('zzz_lf_prop', 'Line Filter Test Private', false);

  INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES
    ('lf_obs_pub',  'zzz_lf_pub',  9983, 'zzz_lf_field'),
    ('lf_obs_prop', 'zzz_lf_prop', 9984, 'zzz_lf_field');

  INSERT INTO auth.users (id, email) VALUES
    ('00000000-0000-0000-0000-0000000000c1', 'zzz-lf-admin@test.invalid'),
    ('00000000-0000-0000-0000-0000000000c2', 'zzz-lf-user@test.invalid');
  INSERT INTO user_profiles (user_id, username, full_name, is_admin, can_comment, can_inspect) VALUES
    ('00000000-0000-0000-0000-0000000000c1', 'zzz-lf-admin', 'ZZZ LF Admin', true,  true, true),
    ('00000000-0000-0000-0000-0000000000c2', 'zzz-lf-user',  'ZZZ LF User',  false, true, true);
  -- The ordinary user holds a grant on the private program (and is not admin).
  INSERT INTO user_program_access (user_id, program_slug) VALUES
    ('00000000-0000-0000-0000-0000000000c2', 'zzz_lf_prop');

  -- Every object is inspected at z = 5 with quality 3; the fits below record
  -- the same (version, quality, z) except the stale one.
  INSERT INTO objects (object_id, field, ra, dec, programs, observations, has_published_spectrum,
                       redshift_inspected, redshift_quality) VALUES
    ('TEST-LF-PUB',   'zzz_lf_field', 150.00, 2.00, '{zzz_lf_pub}',             '{lf_obs_pub}',             true,  5.0, 3),
    ('TEST-LF-DUAL',  'zzz_lf_field', 150.01, 2.00, '{zzz_lf_pub}',             '{lf_obs_pub}',             true,  5.0, 3),
    ('TEST-LF-STALE', 'zzz_lf_field', 150.02, 2.00, '{zzz_lf_pub}',             '{lf_obs_pub}',             true,  5.0, 3),
    ('TEST-LF-DRAFT', 'zzz_lf_field', 150.03, 2.00, '{zzz_lf_pub}',             '{lf_obs_pub}',             false, 5.0, 3),
    ('TEST-LF-PROP',  'zzz_lf_field', 150.04, 2.00, '{zzz_lf_prop}',            '{lf_obs_prop}',            true,  5.0, 3),
    ('TEST-LF-MIXED', 'zzz_lf_field', 150.05, 2.00, '{zzz_lf_pub,zzz_lf_prop}', '{lf_obs_pub,lf_obs_prop}', true,  5.0, 3);

  SELECT id INTO v_pub   FROM objects WHERE object_id = 'TEST-LF-PUB';
  SELECT id INTO v_dual  FROM objects WHERE object_id = 'TEST-LF-DUAL';
  SELECT id INTO v_stale FROM objects WHERE object_id = 'TEST-LF-STALE';
  SELECT id INTO v_draft FROM objects WHERE object_id = 'TEST-LF-DRAFT';
  SELECT id INTO v_prop  FROM objects WHERE object_id = 'TEST-LF-PROP';
  SELECT id INTO v_mixed FROM objects WHERE object_id = 'TEST-LF-MIXED';

  INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, object_id) VALUES
    ('test-lf-pub',     'zzz_lf_field', 150.00, 2.00, 'zzz_lf_pub',  'lf_obs_pub',  v_pub),
    ('test-lf-dual',    'zzz_lf_field', 150.01, 2.00, 'zzz_lf_pub',  'lf_obs_pub',  v_dual),
    ('test-lf-stale',   'zzz_lf_field', 150.02, 2.00, 'zzz_lf_pub',  'lf_obs_pub',  v_stale),
    ('test-lf-draft',   'zzz_lf_field', 150.03, 2.00, 'zzz_lf_pub',  'lf_obs_pub',  v_draft),
    ('test-lf-prop',    'zzz_lf_field', 150.04, 2.00, 'zzz_lf_prop', 'lf_obs_prop', v_prop),
    ('test-lf-mixed-a', 'zzz_lf_field', 150.05, 2.00, 'zzz_lf_pub',  'lf_obs_pub',  v_mixed),
    ('test-lf-mixed-b', 'zzz_lf_field', 150.05, 2.00, 'zzz_lf_prop', 'lf_obs_prop', v_mixed);
  INSERT INTO spectra (target_id, grating, fits_path, signal_to_noise, exposure_time, deploy_status) VALUES
    ('test-lf-pub',     'PRISM', '/tmp/lf_pub_prism.fits',     10.0, 1000.0, 'published'),
    ('test-lf-dual',    'PRISM', '/tmp/lf_dual_prism.fits',    10.0, 1000.0, 'published'),
    ('test-lf-dual',    'G395M', '/tmp/lf_dual_g395m.fits',    10.0, 1000.0, 'published'),
    ('test-lf-stale',   'PRISM', '/tmp/lf_stale_prism.fits',   10.0, 1000.0, 'published'),
    ('test-lf-draft',   'PRISM', '/tmp/lf_draft_prism.fits',   10.0, 1000.0, 'draft'),
    ('test-lf-prop',    'PRISM', '/tmp/lf_prop_prism.fits',    10.0, 1000.0, 'published'),
    ('test-lf-mixed-a', 'PRISM', '/tmp/lf_mixed_a_prism.fits', 10.0, 1000.0, 'published'),
    ('test-lf-mixed-b', 'PRISM', '/tmp/lf_mixed_b_prism.fits', 10.0, 1000.0, 'published');

  -- One fit per spectrum, written as the deploy writes it (line_fits_upsert:
  -- the fit row plus its lines rows).
  -- z_used / z_quality / object_version match the object (not stale) except
  -- for the stale fixture, which was measured at z = 4.
  FOR r IN
    SELECT s.id AS spectrum_id, s.target_id, s.grating, t.program_slug, t.observation,
           o.id AS oid, o.object_id, o.version, o.redshift_quality,
           c.snr, c.z_used
    FROM (VALUES
      ('/tmp/lf_pub_prism.fits',     10.0, 5.0),
      ('/tmp/lf_dual_prism.fits',     4.0, 5.0),
      ('/tmp/lf_dual_g395m.fits',     8.0, 5.0),
      ('/tmp/lf_stale_prism.fits',   20.0, 4.0),
      ('/tmp/lf_draft_prism.fits',   50.0, 5.0),
      ('/tmp/lf_prop_prism.fits',    30.0, 5.0),
      ('/tmp/lf_mixed_a_prism.fits',  2.0, 5.0),
      ('/tmp/lf_mixed_b_prism.fits', 12.0, 5.0)
    ) AS c(fits_path, snr, z_used)
    JOIN spectra s ON s.fits_path = c.fits_path
    JOIN targets t ON t.target_id = s.target_id
    JOIN objects o ON o.id = t.object_id
  LOOP
    PERFORM public.line_fits_upsert(jsonb_build_array(jsonb_build_object(
      'spectrum_id', r.spectrum_id, 'target_id', r.target_id, 'grating', r.grating,
      'program_slug', r.program_slug, 'observation', r.observation,
      'z_used', r.z_used, 'z_source', 'inspected', 'z_quality', r.redshift_quality,
      'object_id', r.object_id, 'object_version', r.version, 'n_lines', 1, 'n_detected', 1,
      'lines', jsonb_build_object('CIII1908', jsonb_build_object(
        'component', 'doublet', 'wave_rest', 1907.7,
        'flux', r.snr * 1e-19, 'flux_err', 1e-19, 'snr', r.snr)),
      'fit_version', 'test')));
  END LOOP;

  IF (SELECT count(*) FROM lines l JOIN spectra s ON s.id = l.spectrum_id
      WHERE s.fits_path LIKE '/tmp/lf\_%' AND l.line = 'CIII1908') <> 8 THEN
    RAISE EXCEPTION 'fixture: trigger did not unnest 8 line rows';
  END IF;

  -- 1) Helper semantics as the service role (role claim 'service_role': the
  --    explicit arguments are the only gate, exactly the /api/v1 bearer-route
  --    path, which authorizes the user in the web layer) ----------------------
  PERFORM set_config('request.jwt.claims', json_build_object('role', 'service_role')::text, true);
  v_ids := ARRAY(SELECT v.object_id FROM public.object_line_values('CIII1908', NULL, NULL, false, c_pub) v ORDER BY 1);
  IF v_ids <> ARRAY(SELECT x FROM unnest(ARRAY[v_pub, v_dual, v_mixed]) x ORDER BY 1) THEN
    RAISE EXCEPTION 'helper: public slugs, published, non-stale: want {pub, dual, mixed}, got %', v_ids;
  END IF;
  -- ...and the mixed object's S/N is its PUBLIC spectrum's: the proprietary
  -- detection does not count towards an object seen through another program.
  SELECT v.snr INTO v_snr FROM public.object_line_values('CIII1908', NULL, NULL, false, c_pub) v WHERE v.object_id = v_mixed;
  IF abs(v_snr - 2.0) > 1e-9 THEN
    RAISE EXCEPTION 'helper: mixed object under public slugs must report its public spectrum (2), got %', v_snr;
  END IF;
  -- Staleness: the stale fit is admitted only on request.
  v_ids := ARRAY(SELECT v.object_id FROM public.object_line_values('CIII1908', NULL, NULL, true, c_pub) v);
  IF NOT (v_stale = ANY(v_ids)) OR array_length(v_ids, 1) <> 4 THEN
    RAISE EXCEPTION 'helper: include_stale should add exactly the stale object: %', v_ids;
  END IF;
  -- An API user authorized for the private program gets it (the service-role
  -- client carries no auth.uid(), so the explicit slugs must decide).
  v_ids := ARRAY(SELECT v.object_id FROM public.object_line_values('CIII1908', NULL, NULL, false, c_both) v);
  IF NOT (v_prop = ANY(v_ids)) OR v_draft = ANY(v_ids) OR array_length(v_ids, 1) <> 4 THEN
    RAISE EXCEPTION 'helper: both slugs, published only: want {pub, dual, prop, mixed}: %', v_ids;
  END IF;
  SELECT v.snr INTO v_snr FROM public.object_line_values('CIII1908', NULL, NULL, false, c_both) v WHERE v.object_id = v_mixed;
  IF abs(v_snr - 12.0) > 1e-9 THEN
    RAISE EXCEPTION 'helper: mixed object under both slugs must report its best spectrum (12), got %', v_snr;
  END IF;
  v_ids := ARRAY(SELECT v.object_id FROM public.object_line_values('CIII1908', NULL, NULL, true, c_both, true) v);
  IF NOT (v_draft = ANY(v_ids)) OR array_length(v_ids, 1) <> 6 THEN
    RAISE EXCEPTION 'helper: both slugs + include_unpublished + include_stale: want all 6 fixtures: %', v_ids;
  END IF;

  -- Per-object aggregate across two spectra: max(snr).
  SELECT v.snr INTO v_snr FROM public.object_line_values('CIII1908', NULL, NULL, false, c_pub) v WHERE v.object_id = v_dual;
  IF abs(v_snr - 8.0) > 1e-9 THEN
    RAISE EXCEPTION 'helper: two-spectrum object must carry its best S/N (8), got %', v_snr;
  END IF;

  -- Windows are tested on that aggregate.
  v_ids := ARRAY(SELECT v.object_id FROM public.object_line_values('CIII1908', 5, NULL, false, c_pub) v ORDER BY 1);
  IF v_ids <> ARRAY[LEAST(v_pub, v_dual), GREATEST(v_pub, v_dual)] THEN
    RAISE EXCEPTION 'helper: snr >= 5 should be exactly {pub, dual} (dual via its BEST spectrum): %', v_ids;
  END IF;
  v_ids := ARRAY(SELECT v.object_id FROM public.object_line_values('CIII1908', NULL, 5, false, c_pub) v);
  IF v_ids <> ARRAY[v_mixed] THEN
    RAISE EXCEPTION 'helper: snr <= 5 should be exactly {mixed} (dual is 8 at best): %', v_ids;
  END IF;
  v_ids := ARRAY(SELECT v.object_id FROM public.object_line_values('CIII1908', 3, 9, false, c_pub) v);
  IF v_ids <> ARRAY[v_dual] THEN
    RAISE EXCEPTION 'helper: 3 <= snr <= 9 should be exactly {dual}: %', v_ids;
  END IF;

  -- The id projection and the single-object lookup agree; NULL / unknown
  -- line yield nothing.
  IF ARRAY(SELECT public.objects_matching_line_filter('CIII1908', 5, NULL, false, c_pub) ORDER BY 1)
     <> ARRAY(SELECT v.object_id FROM public.object_line_values('CIII1908', 5, NULL, false, c_pub) v ORDER BY 1) THEN
    RAISE EXCEPTION 'objects_matching_line_filter disagrees with object_line_values';
  END IF;
  IF abs(public.object_line_snr(v_pub, 'CIII1908', false, c_pub) - 10.0) > 1e-9
     OR public.object_line_snr(v_stale, 'CIII1908', false, c_pub) IS NOT NULL
     OR abs(public.object_line_snr(v_stale, 'CIII1908', true, c_pub) - 20.0) > 1e-9 THEN
    RAISE EXCEPTION 'object_line_snr disagrees with object_line_values (pub=%, stale=%/%)',
      public.object_line_snr(v_pub, 'CIII1908', false, c_pub),
      public.object_line_snr(v_stale, 'CIII1908', false, c_pub),
      public.object_line_snr(v_stale, 'CIII1908', true, c_pub);
  END IF;
  IF EXISTS (SELECT 1 FROM public.object_line_values(NULL, NULL, NULL, false, c_both, true))
     OR EXISTS (SELECT 1 FROM public.object_line_values('zzz_no_such_line', NULL, NULL, false, c_both, true)) THEN
    RAISE EXCEPTION 'helper: NULL or unknown line must yield no rows';
  END IF;

  -- 2) Public-only authenticated viewer, through RLS ---------------------------
  -- zzz_lf_pub is public, so it is accessible with no subject claim; the role
  -- claim is what tells the helper this is NOT the service role.
  PERFORM set_config('request.jwt.claims', json_build_object('role', 'authenticated')::text, true);
  PERFORM set_config('role', 'authenticated', true);

  -- Direct call of the SECURITY DEFINER helper must not widen the viewer's
  -- set, even with slugs the viewer does not hold and the unpublished opt-in:
  -- no proprietary or draft object, and the mixed object at its PUBLIC S/N.
  v_ids := ARRAY(SELECT v.object_id FROM public.object_line_values('CIII1908', NULL, NULL, false, c_both, true) v ORDER BY 1);
  IF v_ids <> ARRAY(SELECT x FROM unnest(ARRAY[v_pub, v_dual, v_mixed]) x ORDER BY 1) THEN
    RAISE EXCEPTION 'authenticated: direct helper call with unheld slugs leaked or dropped rows: %', v_ids;
  END IF;
  SELECT v.snr INTO v_snr FROM public.object_line_values('CIII1908', NULL, NULL, false, c_both, true) v WHERE v.object_id = v_mixed;
  IF abs(v_snr - 2.0) > 1e-9 THEN
    RAISE EXCEPTION 'authenticated: a proprietary detection leaked into the mixed object''s S/N: %', v_snr;
  END IF;

  -- Objects list: window + sort on the line; displayed values are the tested ones.
  SELECT targets, total_count INTO v_json, v_count
    FROM public.get_filtered_objects_paginated(
      p_program_slugs  => c_pub,
      p_fields         => ARRAY['zzz_lf_field'],
      p_line           => 'CIII1908',
      p_line_snr_min   => 5,
      p_sort_column    => 'line_snr',
      p_sort_direction => 'desc'
    );
  IF v_count <> 2 OR jsonb_array_length(v_json) <> 2 THEN
    RAISE EXCEPTION 'paginated objects snr >= 5: want 2 rows, got count=% rows=%', v_count, v_json;
  END IF;
  IF v_json -> 0 ->> 'object_id' <> 'TEST-LF-PUB' OR v_json -> 1 ->> 'object_id' <> 'TEST-LF-DUAL' THEN
    RAISE EXCEPTION 'paginated objects sorted by line_snr desc: wrong order: %', v_json;
  END IF;
  IF EXISTS (SELECT 1 FROM jsonb_array_elements(v_json) e(row) WHERE (e.row ->> 'line_snr')::float8 < 5) THEN
    RAISE EXCEPTION 'paginated objects: a row displays a Line S/N outside the window that admitted it: %', v_json;
  END IF;
  SELECT e.row INTO v_row FROM jsonb_array_elements(v_json) e(row) WHERE e.row ->> 'object_id' = 'TEST-LF-DUAL';
  IF abs((v_row ->> 'line_snr')::float8 - 8.0) > 1e-9 THEN
    RAISE EXCEPTION 'paginated objects: two-spectrum row displays the wrong aggregate: %', v_row;
  END IF;

  -- No window: every visible non-stale detection, mixed at its public S/N,
  -- sorted ascending so it comes first.
  SELECT targets, total_count INTO v_json, v_count
    FROM public.get_filtered_objects_paginated(
      p_program_slugs  => c_pub,
      p_fields         => ARRAY['zzz_lf_field'],
      p_line           => 'CIII1908',
      p_sort_column    => 'line_snr',
      p_sort_direction => 'asc'
    );
  IF v_count <> 3 OR v_json -> 0 ->> 'object_id' <> 'TEST-LF-MIXED'
     OR abs((v_json -> 0 ->> 'line_snr')::float8 - 2.0) > 1e-9 THEN
    RAISE EXCEPTION 'paginated objects unwindowed: want 3 rows with mixed first at S/N 2: count=% rows=%', v_count, v_json;
  END IF;

  -- get_filtered_object_ids agrees (it sorts through the same join).
  v_text := ARRAY(SELECT object_id FROM public.get_filtered_object_ids(
      p_program_slugs  => c_pub,
      p_fields         => ARRAY['zzz_lf_field'],
      p_line           => 'CIII1908',
      p_line_snr_min   => 5,
      p_sort_column    => 'line_snr',
      p_sort_direction => 'desc'));
  IF v_text <> ARRAY['TEST-LF-PUB', 'TEST-LF-DUAL'] THEN
    RAISE EXCEPTION 'get_filtered_object_ids disagrees with the paginated RPC: %', v_text;
  END IF;

  -- Spectra list: per-spectrum S/N, the public spectra only.
  SELECT targets, total_count INTO v_json, v_count
    FROM public.get_filtered_spectra_paginated(
      p_program_slugs => c_pub,
      p_fields        => ARRAY['zzz_lf_field'],
      p_line          => 'CIII1908',
      p_line_snr_min  => 5
    );
  IF v_count <> 2 OR EXISTS (SELECT 1 FROM jsonb_array_elements(v_json) e(row)
                             WHERE e.row ->> 'target_id' NOT IN ('test-lf-pub', 'test-lf-dual')) THEN
    RAISE EXCEPTION 'paginated spectra snr >= 5: want the pub and dual/G395M spectra: count=% rows=%', v_count, v_json;
  END IF;

  -- 3) Admin: every program, drafts included ----------------------------------
  PERFORM set_config('request.jwt.claims',
    json_build_object('sub', '00000000-0000-0000-0000-0000000000c1', 'role', 'authenticated')::text, true);
  v_ids := ARRAY(SELECT v.object_id FROM public.object_line_values('CIII1908', NULL, NULL, false, c_both, true) v);
  IF NOT (v_prop = ANY(v_ids) AND v_draft = ANY(v_ids) AND v_pub = ANY(v_ids)) OR v_stale = ANY(v_ids)
     OR array_length(v_ids, 1) <> 5 THEN
    RAISE EXCEPTION 'admin: helper should see pub, dual, draft, prop, mixed (never the stale fit unasked): %', v_ids;
  END IF;
  SELECT total_count INTO v_count
    FROM public.get_filtered_objects_paginated(
      p_program_slugs       => c_both,
      p_fields              => ARRAY['zzz_lf_field'],
      p_line                => 'CIII1908',
      p_include_unpublished => true
    );
  IF v_count <> 5 THEN
    RAISE EXCEPTION 'admin: paginated objects with line filter should see 5 fixtures, got %', v_count;
  END IF;

  -- 4) Ordinary viewer with a grant on the private program --------------------
  PERFORM set_config('request.jwt.claims',
    json_build_object('sub', '00000000-0000-0000-0000-0000000000c2', 'role', 'authenticated')::text, true);
  v_ids := ARRAY(SELECT v.object_id FROM public.object_line_values('CIII1908', NULL, NULL, false, c_both, true) v);
  IF NOT (v_prop = ANY(v_ids)) OR v_draft = ANY(v_ids) OR array_length(v_ids, 1) <> 4 THEN
    RAISE EXCEPTION 'granted viewer: helper should include the proprietary object and never the draft one: %', v_ids;
  END IF;
  SELECT v.snr INTO v_snr FROM public.object_line_values('CIII1908', NULL, NULL, false, c_both) v WHERE v.object_id = v_mixed;
  IF abs(v_snr - 12.0) > 1e-9 THEN
    RAISE EXCEPTION 'granted viewer: mixed object should report its proprietary spectrum (12), got %', v_snr;
  END IF;
  SELECT total_count INTO v_count
    FROM public.get_filtered_objects_paginated(
      p_program_slugs => c_both,
      p_fields        => ARRAY['zzz_lf_field'],
      p_line          => 'CIII1908'
    );
  IF v_count <> 4 THEN
    RAISE EXCEPTION 'granted viewer: paginated objects with line filter should see 4 (pub, dual, prop, mixed), got %', v_count;
  END IF;

  PERFORM set_config('request.jwt.claims', '', true);
  PERFORM set_config('role', 'none', true);

  RAISE NOTICE 'OK: line filter scoping holds (helper + objects / object_ids / spectra RPCs; service-role, public, admin and granted callers; staleness; per-object max).';
END $$;

ROLLBACK;
