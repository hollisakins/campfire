-- Migration: Add remaining performance indexes for 10k+ object scale
-- Companion to 006_optimize_rpc_function.sql
--
-- INDEXES ADDED:
-- 1. Trigram index for fuzzy text search on object_id (Option A from analysis)
-- 2. Redshift index on generated column (instead of redshift_auto)
--
-- NOTE: At 2,500 objects, index creation is very fast (seconds)
-- DATABASE SIZE IMPACT: ~1-2 MB at 2,500 objects, ~5-10 MB at 10k objects
--
-- For larger databases (25k+ objects), run these CREATE INDEX statements
-- individually outside a transaction using CONCURRENTLY option

-- Enable trigram extension for fuzzy text search
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Trigram index for fuzzy text search on object_id
-- Supports patterns like '%cosmos%', '%66964%' etc.
-- Benefits queries like: WHERE object_id ILIKE '%' || p_search || '%'
CREATE INDEX IF NOT EXISTS idx_objects_object_id_trgm
ON objects USING gin (object_id gin_trgm_ops);

-- Drop old redshift index on redshift_auto (if it exists)
DROP INDEX IF EXISTS idx_objects_redshift;

-- Create new index on generated redshift column
-- This column is: COALESCE(redshift_inspected, redshift_auto)
-- Benefits range queries: WHERE redshift >= p_redshift_min AND redshift <= p_redshift_max
-- NOTE: Partial index excludes NULL values to save space
CREATE INDEX IF NOT EXISTS idx_objects_redshift_generated
ON objects(redshift) WHERE redshift IS NOT NULL;

-- Add comment documenting the indexing strategy
COMMENT ON INDEX idx_objects_object_id_trgm IS
'Trigram index for fuzzy text search on object_id. Supports ILIKE with leading/trailing wildcards.
Example: WHERE object_id ILIKE ''%cosmos%'' will use this index.
Alternative: For prefix-only search, use text_pattern_ops index instead.';

COMMENT ON INDEX idx_objects_redshift_generated IS
'Index on generated redshift column (COALESCE(redshift_inspected, redshift_auto)).
Partial index excludes NULL values. Used for range queries on redshift values.';
