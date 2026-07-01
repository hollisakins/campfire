-- Fast path for object_scoped_aggregates (perf, issue #103).
--
-- The helper recomputes an object's aggregate columns (programs, gratings,
-- observations, n_targets, n_spectra, max_snr, max_exposure_time) scoped to the
-- caller's accessible programs, so a partial-access viewer of a mixed-program
-- object does not see proprietary members. It is invoked once per returned row
-- (LEFT JOIN LATERAL) by get_objects_for_sync, get_filtered_objects_paginated,
-- and get_csv_export_objects, and each invocation scanned targets + spectra --
-- the dominant per-page cost of the /sync/objects RPC at ~30k objects.
--
-- For the common full-access case (the caller can access every program the
-- object belongs to) and the published-only gate (NOT p_include_unpublished),
-- that recompute is provably identical to the aggregate columns already stored
-- on the object: both are the deploy-time builder's aggregation over published
-- members, kept in lockstep by reconcile_field_objects(), and restricting to a
-- superset of the object's programs drops nothing. So `o.programs <@
-- p_program_slugs` is exactly the "recompute == stored" condition, and the fast
-- path returns the stored columns via a single PK lookup instead of two scans.
-- Partial-access or draft-inclusive callers fall through to the unchanged
-- recompute, preserving the anti-leak scoping guarded by
-- supabase/tests/check_object_aggregate_scoping.sql.
--
-- Signature is unchanged (CREATE OR REPLACE), so existing GRANTs are preserved.

CREATE OR REPLACE FUNCTION public.object_scoped_aggregates(
  p_object_id INTEGER,
  p_program_slugs TEXT[],
  p_include_unpublished BOOLEAN DEFAULT false
)
RETURNS TABLE(
  programs          TEXT[],
  gratings          TEXT[],
  observations      TEXT[],
  n_targets         INTEGER,
  n_spectra         INTEGER,
  max_snr           DOUBLE PRECISION,
  max_exposure_time DOUBLE PRECISION
)
LANGUAGE plpgsql STABLE
AS $$
BEGIN
  IF NOT p_include_unpublished THEN
    RETURN QUERY
    SELECT o.programs, o.gratings, o.observations,
           o.n_targets, o.n_spectra, o.max_snr, o.max_exposure_time
    FROM objects o
    WHERE o.id = p_object_id
      AND o.programs <@ p_program_slugs;
    IF FOUND THEN
      RETURN;
    END IF;
  END IF;

  RETURN QUERY
  WITH m AS (
    SELECT t.target_id, t.program_slug, t.observation
    FROM targets t
    WHERE t.object_id = p_object_id
      AND t.program_slug = ANY(p_program_slugs)
      AND (p_include_unpublished OR t.has_published_spectrum)
  ),
  sp AS (
    SELECT s.grating, s.signal_to_noise, s.exposure_time
    FROM spectra s
    WHERE s.target_id IN (SELECT target_id FROM m)
      AND (p_include_unpublished OR s.deploy_status = 'published')
  )
  SELECT
    COALESCE((SELECT array_agg(DISTINCT m.program_slug ORDER BY m.program_slug) FROM m), '{}')::text[],
    COALESCE((SELECT array_agg(DISTINCT sp.grating ORDER BY sp.grating) FROM sp WHERE sp.grating IS NOT NULL), '{}')::text[],
    COALESCE((SELECT array_agg(DISTINCT m.observation ORDER BY m.observation) FROM m WHERE m.observation IS NOT NULL), '{}')::text[],
    (SELECT COUNT(*) FROM m)::integer,
    (SELECT COUNT(*) FROM sp)::integer,
    (SELECT MAX(sp.signal_to_noise) FROM sp),
    (SELECT MAX(sp.exposure_time) FROM sp);
END;
$$;
