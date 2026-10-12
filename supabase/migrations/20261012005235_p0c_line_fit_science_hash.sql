SET lock_timeout = '5s'; SET statement_timeout = '60s';

SET local check_function_bodies = off;

DROP VIEW "public"."spectrum_line_fits_status";

ALTER TABLE "public"."spectrum_line_fits"
  ADD COLUMN "spectrum_science_hash" text;

CREATE OR REPLACE FUNCTION public.get_line_fits_for_sync (
  p_program_slugs       text[],
  p_updated_since       timestamp with time zone DEFAULT NULL::timestamp WITH time zone,
  p_limit               integer                  DEFAULT 1000,
  p_include_unpublished boolean                  DEFAULT false,
  p_include_counts      boolean                  DEFAULT true,
  p_after_id            integer                  DEFAULT NULL::integer
)
  RETURNS TABLE (
    line_fit_records jsonb,
    total_count      bigint
  )
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  SET statement_timeout TO '120s'
  AS $function$
BEGIN
  RETURN QUERY
  WITH matched AS (
    SELECT f.spectrum_id, f.target_id, f.grating, f.program_slug, f.observation,
           s.spectrum_id AS spectrum_name, t.field, o.object_id AS current_object_id,
           f.z_used, f.z_source, f.z_quality, f.object_id, f.object_version,
           f.z_fit, f.z_fit_err, f.dv, f.dv_err, f.sigma_v, f.sigma_v_err, f.kin_source,
           f.n_lines, f.n_detected, f.n_broad, f.chi2, f.dof, f.lines,
           f.fit_version, f.cfpipe_version, f.f_lsf, f.spectrum_hash, f.fitted_at,
           f.created_at, f.updated_at,
           -- The record's sync timestamp: the latest of the fit row and the two
           -- parent rows its staleness flags derive from, so the client's
           -- incremental cursor (max local updated_at) advances past a
           -- re-inspection or redeploy instead of re-sending the row forever.
           GREATEST(f.updated_at, s.updated_at, o.updated_at) AS effective_updated_at,
           -- staleness vs the live inspection state: the same two functions
           -- the spectrum_line_fits_status view calls, so the mirrored record
           -- and the ledger cannot disagree.
           (o.id IS NOT NULL AND public.line_fit_stale_redshift(
              o.version, o.redshift_quality, (o.redshift)::double precision,
              f.object_version, f.z_quality, f.z_used)) AS stale_redshift,
           public.line_fit_stale_spectrum(
              s.file_hash, s.science_hash,
              f.spectrum_hash, f.spectrum_science_hash) AS stale_spectrum
    FROM spectrum_line_fits f
    JOIN spectra s ON s.id = f.spectrum_id
    LEFT JOIN targets t ON t.target_id = f.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE f.program_slug = ANY(p_program_slugs)
      -- Hide fits whose parent object was soft-deleted (same guard as
      -- get_spectra_for_sync), so the line catalog never carries a row the
      -- spectra / objects streams hide.
      AND (o.id IS NULL OR o.is_active = true)
      AND (p_include_unpublished OR s.deploy_status = 'published')
      -- Incremental: stale_redshift / stale_spectrum are derived from the
      -- parent rows, so a re-inspected object or a redeployed spectrum must
      -- re-send the fit even though the fit row itself did not change.
      AND (p_updated_since IS NULL
           OR f.updated_at > p_updated_since
           OR s.updated_at > p_updated_since
           OR o.updated_at > p_updated_since)
      AND (p_after_id IS NULL OR f.spectrum_id > p_after_id)
    ORDER BY f.spectrum_id
    LIMIT p_limit
  ),
  total AS (
    SELECT COUNT(*) AS cnt
    FROM spectrum_line_fits f
    JOIN spectra s ON s.id = f.spectrum_id
    LEFT JOIN targets t ON t.target_id = f.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE p_include_counts
      AND f.program_slug = ANY(p_program_slugs)
      AND (o.id IS NULL OR o.is_active = true)
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND (p_updated_since IS NULL
           OR f.updated_at > p_updated_since
           OR s.updated_at > p_updated_since
           OR o.updated_at > p_updated_since)
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'spectrum_id', m.spectrum_id,
        'spectrum_name', m.spectrum_name,
        'target_id', m.target_id,
        'grating', m.grating,
        'program_slug', m.program_slug,
        'observation', m.observation,
        'field', m.field,
        'current_object_id', m.current_object_id,
        'z_used', m.z_used,
        'z_source', m.z_source,
        'z_quality', m.z_quality,
        'object_id', m.object_id,
        'object_version', m.object_version,
        'z_fit', m.z_fit,
        'z_fit_err', m.z_fit_err,
        'dv', m.dv,
        'dv_err', m.dv_err,
        'sigma_v', m.sigma_v,
        'sigma_v_err', m.sigma_v_err,
        'kin_source', m.kin_source,
        'n_lines', m.n_lines,
        'n_detected', m.n_detected,
        'n_broad', m.n_broad,
        'chi2', m.chi2,
        'dof', m.dof,
        'lines', m.lines,
        'fit_version', m.fit_version,
        'cfpipe_version', m.cfpipe_version,
        'f_lsf', m.f_lsf,
        'spectrum_hash', m.spectrum_hash,
        'fitted_at', m.fitted_at,
        'stale_redshift', m.stale_redshift,
        'stale_spectrum', m.stale_spectrum,
        'created_at', m.created_at,
        'fit_updated_at', m.updated_at,
        'updated_at', m.effective_updated_at
      ) ORDER BY m.spectrum_id
    ), '[]'::jsonb) AS line_fit_records,
    (SELECT cnt FROM total) AS total_count
  FROM matched m;
END;
$function$;

CREATE OR REPLACE FUNCTION public.line_fit_stale_spectrum (
  p_file_hash         text,
  p_science_hash      text,
  p_fit_spectrum_hash text,
  p_fit_science_hash  text
)
  RETURNS boolean
  LANGUAGE sql
  IMMUTABLE
  AS $function$
  SELECT CASE
    WHEN p_science_hash IS NOT NULL AND p_fit_science_hash IS NOT NULL THEN
      regexp_replace(p_science_hash, '^sha256:', '')
        IS DISTINCT FROM regexp_replace(p_fit_science_hash, '^sha256:', '')
    ELSE
      regexp_replace(p_file_hash, '^sha256:', '')
        IS DISTINCT FROM regexp_replace(p_fit_spectrum_hash, '^sha256:', '')
  END;
$function$;

REVOKE ALL ON FUNCTION "public"."line_fit_stale_spectrum"(text, text, text, text) FROM PUBLIC, "anon";

ALTER TABLE "public"."spectrum_line_fits"
  ADD CONSTRAINT "spectrum_line_fits_spectrum_science_hash_check" CHECK (((spectrum_science_hash IS NULL) OR (spectrum_science_hash ~ '^sha256:[0-9a-f]{64}$'::text)));

CREATE VIEW "public"."spectrum_line_fits_status" WITH (security_invoker=true) AS  SELECT f.spectrum_id,
    f.target_id,
    f.grating,
    f.program_slug,
    f.observation,
    f.z_used,
    f.z_source,
    f.z_quality,
    f.object_id,
    f.object_version,
    f.fit_version,
    f.fitted_at,
    o.object_id AS current_object_id,
    o.redshift AS current_redshift,
    o.redshift_quality AS current_quality,
    o.version AS current_object_version,
    ((o.id IS NOT NULL) AND public.line_fit_stale_redshift(o.version, o.redshift_quality, (o.redshift)::double precision, f.object_version, f.z_quality, f.z_used)) AS stale_redshift,
    public.line_fit_stale_spectrum(s.file_hash, s.science_hash, f.spectrum_hash, f.spectrum_science_hash) AS stale_spectrum
   FROM (((public.spectrum_line_fits f
     JOIN public.spectra s ON ((s.id = f.spectrum_id)))
     LEFT JOIN public.targets t ON ((t.target_id = f.target_id)))
     LEFT JOIN public.objects o ON ((o.id = t.object_id)))
  WHERE ((o.id IS NULL) OR (o.is_active = true));

REVOKE ALL ON TABLE "public"."spectrum_line_fits_status" FROM "anon";

COMMENT ON COLUMN "public"."spectrum_line_fits"."spectrum_science_hash" IS 'Science-content sha256 of the spectrum the fit was made from (the SCIHASH card cfpipe nirspec linefit writes since P0b; same definition as spectra.science_hash). line_fit_stale_spectrum compares it with spectra.science_hash when both exist, so a redeploy that only rewrote volatile header cards does not read as a stale fit (P0c); NULL on products fitted before the card existed, which fall back to spectrum_hash.';

GRANT EXECUTE ON FUNCTION "public"."line_fit_stale_spectrum"(text, text, text, text) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."line_fit_stale_spectrum"(text, text, text, text) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."line_fit_stale_spectrum"(text, text, text, text) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."line_fit_stale_spectrum"(text, text, text, text) TO "service_role";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."spectrum_line_fits_status" TO "authenticated";

REVOKE ALL ON TABLE "public"."spectrum_line_fits_status" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."spectrum_line_fits_status" TO "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."spectrum_line_fits_status" TO "service_role";
