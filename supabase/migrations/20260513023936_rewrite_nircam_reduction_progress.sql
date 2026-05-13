-- Widen nircam_reduction_progress to the canonical pipeline step vocabulary
-- (campfire_pipeline.common.cfp.CFP_KEYS). The old 5-value enum
-- (uncal, rate, cal, jhat, crf) is replaced by a per-step bucket count
-- across all 16 NIRCam process + combine steps. `stage` column type is
-- unchanged (text); only the view changes.

DROP VIEW IF EXISTS public.nircam_reduction_progress;

CREATE VIEW public.nircam_reduction_progress AS
SELECT
    field,
    filter,
    count(*) AS total,
    count(*) FILTER (WHERE stage = 'uncal')         AS at_uncal,
    count(*) FILTER (WHERE stage = 'detector1')     AS at_detector1,
    count(*) FILTER (WHERE stage = 'persistence')   AS at_persistence,
    count(*) FILTER (WHERE stage = 'wisp')          AS at_wisp,
    count(*) FILTER (WHERE stage = 'striping')      AS at_striping,
    count(*) FILTER (WHERE stage = 'image2')        AS at_image2,
    count(*) FILTER (WHERE stage = 'edge')          AS at_edge,
    count(*) FILTER (WHERE stage = 'sky')           AS at_sky,
    count(*) FILTER (WHERE stage = 'diag_striping') AS at_diag_striping,
    count(*) FILTER (WHERE stage = 'variance')      AS at_variance,
    count(*) FILTER (WHERE stage = 'wcs_shift')     AS at_wcs_shift,
    count(*) FILTER (WHERE stage = 'preview')       AS at_preview,
    count(*) FILTER (WHERE stage = 'jhat')          AS at_jhat,
    count(*) FILTER (WHERE stage = 'apply_mask')    AS at_apply_mask,
    count(*) FILTER (WHERE stage = 'bad_pixel')     AS at_bad_pixel,
    count(*) FILTER (WHERE stage = 'outlier')       AS at_outlier,
    count(*) FILTER (WHERE review_status = 'pending')  AS pending_review,
    count(*) FILTER (WHERE review_status = 'approved') AS approved,
    count(*) FILTER (WHERE review_status = 'excluded') AS excluded,
    count(*) FILTER (WHERE masking = 'needed')         AS needs_masking,
    count(*) FILTER (WHERE correction = 'needed')      AS needs_correction
FROM public.nircam_exposures
GROUP BY field, filter;

GRANT SELECT ON public.nircam_reduction_progress TO authenticated;
