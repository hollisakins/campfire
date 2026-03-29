-- Add exposure_time column to spectra table
ALTER TABLE public.spectra ADD COLUMN exposure_time double precision;

COMMENT ON COLUMN public.spectra.exposure_time IS 'Effective exposure time in seconds, from EFFEXPTM FITS header.';
