-- Rewrite get_programs_overview and get_observation_stats as SQL language functions
-- so PostgreSQL can inline them, avoiding ~800ms of PL/pgSQL row-copying overhead.

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
) LANGUAGE sql STABLE AS $$
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
$$;

CREATE OR REPLACE FUNCTION public.get_observation_stats(p_program_ids integer[])
RETURNS TABLE(
  observation text,
  program_id integer,
  program_name text,
  field text,
  object_count bigint,
  spectrum_count bigint,
  total_size_bytes bigint
) LANGUAGE sql STABLE AS $$
  SELECT
    o.observation,
    o.program_id,
    p.program_name,
    o.field,
    COUNT(DISTINCT o.object_id) AS object_count,
    COUNT(s.id) AS spectrum_count,
    COALESCE(SUM(s.file_size), 0)::bigint AS total_size_bytes
  FROM objects o
  JOIN programs p ON p.program_id = o.program_id
  LEFT JOIN spectra s ON s.object_id = o.object_id
  WHERE o.program_id = ANY(p_program_ids)
  GROUP BY o.observation, o.program_id, p.program_name, o.field
  ORDER BY o.observation;
$$;
