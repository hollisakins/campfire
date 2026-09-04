-- Perf T1-2 (#498, epic #515): restore SQL inlining of object_scoped_aggregates.
--
-- The helper became LANGUAGE plpgsql when the deploy_status fast path landed
-- (#217, 84ca3953). plpgsql is not inlinable, so every per-row LATERAL call in
-- the catalog RPCs re-ran the spectra RLS subplan (~35 k rows) as a separate
-- statement: get_filtered_objects_paginated page 1/50 measured 2.0–2.7 s /
-- 460–660 k buffers on prod (46 ms in June), with three shapes hitting the 8 s
-- statement timeout. Same semantics (fast path + scoped recompute), expressed
-- as one SELECT — `stored` CTE + UNION ALL gated on NOT EXISTS — so it inlines
-- again. The `SET plan_cache_mode` clause (#490) is dropped: a SET blocks
-- inlining and an inlined body has no nested statements to re-plan.
--
-- Hand-authored (CREATE OR REPLACE, signature unchanged): no local Docker for
-- `supabase db diff`. Matches supabase/schemas/functions.sql. Validated on a
-- throwaway PG 17 with the real schema: the three supabase/tests/check_*_scoping
-- guards pass and 114 old-vs-new comparisons (8 fixture objects × slug sets ×
-- include_unpublished, as owner and as an RLS-restricted viewer) are identical.

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
LANGUAGE sql STABLE
ROWS 1
AS $$
  WITH stored AS MATERIALIZED (
    SELECT o.programs, o.gratings, o.observations,
           o.n_targets, o.n_spectra, o.max_snr, o.max_exposure_time
    FROM objects o
    WHERE o.id = p_object_id
      AND NOT p_include_unpublished
      AND o.programs <@ p_program_slugs
      AND NOT EXISTS (
        SELECT 1
        FROM targets t
        JOIN spectra s ON s.target_id = t.target_id
        WHERE t.object_id = p_object_id
          AND s.deploy_status <> 'published'
      )
      AND o.n_spectra = (
        SELECT COUNT(*)::integer
        FROM targets t
        JOIN spectra s ON s.target_id = t.target_id
        WHERE t.object_id = p_object_id
      )
  ),
  m AS (
    SELECT t.target_id, t.program_slug, t.observation
    FROM targets t
    WHERE t.object_id = p_object_id
      AND t.program_slug = ANY(p_program_slugs)
      -- B1: only count targets that contribute a published spectrum so
      -- n_targets / programs / observations don't include draft-only members.
      AND (p_include_unpublished OR t.has_published_spectrum)
  ),
  sp AS (
    SELECT s.grating, s.signal_to_noise, s.exposure_time
    FROM spectra s
    WHERE s.target_id IN (SELECT target_id FROM m)
      AND (p_include_unpublished OR s.deploy_status = 'published')
  )
  SELECT st.programs, st.gratings, st.observations,
         st.n_targets, st.n_spectra, st.max_snr, st.max_exposure_time
  FROM stored st
  UNION ALL
  SELECT
    COALESCE((SELECT array_agg(DISTINCT m.program_slug ORDER BY m.program_slug) FROM m), '{}')::text[],
    COALESCE((SELECT array_agg(DISTINCT sp.grating ORDER BY sp.grating) FROM sp WHERE sp.grating IS NOT NULL), '{}')::text[],
    COALESCE((SELECT array_agg(DISTINCT m.observation ORDER BY m.observation) FROM m WHERE m.observation IS NOT NULL), '{}')::text[],
    (SELECT COUNT(*) FROM m)::integer,
    (SELECT COUNT(*) FROM sp)::integer,
    (SELECT MAX(sp.signal_to_noise) FROM sp),
    (SELECT MAX(sp.exposure_time) FROM sp)
  WHERE NOT EXISTS (SELECT 1 FROM stored);
$$;

