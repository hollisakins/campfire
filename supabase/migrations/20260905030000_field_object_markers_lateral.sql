-- Perf follow-up to T1-6 (#502) on epic #515: get_field_object_markers
-- aggregates its page's members with LEFT JOIN LATERAL instead of set-based
-- CTEs joined back to the page.
--
-- Why. Under RLS the planner cannot estimate `programs &&
-- accessible_program_slugs()` (an InitPlan), guesses ~1 % selectivity, and
-- estimated the 5000-row page CTE at 9 rows; it then joined the member and
-- spectra aggregates with nested loops over materialized subqueries,
-- discarding 12.5 M rows per join. Measured on prod as an admin session on
-- COSMOS: 5.9 s for the first page, and later pages hit the 8 s
-- statement timeout (PostgREST 57014), which the map's pagination loop
-- turned into minutes. The LATERAL form does one index probe per page row
-- regardless of the estimate: 0.24 s for the same page. Same filters, same
-- output columns; only the join shape changes. Signature unchanged.
--
-- Hand-authored: no local Docker for `supabase db diff`. The function body
-- is copied verbatim from supabase/schemas/functions.sql (the source of
-- truth). CREATE OR REPLACE keeps the existing grants.

CREATE OR REPLACE FUNCTION public.get_field_object_markers(
  p_field TEXT,
  p_include_unpublished BOOLEAN DEFAULT false,
  p_cursor TEXT DEFAULT NULL,
  p_page_size INTEGER DEFAULT 5000
)
RETURNS TABLE (
  object_id           TEXT,
  ra                  DOUBLE PRECISION,
  "dec"               DOUBLE PRECISION,
  redshift            DOUBLE PRECISION,
  redshift_quality    INTEGER,
  field               TEXT,
  n_targets           INTEGER,
  n_spectra           INTEGER,
  programs            TEXT[],
  member_target_ids   TEXT[]
)
LANGUAGE sql STABLE
AS $$
  -- programs, n_targets, n_spectra and member_target_ids are scoped to the
  -- caller's accessible programs so mixed-program objects don't leak proprietary
  -- member metadata on the map. redshift / redshift_quality stay visible (object
  -- science, per the access policy). Object row visibility is enforced by RLS
  -- (programs && accessible); this function is SECURITY INVOKER and additionally
  -- gates the member aggregates by an explicit accessible_program_slugs()
  -- filter. The logic mirrors object_scoped_aggregates(), per page row.
  WITH acc AS (
    SELECT public.accessible_program_slugs() AS slugs
  ),
  page AS (
    SELECT o.id, o.object_id, o.ra, o.dec, o.redshift, o.redshift_quality, o.field
    FROM public.objects o
    CROSS JOIN acc
    WHERE o.field = p_field
      AND o.is_active
      AND (p_cursor IS NULL OR o.object_id > p_cursor)
      -- Same membership rule as the old inner JOIN: an object appears only if
      -- it has >= 1 visible member (accessible program; B1: contributing a
      -- published spectrum unless the admin opts in). Applied BEFORE the
      -- LIMIT so a page is never short for a reason other than exhaustion.
      AND EXISTS (
        SELECT 1 FROM public.targets t
        WHERE t.object_id = o.id
          AND t.program_slug = ANY(acc.slugs)
          AND (p_include_unpublished OR t.has_published_spectrum)
      )
    ORDER BY o.object_id
    LIMIT GREATEST(1, LEAST(COALESCE(p_page_size, 5000), 50000))
  )
  -- Per-row LATERAL aggregation rather than set-based CTEs joined back to the
  -- page (perf, #515 follow-up to T1-6). The RLS predicate `programs &&
  -- accessible_program_slugs()` reaches the planner as an InitPlan, so it is
  -- estimated at the default ~1 % selectivity while it really matches nearly
  -- every row; the page CTE was estimated at 9 rows (actual 5000) and the
  -- planner joined the aggregates with nested loops over materialized
  -- subqueries, discarding 12.5 M rows per join — 5.9 s per page under RLS,
  -- over the 8 s statement timeout on later pages. A LATERAL aggregate per
  -- page row is the plan an accurate estimate would produce anyway (one index
  -- probe per object) and does not depend on the estimate: 0.24 s for the
  -- same page. Same filters, same output; only the join shape changed.
  SELECT
    p.object_id,
    p.ra,
    p.dec,
    p.redshift::double precision,
    p.redshift_quality,
    p.field,
    COALESCE(mt.n_targets, 0)                       AS n_targets,
    COALESCE(sp.n_spectra, 0)                       AS n_spectra,
    COALESCE(mt.programs, ARRAY[]::TEXT[])          AS programs,
    COALESCE(mt.member_target_ids, ARRAY[]::TEXT[]) AS member_target_ids
  FROM page p
  CROSS JOIN acc
  LEFT JOIN LATERAL (
    SELECT array_agg(t.target_id ORDER BY t.target_id)                AS member_target_ids,
           array_agg(DISTINCT t.program_slug ORDER BY t.program_slug) AS programs,
           COUNT(*)::int                                              AS n_targets
    FROM public.targets t
    WHERE t.object_id = p.id
      AND t.program_slug = ANY(acc.slugs)
      AND (p_include_unpublished OR t.has_published_spectrum)
  ) mt ON true
  LEFT JOIN LATERAL (
    SELECT COUNT(*)::int AS n_spectra
    FROM public.spectra s
    JOIN public.targets t ON t.target_id = s.target_id
    WHERE t.object_id = p.id
      AND t.program_slug = ANY(acc.slugs)
      AND (p_include_unpublished OR s.deploy_status = 'published')
  ) sp ON true
  ORDER BY p.object_id;
$$;
