-- Unify the NIRCam striping/sky/variance stages into a single "bkg" stage.
--
-- The pipeline replaced the striping (CFP_1F) + sky (CFP_SKY) + variance
-- (CFP_VAR) steps with one `bkg` step (CFP_BKG); the deploy layer now reports
-- stage='bkg' for those exposures. Redefine nircam_reduction_progress to drop
-- the at_striping / at_sky / at_variance columns and add at_bkg. The column set
-- changes, so the view is dropped and recreated (CREATE OR REPLACE cannot drop
-- columns).
--
-- NOTE: hand-authored (no supabase CLI in the authoring environment). Verify
-- locally with `supabase db reset && supabase db diff` — it should report no
-- drift against supabase/schemas/views.sql.
--
-- Historical rows that still carry stage='striping'|'sky'|'variance' are left
-- as-is; they re-resolve to 'bkg' on the next deploy. No data backfill.

DROP VIEW IF EXISTS public.nircam_reduction_progress;

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
    count(*) FILTER (WHERE masking = 'needed')         AS needs_masking,
    count(*) FILTER (WHERE correction = 'needed')      AS needs_correction
FROM public.nircam_exposures
GROUP BY field, filter;

GRANT SELECT ON public.nircam_reduction_progress TO authenticated;
