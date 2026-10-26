-- Resumable backfill for Catalog Read Model phase 2c-1: the fields the long
-- tables did not carry until now, copied out of the jsonb blobs that are
-- still the source of truth for one more phase.
--
--   lines.{label, wave_obs, cont, cont_err, dv, dv_err, sigma_v, sigma_v_err,
--          sigma_lsf_kms, complex, chi2, dof, npix, tied_to, members}
--          <- line_fits.lines -> <line> -> <field>
--   photometry.{wav_min, wav_max}
--          <- catalog_matches.photometry -> 'bands' -> <band> -> <field>
--
-- The 2c-1 unnest triggers write every field for rows written after the
-- deploy; this fills the rows that predate it. Batched by parent id range,
-- one transaction per batch (a top-level DO block may COMMIT), so it can be
-- interrupted and rerun: a line row is "not yet filled" while label IS NULL
-- (every record carries a label), a band row while wav_min IS NULL and its
-- blob entry has one (7,899 production entries legitimately have no edges and
-- are left alone). Row-level only: no table lock, because each row's value
-- comes from its own parent's blob and the parent's trigger rewrites all of a
-- parent's rows whenever that blob changes.
--
-- Run on the VM after the phase 2c-1 deploy, as the database superuser:
--   docker exec -i campfire-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
--     < scripts/backfill_long_table_fields.sql
\timing on
SET statement_timeout = '10min';
SET lock_timeout = '1min';

DO $$
DECLARE
  lo int; hi int; maxid int; step int := 500;
  n bigint; total bigint := 0;
BEGIN
  SELECT min(spectrum_id), max(spectrum_id) INTO lo, maxid FROM public.lines WHERE label IS NULL;
  IF lo IS NULL THEN
    RAISE NOTICE 'lines: nothing to fill';
    RETURN;
  END IF;
  WHILE lo <= maxid LOOP
    hi := lo + step - 1;
    UPDATE public.lines l
       SET label         = e.value ->> 'label',
           wave_obs      = (e.value ->> 'wave_obs')::double precision,
           cont          = (e.value ->> 'cont')::double precision,
           cont_err      = (e.value ->> 'cont_err')::double precision,
           dv            = (e.value ->> 'dv')::double precision,
           dv_err        = (e.value ->> 'dv_err')::double precision,
           sigma_v       = (e.value ->> 'sigma_v')::double precision,
           sigma_v_err   = (e.value ->> 'sigma_v_err')::double precision,
           sigma_lsf_kms = (e.value ->> 'sigma_lsf_kms')::double precision,
           complex       = (e.value ->> 'complex')::integer,
           chi2          = (e.value ->> 'chi2')::double precision,
           dof           = (e.value ->> 'dof')::integer,
           npix          = (e.value ->> 'npix')::integer,
           tied_to       = e.value ->> 'tied_to',
           members       = CASE WHEN jsonb_typeof(e.value -> 'members') = 'array'
                                THEN ARRAY(SELECT jsonb_array_elements_text(e.value -> 'members')) END
      FROM public.line_fits f, jsonb_each(f.lines) e
     WHERE f.spectrum_id = l.spectrum_id
       AND e.key = l.line
       AND l.spectrum_id BETWEEN lo AND hi
       AND l.label IS NULL;
    GET DIAGNOSTICS n = ROW_COUNT;
    total := total + n;
    COMMIT;
    lo := hi + 1;
  END LOOP;
  RAISE NOTICE 'lines: % rows filled', total;
END $$;

DO $$
DECLARE
  lo int; hi int; maxid int; step int := 2000;
  n bigint; total bigint := 0;
BEGIN
  SELECT min(match_id), max(match_id) INTO lo, maxid FROM public.photometry WHERE wav_min IS NULL;
  IF lo IS NULL THEN
    RAISE NOTICE 'photometry: nothing to fill';
    RETURN;
  END IF;
  WHILE lo <= maxid LOOP
    hi := lo + step - 1;
    UPDATE public.photometry b
       SET wav_min = (e.value ->> 'wav_min')::double precision,
           wav_max = (e.value ->> 'wav_max')::double precision
      FROM public.catalog_matches m, jsonb_each(m.photometry -> 'bands') e
     WHERE m.id = b.match_id
       AND e.key = b.band
       AND e.value ? 'wav_min'
       AND b.match_id BETWEEN lo AND hi
       AND b.wav_min IS NULL;
    GET DIAGNOSTICS n = ROW_COUNT;
    total := total + n;
    COMMIT;
    lo := hi + 1;
  END LOOP;
  RAISE NOTICE 'photometry: % rows filled', total;
END $$;

ANALYZE public.lines;
ANALYZE public.photometry;

-- Verification: every row agrees with its blob entry (all counts must be 0).
SELECT 'lines unfilled' AS what, count(*) FROM public.lines WHERE label IS NULL
UNION ALL
SELECT 'lines disagree', count(*)
  FROM public.lines l
  JOIN public.line_fits f ON f.spectrum_id = l.spectrum_id
  CROSS JOIN LATERAL (SELECT f.lines -> l.line AS v) r
 WHERE l.label IS DISTINCT FROM (r.v ->> 'label')
    OR l.chi2 IS DISTINCT FROM (r.v ->> 'chi2')::double precision
    OR l.complex IS DISTINCT FROM (r.v ->> 'complex')::integer
    OR l.npix IS DISTINCT FROM (r.v ->> 'npix')::integer
    OR l.sigma_lsf_kms IS DISTINCT FROM (r.v ->> 'sigma_lsf_kms')::double precision
    OR l.tied_to IS DISTINCT FROM (r.v ->> 'tied_to')
    OR coalesce(cardinality(l.members), 0) IS DISTINCT FROM coalesce(jsonb_array_length(r.v -> 'members'), 0)
UNION ALL
SELECT 'photometry unfilled', count(*)
  FROM public.photometry b
  JOIN public.catalog_matches m ON m.id = b.match_id
 WHERE b.wav_min IS NULL AND (m.photometry -> 'bands' -> b.band) ? 'wav_min'
UNION ALL
SELECT 'photometry disagree', count(*)
  FROM public.photometry b
  JOIN public.catalog_matches m ON m.id = b.match_id
 WHERE b.wav_min IS DISTINCT FROM (m.photometry -> 'bands' -> b.band ->> 'wav_min')::double precision
    OR b.wav_max IS DISTINCT FROM (m.photometry -> 'bands' -> b.band ->> 'wav_max')::double precision;
