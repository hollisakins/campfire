-- Fix: drop the old get_filtered_object_ids overload left behind
-- when 20260319100000 created the new version with p_updated_since.
--
-- The old 34-parameter signature (from 20260316200000_multi_pid_programs)
-- coexists with the new 35-parameter version (which adds p_updated_since),
-- causing PostgREST to fail with an ambiguous function error when the
-- map view calls the RPC directly. This is the same class of bug that
-- 20260319100001 fixed for get_filtered_objects_paginated.

-- Drop old overload by its exact 34-parameter signature
DROP FUNCTION IF EXISTS public.get_filtered_object_ids(
  TEXT[],              -- p_program_slugs
  TEXT[],              -- p_filter_programs
  TEXT[],              -- p_fields
  TEXT[],              -- p_gratings
  TEXT,                -- p_gratings_mode
  TEXT[],              -- p_observations
  INTEGER[],           -- p_redshift_quality
  DOUBLE PRECISION,    -- p_redshift_min
  DOUBLE PRECISION,    -- p_redshift_max
  DOUBLE PRECISION,    -- p_max_snr_min
  DOUBLE PRECISION,    -- p_max_snr_max
  DOUBLE PRECISION,    -- p_max_exposure_time_min
  DOUBLE PRECISION,    -- p_max_exposure_time_max
  INTEGER,             -- p_spectral_features_include_any
  INTEGER,             -- p_spectral_features_include_all
  INTEGER,             -- p_spectral_features_exclude
  INTEGER,             -- p_object_flags_include_any
  INTEGER,             -- p_object_flags_include_all
  INTEGER,             -- p_object_flags_exclude
  INTEGER,             -- p_dq_flags_include_any
  INTEGER,             -- p_dq_flags_include_all
  INTEGER,             -- p_dq_flags_exclude
  TEXT,                -- p_search
  BOOLEAN,             -- p_inspected_only
  TEXT,                -- p_comment_search
  TEXT,                -- p_comment_search_scope
  UUID,                -- p_comment_user_id
  DOUBLE PRECISION,    -- p_coord_ra
  DOUBLE PRECISION,    -- p_coord_dec
  DOUBLE PRECISION,    -- p_radius_degrees
  TEXT,                -- p_sort_column
  TEXT,                -- p_sort_direction
  INTEGER,             -- p_page
  INTEGER              -- p_page_size
);

-- Now the function name is unique — grant execute on the remaining version
GRANT EXECUTE ON FUNCTION public.get_filtered_object_ids TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_filtered_object_ids TO service_role;
