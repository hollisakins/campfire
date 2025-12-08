-- Migration: Add RPC function to get distinct fields and observations efficiently
-- This replaces the inefficient approach of fetching all objects just to extract unique values

CREATE OR REPLACE FUNCTION get_distinct_filter_options(
  p_program_ids INTEGER[]
)
RETURNS TABLE (
  fields TEXT[],
  observations TEXT[]
)
LANGUAGE plpgsql
STABLE
AS $$
BEGIN
  RETURN QUERY
  SELECT
    ARRAY(
      SELECT DISTINCT field
      FROM objects
      WHERE program_id = ANY(p_program_ids)
      ORDER BY field
    ) AS fields,
    ARRAY(
      SELECT DISTINCT observation
      FROM objects
      WHERE program_id = ANY(p_program_ids)
        AND observation IS NOT NULL
      ORDER BY observation
    ) AS observations;
END;
$$;

-- Add comment
COMMENT ON FUNCTION get_distinct_filter_options IS
  'Efficiently returns distinct fields and observations for given program IDs without fetching all rows';
