-- campfire_reader (Catalog Read Model phase 4, docs/reader-role.md): the
-- read-only Postgres login for public data.
--
--   1. the role exists with its settings (statement timeout, read-only
--      transactions, search_path), no superuser / createrole / createdb;
--   2. its grant set is exactly the allowlist (SELECT on the catalog tables and
--      the dr_* twins), no sequence, no function, no other table, no other
--      schema;
--   3. on a fixture with a public and a private program, a draft spectrum, an
--      inactive object and an unpublished-object target, the reader sees the
--      public published rows and not one private, draft or inactive row, on
--      every granted table, children through their parents;
--   4. the seed's DR-seed release (published) reads as frozen: the reader's
--      counts on the dr_* twins equal postgres' counts under the frozen
--      public / published / active columns, and zero rows of a release that
--      is not published;
--   5. writes are refused, every function is permission denied, and the
--      Figure 5 query of the design doc runs.
--
-- Run: psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_reader_role.sql
-- Everything is inside one rolled-back transaction.
BEGIN;

-- ============================================================================
-- 1. the role
-- ============================================================================
DO $$
DECLARE r record; cfg text[];
BEGIN
  SELECT * INTO r FROM pg_roles WHERE rolname = 'campfire_reader';
  IF NOT FOUND THEN RAISE EXCEPTION 'READER 1: role campfire_reader is missing (supabase/roles.sql / the phase4 migration)'; END IF;
  IF r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication OR r.rolbypassrls THEN
    RAISE EXCEPTION 'READER 1: campfire_reader carries a privilege attribute';
  END IF;
  IF NOT r.rolcanlogin THEN RAISE EXCEPTION 'READER 1: campfire_reader cannot log in'; END IF;
  cfg := r.rolconfig;
  IF NOT ('statement_timeout=30s' = ANY (cfg) AND 'default_transaction_read_only=on' = ANY (cfg)
          AND 'search_path=public' = ANY (cfg) AND 'idle_in_transaction_session_timeout=60s' = ANY (cfg)) THEN
    RAISE EXCEPTION 'READER 1: role settings are %, expected the timeout, read-only and search_path settings', cfg;
  END IF;
  RAISE NOTICE 'OK 1: role present, no privilege attributes, settings %', cfg;
END $$;

-- ============================================================================
-- 2. the grant set is the allowlist
-- ============================================================================
DO $$
DECLARE
  allow text[] := ARRAY['programs','observations','fields','nircam_images','objects','targets','spectra',
                        'line_fits','lines','catalog_matches','photometry',
                        'dr_releases','dr_objects','dr_targets','dr_spectra','dr_line_fits','dr_lines',
                        'dr_catalog_matches','dr_photometry','dr_nircam_images','dr_fields','dr_filter_options','dr_manifest'];
  got text[]; extra text[]; missing text[]; n int; bad text;
BEGIN
  SELECT array_agg(DISTINCT table_name ORDER BY table_name) INTO got
  FROM information_schema.role_table_grants
  WHERE grantee = 'campfire_reader' AND privilege_type = 'SELECT' AND table_schema = 'public';
  SELECT array_agg(t) INTO extra FROM unnest(got) t WHERE t <> ALL (allow);
  SELECT array_agg(t) INTO missing FROM unnest(allow) t WHERE t <> ALL (COALESCE(got, '{}'));
  IF extra IS NOT NULL THEN RAISE EXCEPTION 'READER 2: SELECT granted beyond the allowlist: %', extra; END IF;
  IF missing IS NOT NULL THEN RAISE EXCEPTION 'READER 2: allowlisted tables without SELECT: %', missing; END IF;
  SELECT count(*), string_agg(table_name || ':' || privilege_type, ', ') INTO n, bad
  FROM information_schema.role_table_grants
  WHERE grantee = 'campfire_reader' AND privilege_type <> 'SELECT';
  IF n > 0 THEN RAISE EXCEPTION 'READER 2: non-SELECT privileges: %', bad; END IF;
  SELECT count(*) INTO n FROM information_schema.role_table_grants
  WHERE grantee = 'campfire_reader' AND table_schema <> 'public';
  IF n > 0 THEN RAISE EXCEPTION 'READER 2: grants outside public'; END IF;
  -- the project's functions (owned by postgres; extension functions such as
  -- pg_trgm's are PUBLIC by design and carry no data)
  SELECT count(*), string_agg(p.proname, ', ') INTO n, bad
  FROM pg_proc p JOIN pg_namespace ns ON ns.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner
  WHERE ns.nspname = 'public' AND r.rolname = 'postgres'
    AND has_function_privilege('campfire_reader', p.oid, 'EXECUTE');
  IF n > 0 THEN RAISE EXCEPTION 'READER 2: % function(s) executable by the reader: %', n, bad; END IF;
  SELECT count(*) INTO n FROM information_schema.sequences s
  WHERE s.sequence_schema = 'public' AND has_sequence_privilege('campfire_reader', format('%I.%I', s.sequence_schema, s.sequence_name), 'SELECT,USAGE');
  IF n > 0 THEN RAISE EXCEPTION 'READER 2: sequence privileges'; END IF;
  -- every allowlisted table has RLS on and a reader policy
  SELECT count(*), string_agg(t, ', ') INTO n, bad FROM unnest(allow) t
  WHERE NOT EXISTS (SELECT 1 FROM pg_policies p WHERE p.schemaname = 'public' AND p.tablename = t AND 'campfire_reader' = ANY (p.roles))
     OR NOT (SELECT c.relrowsecurity FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace WHERE ns.nspname = 'public' AND c.relname = t);
  IF n > 0 THEN RAISE EXCEPTION 'READER 2: allowlisted tables without RLS or a reader policy: %', bad; END IF;
  RAISE NOTICE 'OK 2: grant set = allowlist (% tables), SELECT only, no functions, no sequences, every table under RLS with a reader policy', array_length(allow, 1);
END $$;

-- ============================================================================
-- 3. live fixture: one public and one private program
-- ============================================================================
INSERT INTO programs (slug, program_name, is_public) VALUES
  ('zzz_rr_pub',  'Reader Public Program',  true),
  ('zzz_rr_priv', 'Reader Private Program', false);
INSERT INTO fields (name) VALUES ('zzz_rr_field');
INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES
  ('zzz_rr_obs_pub',  'zzz_rr_pub',  9301, 'zzz_rr_field'),
  ('zzz_rr_obs_priv', 'zzz_rr_priv', 9302, 'zzz_rr_field');
INSERT INTO objects (object_id, field, ra, dec, programs, observations, has_published_spectrum, is_active) VALUES
  ('ZZZ-RR-PUB',      'zzz_rr_field', 161.0, 3.0, ARRAY['zzz_rr_pub'],  ARRAY['zzz_rr_obs_pub'],  true,  true),
  ('ZZZ-RR-PRIV',     'zzz_rr_field', 161.1, 3.1, ARRAY['zzz_rr_priv'], ARRAY['zzz_rr_obs_priv'], true,  true),
  ('ZZZ-RR-INACTIVE', 'zzz_rr_field', 161.2, 3.2, ARRAY['zzz_rr_pub'],  ARRAY['zzz_rr_obs_pub'],  true,  false),
  ('ZZZ-RR-UNPUB',    'zzz_rr_field', 161.3, 3.3, ARRAY['zzz_rr_pub'],  ARRAY['zzz_rr_obs_pub'],  false, true);
INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, object_id, has_published_spectrum)
SELECT v.tid, 'zzz_rr_field', v.ra, 3.0, v.prog, v.obs, o.id, v.pub
FROM (VALUES
  ('zzz-rr-t-pub',   161.0, 'zzz_rr_pub',  'zzz_rr_obs_pub',  'ZZZ-RR-PUB',   true),
  ('zzz-rr-t-priv',  161.1, 'zzz_rr_priv', 'zzz_rr_obs_priv', 'ZZZ-RR-PRIV',  true),
  ('zzz-rr-t-unpub', 161.3, 'zzz_rr_pub',  'zzz_rr_obs_pub',  'ZZZ-RR-UNPUB', false)
) AS v(tid, ra, prog, obs, oid, pub)
JOIN objects o ON o.object_id = v.oid;
INSERT INTO spectra (target_id, grating, fits_path, deploy_status) VALUES
  ('zzz-rr-t-pub',   'PRISM', '/tmp/zzz_rr_pub_spec.fits',   'published'),
  ('zzz-rr-t-pub',   'G395M', '/tmp/zzz_rr_pub_draft.fits',  'draft'),
  ('zzz-rr-t-priv',  'PRISM', '/tmp/zzz_rr_priv_spec.fits',  'published'),
  ('zzz-rr-t-unpub', 'PRISM', '/tmp/zzz_rr_unpub_spec.fits', 'draft');
INSERT INTO line_fits (spectrum_id, target_id, grating, program_slug, observation, z_used, z_source, fit_version)
SELECT s.id, s.target_id, s.grating, s.program_slug, s.observation, 5.0, 'inspected', 'rr'
FROM spectra s WHERE s.fits_path LIKE '/tmp/zzz_rr_%';
INSERT INTO lines (spectrum_id, line, component, wave_rest, flux, flux_err, snr)
SELECT s.id, 'Halpha', 'narrow', 6564.6, 1e-18, 1e-19, 10.0
FROM spectra s WHERE s.fits_path LIKE '/tmp/zzz_rr_%';
INSERT INTO catalog_matches (object_id, field, ra, dec, catalog_name)
SELECT o.id, 'zzz_rr_field', o.ra, o.dec, 'zzz_rr_cat' FROM objects o WHERE o.object_id LIKE 'ZZZ-RR-%';
INSERT INTO photometry (match_id, band, flux, flux_err, wav)
SELECT m.id, 'f444w', 1.0, 0.1, 4.4 FROM catalog_matches m WHERE m.catalog_name = 'zzz_rr_cat';
INSERT INTO nircam_images (field, tile, filter, pixel_scale, extension, epoch, file_path, deploy_status) VALUES
  ('zzz_rr_field', 'a', 'f444w', 0.03, 'sci', 'e1', '/tmp/zzz_rr_pub.fits', 'published'),
  ('zzz_rr_field', 'a', 'f200w', 0.03, 'sci', 'e1', '/tmp/zzz_rr_draft.fits', 'draft');

-- SET ROLE needs membership with the SET option. A superuser has it; postgres
-- holds ADMIN on the role (creator, or the grant in supabase/roles.sql) and
-- can give itself SET inside this rolled-back transaction when it lacks it
-- (a role created by postgres itself through psql -f gets ADMIN only).
DO $$
BEGIN
  IF NOT pg_has_role(current_user, 'campfire_reader', 'SET') THEN
    EXECUTE format('GRANT campfire_reader TO %I WITH SET TRUE', current_user);
  END IF;
END $$;

CREATE FUNCTION pg_temp.zzz_rr_count(p_sql text) RETURNS int
LANGUAGE plpgsql AS $$
DECLARE n int;
BEGIN
  PERFORM set_config('request.jwt.claims', '', true);
  SET LOCAL ROLE campfire_reader;
  EXECUTE p_sql INTO n;
  RESET ROLE;
  RETURN n;
EXCEPTION WHEN insufficient_privilege THEN
  RESET ROLE;
  RETURN -1;
END $$;

DO $$
DECLARE
  cases text[][] := ARRAY[
    ['programs',        $q$SELECT count(*) FROM programs WHERE slug LIKE 'zzz_rr_%'$q$,                              '1'],
    ['programs:private',$q$SELECT count(*) FROM programs WHERE slug LIKE 'zzz_rr_%' AND NOT is_public$q$,           '0'],
    ['observations',    $q$SELECT count(*) FROM observations WHERE name LIKE 'zzz_rr_%'$q$,                          '1'],
    ['fields',          $q$SELECT count(*) FROM fields WHERE name = 'zzz_rr_field'$q$,                               '1'],
    ['nircam_images',   $q$SELECT count(*) FROM nircam_images WHERE field = 'zzz_rr_field'$q$,                       '1'],
    ['objects',         $q$SELECT count(*) FROM objects WHERE object_id LIKE 'ZZZ-RR-%'$q$,                          '1'],
    ['objects:hidden',  $q$SELECT count(*) FROM objects WHERE object_id LIKE 'ZZZ-RR-%' AND (NOT is_public OR NOT is_active OR NOT has_published_spectrum)$q$, '0'],
    ['targets',         $q$SELECT count(*) FROM targets WHERE target_id LIKE 'zzz-rr-%'$q$,                          '1'],
    ['spectra',         $q$SELECT count(*) FROM spectra WHERE fits_path LIKE '/tmp/zzz_rr_%'$q$,                     '1'],
    ['spectra:hidden',  $q$SELECT count(*) FROM spectra WHERE fits_path LIKE '/tmp/zzz_rr_%' AND (NOT is_public OR deploy_status <> 'published')$q$, '0'],
    ['line_fits',       $q$SELECT count(*) FROM line_fits WHERE fit_version = 'rr'$q$,                               '1'],
    ['lines',           $q$SELECT count(*) FROM lines l JOIN spectra s ON s.id = l.spectrum_id WHERE s.fits_path LIKE '/tmp/zzz_rr_%'$q$, '1'],
    ['lines:raw',       $q$SELECT count(*) FROM lines l WHERE l.spectrum_id IN (SELECT id FROM spectra WHERE fits_path LIKE '/tmp/zzz_rr_%')$q$, '1'],
    ['catalog_matches', $q$SELECT count(*) FROM catalog_matches WHERE catalog_name = 'zzz_rr_cat'$q$,                '1'],
    ['photometry',      $q$SELECT count(*) FROM photometry p JOIN catalog_matches m ON m.id = p.match_id WHERE m.catalog_name = 'zzz_rr_cat'$q$, '1']
  ];
  i int; got int;
BEGIN
  FOR i IN 1 .. array_length(cases, 1) LOOP
    got := pg_temp.zzz_rr_count(cases[i][2]);
    IF got IS DISTINCT FROM cases[i][3]::int THEN
      RAISE EXCEPTION 'READER 3 %: reader got % (expected %)', cases[i][1], got, cases[i][3];
    END IF;
  END LOOP;
  -- the whole fixture is visible to postgres, so the reader's counts above are selections, not absences
  IF (SELECT count(*) FROM spectra WHERE fits_path LIKE '/tmp/zzz_rr_%') <> 4
     OR (SELECT count(*) FROM lines l JOIN spectra s ON s.id = l.spectrum_id WHERE s.fits_path LIKE '/tmp/zzz_rr_%') <> 4 THEN
    RAISE EXCEPTION 'READER 3: fixture incomplete';
  END IF;
  RAISE NOTICE 'OK 3: live tables: public published rows only, children through their parents';
END $$;

-- ============================================================================
-- 4. the published release reads as frozen; an unpublished one is invisible
-- ============================================================================
DO $$
DECLARE
  tag_pub text; want int; got int; t text;
  twins text[] := ARRAY['dr_objects','dr_targets','dr_spectra','dr_line_fits','dr_lines','dr_catalog_matches',
                        'dr_photometry','dr_nircam_images','dr_fields','dr_filter_options','dr_manifest'];
BEGIN
  SELECT tag INTO tag_pub FROM dr_releases WHERE state = 'published' ORDER BY tag LIMIT 1;
  IF tag_pub IS NULL THEN
    RAISE EXCEPTION 'READER 4: the seed carries no published release (DR-seed expected)';
  END IF;
  -- the release row itself
  IF pg_temp.zzz_rr_count(format($q$SELECT count(*) FROM dr_releases WHERE tag = %L$q$, tag_pub)) <> 1 THEN
    RAISE EXCEPTION 'READER 4: the reader does not see the published release %', tag_pub;
  END IF;
  -- frozen rows: the reader's count equals postgres' count under the frozen public/published/active columns
  SELECT count(*) INTO want FROM dr_objects WHERE tag = tag_pub AND is_public AND is_active AND has_published_spectrum;
  got := pg_temp.zzz_rr_count(format($q$SELECT count(*) FROM dr_objects WHERE tag = %L$q$, tag_pub));
  IF got <> want OR want = 0 THEN RAISE EXCEPTION 'READER 4 dr_objects: reader % vs frozen public %', got, want; END IF;
  SELECT count(*) INTO want FROM dr_spectra WHERE tag = tag_pub AND is_public AND deploy_status = 'published';
  got := pg_temp.zzz_rr_count(format($q$SELECT count(*) FROM dr_spectra WHERE tag = %L$q$, tag_pub));
  IF got <> want OR want = 0 THEN RAISE EXCEPTION 'READER 4 dr_spectra: reader % vs frozen public %', got, want; END IF;
  SELECT count(*) INTO want FROM dr_lines l WHERE l.tag = tag_pub
     AND EXISTS (SELECT 1 FROM dr_spectra s WHERE s.tag = l.tag AND s.id = l.spectrum_id AND s.is_public AND s.deploy_status = 'published');
  got := pg_temp.zzz_rr_count(format($q$SELECT count(*) FROM dr_lines WHERE tag = %L$q$, tag_pub));
  IF got <> want THEN RAISE EXCEPTION 'READER 4 dr_lines: reader % vs frozen public %', got, want; END IF;
  SELECT count(*) INTO want FROM dr_photometry p WHERE p.tag = tag_pub
     AND EXISTS (SELECT 1 FROM dr_catalog_matches m JOIN dr_objects o ON o.tag = m.tag AND o.id = m.object_id
                 WHERE m.tag = p.tag AND m.id = p.match_id AND o.is_public AND o.is_active AND o.has_published_spectrum);
  got := pg_temp.zzz_rr_count(format($q$SELECT count(*) FROM dr_photometry WHERE tag = %L$q$, tag_pub));
  IF got <> want THEN RAISE EXCEPTION 'READER 4 dr_photometry: reader % vs frozen public %', got, want; END IF;
  -- a private frozen row never shows
  IF pg_temp.zzz_rr_count(format($q$SELECT count(*) FROM dr_spectra WHERE tag = %L AND NOT is_public$q$, tag_pub)) <> 0
     OR pg_temp.zzz_rr_count(format($q$SELECT count(*) FROM dr_objects WHERE tag = %L AND NOT is_public$q$, tag_pub)) <> 0 THEN
    RAISE EXCEPTION 'READER 4: a private frozen row is visible';
  END IF;
  -- unpublish the release: every twin goes dark for the reader
  UPDATE dr_releases SET state = 'withdrawn' WHERE tag = tag_pub;
  FOREACH t IN ARRAY twins LOOP
    got := pg_temp.zzz_rr_count(format($q$SELECT count(*) FROM %I WHERE tag = %L$q$, t, tag_pub));
    IF got <> 0 THEN RAISE EXCEPTION 'READER 4 %: % row(s) visible of a withdrawn release', t, got; END IF;
  END LOOP;
  IF pg_temp.zzz_rr_count('SELECT count(*) FROM dr_releases') <> 0 THEN
    RAISE EXCEPTION 'READER 4: a withdrawn release is listed';
  END IF;
  UPDATE dr_releases SET state = 'published' WHERE tag = tag_pub;
  RAISE NOTICE 'OK 4: % reads as frozen for the reader; withdrawn = invisible on all % twins', tag_pub, array_length(twins, 1);
END $$;

-- ============================================================================
-- 5. writes refused, functions denied, the Figure 5 query runs
-- ============================================================================
DO $$
DECLARE n int; ok boolean;
BEGIN
  SET LOCAL ROLE campfire_reader;
  BEGIN
    INSERT INTO programs (slug, program_name, is_public) VALUES ('zzz_rr_write', 'x', true);
    RAISE EXCEPTION 'READER 5: the reader could INSERT';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  BEGIN
    UPDATE objects SET field = 'zzz_rr_other' WHERE object_id = 'ZZZ-RR-PUB';
    RAISE EXCEPTION 'READER 5: the reader could UPDATE';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  BEGIN
    PERFORM public.accessible_program_slugs();
    RAISE EXCEPTION 'READER 5: the reader could call accessible_program_slugs()';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  BEGIN
    PERFORM count(*) FROM user_profiles;
    RAISE EXCEPTION 'READER 5: the reader could read user_profiles';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  BEGIN
    PERFORM count(*) FROM storage_objects;
    RAISE EXCEPTION 'READER 5: the reader could read storage_objects';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  -- Figure 5 of the design doc, as a reader would type it
  SELECT count(*) INTO n
  FROM objects o
  WHERE o.redshift >= 4.9
    AND (o.gratings && ARRAY['PRISM', 'G395M'])
    AND EXISTS (SELECT 1 FROM lines l WHERE l.object_id = o.id AND l.line = 'CIII1908' AND l.snr >= 5)
    AND EXISTS (SELECT 1 FROM lines l WHERE l.object_id = o.id AND l.line = 'OIII5007' AND l.snr >= 3);
  RESET ROLE;
  RAISE NOTICE 'OK 5: writes refused, functions and internal tables denied, the Figure 5 query ran (% object(s) on the seed)', n;
END $$;

ROLLBACK;
