-- Add unique index on spectra.fits_path
-- This column is queried by every spectrum/redshift-fit/download API route
-- via PostgREST .eq('fits_path', ...) but had no index, causing sequential scans.
-- Observed as 30% CPU usage with 7s max query time in Supabase query performance.
CREATE UNIQUE INDEX idx_spectra_fits_path ON public.spectra USING btree (fits_path);

-- Replace plain object_id index with a covering index.
-- Queries selecting grating + fits_path WHERE object_id = $1 can now do an
-- index-only scan instead of index scan + heap lookups for every matching row.
DROP INDEX IF EXISTS public.idx_spectra_object_id;
CREATE INDEX idx_spectra_object_id ON public.spectra USING btree (object_id) INCLUDE (grating, fits_path);
