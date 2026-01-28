-- Add has_sed_plot column to objects table
-- This column is populated during deployment to avoid R2 HeadObject calls at runtime

ALTER TABLE objects ADD COLUMN has_sed_plot BOOLEAN NOT NULL DEFAULT false;

-- Partial index for efficient filtering on objects with SED plots
CREATE INDEX idx_objects_has_sed_plot ON objects(has_sed_plot) WHERE has_sed_plot = true;

-- Comment explaining the column purpose
COMMENT ON COLUMN objects.has_sed_plot IS 'Indicates whether an SED plot PDF exists in R2. Set during deployment to avoid runtime R2 HeadObject calls.';
