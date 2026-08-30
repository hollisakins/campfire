-- Admin dashboard redesign: rework nircam_reduction_progress around
-- inspection triage. Grouping gains detector (for per-detector pending
-- quick-filter links), the per-stage at_<step> distribution columns are
-- dropped (no signal — whole filters sit at the same stage), and a masked
-- count (mask_regions IS NOT NULL) is added.
-- Hand-authored to match `supabase db diff` output for a view swap.

drop view if exists "public"."nircam_reduction_progress";

create or replace view "public"."nircam_reduction_progress" with (security_invoker = true) as  SELECT field,
    filter,
    detector,
    count(*) AS total,
    count(*) FILTER (WHERE (review_status = 'pending'::text)) AS pending_review,
    count(*) FILTER (WHERE (review_status = 'approved'::text)) AS approved,
    count(*) FILTER (WHERE (review_status = 'excluded'::text)) AS excluded,
    count(*) FILTER (WHERE (mask_regions IS NOT NULL)) AS masked,
    count(*) FILTER (WHERE (correction = 'needed'::text)) AS needs_correction
   FROM public.nircam_exposures
  GROUP BY field, filter, detector;
