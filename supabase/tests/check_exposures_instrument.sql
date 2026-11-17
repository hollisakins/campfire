-- Guard: `exposures` holds every imaging instrument (multi-instrument review).
--
-- The table was `nircam_exposures` until 2026-11. This checks what the rename
-- and the `instrument` column promise:
--   * a row with no instrument given is NIRCam's (existing rows and callers
--     that predate the column), and an unknown instrument is refused;
--   * the instrument is part of a row's identity: the same field, filter and
--     file name can exist once per instrument and no more;
--   * get_admin_exposures / get_admin_exposure_neighbors filter on
--     p_instrument, return every instrument without it, and keep a field's
--     instruments together in the list order (field, instrument, filter,
--     filename); the facets list the instruments;
--   * exposure_review_progress groups by instrument;
--   * a non-admin reads none of it.
--
-- Fixtures build their own field; the actor is the seed admin (11111111-...).
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_exposures_instrument.sql
BEGIN;

INSERT INTO fields (name, display_name) VALUES ('zzz_ei_field', 'EI field')
ON CONFLICT (name) DO NOTHING;

-- No instrument given: NIRCam.
INSERT INTO exposures (field, filter, detector, filename, stage, review_status) VALUES
  ('zzz_ei_field', 'f444w', 'nrca1', 'zzz_ei_nrc_1', 'cal', 'pending'),
  ('zzz_ei_field', 'f444w', 'nrca2', 'zzz_ei_nrc_2', 'cal', 'approved');
INSERT INTO exposures (instrument, field, filter, detector, filename, stage, review_status, mask_regions) VALUES
  ('acswfc', 'zzz_ei_field', 'f814w', 'acswfc', 'jzzz01a0q_flc', 'align', 'pending',
   '{"version": 1, "polygons": [{"id": "t1", "vertices": [[1, 1], [9, 1], [9, 5]], "label": "satellite trail", "source": "imported"}]}'),
  ('acswfc', 'zzz_ei_field', 'f814w', 'acswfc', 'jzzz01a1q_flc', 'align', 'pending', NULL),
  ('wfc3ir', 'zzz_ei_field', 'f160w', 'wfc3ir', 'izzz01a0q_flt', 'align', 'excluded', NULL),
  -- the same field, filter and file name under another instrument: allowed
  ('wfc3ir', 'zzz_ei_field', 'f444w', 'wfc3ir', 'zzz_ei_nrc_1', 'align', 'pending', NULL);

DO $$
DECLARE
  n bigint;
BEGIN
  SELECT count(*) INTO n FROM exposures
  WHERE field = 'zzz_ei_field' AND instrument = 'nircam';
  IF n <> 2 THEN RAISE EXCEPTION 'default instrument: % nircam rows, expected 2', n; END IF;

  BEGIN
    INSERT INTO exposures (instrument, field, filter, detector, filename)
    VALUES ('acswfc', 'zzz_ei_field', 'f814w', 'acswfc', 'jzzz01a0q_flc');
    RAISE EXCEPTION 'a second row of one instrument, field, filter and file name was accepted';
  EXCEPTION WHEN unique_violation THEN NULL;
  END;

  BEGIN
    INSERT INTO exposures (instrument, field, filter, detector, filename)
    VALUES ('wfc3uvis', 'zzz_ei_field', 'f606w', 'wfc3uvis', 'izzz09a0q_flc');
    RAISE EXCEPTION 'an unknown instrument was accepted';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
END $$;

-- Read as the seed admin.
SET LOCAL role authenticated;
SELECT set_config('request.jwt.claims',
  json_build_object('sub', '11111111-1111-1111-1111-111111111111', 'role', 'authenticated')::text, true);

DO $$
DECLARE
  n bigint;
  got text[];
  cur integer;
BEGIN
  -- no instrument: every instrument of the field
  SELECT count(*) INTO n FROM public.get_admin_exposures(p_field => 'zzz_ei_field');
  IF n <> 6 THEN RAISE EXCEPTION 'all instruments: % rows, expected 6', n; END IF;

  -- p_instrument narrows, and total_count follows
  SELECT count(*), max(total_count) INTO n, cur
  FROM public.get_admin_exposures(p_field => 'zzz_ei_field', p_instrument => 'acswfc');
  IF n <> 2 OR cur <> 2 THEN RAISE EXCEPTION 'acswfc: % rows (total %), expected 2', n, cur; END IF;
  SELECT count(*) INTO n
  FROM public.get_admin_exposures(p_field => 'zzz_ei_field', p_instrument => 'nircam');
  IF n <> 2 THEN RAISE EXCEPTION 'nircam: % rows, expected 2', n; END IF;
  IF EXISTS (SELECT 1 FROM public.get_admin_exposures(p_field => 'zzz_ei_field', p_instrument => 'acswfc')
             WHERE instrument <> 'acswfc') THEN
    RAISE EXCEPTION 'p_instrument returned a row of another instrument';
  END IF;

  -- the list order keeps a field's instruments together
  SELECT array_agg(instrument || ':' || filename) INTO got
  FROM public.get_admin_exposures(p_field => 'zzz_ei_field');
  IF got <> ARRAY['acswfc:jzzz01a0q_flc', 'acswfc:jzzz01a1q_flc',
                  'nircam:zzz_ei_nrc_1', 'nircam:zzz_ei_nrc_2',
                  'wfc3ir:izzz01a0q_flt', 'wfc3ir:zzz_ei_nrc_1'] THEN
    RAISE EXCEPTION 'list order: %', got;
  END IF;

  -- the neighbors walk the same filtered, ordered set
  SELECT id INTO cur FROM exposures
  WHERE field = 'zzz_ei_field' AND instrument = 'acswfc' AND filename = 'jzzz01a1q_flc';
  SELECT max(total_count) INTO n
  FROM public.get_admin_exposure_neighbors(cur, p_field => 'zzz_ei_field', p_instrument => 'acswfc');
  IF n <> 2 THEN RAISE EXCEPTION 'neighbors within acswfc: total %, expected 2', n; END IF;
  SELECT nav_position INTO n
  FROM public.get_admin_exposure_neighbors(cur, p_field => 'zzz_ei_field')
  WHERE id = cur;
  IF n <> 2 THEN RAISE EXCEPTION 'neighbors across instruments: position %, expected 2', n; END IF;

  -- facets
  SELECT count(*) INTO n FROM public.get_admin_exposure_facets()
  WHERE kind = 'instrument' AND value IN ('nircam', 'acswfc', 'wfc3ir');
  IF n <> 3 THEN RAISE EXCEPTION 'instrument facet: % of 3 instruments listed', n; END IF;

  -- the progress view groups by instrument
  SELECT total INTO n FROM exposure_review_progress
  WHERE field = 'zzz_ei_field' AND instrument = 'acswfc' AND filter = 'f814w';
  IF n <> 2 THEN RAISE EXCEPTION 'progress acswfc/f814w total %, expected 2', n; END IF;
  SELECT masked INTO n FROM exposure_review_progress
  WHERE field = 'zzz_ei_field' AND instrument = 'acswfc' AND filter = 'f814w';
  IF n <> 1 THEN RAISE EXCEPTION 'progress acswfc/f814w masked %, expected 1', n; END IF;
  SELECT count(*) INTO n FROM exposure_review_progress
  WHERE field = 'zzz_ei_field' AND filter = 'f444w';
  IF n <> 3 THEN RAISE EXCEPTION 'progress f444w: % groups, expected 3 (2 nircam detectors + wfc3ir)', n; END IF;
END $$;

-- A non-admin (the seed viewer) reads nothing and is refused by the RPCs.
SELECT set_config('request.jwt.claims',
  json_build_object('sub', '33333333-3333-3333-3333-333333333333', 'role', 'authenticated')::text, true);

DO $$
DECLARE
  n bigint;
BEGIN
  SELECT count(*) INTO n FROM exposures WHERE field = 'zzz_ei_field';
  IF n <> 0 THEN RAISE EXCEPTION 'a non-admin read % exposure rows', n; END IF;
  SELECT count(*) INTO n FROM exposure_review_progress WHERE field = 'zzz_ei_field';
  IF n <> 0 THEN RAISE EXCEPTION 'a non-admin read % progress rows', n; END IF;
  BEGIN
    PERFORM public.get_admin_exposures(p_instrument => 'acswfc');
    RAISE EXCEPTION 'a non-admin called get_admin_exposures';
  EXCEPTION WHEN raise_exception THEN
    IF SQLERRM NOT LIKE 'Access denied%' THEN RAISE; END IF;
  END;
END $$;

ROLLBACK;
