-- RPC: Get all programs with aggregated stats (for programs listing page)
CREATE OR REPLACE FUNCTION public.get_programs_overview()
RETURNS TABLE(
  program_id integer,
  program_name text,
  pi_name text,
  description text,
  is_public boolean,
  object_count bigint,
  gratings text[],
  fields text[],
  observations text[]
) LANGUAGE plpgsql STABLE AS $$
BEGIN
  RETURN QUERY
  SELECT
    p.program_id, p.program_name, p.pi_name, p.description, p.is_public,
    COALESCE(stats.object_count, 0)::bigint,
    COALESCE(stats.gratings, ARRAY[]::text[]),
    COALESCE(stats.fields, ARRAY[]::text[]),
    COALESCE(stats.observations, ARRAY[]::text[])
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
  ORDER BY p.program_name;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_programs_overview() TO authenticated;
