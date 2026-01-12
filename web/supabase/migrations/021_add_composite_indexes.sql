-- Migration 021: Add composite indexes for improved query performance at 16k+ scale
-- These indexes optimize common filter combinations in get_filtered_objects_paginated

-- Composite index for program + field filtering (very common pattern)
CREATE INDEX IF NOT EXISTS idx_objects_program_field
ON objects(program_id, field);

-- Composite index for program + redshift filtering (common for science queries)
CREATE INDEX IF NOT EXISTS idx_objects_program_redshift
ON objects(program_id, redshift)
WHERE redshift IS NOT NULL;

-- Composite index for field + observation (common drill-down pattern)
CREATE INDEX IF NOT EXISTS idx_objects_field_observation
ON objects(field, observation);

-- Composite index for program + redshift_quality (inspection workflow)
CREATE INDEX IF NOT EXISTS idx_objects_program_quality
ON objects(program_id, redshift_quality);

-- Add comments for documentation
COMMENT ON INDEX idx_objects_program_field IS 'Composite index for program + field filtering';
COMMENT ON INDEX idx_objects_program_redshift IS 'Composite index for program + redshift range queries';
COMMENT ON INDEX idx_objects_field_observation IS 'Composite index for field + observation drill-down';
COMMENT ON INDEX idx_objects_program_quality IS 'Composite index for program + inspection quality filtering';
