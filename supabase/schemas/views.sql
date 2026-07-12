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

-- Published-only (B1): this is a GLOBAL matview with no per-viewer scope, so it
-- is restricted to published data for everyone. fields/observations are derived
-- from targets via has_published_spectrum; gratings from spectra via
-- deploy_status. Admins get draft filter options through a live query elsewhere,
-- not this matview. In B1 every predicate is a no-op (nothing is unpublished yet).
CREATE MATERIALIZED VIEW public.mv_filter_options AS
SELECT 1 AS id,
    ARRAY(SELECT DISTINCT targets.field FROM public.targets WHERE targets.has_published_spectrum ORDER BY targets.field) AS fields,
    ARRAY(SELECT DISTINCT targets.observation FROM public.targets WHERE targets.observation IS NOT NULL AND targets.has_published_spectrum ORDER BY targets.observation) AS observations,
    ARRAY(SELECT DISTINCT spectra.grating FROM public.spectra WHERE spectra.deploy_status = 'published' ORDER BY spectra.grating) AS gratings
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
    -- Published-only (B1): restrict the grating aggregation to published
    -- spectra. Predicate lives in the LEFT JOIN ON clause so targets with no
    -- published spectrum are still counted (just contribute no gratings).
    -- No-op in B1 (nothing is unpublished yet); guards B2 draft data.
    LEFT JOIN spectra s ON s.target_id = t.target_id AND s.deploy_status = 'published'
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
-- security_invoker (B1): this was a plain (definer) view that bypassed the
-- spectra RLS policy. Flip to invoker semantics so the caller's RLS applies, and
-- add a published predicate so draft spectra are hidden from non-admins. In B1
-- every spectrum is published, so this is a no-op; it guards B2 draft data.
CREATE VIEW public.spectrum_flag_summary
WITH (security_invoker = true) AS
SELECT
    s.id,
    s.target_id,
    s.grating,
    array_agg(DISTINCT fd.label) FILTER (WHERE fd.category = 'dq_flags' AND (s.dq_flags & fd.value) > 0) AS dq_flags_labels
FROM public.spectra s
CROSS JOIN public.flag_definitions fd
WHERE (s.deploy_status = 'published' OR public.is_admin())
GROUP BY s.id, s.target_id, s.grating;

GRANT ALL ON TABLE public.spectrum_flag_summary TO anon;
GRANT ALL ON TABLE public.spectrum_flag_summary TO authenticated;
GRANT ALL ON TABLE public.spectrum_flag_summary TO service_role;


-- 4. targets_with_flags — dropped in Phase D (no consumers after the
-- targets-list view was removed).
DROP VIEW IF EXISTS public.targets_with_flags;


-- 5. nircam_reduction_progress
--    Aggregated reduction progress per field/filter for the admin dashboard.
--    Bucketed by the canonical NIRCam pipeline step names (matching
--    `cfpipe nircam <step>` and the campfire_pipeline.common.cfp.CFP_KEYS
--    chain). `uncal` means the raw exposure exists but no canonical file
--    has been produced yet by `detector1`.
DROP VIEW IF EXISTS public.nircam_reduction_progress;
-- security_invoker (B1): this was a plain (definer) view that bypassed the
-- admin-only RLS on nircam_exposures, leaking QA aggregates to all authenticated
-- users. Flip to invoker semantics so the nircam_exposures RLS policy applies.
-- (NIRCam deploy_status gating is deferred to B2; this is only the leak fix.)
CREATE VIEW public.nircam_reduction_progress
WITH (security_invoker = true) AS
SELECT
    field,
    filter,
    count(*) AS total,
    -- Per-step distribution: count of exposures whose highest-completed
    -- CFP key is exactly this step. (Diag_striping and wcs_shift are
    -- opt-in; expect zeros for fields that don't enable them.)
    count(*) FILTER (WHERE stage = 'uncal')         AS at_uncal,
    count(*) FILTER (WHERE stage = 'detector1')     AS at_detector1,
    count(*) FILTER (WHERE stage = 'persistence')   AS at_persistence,
    count(*) FILTER (WHERE stage = 'wisp')          AS at_wisp,
    count(*) FILTER (WHERE stage = 'image2')        AS at_image2,
    count(*) FILTER (WHERE stage = 'edge')          AS at_edge,
    -- bkg unifies the former striping/sky/variance stages
    count(*) FILTER (WHERE stage = 'bkg')           AS at_bkg,
    count(*) FILTER (WHERE stage = 'diag_striping') AS at_diag_striping,
    count(*) FILTER (WHERE stage = 'wcs_shift')     AS at_wcs_shift,
    count(*) FILTER (WHERE stage = 'preview')       AS at_preview,
    count(*) FILTER (WHERE stage = 'jhat')          AS at_jhat,
    count(*) FILTER (WHERE stage = 'apply_mask')    AS at_apply_mask,
    count(*) FILTER (WHERE stage = 'bad_pixel')     AS at_bad_pixel,
    count(*) FILTER (WHERE stage = 'outlier')       AS at_outlier,
    -- Triage summary
    count(*) FILTER (WHERE review_status = 'pending')  AS pending_review,
    count(*) FILTER (WHERE review_status = 'approved') AS approved,
    count(*) FILTER (WHERE review_status = 'excluded') AS excluded,
    count(*) FILTER (WHERE correction = 'needed')      AS needs_correction
FROM public.nircam_exposures
GROUP BY field, filter;

GRANT SELECT ON public.nircam_reduction_progress TO authenticated;
