-- =============================================================================
-- CAMPFIRE Supabase Schema: Views
-- =============================================================================
-- Canonical source of truth for all views and materialized views.
-- Do NOT read migration files to understand current signatures or behavior.
--
-- Workflow: edit here → run apply.sh → supabase db diff → commit migration
-- =============================================================================


-- ============================================================
-- MATERIALIZED VIEWS
-- ============================================================

-- 1. mv_filter_options
--    Cached distinct filter options (fields, observations, gratings).
--    Refresh after data deployments using refresh_filter_options().
DROP MATERIALIZED VIEW IF EXISTS public.mv_filter_options;

CREATE MATERIALIZED VIEW public.mv_filter_options AS
SELECT 1 AS id,
    ARRAY(SELECT DISTINCT targets.field FROM public.targets ORDER BY targets.field) AS fields,
    ARRAY(SELECT DISTINCT targets.observation FROM public.targets WHERE targets.observation IS NOT NULL ORDER BY targets.observation) AS observations,
    ARRAY(SELECT DISTINCT spectra.grating FROM public.spectra ORDER BY spectra.grating) AS gratings
WITH DATA;

CREATE UNIQUE INDEX mv_filter_options_id ON public.mv_filter_options USING btree (id);

GRANT ALL ON TABLE public.mv_filter_options TO anon;
GRANT ALL ON TABLE public.mv_filter_options TO authenticated;
GRANT ALL ON TABLE public.mv_filter_options TO service_role;


-- 2. mv_programs_overview
--    Pre-aggregated program stats (target counts, gratings, fields, observations).
--    Refresh after data deployments using refresh_programs_overview().
DROP MATERIALIZED VIEW IF EXISTS public.mv_programs_overview;

CREATE MATERIALIZED VIEW public.mv_programs_overview AS
SELECT
    p.slug,
    p.program_name,
    p.pi_name,
    p.description,
    p.is_public,
    p.cycle,
    COALESCE(stats.target_count, 0)::bigint AS target_count,
    COALESCE(stats.gratings, ARRAY[]::text[]) AS gratings,
    COALESCE(stats.fields, ARRAY[]::text[]) AS fields,
    COALESCE(stats.observations, ARRAY[]::text[]) AS observations,
    COALESCE(pids.jwst_pids, ARRAY[]::integer[]) AS jwst_pids
FROM programs p
LEFT JOIN (
    SELECT t.program_slug,
        COUNT(DISTINCT t.target_id) AS target_count,
        ARRAY_AGG(DISTINCT s.grating ORDER BY s.grating)
            FILTER (WHERE s.grating IS NOT NULL) AS gratings,
        ARRAY_AGG(DISTINCT t.field ORDER BY t.field) AS fields,
        ARRAY_AGG(DISTINCT t.observation ORDER BY t.observation) AS observations
    FROM targets t
    LEFT JOIN spectra s ON s.target_id = t.target_id
    GROUP BY t.program_slug
) stats ON p.slug = stats.program_slug
LEFT JOIN (
    SELECT program_slug,
        ARRAY_AGG(DISTINCT jwst_program_id ORDER BY jwst_program_id) AS jwst_pids
    FROM observations
    GROUP BY program_slug
) pids ON p.slug = pids.program_slug
WITH DATA;

CREATE UNIQUE INDEX mv_programs_overview_slug ON public.mv_programs_overview (slug);

GRANT SELECT ON public.mv_programs_overview TO authenticated;


-- ============================================================
-- VIEWS
-- ============================================================

-- 3. target_flag_summary
--    Expands bitmask flag columns into label arrays via cross join
--    with flag_definitions.
DROP VIEW IF EXISTS public.target_flag_summary;
CREATE VIEW public.target_flag_summary AS
SELECT
    t.id,
    t.target_id,
    array_agg(DISTINCT fd.label) FILTER (WHERE fd.category = 'spectral_features' AND (t.spectral_features & fd.value) > 0) AS spectral_features_labels,
    array_agg(DISTINCT fd.label) FILTER (WHERE fd.category = 'dq_flags' AND (t.dq_flags & fd.value) > 0) AS dq_flags_labels
FROM public.targets t
CROSS JOIN public.flag_definitions fd
GROUP BY t.id, t.target_id;

GRANT ALL ON TABLE public.target_flag_summary TO anon;
GRANT ALL ON TABLE public.target_flag_summary TO authenticated;
GRANT ALL ON TABLE public.target_flag_summary TO service_role;


-- 4. targets_with_flags
--    Joins targets with redshift quality label/icon/color from flag_definitions.
DROP VIEW IF EXISTS public.targets_with_flags;
CREATE VIEW public.targets_with_flags AS
SELECT
    t.id,
    t.target_id,
    t.program_slug,
    t.field,
    t.ra,
    t.dec,
    t.redshift_auto AS redshift,
    t.redshift_quality,
    t.spectral_features,
    t.dq_flags,
    t.created_at,
    t.updated_at,
    rq.label AS redshift_quality_label,
    rq.icon AS redshift_quality_icon,
    rq.color AS redshift_quality_color
FROM public.targets t
LEFT JOIN public.flag_definitions rq ON (rq.category = 'redshift_quality' AND rq.value = t.redshift_quality);

GRANT ALL ON TABLE public.targets_with_flags TO anon;
GRANT ALL ON TABLE public.targets_with_flags TO authenticated;
GRANT ALL ON TABLE public.targets_with_flags TO service_role;


-- 5. nircam_reduction_progress
--    Aggregated reduction progress per field/filter for the admin dashboard.
DROP VIEW IF EXISTS public.nircam_reduction_progress;
CREATE VIEW public.nircam_reduction_progress AS
SELECT
    field,
    filter,
    count(*) AS total,
    count(*) FILTER (WHERE stage = 'uncal') AS at_uncal,
    count(*) FILTER (WHERE stage = 'rate') AS at_rate,
    count(*) FILTER (WHERE stage = 'cal') AS at_cal,
    count(*) FILTER (WHERE stage = 'jhat') AS at_jhat,
    count(*) FILTER (WHERE stage = 'crf') AS at_crf,
    count(*) FILTER (WHERE review_status = 'pending') AS pending_review,
    count(*) FILTER (WHERE review_status = 'approved') AS approved,
    count(*) FILTER (WHERE review_status = 'excluded') AS excluded,
    count(*) FILTER (WHERE masking = 'needed') AS needs_masking,
    count(*) FILTER (WHERE correction = 'needed') AS needs_correction
FROM public.nircam_exposures
GROUP BY field, filter;

GRANT SELECT ON public.nircam_reduction_progress TO authenticated;
