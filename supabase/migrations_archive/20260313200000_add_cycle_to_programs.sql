-- Add JWST cycle number to programs table and propagate through materialized view.

-- =============================================================================
-- 1. Add column
-- =============================================================================

ALTER TABLE public.programs ADD COLUMN IF NOT EXISTS cycle integer;

-- =============================================================================
-- 2. Recreate materialized view with cycle column
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_programs_overview;

CREATE MATERIALIZED VIEW public.mv_programs_overview AS
SELECT
  p.program_id,
  p.program_name,
  p.pi_name,
  p.description,
  p.is_public,
  p.cycle,
  COALESCE(stats.object_count, 0)::bigint AS object_count,
  COALESCE(stats.gratings, ARRAY[]::text[]) AS gratings,
  COALESCE(stats.fields, ARRAY[]::text[]) AS fields,
  COALESCE(stats.observations, ARRAY[]::text[]) AS observations
FROM programs p
LEFT JOIN (
  SELECT
    o.program_id AS pid,
    COUNT(DISTINCT o.object_id) AS object_count,
    ARRAY_AGG(DISTINCT s.grating ORDER BY s.grating) FILTER (WHERE s.grating IS NOT NULL) AS gratings,
    ARRAY_AGG(DISTINCT o.field ORDER BY o.field) FILTER (WHERE o.field IS NOT NULL) AS fields,
    ARRAY_AGG(DISTINCT o.observation ORDER BY o.observation) FILTER (WHERE o.observation IS NOT NULL) AS observations
  FROM objects o
  LEFT JOIN spectra s ON s.object_id = o.object_id
  GROUP BY o.program_id
) stats ON p.program_id = stats.pid
ORDER BY p.program_name
WITH DATA;

CREATE UNIQUE INDEX mv_programs_overview_program_id ON public.mv_programs_overview (program_id);

GRANT SELECT ON public.mv_programs_overview TO authenticated;

-- =============================================================================
-- 3. Recreate RPC to include cycle
-- =============================================================================

DROP FUNCTION IF EXISTS public.get_programs_overview();

CREATE OR REPLACE FUNCTION public.get_programs_overview()
RETURNS TABLE(
  program_id integer,
  program_name text,
  pi_name text,
  description text,
  is_public boolean,
  cycle integer,
  object_count bigint,
  gratings text[],
  fields text[],
  observations text[]
) LANGUAGE sql STABLE AS $$
  SELECT
    mv.program_id,
    mv.program_name,
    mv.pi_name,
    mv.description,
    mv.is_public,
    mv.cycle,
    mv.object_count,
    mv.gratings,
    mv.fields,
    mv.observations
  FROM public.mv_programs_overview mv
  ORDER BY mv.program_name;
$$;
