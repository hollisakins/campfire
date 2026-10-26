-- =============================================================================
-- Frozen-table parity (P6b, docs/design-data-releases-impl-plan.md §8 P6b).
-- =============================================================================
-- Every frozen catalog table is `tag` + the live table's columns, same names
-- and types (a generated live column is a plain column of the same type in
-- the copy), plus the documented frozen-only extras and nothing else; and
-- dr_freeze_job's INSERT for each table names every live column (it copies by
-- name, never `x.*`). A live column added without its copy, or without its
-- place in the freeze, fails CI here before a production freeze does. Also:
-- no foreign keys, RLS on, authenticated SELECT only, never anon.
-- Run: psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_dr_parity.sql
\set ON_ERROR_STOP on

DO $$
DECLARE
  pairs text[][] := ARRAY[
    ['spectra',                 'dr_spectra',          ''],
    ['targets',                 'dr_targets',          ''],
    ['objects',                 'dr_objects',          'evidence_programs,z_source_spectrum_id'],
    ['line_fits',      'dr_line_fits',        'stale_spectrum'],  -- stale_redshift is live since phase 2a
    ['lines',          'dr_lines',   ''],
    ['catalog_matches',       'dr_catalog_matches',       ''],
    ['photometry', 'dr_photometry', ''],
    ['nircam_images',           'dr_nircam_images',    ''],
    ['fields',                  'dr_fields',           ''],
    ['mv_filter_options',       'dr_filter_options',   '']
  ];
  live text; frozen text; extras text[];
  missing text; extra text; badtype text; col text; n_live int;
  freeze_src text := pg_get_functiondef('public.dr_freeze_job(text)'::regprocedure);
  ins_list text;
  i int;
BEGIN
  FOR i IN 1 .. array_length(pairs, 1) LOOP
    live := pairs[i][1]; frozen := pairs[i][2];
    extras := CASE WHEN pairs[i][3] = '' THEN '{}'::text[] ELSE string_to_array(pairs[i][3], ',') END;

    -- 1. the frozen table carries every live column, by name and type
    --    (physical order is not compared: the freeze copies by name)
    SELECT string_agg(l.attname, ', ' ORDER BY l.attnum) INTO missing
    FROM pg_attribute l
    WHERE l.attrelid = ('public.' || live)::regclass AND l.attnum > 0 AND NOT l.attisdropped
      AND NOT EXISTS (SELECT 1 FROM pg_attribute f WHERE f.attrelid = ('public.' || frozen)::regclass
                      AND f.attnum > 0 AND NOT f.attisdropped AND f.attname = l.attname);
    IF missing IS NOT NULL THEN
      RAISE EXCEPTION 'parity: % lacks live column(s) of %: % (add them to the frozen table and to dr_freeze_job)', frozen, live, missing;
    END IF;
    SELECT string_agg(f.attname || ' ' || format_type(f.atttypid, f.atttypmod) || ' vs ' || format_type(l.atttypid, l.atttypmod), ', ') INTO badtype
    FROM pg_attribute f JOIN pg_attribute l ON l.attname = f.attname
    WHERE f.attrelid = ('public.' || frozen)::regclass AND l.attrelid = ('public.' || live)::regclass
      AND f.attnum > 0 AND l.attnum > 0 AND NOT f.attisdropped AND NOT l.attisdropped
      AND format_type(f.atttypid, f.atttypmod) <> format_type(l.atttypid, l.atttypmod);
    IF badtype IS NOT NULL THEN
      RAISE EXCEPTION 'parity: % column type(s) differ from %: %', frozen, live, badtype;
    END IF;
    -- a frozen NOT NULL needs the live column NOT NULL (a nullable live
    -- value would fail the copy); the frozen copy may be looser, never stricter
    SELECT string_agg(f.attname, ', ') INTO badtype
    FROM pg_attribute f JOIN pg_attribute l ON l.attname = f.attname
    WHERE f.attrelid = ('public.' || frozen)::regclass AND l.attrelid = ('public.' || live)::regclass
      AND f.attnum > 0 AND l.attnum > 0 AND NOT f.attisdropped AND NOT l.attisdropped
      AND f.attnotnull AND NOT l.attnotnull;
    IF badtype IS NOT NULL THEN
      RAISE EXCEPTION 'parity: % has NOT NULL on column(s) nullable in %: %', frozen, live, badtype;
    END IF;
    -- 2. ...and nothing else but tag + the documented extras
    SELECT string_agg(f.attname, ', ' ORDER BY f.attnum) INTO extra
    FROM pg_attribute f
    WHERE f.attrelid = ('public.' || frozen)::regclass AND f.attnum > 0 AND NOT f.attisdropped
      AND f.attname <> 'tag' AND NOT (f.attname = ANY (extras))
      AND NOT EXISTS (SELECT 1 FROM pg_attribute l WHERE l.attrelid = ('public.' || live)::regclass
                      AND l.attnum > 0 AND NOT l.attisdropped AND l.attname = f.attname);
    IF extra IS NOT NULL THEN
      RAISE EXCEPTION 'parity: % has column(s) that are neither live nor documented extras: %', frozen, extra;
    END IF;
    FOREACH col IN ARRAY extras LOOP
      IF NOT EXISTS (SELECT 1 FROM pg_attribute f WHERE f.attrelid = ('public.' || frozen)::regclass AND f.attname = col) THEN
        RAISE EXCEPTION 'parity: % lacks its extra column %', frozen, col;
      END IF;
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM pg_attribute f WHERE f.attrelid = ('public.' || frozen)::regclass AND f.attname = 'tag' AND format_type(f.atttypid, f.atttypmod) = 'text' AND f.attnotnull) THEN
      RAISE EXCEPTION 'parity: % has no tag text NOT NULL column', frozen;
    END IF;
    -- 3. dr_freeze_job's INSERT INTO <frozen> (...) names every live column
    --    (dr_filter_options is computed, not copied: its INSERT is checked by
    --    check_dr_freeze through the seed)
    IF frozen <> 'dr_filter_options' THEN
      ins_list := substring(freeze_src FROM 'INSERT INTO ' || frozen || ' \(([^)]*)\)');
      IF ins_list IS NULL THEN
        RAISE EXCEPTION 'parity: dr_freeze_job has no "INSERT INTO % (...)" with an explicit column list', frozen;
      END IF;
      SELECT string_agg(l.attname, ', ' ORDER BY l.attnum) INTO missing
      FROM pg_attribute l
      WHERE l.attrelid = ('public.' || live)::regclass AND l.attnum > 0 AND NOT l.attisdropped
        AND NOT (l.attname = ANY (string_to_array(regexp_replace(ins_list, '\s', '', 'g'), ',')));
      IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'parity: dr_freeze_job''s INSERT INTO % does not copy live column(s) %', frozen, missing;
      END IF;
    END IF;
    -- 4. no generated columns in the copy (the freeze inserts the live value)
    IF EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = ('public.' || frozen)::regclass AND a.attgenerated <> '') THEN
      RAISE EXCEPTION 'parity: % has a generated column; the copy holds plain values', frozen;
    END IF;
    -- 5. no foreign keys: a frozen row outlives whatever it pointed at
    IF EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conrelid = ('public.' || frozen)::regclass AND c.contype = 'f') THEN
      RAISE EXCEPTION 'parity: % has a foreign key', frozen;
    END IF;
    -- 6. read under RLS by authenticated, never anon, never written directly
    IF NOT (SELECT relrowsecurity FROM pg_class WHERE oid = ('public.' || frozen)::regclass) THEN
      RAISE EXCEPTION 'parity: % has RLS disabled', frozen;
    END IF;
    IF has_table_privilege('anon', 'public.' || frozen, 'SELECT') THEN
      RAISE EXCEPTION 'parity: anon can read %', frozen;
    END IF;
    IF NOT has_table_privilege('authenticated', 'public.' || frozen, 'SELECT')
       OR has_table_privilege('authenticated', 'public.' || frozen, 'INSERT')
       OR has_table_privilege('authenticated', 'public.' || frozen, 'UPDATE')
       OR has_table_privilege('authenticated', 'public.' || frozen, 'DELETE') THEN
      RAISE EXCEPTION 'parity: authenticated privileges on % are not SELECT alone', frozen;
    END IF;
    SELECT count(*) INTO n_live FROM pg_attribute l WHERE l.attrelid = ('public.' || live)::regclass AND l.attnum > 0 AND NOT l.attisdropped;
    RAISE NOTICE 'OK %: % = tag + the % columns of % + %', i, frozen, n_live, live, extras;
  END LOOP;
END $$;
