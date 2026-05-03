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
--    Pre-aggregated program stats (target counts, gratings, fields, observations,
--    last full-deployment timestamp). Refresh via refresh_programs_overview().
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
    COALESCE(pids.jwst_pids, ARRAY[]::integer[]) AS jwst_pids,
    COALESCE(pids.n_observations, 0)::bigint AS n_observations,
    last_red.last_reduced_at
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
        ARRAY_AGG(DISTINCT jwst_program_id ORDER BY jwst_program_id) AS jwst_pids,
        COUNT(*)::bigint AS n_observations
    FROM observations
    GROUP BY program_slug
) pids ON p.slug = pids.program_slug
LEFT JOIN (
    SELECT o.program_slug,
        MAX(d.reduced_at) AS last_reduced_at
    FROM observations o
    JOIN deployments d ON d.observation = o.name
    WHERE d.source_ids_filter IS NULL
    GROUP BY o.program_slug
) last_red ON p.slug = last_red.program_slug
WITH DATA;

CREATE UNIQUE INDEX mv_programs_overview_slug ON public.mv_programs_overview (slug);

GRANT SELECT ON public.mv_programs_overview TO authenticated;


-- ============================================================
-- VIEWS
-- ============================================================

-- 3. spectrum_flag_summary
--    Expands per-spectrum dq_flags bitmask into a label array via cross join
--    with flag_definitions. Replaces the Phase-D-deprecated target_flag_summary
--    (which also covered spectral_features — that flag category is dropped).
DROP VIEW IF EXISTS public.spectrum_flag_summary;
DROP VIEW IF EXISTS public.target_flag_summary;
CREATE VIEW public.spectrum_flag_summary AS
SELECT
    s.id,
    s.target_id,
    s.grating,
    array_agg(DISTINCT fd.label) FILTER (WHERE fd.category = 'dq_flags' AND (s.dq_flags & fd.value) > 0) AS dq_flags_labels
FROM public.spectra s
CROSS JOIN public.flag_definitions fd
GROUP BY s.id, s.target_id, s.grating;

GRANT ALL ON TABLE public.spectrum_flag_summary TO anon;
GRANT ALL ON TABLE public.spectrum_flag_summary TO authenticated;
GRANT ALL ON TABLE public.spectrum_flag_summary TO service_role;


-- 4. targets_with_flags — dropped in Phase D (no consumers after the
-- targets-list view was removed).
DROP VIEW IF EXISTS public.targets_with_flags;
