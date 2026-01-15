-- Migration 027: Make redshift NULL when redshift_quality = 1 (Impossible)
--
-- RATIONALE:
-- When an inspector marks redshift_quality = 1 ("Impossible"), it means no reliable
-- redshift can be determined. The redshift value should therefore be NULL to:
-- 1. Exclude these objects from redshift range filters (desired behavior)
-- 2. Accurately reflect that no redshift is available
-- 3. Preserve redshift_auto and redshift_inspected for reference
--
-- EDGE CASE HANDLING:
-- If quality is changed from Impossible back to another value, the original
-- COALESCE logic kicks in and restores the redshift automatically.

-- Step 1: Drop the existing generated column
-- PostgreSQL doesn't support ALTER on generated columns, so we must drop and recreate
ALTER TABLE objects DROP COLUMN redshift;

-- Step 2: Recreate with new logic
-- Logic: If quality = 1 (Impossible), return NULL regardless of auto/inspected values
-- Otherwise, use COALESCE(redshift_inspected, redshift_auto) as before
ALTER TABLE objects ADD COLUMN redshift numeric(10,6) GENERATED ALWAYS AS (
  CASE
    WHEN redshift_quality = 1 THEN NULL
    ELSE COALESCE(redshift_inspected::double precision, redshift_auto)
  END
) STORED;

-- Step 3: Recreate the index on the generated column
-- The partial index excludes NULL values for efficient range queries
-- Now "Impossible" objects will naturally be excluded from the index
CREATE INDEX IF NOT EXISTS idx_objects_redshift_generated
ON objects(redshift) WHERE redshift IS NOT NULL;

-- Add comment explaining the new behavior
COMMENT ON COLUMN objects.redshift IS
'Generated column: NULL when redshift_quality = 1 (Impossible), otherwise COALESCE(redshift_inspected, redshift_auto). This allows "Impossible" objects to be excluded from redshift range filters.';
