-- Catalog Read Model phase 3: the predicate tree.
--   1. the compiler takes identifiers only from the whitelist and values only
--      through literals: unknown tables / columns / operators, a type
--      mismatch, an over-deep or over-long tree, a bad root, a bad version
--      and a cone with a silly radius all raise 22023; the emitted text is
--      the documented shape;
--   2. semantics on fixtures: AND / OR / NOT EXISTS, two lines at once (#607),
--      a nested exists (lines whose fit is fresh), arrays, bits, a cone;
--   3. parity: for each legacy filter the portal has today, the tree that
--      expresses it selects exactly the ids the legacy RPC returns, as a
--      non-admin viewer (so under RLS);
--   4. RLS: a viewer's ids are a subset of an admin's, and a private
--      program's objects never reach the viewer;
--   5. browsing a release: the compiled text reads cat_* views and the ids
--      come from the frozen rows of that tag;
--   6. catalog_query_objects returns the legacy RPC's row shape and the same
--      page, an empty selection returns an empty page without erroring, and
--      the display line / band never narrows the tree's selection (Codex on
--      #659);
--   7. the service role with a viewer's explicit scope selects exactly what
--      the viewer selects under RLS, through every node kind, lists included
--      (Codex on #659: the bearer routes run on the service-role client).
-- Run: psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_catalog_query.sql
\set ON_ERROR_STOP on
BEGIN;

-- ------------------------------------------------------------------ 1
DO $$
DECLARE
  bad jsonb[] := ARRAY[
    '{"exists": "pg_shadow"}',
    '{"col": "passwd", "op": "eq", "v": 1}',
    '{"col": "redshift; drop table objects", "op": "eq", "v": 1}',
    '{"col": "redshift", "op": "ilike", "v": "x"}',
    '{"col": "redshift", "op": "gte", "v": "4.9"}',
    '{"col": "field", "op": "eq", "v": 1}',
    '{"col": "redshift", "op": "between", "v": [null, null]}',
    '{"col": "programs", "op": "overlaps", "v": []}',
    '{"and": []}',
    '{"cone": {"ra": 150, "dec": 2, "radius": 30}}',
    '{"exists": "lines", "where": [{"cone": {"ra": 150, "dec": 2, "radius": 0.1}}]}',
    '{"v": 2, "and": [{"col": "redshift", "op": "gte", "v": 1}]}',
    '{"frob": 1}',
    '[1, 2]'
  ]::jsonb[];
  t jsonb;
  got text;
  deep jsonb := '{"col": "redshift", "op": "gte", "v": 1}';
  i int;
BEGIN
  FOREACH t IN ARRAY bad LOOP
    BEGIN
      got := public.catalog_tree_sql(t, 'objects');
      RAISE EXCEPTION 'accepted %: %', t, got;
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
  END LOOP;
  -- root must be a catalog root
  BEGIN
    got := public.catalog_tree_sql('{"col": "snr", "op": "gte", "v": 1}', 'lines');
    RAISE EXCEPTION 'accepted lines as a root';
  EXCEPTION WHEN invalid_parameter_value THEN NULL;
  END;
  -- depth
  FOR i IN 1..8 LOOP deep := jsonb_build_object('and', jsonb_build_array(deep)); END LOOP;
  BEGIN
    got := public.catalog_tree_sql(deep, 'objects');
    RAISE EXCEPTION 'accepted a tree nested 9 deep';
  EXCEPTION WHEN invalid_parameter_value THEN NULL;
  END;
  -- the emitted shape
  got := public.catalog_tree_sql('{"v": 1, "and": [{"col": "objects.redshift", "op": "gte", "v": 4.9}, {"exists": "lines", "where": [{"col": "line", "op": "eq", "v": "CIII1908"}, {"col": "snr", "op": "gte", "v": 5}]}]}', 'objects');
  IF got <> '(r.redshift >= (4.9)::double precision AND EXISTS (SELECT 1 FROM public.lines x1_2 WHERE x1_2.object_id = r.id AND (x1_2.line = ''CIII1908'' AND x1_2.snr >= (5)::double precision)))' THEN
    RAISE EXCEPTION 'unexpected SQL: %', got;
  END IF;
  -- a value with a quote is a literal, never SQL
  got := public.catalog_tree_sql('{"col": "field", "op": "eq", "v": "x'' OR 1=1 --"}', 'objects');
  IF got <> 'r.field = ''x'''' OR 1=1 --''' THEN RAISE EXCEPTION 'quoting: %', got; END IF;
  RAISE NOTICE 'OK 1: whitelist, limits and quoting';
END $$;

-- ------------------------------------------------------------------ fixtures
INSERT INTO programs (slug, program_name, is_public) VALUES ('zzz_cq_pub', 'CQ public', true), ('zzz_cq_priv', 'CQ private', false);
INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES ('cq_obs_pub', 'zzz_cq_pub', 9984, 'zzz_cq_field'), ('cq_obs_priv', 'zzz_cq_priv', 9983, 'zzz_cq_field');
INSERT INTO objects (object_id, field, ra, dec, programs, observations, has_published_spectrum, redshift_inspected, redshift_quality, gratings)
  VALUES ('CQ-A', 'zzz_cq_field', 158.0, 2.0, '{zzz_cq_pub}', '{cq_obs_pub}', true, 5.0, 4, '{PRISM}'),
         ('CQ-B', 'zzz_cq_field', 158.1, 2.0, '{zzz_cq_pub}', '{cq_obs_pub}', true, 5.5, 3, '{PRISM,G395M}'),
         ('CQ-C', 'zzz_cq_field', 158.2, 2.0, '{zzz_cq_pub}', '{cq_obs_pub}', true, 2.0, 2, '{PRISM}'),
         ('CQ-P', 'zzz_cq_field', 158.3, 2.0, '{zzz_cq_priv}', '{cq_obs_priv}', true, 6.0, 4, '{PRISM}'),
         -- just west of RA = 0: a cone centred at 0.02 deg must reach it (Codex on #659)
         ('CQW-1', 'zzz_cq_wrap', 359.95, 2.0, '{zzz_cq_pub}', '{cq_obs_pub}', true, 1.0, 1, '{PRISM}'),
         -- 9.94 deg from (0, 60): inside a 10 deg cone, outside its 1/cos(dec) RA box (Codex on #659)
         ('CQW-2', 'zzz_cq_wrap', 20.2, 61.5, '{zzz_cq_pub}', '{cq_obs_pub}', true, 1.0, 1, '{PRISM}');
INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, object_id)
  SELECT lower(object_id), field, ra, dec, programs[1], observations[1], id FROM objects WHERE object_id LIKE 'CQ-%';
INSERT INTO spectra (target_id, grating, fits_path, signal_to_noise, exposure_time, deploy_status)
  SELECT lower(object_id), 'PRISM', '/tmp/cq_' || lower(object_id) || '.fits', 10.0, 1000.0, 'published' FROM objects WHERE object_id LIKE 'CQ-%';
INSERT INTO spectra (target_id, grating, fits_path, signal_to_noise, exposure_time, deploy_status)
  VALUES ('cq-b', 'G395M', '/tmp/cq_b_g395m.fits', 20.0, 2000.0, 'published');
-- fits: A has CIII1908 (S/N 10) and OIII5007 (S/N 8); B has CIII1908 (S/N 3)
-- and OIII5007 (S/N 12); C has OIII5007 only (S/N 20); P (private) has both.
-- A's fit is at the object's current redshift (fresh); B's was measured at an
-- older version (stale).
DO $$
DECLARE r record; v_ver int;
BEGIN
  FOR r IN SELECT s.id AS sid, s.target_id, o.object_id, o.version, o.redshift_quality, o.redshift
             FROM spectra s JOIN targets t ON t.target_id = s.target_id JOIN objects o ON o.id = t.object_id
            WHERE s.fits_path LIKE '/tmp/cq_%' AND s.grating = 'PRISM' LOOP
    v_ver := CASE WHEN r.object_id = 'CQ-B' THEN r.version - 1 ELSE r.version END;
    PERFORM public.line_fits_upsert(jsonb_build_array(jsonb_build_object(
      'spectrum_id', r.sid, 'target_id', r.target_id, 'grating', 'PRISM', 'program_slug', CASE WHEN r.object_id = 'CQ-P' THEN 'zzz_cq_priv' ELSE 'zzz_cq_pub' END,
      'observation', CASE WHEN r.object_id = 'CQ-P' THEN 'cq_obs_priv' ELSE 'cq_obs_pub' END,
      'z_used', r.redshift, 'z_source', 'inspected', 'z_quality', r.redshift_quality, 'object_id', r.object_id,
      'object_version', v_ver, 'n_lines', 2, 'n_detected', 2, 'fit_version', 'test',
      'lines', CASE r.object_id
        WHEN 'CQ-A' THEN jsonb_build_object('CIII1908', jsonb_build_object('component', 'doublet', 'snr', 10.0, 'flux', 1e-18, 'flux_err', 1e-19), 'OIII5007', jsonb_build_object('component', 'narrow', 'snr', 8.0, 'flux', 1e-18, 'flux_err', 1e-19))
        WHEN 'CQ-B' THEN jsonb_build_object('CIII1908', jsonb_build_object('component', 'doublet', 'snr', 3.0, 'flux', 1e-18, 'flux_err', 1e-19), 'OIII5007', jsonb_build_object('component', 'narrow', 'snr', 12.0, 'flux', 1e-18, 'flux_err', 1e-19))
        WHEN 'CQ-C' THEN jsonb_build_object('OIII5007', jsonb_build_object('component', 'narrow', 'snr', 20.0, 'flux', 1e-18, 'flux_err', 1e-19))
        ELSE jsonb_build_object('CIII1908', jsonb_build_object('component', 'doublet', 'snr', 30.0, 'flux', 1e-18, 'flux_err', 1e-19), 'OIII5007', jsonb_build_object('component', 'narrow', 'snr', 30.0, 'flux', 1e-18, 'flux_err', 1e-19))
      END)));
  END LOOP;
  -- photometry: A bright in f444w (mag 24), C faint (mag 27)
  PERFORM public.catalog_matches_upsert((
    SELECT jsonb_agg(jsonb_build_object('object_id', o.id, 'field', o.field, 'ra', o.ra, 'dec', o.dec, 'catalog_name', 'zzz_cq_cat', 'catalog_id', o.object_id,
             'photometry', jsonb_build_object('flux_unit', 'uJy', 'bands', jsonb_build_object('f444w', jsonb_build_object('flux', CASE WHEN o.object_id = 'CQ-A' THEN 0.91201 ELSE 0.05754 END, 'flux_err', 0.01, 'wav', 4.4)))))
    FROM objects o WHERE o.object_id IN ('CQ-A', 'CQ-C')));
END $$;

-- ------------------------------------------------------------------ 2
CREATE FUNCTION pg_temp.cq_sel(p_tree jsonb, p_root text DEFAULT 'objects') RETURNS text[] AS $$
  SELECT COALESCE(array_agg(o.object_id ORDER BY o.object_id), '{}')
    FROM unnest(public.catalog_match_ids(p_root, p_tree)) AS i(id)
    JOIN public.objects o ON o.id = i.id
   WHERE o.object_id LIKE 'CQ-%';
$$ LANGUAGE sql STABLE;
GRANT EXECUTE ON FUNCTION pg_temp.cq_sel(jsonb, text) TO authenticated, service_role;

DO $$
BEGIN
  -- two lines at once (#607)
  IF pg_temp.cq_sel('{"and": [{"exists": "lines", "where": [{"col": "line", "op": "eq", "v": "CIII1908"}, {"col": "snr", "op": "gte", "v": 5}]}, {"exists": "lines", "where": [{"col": "line", "op": "eq", "v": "OIII5007"}, {"col": "snr", "op": "gte", "v": 5}]}]}')
     <> ARRAY['CQ-A', 'CQ-P'] THEN RAISE EXCEPTION 'two-line AND: %', pg_temp.cq_sel('{"and": [{"exists": "lines", "where": [{"col": "line", "op": "eq", "v": "CIII1908"}, {"col": "snr", "op": "gte", "v": 5}]}, {"exists": "lines", "where": [{"col": "line", "op": "eq", "v": "OIII5007"}, {"col": "snr", "op": "gte", "v": 5}]}]}'); END IF;
  -- OR across tables: CIII at S/N >= 5, or bright in f444w
  IF pg_temp.cq_sel('{"or": [{"exists": "lines", "where": [{"col": "line", "op": "eq", "v": "CIII1908"}, {"col": "snr", "op": "gte", "v": 5}]}, {"exists": "photometry", "where": [{"col": "band", "op": "eq", "v": "f444w"}, {"col": "mag", "op": "lte", "v": 25}]}]}')
     <> ARRAY['CQ-A', 'CQ-P'] THEN RAISE EXCEPTION 'OR across tables failed'; END IF;
  -- NOT EXISTS: objects with no CIII1908 at all
  IF pg_temp.cq_sel('{"and": [{"col": "field", "op": "eq", "v": "zzz_cq_field"}, {"exists": "lines", "not": true, "where": [{"col": "line", "op": "eq", "v": "CIII1908"}]}]}')
     <> ARRAY['CQ-C'] THEN RAISE EXCEPTION 'NOT EXISTS failed'; END IF;
  -- nested exists: OIII5007 from a fresh fit only (B's fit is stale)
  IF pg_temp.cq_sel('{"exists": "lines", "where": [{"col": "line", "op": "eq", "v": "OIII5007"}, {"col": "snr", "op": "gte", "v": 5}, {"exists": "line_fits", "where": [{"col": "stale_redshift", "op": "eq", "v": false}]}]}')
     <> ARRAY['CQ-A', 'CQ-C', 'CQ-P'] THEN RAISE EXCEPTION 'fresh-only nested exists failed: %', pg_temp.cq_sel('{"exists": "lines", "where": [{"col": "line", "op": "eq", "v": "OIII5007"}, {"col": "snr", "op": "gte", "v": 5}, {"exists": "line_fits", "where": [{"col": "stale_redshift", "op": "eq", "v": false}]}]}'); END IF;
  -- arrays: gratings has G395M; programs overlaps
  IF pg_temp.cq_sel('{"col": "gratings", "op": "has", "v": "G395M"}') <> ARRAY['CQ-B'] THEN RAISE EXCEPTION 'array has failed'; END IF;
  IF pg_temp.cq_sel('{"col": "programs", "op": "overlaps", "v": ["zzz_cq_priv", "nothing"]}') <> ARRAY['CQ-P'] THEN RAISE EXCEPTION 'array overlaps failed'; END IF;
  -- exists over spectra from objects: a G395M spectrum
  IF pg_temp.cq_sel('{"exists": "spectra", "where": [{"col": "grating", "op": "eq", "v": "G395M"}]}') <> ARRAY['CQ-B'] THEN RAISE EXCEPTION 'objects -> spectra failed'; END IF;
  -- between with an open end, in, cone
  IF pg_temp.cq_sel('{"and": [{"col": "field", "op": "eq", "v": "zzz_cq_field"}, {"col": "redshift", "op": "between", "v": [5.2, null]}]}') <> ARRAY['CQ-B', 'CQ-P'] THEN RAISE EXCEPTION 'open between failed'; END IF;
  IF pg_temp.cq_sel('{"and": [{"col": "field", "op": "eq", "v": "zzz_cq_field"}, {"col": "redshift_quality", "op": "in", "v": [2, 3]}]}') <> ARRAY['CQ-B', 'CQ-C'] THEN RAISE EXCEPTION 'in failed'; END IF;
  -- the fixtures sit 0.1 deg apart in RA: a 0.06 deg cone holds B alone, 0.15 deg holds A, B, C and not P (0.2 deg away)
  IF pg_temp.cq_sel('{"cone": {"ra": 158.1, "dec": 2.0, "radius": 0.06}}') <> ARRAY['CQ-B'] THEN RAISE EXCEPTION 'cone failed: %', pg_temp.cq_sel('{"cone": {"ra": 158.1, "dec": 2.0, "radius": 0.06}}'); END IF;
  IF pg_temp.cq_sel('{"cone": {"ra": 158.1, "dec": 2.0, "radius": 0.15}}') <> ARRAY['CQ-A', 'CQ-B', 'CQ-C'] THEN RAISE EXCEPTION 'wide cone failed: %', pg_temp.cq_sel('{"cone": {"ra": 158.1, "dec": 2.0, "radius": 0.15}}'); END IF;
  -- a cone straddling RA = 0 reaches the object at 359.95 from either side
  IF (SELECT COALESCE(array_agg(o.object_id), '{}') FROM unnest(public.catalog_match_ids('objects', '{"cone": {"ra": 0.02, "dec": 2.0, "radius": 0.1}}')) i(id) JOIN objects o ON o.id = i.id WHERE o.object_id LIKE 'CQW-%') <> ARRAY['CQW-1']
     OR (SELECT COALESCE(array_agg(o.object_id), '{}') FROM unnest(public.catalog_match_ids('objects', '{"cone": {"ra": 359.99, "dec": 2.0, "radius": 0.1}}')) i(id) JOIN objects o ON o.id = i.id WHERE o.object_id LIKE 'CQW-%') <> ARRAY['CQW-1'] THEN
    RAISE EXCEPTION 'cone across RA = 0 failed';
  END IF;
  -- a wide cone away from the equator: the RA box must use the exact small-circle half-width
  IF (SELECT COALESCE(array_agg(o.object_id), '{}') FROM unnest(public.catalog_match_ids('objects', '{"cone": {"ra": 0, "dec": 60, "radius": 10}}')) i(id) JOIN objects o ON o.id = i.id WHERE o.object_id LIKE 'CQW-%') <> ARRAY['CQW-2'] THEN
    RAISE EXCEPTION 'wide high-declination cone failed: %', (SELECT COALESCE(array_agg(o.object_id), '{}') FROM unnest(public.catalog_match_ids('objects', '{"cone": {"ra": 0, "dec": 60, "radius": 10}}')) i(id) JOIN objects o ON o.id = i.id WHERE o.object_id LIKE 'CQW-%');
  END IF;
  -- spectra root: spectra of objects above z = 5 with a CIII line in the fit
  IF (SELECT count(*) FROM unnest(public.catalog_match_ids('spectra', '{"and": [{"exists": "objects", "where": [{"col": "redshift", "op": "gte", "v": 5}]}, {"exists": "lines", "where": [{"col": "line", "op": "eq", "v": "CIII1908"}]}]}')) i(id) JOIN spectra s ON s.id = i.id WHERE s.fits_path LIKE '/tmp/cq_%') <> 3 THEN
    RAISE EXCEPTION 'spectra root failed';
  END IF;
  RAISE NOTICE 'OK 2: semantics';
END $$;

-- ------------------------------------------------------------------ 3 + 4 (as the seeded non-admin user)
SELECT set_config('request.jwt.claims', '{"sub":"22222222-2222-2222-2222-222222222222","role":"authenticated"}', true);
SET LOCAL ROLE authenticated;
DO $$
DECLARE
  slugs text[] := public.accessible_program_slugs();
  legacy text[]; tree text[];
  cases jsonb := '[
    {"name": "redshift + quality", "tree": {"and": [{"col": "redshift", "op": "between", "v": [4.5, 6.5]}, {"col": "redshift_quality", "op": "in", "v": [3, 4]}]},
     "legacy": {"p_redshift_min": 4.5, "p_redshift_max": 6.5, "p_redshift_quality": [3, 4]}},
    {"name": "line filter", "tree": {"exists": "lines", "where": [{"col": "line", "op": "eq", "v": "CIII1908"}, {"col": "snr", "op": "gte", "v": 5}, {"exists": "line_fits", "where": [{"col": "stale_redshift", "op": "eq", "v": false}]}]},
     "legacy": {"p_line": "CIII1908", "p_line_snr_min": 5}},
    {"name": "band filter", "tree": {"exists": "photometry", "where": [{"col": "band", "op": "eq", "v": "f444w"}, {"col": "mag", "op": "lte", "v": 25}]},
     "legacy": {"p_band": "f444w", "p_band_mag_max": 25}},
    {"name": "gratings any", "tree": {"exists": "spectra", "where": [{"col": "grating", "op": "in", "v": ["G395M"]}]},
     "legacy": {"p_gratings": ["G395M"], "p_gratings_mode": "any"}},
    {"name": "field + inspected", "tree": {"and": [{"col": "field", "op": "eq", "v": "zzz_cq_field"}, {"col": "redshift_quality", "op": "gt", "v": 0}]},
     "legacy": {"p_fields": ["zzz_cq_field"], "p_inspected_only": true}}
  ]';
  c jsonb;
BEGIN
  FOR c IN SELECT jsonb_array_elements(cases) LOOP
    SELECT COALESCE(array_agg(e ->> 'object_id' ORDER BY e ->> 'object_id'), '{}') INTO legacy
      FROM public.get_filtered_objects_paginated(
             p_program_slugs := slugs, p_page_size := 1000,
             p_redshift_min := (c #>> '{legacy,p_redshift_min}')::double precision,
             p_redshift_max := (c #>> '{legacy,p_redshift_max}')::double precision,
             p_redshift_quality := (SELECT array_agg(x::int) FROM jsonb_array_elements_text(c #> '{legacy,p_redshift_quality}') x),
             p_line := c #>> '{legacy,p_line}', p_line_snr_min := (c #>> '{legacy,p_line_snr_min}')::double precision,
             p_band := c #>> '{legacy,p_band}', p_band_mag_max := (c #>> '{legacy,p_band_mag_max}')::double precision,
             p_gratings := (SELECT array_agg(x) FROM jsonb_array_elements_text(c #> '{legacy,p_gratings}') x),
             p_gratings_mode := COALESCE(c #>> '{legacy,p_gratings_mode}', 'any'),
             p_fields := (SELECT array_agg(x) FROM jsonb_array_elements_text(c #> '{legacy,p_fields}') x),
             p_inspected_only := (c #>> '{legacy,p_inspected_only}')::boolean) g,
           jsonb_array_elements(g.targets) e;
    SELECT COALESCE(array_agg(e ->> 'object_id' ORDER BY e ->> 'object_id'), '{}') INTO tree
      FROM public.catalog_query_objects(c -> 'tree', slugs, p_page_size := 1000) g, jsonb_array_elements(g.targets) e;
    IF legacy IS DISTINCT FROM tree THEN
      RAISE EXCEPTION 'parity (%): legacy % vs tree %', c ->> 'name', legacy, tree;
    END IF;
    IF cardinality(tree) = 0 THEN RAISE EXCEPTION 'parity (%): empty on both sides, fixture broken', c ->> 'name'; END IF;
  END LOOP;
  -- the private program's object never reaches the viewer, through any node
  IF 'CQ-P' = ANY (pg_temp.cq_sel('{"col": "field", "op": "eq", "v": "zzz_cq_field"}'))
     OR 'CQ-P' = ANY (pg_temp.cq_sel('{"exists": "lines", "where": [{"col": "snr", "op": "gte", "v": 25}]}')) THEN
    RAISE EXCEPTION 'a private object leaked to the viewer';
  END IF;
  IF pg_temp.cq_sel('{"col": "field", "op": "eq", "v": "zzz_cq_field"}') <> ARRAY['CQ-A', 'CQ-B', 'CQ-C'] THEN
    RAISE EXCEPTION 'viewer sees % of the public fixtures', pg_temp.cq_sel('{"col": "field", "op": "eq", "v": "zzz_cq_field"}');
  END IF;
  RAISE NOTICE 'OK 3+4: parity with the legacy filters and RLS, as a viewer';
END $$;
RESET ROLE;

-- ------------------------------------------------------------------ 5 (a browsed release)
DO $$
DECLARE got text; ids int[];
BEGIN
  PERFORM set_config('campfire.dr_tag', 'DR-seed', true);
  got := public.catalog_tree_sql('{"exists": "lines", "where": [{"col": "snr", "op": "gte", "v": 1}]}', 'objects');
  IF got NOT LIKE '%public.cat_lines %' THEN RAISE EXCEPTION 'browsing a release must read cat_*: %', got; END IF;
  ids := public.catalog_match_ids('objects', '{"col": "redshift_quality", "op": "gte", "v": 0}');
  IF cardinality(ids) = 0 THEN RAISE EXCEPTION 'no frozen objects selected'; END IF;
  IF NOT (ids <@ (SELECT array_agg(id) FROM public.dr_objects WHERE tag = 'DR-seed')) THEN
    RAISE EXCEPTION 'browsing a release selected ids outside its frozen rows';
  END IF;
  IF (SELECT count(*) FROM unnest(ids) i JOIN public.objects o ON o.id = i WHERE o.object_id LIKE 'CQ-%') > 0 THEN
    RAISE EXCEPTION 'browsing a release selected live-only fixtures';
  END IF;
  -- lists are never frozen: an exists over them still compiles while browsing
  got := public.catalog_tree_sql('{"exists": "object_list_members"}', 'objects');
  IF got NOT LIKE '%public.object_list_members %' THEN RAISE EXCEPTION 'lists must stay live while browsing: %', got; END IF;
  PERFORM set_config('campfire.dr_tag', '', true);
  RAISE NOTICE 'OK 5: a browsed release reads its frozen rows';
END $$;

-- ------------------------------------------------------------------ 6
DO $$
DECLARE r record; n int;
BEGIN
  SELECT * INTO r FROM public.catalog_query_objects('{"col": "field", "op": "eq", "v": "zzz_cq_field"}', ARRAY['zzz_cq_pub', 'zzz_cq_priv'], p_sort_column := 'redshift', p_sort_direction := 'desc', p_page_size := 2);
  IF r.total_count <> 4 OR jsonb_array_length(r.targets) <> 2 OR NOT r.has_more
     OR (r.targets -> 0 ->> 'object_id') <> 'CQ-P' OR r.next_sort_num IS NULL THEN
    RAISE EXCEPTION 'catalog_query_objects page: total %, rows %, has_more %, first %', r.total_count, jsonb_array_length(r.targets), r.has_more, r.targets -> 0 ->> 'object_id';
  END IF;
  -- the Line S/N column follows the display line
  SELECT * INTO r FROM public.catalog_query_objects('{"col": "object_id", "op": "eq", "v": "CQ-A"}', ARRAY['zzz_cq_pub'], p_line := 'CIII1908', p_line_include_stale := true);
  IF (r.targets -> 0 ->> 'line_snr')::double precision <> 10.0 THEN RAISE EXCEPTION 'line_snr column: %', r.targets -> 0 ->> 'line_snr'; END IF;
  -- the display line never filters: OR over line and photometry keeps the
  -- photometry-only object, with a NULL Line S/N
  SELECT * INTO r FROM public.catalog_query_objects(
    '{"or": [{"exists": "lines", "where": [{"col": "line", "op": "eq", "v": "CIII1908"}, {"col": "snr", "op": "gte", "v": 5}]}, {"exists": "photometry", "where": [{"col": "band", "op": "eq", "v": "f444w"}, {"col": "mag", "op": "gte", "v": 26}]}]}',
    ARRAY['zzz_cq_pub'], p_line := 'CIII1908', p_line_snr_min := 5, p_sort_column := 'object_id');
  IF r.total_count <> 2 OR (r.targets -> 1 ->> 'object_id') <> 'CQ-C' OR (r.targets -> 1 -> 'line_snr') IS DISTINCT FROM 'null'::jsonb THEN
    RAISE EXCEPTION 'display line narrowed the selection: total %, rows %', r.total_count, r.targets;
  END IF;
  -- same for the display band on a line-only tree
  SELECT * INTO r FROM public.catalog_query_objects(
    '{"exists": "lines", "where": [{"col": "line", "op": "eq", "v": "OIII5007"}, {"col": "snr", "op": "gte", "v": 5}]}',
    ARRAY['zzz_cq_pub'], p_band := 'f444w', p_band_mag_max := 25, p_sort_column := 'object_id');
  IF r.total_count <> 3 THEN RAISE EXCEPTION 'display band narrowed the selection: %', r.total_count; END IF;
  -- an empty selection is an empty page
  SELECT * INTO r FROM public.catalog_query_objects('{"col": "object_id", "op": "eq", "v": "CQ-NONE"}', ARRAY['zzz_cq_pub']);
  IF r.total_count <> 0 OR jsonb_array_length(r.targets) <> 0 OR r.has_more THEN RAISE EXCEPTION 'empty selection'; END IF;
  -- spectra twin
  SELECT * INTO r FROM public.catalog_query_spectra('{"exists": "objects", "where": [{"col": "object_id", "op": "eq", "v": "CQ-B"}]}', ARRAY['zzz_cq_pub'], p_sort_column := 'grating');
  IF r.total_count <> 2 THEN RAISE EXCEPTION 'catalog_query_spectra: %', r.total_count; END IF;
  RAISE NOTICE 'OK 6: catalog_query_objects / _spectra';
END $$;

-- ------------------------------------------------------------------ 7 (the service role with an explicit scope)
-- a private list of the admin with CQ-A in it; the seeded user must not see it
INSERT INTO object_lists (slug, name, visibility, created_by) VALUES ('zzz-cq-private', 'CQ private list', 'private', '11111111-1111-1111-1111-111111111111');
INSERT INTO object_list_members (list_id, object_id, ra, dec) SELECT l.id, o.id, o.ra, o.dec FROM object_lists l, objects o WHERE l.slug = 'zzz-cq-private' AND o.object_id = 'CQ-A';
CREATE TEMP TABLE cq_scope AS SELECT NULL::text[] AS slugs;
GRANT ALL ON pg_temp.cq_scope TO authenticated, service_role;
SELECT set_config('request.jwt.claims', '{"sub":"22222222-2222-2222-2222-222222222222","role":"authenticated"}', true);
SET LOCAL ROLE authenticated;
UPDATE pg_temp.cq_scope SET slugs = public.accessible_program_slugs();
RESET ROLE;
SET LOCAL ROLE service_role;
DO $$
DECLARE
  slugs text[] := (SELECT s.slugs FROM pg_temp.cq_scope s);
  viewer uuid := '22222222-2222-2222-2222-222222222222';
  admin uuid := '11111111-1111-1111-1111-111111111111';
  trees jsonb := '[
    {"col": "field", "op": "eq", "v": "zzz_cq_field"},
    {"exists": "lines", "where": [{"col": "snr", "op": "gte", "v": 25}]},
    {"exists": "spectra", "where": [{"col": "grating", "op": "eq", "v": "PRISM"}]},
    {"exists": "targets"},
    {"exists": "line_fits"},
    {"exists": "photometry"},
    {"exists": "catalog_matches"},
    {"exists": "object_list_members"},
    {"or": [{"exists": "lines", "where": [{"col": "snr", "op": "gte", "v": 25}]}, {"col": "redshift_quality", "op": "eq", "v": 4}]}
  ]';
  t jsonb; got text[];
BEGIN
  -- no scope: the service role sees everything, as it should
  IF NOT ('CQ-P' = ANY (pg_temp.cq_sel('{"col": "field", "op": "eq", "v": "zzz_cq_field"}'))) THEN
    RAISE EXCEPTION 'the service role without a scope should see the private object';
  END IF;
  -- with the viewer's scope: never the private object, through any node
  FOR t IN SELECT jsonb_array_elements(trees) LOOP
    SELECT COALESCE(array_agg(o.object_id ORDER BY o.object_id), '{}') INTO got
      FROM unnest(public.catalog_match_ids('objects', t, slugs, false, viewer)) i(id)
      JOIN public.objects o ON o.id = i.id WHERE o.object_id LIKE 'CQ-%';
    IF 'CQ-P' = ANY (got) THEN RAISE EXCEPTION 'private object leaked through % under the viewer''s scope', t; END IF;
  END LOOP;
  -- the admin's private list is invisible to the viewer, visible to its owner
  SELECT COALESCE(array_agg(o.object_id), '{}') INTO got
    FROM unnest(public.catalog_match_ids('objects', '{"exists": "object_list_members"}', slugs, false, viewer)) i(id)
    JOIN public.objects o ON o.id = i.id WHERE o.object_id LIKE 'CQ-%';
  IF 'CQ-A' = ANY (got) THEN RAISE EXCEPTION 'a private list leaked to the viewer'; END IF;
  SELECT COALESCE(array_agg(o.object_id), '{}') INTO got
    FROM unnest(public.catalog_match_ids('objects', '{"exists": "object_list_members"}', slugs, false, admin)) i(id)
    JOIN public.objects o ON o.id = i.id WHERE o.object_id LIKE 'CQ-%';
  IF NOT ('CQ-A' = ANY (got)) THEN RAISE EXCEPTION 'the owner does not see their own list'; END IF;
  -- unpublished stays hidden unless asked
  UPDATE public.spectra SET deploy_status = 'draft' WHERE fits_path = '/tmp/cq_cq-c.fits';
  SELECT COALESCE(array_agg(o.object_id), '{}') INTO got
    FROM unnest(public.catalog_match_ids('objects', '{"exists": "spectra", "where": [{"col": "grating", "op": "eq", "v": "PRISM"}]}', slugs, false, viewer)) i(id)
    JOIN public.objects o ON o.id = i.id WHERE o.object_id LIKE 'CQ-%';
  IF 'CQ-C' = ANY (got) THEN RAISE EXCEPTION 'a draft spectrum reached the viewer''s scope'; END IF;
  SELECT COALESCE(array_agg(o.object_id), '{}') INTO got
    FROM unnest(public.catalog_match_ids('objects', '{"exists": "spectra", "where": [{"col": "grating", "op": "eq", "v": "PRISM"}]}', slugs, true, viewer)) i(id)
    JOIN public.objects o ON o.id = i.id WHERE o.object_id LIKE 'CQ-%';
  IF NOT ('CQ-C' = ANY (got)) THEN RAISE EXCEPTION 'include_unpublished did not admit the draft'; END IF;
  -- the wrapper carries the scope through
  SELECT COALESCE(array_agg(e ->> 'object_id'), '{}') INTO got
    FROM public.catalog_query_spectra('{"exists": "objects", "where": [{"col": "field", "op": "eq", "v": "zzz_cq_field"}]}', slugs, p_page_size := 100) g, jsonb_array_elements(g.targets) e;
  IF 'CQ-P' = ANY (got) THEN RAISE EXCEPTION 'catalog_query_spectra leaked the private object to the service role'; END IF;
  RAISE NOTICE 'OK 7: the service role with a viewer''s scope selects what the viewer selects';
END $$;
RESET ROLE;

ROLLBACK;
