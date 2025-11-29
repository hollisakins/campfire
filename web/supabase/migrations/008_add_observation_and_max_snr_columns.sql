-- Migration 008: Add observation and max_snr columns to objects table
-- This migration adds:
--   1. observation column (extracted from object_id pattern: {observation}_{srcid})
--   2. max_snr column (maximum signal-to-noise across all spectra for this object)
--   3. Trigger to auto-update max_snr when spectra table changes
--   4. Indexes for efficient filtering and sorting

-- Add observation column
-- Object IDs follow pattern: {observation}_{srcid}
-- Example: "ember_cosmos_p1_12345" -> observation = "ember_cosmos_p1"
ALTER TABLE objects
ADD COLUMN observation TEXT
GENERATED ALWAYS AS (
  SUBSTRING(object_id FROM '^(.+)_[0-9]+$')
) STORED;

-- Add max_snr column (will be populated by trigger and backfill)
ALTER TABLE objects
ADD COLUMN max_snr DOUBLE PRECISION;

-- Create function to update max_snr for an object
-- This function calculates the maximum signal_to_noise across all spectra for a given object
CREATE OR REPLACE FUNCTION update_object_max_snr()
RETURNS TRIGGER AS $$
DECLARE
  target_object_id TEXT;
BEGIN
  -- Determine which object_id was affected
  IF TG_OP = 'DELETE' THEN
    target_object_id := OLD.object_id;
  ELSE
    target_object_id := NEW.object_id;
  END IF;

  -- Update the max_snr for the affected object
  UPDATE objects
  SET max_snr = (
    SELECT MAX(signal_to_noise)
    FROM spectra
    WHERE spectra.object_id = target_object_id
  )
  WHERE objects.object_id = target_object_id;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create trigger to automatically update max_snr when spectra change
CREATE TRIGGER update_max_snr_trigger
AFTER INSERT OR UPDATE OR DELETE ON spectra
FOR EACH ROW
EXECUTE FUNCTION update_object_max_snr();

-- Backfill max_snr for all existing objects
UPDATE objects o
SET max_snr = (
  SELECT MAX(s.signal_to_noise)
  FROM spectra s
  WHERE s.object_id = o.object_id
);

-- Create indexes for efficient filtering and sorting
CREATE INDEX idx_objects_observation ON objects(observation);
CREATE INDEX idx_objects_max_snr ON objects(max_snr) WHERE max_snr IS NOT NULL;

-- Note: observation column is auto-populated via GENERATED ALWAYS expression
-- Note: max_snr is maintained automatically via trigger on spectra table
