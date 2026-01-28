-- Add thumbnail SVG columns to spectra table
-- Pre-generated SVG thumbnails are stored during deployment to avoid R2 fetches at runtime
-- Both fnu and flambda versions are stored to support user preference switching

-- Rename existing thumbnail_svg column to thumbnail_svg_fnu (preserves existing data)
ALTER TABLE spectra RENAME COLUMN thumbnail_svg TO thumbnail_svg_fnu;

-- Add new column for flambda thumbnails
ALTER TABLE spectra ADD COLUMN thumbnail_svg_flambda TEXT;

-- Comments explaining the column purposes
COMMENT ON COLUMN spectra.thumbnail_svg_fnu IS 'Pre-generated SVG sparkline thumbnail in f_nu units. Set during deployment to avoid R2 fetches and CPU-intensive processing at runtime.';
COMMENT ON COLUMN spectra.thumbnail_svg_flambda IS 'Pre-generated SVG sparkline thumbnail in f_lambda units. Set during deployment to avoid R2 fetches and CPU-intensive processing at runtime.';
