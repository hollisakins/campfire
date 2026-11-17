-- Guard: spectra.filter (#683). The NIRSpec filter that pairs with the grating
-- (G140M takes F070LP or F100LP), so a spectrum's disperser setup is
-- (grating, filter).
--
--   1. spectrum_filter_from_path parses {obs}_{grating}_{filter}_{source}_spec.fits,
--      skipping an observation name that holds a grating token.
--   2. set_spectrum_filter fills a NULL filter from fits_path on insert and on a
--      fits_path update, re-derives it when fits_path moves and the filter
--      does not, and never overrides a filter the writer changed; a
--      non-admin cannot change it (enforce_spectra_dq_user_update_scope).
--   2b. (target_id, grating, filter) is the unique key: two filters of one
--      grating coexist, a repeated setup is refused or upserted in place, and
--      a G140M/H write that names no filter fails NOT NULL (the other
--      gratings take their only filter).
--   3. The predicate tree takes `filter` on spectra, and through an exists at
--      the objects root.
--   4. The freeze copies it into dr_spectra.
--
-- Run locally:
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_spectra_filter.sql
BEGIN;

DO $$
DECLARE
  v_target  TEXT;
  v_id      INTEGER;
  v_filter  TEXT;
  v_n       BIGINT;
  v_tree    BIGINT;
BEGIN
  -- 1. parsing ---------------------------------------------------------------
  IF public.spectrum_filter_from_path('nirspec/obs/ember_uds_p4_prism_clear_12345_spec.fits') IS DISTINCT FROM 'CLEAR'
     OR public.spectrum_filter_from_path('obs/capers_egs_p1_g140m_f070lp_9_spec.fits') IS DISTINCT FROM 'F070LP'
     OR public.spectrum_filter_from_path('obs/my_prism_p1_g140m_f100lp_9_spec.fits') IS DISTINCT FROM 'F100LP'
     OR public.spectrum_filter_from_path('obs/G395H_F290LP_SRC_spec.fits') IS DISTINCT FROM 'F290LP'
     OR public.spectrum_filter_from_path('obs/not_a_spectrum.fits') IS NOT NULL
     OR public.spectrum_filter_from_path(NULL) IS NOT NULL THEN
    RAISE EXCEPTION 'spectrum_filter_from_path parsed a name wrongly';
  END IF;

  -- 2. the trigger -----------------------------------------------------------
  -- a seed target without a G140H spectrum, so its G140H keys are free
  SELECT t.target_id INTO v_target
  FROM targets t
  WHERE NOT EXISTS (SELECT 1 FROM spectra s WHERE s.target_id = t.target_id AND s.grating = 'G140H')
  ORDER BY t.target_id LIMIT 1;
  IF v_target IS NULL THEN RAISE EXCEPTION 'fixture: no seed target without G140H'; END IF;

  INSERT INTO spectra (target_id, grating, fits_path, deploy_status)
  VALUES (v_target, 'G140H', 'nirspec/zz/zz_obs_g140h_f070lp_1_spec.fits', 'draft')
  RETURNING id, filter INTO v_id, v_filter;
  IF v_filter IS DISTINCT FROM 'F070LP' THEN
    RAISE EXCEPTION 'insert without filter: expected F070LP from fits_path, got %', v_filter;
  END IF;

  UPDATE spectra SET fits_path = 'nirspec/zz/zz_obs_g140h_f100lp_1_spec.fits', filter = NULL
  WHERE id = v_id RETURNING filter INTO v_filter;
  IF v_filter IS DISTINCT FROM 'F100LP' THEN
    RAISE EXCEPTION 'fits_path update with a NULL filter: expected F100LP, got %', v_filter;
  END IF;

  UPDATE spectra SET filter = 'F070LP' WHERE id = v_id RETURNING filter INTO v_filter;
  IF v_filter IS DISTINCT FROM 'F070LP' THEN
    RAISE EXCEPTION 'an explicit filter was overridden: got %', v_filter;
  END IF;

  -- a fits_path move that leaves the filter alone re-derives it (a redeploy
  -- from a summary without the column)
  UPDATE spectra SET fits_path = 'nirspec/zz/zz_obs_g140h_f100lp_2_spec.fits'
  WHERE id = v_id RETURNING filter INTO v_filter;
  IF v_filter IS DISTINCT FROM 'F100LP' THEN
    RAISE EXCEPTION 'fits_path move with the old filter: expected F100LP, got %', v_filter;
  END IF;

  -- a path and a new filter in one write: the writer's filter stands
  UPDATE spectra SET fits_path = 'nirspec/zz/zz_obs_g140h_f070lp_3_spec.fits', filter = 'F170LP'
  WHERE id = v_id RETURNING filter INTO v_filter;
  IF v_filter IS DISTINCT FROM 'F170LP' THEN
    RAISE EXCEPTION 'a filter changed with the path was overridden: got %', v_filter;
  END IF;

  -- a non-admin (inspector) may change dq_flags only, never the filter
  PERFORM set_config('request.jwt.claims', json_build_object(
    'sub', (SELECT id FROM auth.users WHERE email = 'user@campfire.dev'),
    'role', 'authenticated')::text, true);
  IF public.is_admin() THEN RAISE EXCEPTION 'fixture: user@campfire.dev is an admin'; END IF;
  BEGIN
    UPDATE spectra SET filter = 'F290LP' WHERE id = v_id;
    RAISE EXCEPTION 'a non-admin changed spectra.filter';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;
  END;
  PERFORM set_config('request.jwt.claims', '', true);

  -- 2b. the key (#683) ---------------------------------------------------------
  -- the row above is G140H/F170LP now; one grating through two filters is two
  -- spectra
  INSERT INTO spectra (target_id, grating, fits_path, deploy_status)
  VALUES (v_target, 'G140H', 'nirspec/zz/zz_obs_g140h_f100lp_4_spec.fits', 'draft');
  INSERT INTO spectra (target_id, grating, fits_path, deploy_status)
  VALUES (v_target, 'G140H', 'nirspec/zz/zz_obs_g140h_f070lp_4_spec.fits', 'draft');
  SELECT count(*) INTO v_n FROM spectra WHERE target_id = v_target AND grating = 'G140H';
  IF v_n <> 3 THEN
    RAISE EXCEPTION 'one target, G140H through three filters: expected 3 rows, got %', v_n;
  END IF;

  -- the same setup twice is refused, and the deploy's upsert updates in place
  BEGIN
    INSERT INTO spectra (target_id, grating, fits_path, deploy_status)
    VALUES (v_target, 'G140H', 'nirspec/zz/zz_other_g140h_f100lp_4_spec.fits', 'draft');
    RAISE EXCEPTION 'a second G140H/F100LP row for one target was accepted';
  EXCEPTION WHEN unique_violation THEN
    NULL;
  END;
  INSERT INTO spectra (target_id, grating, filter, fits_path, deploy_status)
  VALUES (v_target, 'G140H', 'F100LP', 'nirspec/zz/zz_obs_g140h_f100lp_4_spec.fits', 'draft')
  ON CONFLICT (target_id, grating, filter) DO UPDATE SET exposure_time = 123;
  SELECT count(*) INTO v_n FROM spectra
  WHERE target_id = v_target AND grating = 'G140H' AND filter = 'F100LP' AND exposure_time = 123;
  IF v_n <> 1 THEN
    RAISE EXCEPTION 'upsert on (target_id, grating, filter) did not update the row in place';
  END IF;

  -- a path naming no filter, sent without one, never lands outside the key;
  -- the trigger fills a NULL filter before the arbiter check, so a write
  -- without the column still upserts on the right row
  -- (a grating with one possible filter takes it: PRISM is always CLEAR)
  INSERT INTO spectra (target_id, grating, fits_path, deploy_status)
  VALUES (v_target, 'PRISM', 'nirspec/zz/not_a_spectrum_prism.fits', 'draft')
  ON CONFLICT (target_id, grating, filter) DO NOTHING;
  SELECT filter INTO v_filter FROM spectra WHERE fits_path = 'nirspec/zz/not_a_spectrum_prism.fits';
  IF v_filter IS NOT NULL AND v_filter <> 'CLEAR' THEN
    RAISE EXCEPTION 'PRISM with an unparseable path: expected CLEAR, got %', v_filter;
  END IF;
  BEGIN
    INSERT INTO spectra (target_id, grating, fits_path, deploy_status)
    VALUES (v_target, 'G140M', 'nirspec/zz/not_a_spectrum.fits', 'draft');
    RAISE EXCEPTION 'a G140M spectrum without a filter was accepted';
  EXCEPTION WHEN not_null_violation THEN
    NULL;
  END;
  INSERT INTO spectra (target_id, grating, fits_path, deploy_status)
  VALUES (v_target, 'G140H', 'nirspec/zz/zz_obs_g140h_f070lp_4_spec.fits', 'draft')
  ON CONFLICT (target_id, grating, filter) DO UPDATE SET exposure_time = 456;
  SELECT count(*) INTO v_n FROM spectra
  WHERE target_id = v_target AND grating = 'G140H' AND filter = 'F070LP' AND exposure_time = 456;
  IF v_n <> 1 THEN
    RAISE EXCEPTION 'an upsert without the filter column missed its row';
  END IF;

  -- 3. the predicate tree ----------------------------------------------------
  SELECT count(*) INTO v_n FROM spectra WHERE filter = 'F070LP';
  SELECT count(*) INTO v_tree
  FROM unnest(public.catalog_match_ids('spectra', '{"col": "filter", "op": "eq", "v": "F070LP"}')) i(id);
  IF v_tree <> v_n OR v_n = 0 THEN
    RAISE EXCEPTION 'tree filter eq F070LP: % ids, % rows', v_tree, v_n;
  END IF;

  SELECT count(DISTINCT t.object_id) INTO v_n
  FROM spectra s JOIN targets t ON t.target_id = s.target_id
  JOIN objects o ON o.id = t.object_id
  WHERE s.filter = 'F070LP' AND o.is_active;
  SELECT count(*) INTO v_tree
  FROM unnest(public.catalog_match_ids('objects',
         '{"exists": "spectra", "where": [{"col": "filter", "op": "in", "v": ["F070LP"]}]}')) i(id)
  JOIN objects o ON o.id = i.id WHERE o.is_active;
  IF v_tree <> v_n THEN
    RAISE EXCEPTION 'objects with an F070LP spectrum: tree % vs %', v_tree, v_n;
  END IF;
END
$$;

-- 4. the freeze copies the column (a frozen seed release, when there is one)
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM dr_spectra d JOIN spectra s ON s.id = d.id
    WHERE d.filter IS DISTINCT FROM s.filter
      AND d.fits_path = s.fits_path
  ) THEN
    RAISE EXCEPTION 'dr_spectra.filter differs from the live row it froze';
  END IF;
END
$$;

ROLLBACK;
