-- Emission-line catalog (docs/design-emission-line-fitting.md).
--
-- Hand-authored: no local Docker for `supabase db diff`. Every definition
-- below is copied verbatim from supabase/schemas/ (the source of truth):
--   tables.sql    — spectrum_line_fits (+ PK, FK to spectra, grants, comments)
--   indexes.sql   — idx_spectrum_line_fits_*
--   triggers.sql  — bump_spectrum_line_fits_updated_at_trigger
--   policies.sql  — RLS: select follows the parent spectrum; admin writes
--   functions.sql — get_line_fits_for_sync (keyset bulk fetch for the Python client)
--   views.sql     — spectrum_line_fits_status (staleness vs live inspection state)
--
-- One row per spectrum, written by `campfire deploy lines` / `campfire deploy
-- --obs` from the `_lines.fits` products of `cfpipe nirspec linefit`, which fits
-- at the INSPECTED redshift materialized by `campfire pull`. Additive: no
-- existing object changes.

-- ============================================================================
-- Table
-- ============================================================================

CREATE TABLE IF NOT EXISTS "public"."spectrum_line_fits" (
    "spectrum_id" integer NOT NULL,
    "target_id" "text" NOT NULL,
    "grating" "text" NOT NULL,
    "program_slug" "text" NOT NULL,
    "observation" "text" NOT NULL,
    "z_used" double precision NOT NULL,
    "z_source" "text" DEFAULT 'inspected'::"text" NOT NULL,
    "z_quality" integer DEFAULT 0 NOT NULL,
    "object_id" "text",
    "object_version" integer,
    "z_fit" double precision,
    "z_fit_err" double precision,
    "dv" double precision,
    "dv_err" double precision,
    "sigma_v" double precision,
    "sigma_v_err" double precision,
    "kin_source" "text",
    "n_lines" integer DEFAULT 0 NOT NULL,
    "n_detected" integer DEFAULT 0 NOT NULL,
    "n_broad" integer DEFAULT 0 NOT NULL,
    "chi2" double precision,
    "dof" integer,
    "lines" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "fit_version" "text" NOT NULL,
    "cfpipe_version" "text",
    "f_lsf" double precision,
    "spectrum_hash" "text",
    "fitted_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "spectrum_line_fits_z_source_check" CHECK (("z_source" = ANY (ARRAY['inspected'::"text", 'auto'::"text"]))),
    CONSTRAINT "spectrum_line_fits_z_quality_check" CHECK ((("z_quality" >= 0) AND ("z_quality" <= 4)))
);

ALTER TABLE "public"."spectrum_line_fits" OWNER TO "postgres";

COMMENT ON TABLE "public"."spectrum_line_fits" IS 'Emission-line fluxes per spectrum, measured at the inspected redshift (cfpipe nirspec linefit -> campfire deploy lines). One row per spectrum, replaced on re-fit; per-line values in `lines` jsonb keyed by pipeline line name. z_used / z_quality / object_version record the inspection state the fit used (see spectrum_line_fits_status for staleness).';

COMMENT ON COLUMN "public"."spectrum_line_fits"."lines" IS 'Per-line records keyed by line name: {label, component (narrow|broad), wave_rest [vacuum A], wave_obs [um], flux, flux_err [erg/s/cm2], snr, ew_rest, ew_rest_err [A], cont, cont_err [erg/s/cm2/A], dv, dv_err, sigma_v, sigma_v_err, sigma_lsf_kms [km/s], complex, chi2, dof, npix, flags (bitmask: 1 tied, 2 blended, 4 blend, 8 edge, 16 kin_global, 32 kin_default, 64 broad, 128 no_continuum, 256 fit_failed, 512 masked, 1024 sigma_unresolved), blend_into, tied_to}. NaN is null; a `blended` line carries no flux of its own (its blend primary reports the total).';

COMMENT ON COLUMN "public"."spectrum_line_fits"."z_source" IS 'inspected = the portal redshift (objects.redshift at quality >= the pipeline min_quality); auto = the pipeline zfit redshift (QA fits, deployed only with --allow-auto-z).';

ALTER TABLE ONLY "public"."spectrum_line_fits"
    ADD CONSTRAINT "spectrum_line_fits_pkey" PRIMARY KEY ("spectrum_id");

ALTER TABLE ONLY "public"."spectrum_line_fits"
    ADD CONSTRAINT "spectrum_line_fits_spectrum_id_fkey" FOREIGN KEY ("spectrum_id") REFERENCES "public"."spectra"("id") ON DELETE CASCADE;

GRANT ALL ON TABLE "public"."spectrum_line_fits" TO "anon";
GRANT ALL ON TABLE "public"."spectrum_line_fits" TO "authenticated";
GRANT ALL ON TABLE "public"."spectrum_line_fits" TO "service_role";

-- ============================================================================
-- Indexes
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_spectrum_line_fits_observation
    ON public.spectrum_line_fits USING btree (observation);

CREATE INDEX IF NOT EXISTS idx_spectrum_line_fits_program_slug
    ON public.spectrum_line_fits USING btree (program_slug);

CREATE INDEX IF NOT EXISTS idx_spectrum_line_fits_target_id
    ON public.spectrum_line_fits USING btree (target_id);

CREATE INDEX IF NOT EXISTS idx_spectrum_line_fits_updated_at
    ON public.spectrum_line_fits USING btree (updated_at);

-- ============================================================================
-- Trigger (updated_at bump; reuses bump_spectra_updated_at)
-- ============================================================================

DROP TRIGGER IF EXISTS bump_spectrum_line_fits_updated_at_trigger ON public.spectrum_line_fits;
CREATE TRIGGER bump_spectrum_line_fits_updated_at_trigger
  BEFORE UPDATE ON public.spectrum_line_fits
  FOR EACH ROW EXECUTE FUNCTION public.bump_spectra_updated_at();

-- ============================================================================
-- RLS
-- ============================================================================

ALTER TABLE spectrum_line_fits ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "select_spectrum_line_fits_by_spectrum" ON spectrum_line_fits;
CREATE POLICY "select_spectrum_line_fits_by_spectrum"
  ON spectrum_line_fits FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.spectra s
      WHERE s.id = spectrum_line_fits.spectrum_id
    )
  );

DROP POLICY IF EXISTS "admin_insert_spectrum_line_fits" ON spectrum_line_fits;
CREATE POLICY "admin_insert_spectrum_line_fits"
  ON spectrum_line_fits FOR INSERT TO authenticated
  WITH CHECK ((SELECT public.is_admin()));

DROP POLICY IF EXISTS "admin_update_spectrum_line_fits" ON spectrum_line_fits;
CREATE POLICY "admin_update_spectrum_line_fits"
  ON spectrum_line_fits FOR UPDATE TO authenticated
  USING ((SELECT public.is_admin()))
  WITH CHECK ((SELECT public.is_admin()));

DROP POLICY IF EXISTS "admin_delete_spectrum_line_fits" ON spectrum_line_fits;
CREATE POLICY "admin_delete_spectrum_line_fits"
  ON spectrum_line_fits FOR DELETE TO authenticated
  USING ((SELECT public.is_admin()));

-- ============================================================================
-- get_line_fits_for_sync
-- ============================================================================

DROP FUNCTION IF EXISTS public.get_line_fits_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, INTEGER);

CREATE OR REPLACE FUNCTION public.get_line_fits_for_sync(
  p_program_slugs TEXT[],
  p_updated_since TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_include_unpublished BOOLEAN DEFAULT false,
  p_include_counts BOOLEAN DEFAULT TRUE,
  p_after_id INTEGER DEFAULT NULL
)
RETURNS TABLE(line_fit_records JSONB, total_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
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
           -- staleness vs the live inspection state (see spectrum_line_fits_status)
           (o.id IS NOT NULL AND (
              o.version IS DISTINCT FROM f.object_version
              OR o.redshift_quality IS DISTINCT FROM f.z_quality
              OR o.redshift IS NULL
              OR abs((o.redshift)::double precision - f.z_used) > 1e-5)) AS stale_redshift,
           -- both sides are 'sha256:<hex>'; strip the scheme defensively so a
           -- bare digest from an older product still compares.
           (regexp_replace(s.file_hash, '^sha256:', '')
              IS DISTINCT FROM regexp_replace(f.spectrum_hash, '^sha256:', '')) AS stale_spectrum
    FROM spectrum_line_fits f
    JOIN spectra s ON s.id = f.spectrum_id
    LEFT JOIN targets t ON t.target_id = f.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE f.program_slug = ANY(p_program_slugs)
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
$$;

GRANT EXECUTE ON FUNCTION public.get_line_fits_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, INTEGER) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_line_fits_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, INTEGER) TO service_role;

-- ============================================================================
-- spectrum_line_fits_status
-- ============================================================================

DROP VIEW IF EXISTS public.spectrum_line_fits_status;

CREATE VIEW public.spectrum_line_fits_status
WITH (security_invoker = true) AS
SELECT f.spectrum_id,
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
       (o.id IS NOT NULL AND (
          o.version IS DISTINCT FROM f.object_version
          OR o.redshift_quality IS DISTINCT FROM f.z_quality
          OR o.redshift IS NULL
          OR abs((o.redshift)::double precision - f.z_used) > 1e-5)) AS stale_redshift,
       (regexp_replace(s.file_hash, '^sha256:', '')
          IS DISTINCT FROM regexp_replace(f.spectrum_hash, '^sha256:', '')) AS stale_spectrum
FROM public.spectrum_line_fits f
JOIN public.spectra s ON s.id = f.spectrum_id
LEFT JOIN public.targets t ON t.target_id = f.target_id
LEFT JOIN public.objects o ON o.id = t.object_id;

GRANT ALL ON TABLE public.spectrum_line_fits_status TO anon;
GRANT ALL ON TABLE public.spectrum_line_fits_status TO authenticated;
GRANT ALL ON TABLE public.spectrum_line_fits_status TO service_role;
