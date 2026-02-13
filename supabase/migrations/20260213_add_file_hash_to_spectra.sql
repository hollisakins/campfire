-- Add file_hash and file_size columns to spectra table for CLI sync support

ALTER TABLE public.spectra ADD COLUMN file_hash text;
ALTER TABLE public.spectra ADD COLUMN file_size bigint;

COMMENT ON COLUMN public.spectra.file_hash IS 'SHA-256 hash of the FITS file in R2. Used for incremental sync to detect changed files.';
COMMENT ON COLUMN public.spectra.file_size IS 'Size of the FITS file in bytes. Used for download size estimation and verification.';

CREATE INDEX idx_spectra_file_hash ON public.spectra(file_hash) WHERE file_hash IS NOT NULL;

-- Add fits_sync to allowed download types
ALTER TABLE public.download_log DROP CONSTRAINT download_log_download_type_check;
ALTER TABLE public.download_log ADD CONSTRAINT download_log_download_type_check
  CHECK (download_type = ANY (ARRAY['fits_single','fits_object','fits_batch','fits_zip','csv','sed_plot','fits_sync']));

-- RPC: Get observation stats for the CLI observations list
CREATE OR REPLACE FUNCTION public.get_observation_stats(p_program_ids integer[])
RETURNS TABLE(
  observation text,
  program_id integer,
  program_name text,
  field text,
  object_count bigint,
  spectrum_count bigint,
  total_size_bytes bigint
) LANGUAGE plpgsql STABLE AS $$
BEGIN
  RETURN QUERY
  SELECT
    o.observation,
    o.program_id,
    p.program_name,
    o.field,
    COUNT(DISTINCT o.object_id) AS object_count,
    COUNT(s.id) AS spectrum_count,
    COALESCE(SUM(s.file_size), 0)::bigint AS total_size_bytes
  FROM objects o
  JOIN programs p ON p.program_id = o.program_id
  LEFT JOIN spectra s ON s.object_id = o.object_id
  WHERE o.program_id = ANY(p_program_ids)
  GROUP BY o.observation, o.program_id, p.program_name, o.field
  ORDER BY o.observation;
END;
$$;

-- RPC: Get manifest for a single observation (spectra + metadata for sync)
CREATE OR REPLACE FUNCTION public.get_observation_manifest(
  p_obs_name text,
  p_program_ids integer[]
)
RETURNS TABLE(
  spectra_id integer,
  object_id text,
  grating text,
  fits_path text,
  file_hash text,
  file_size bigint,
  signal_to_noise double precision,
  reduction_version text
) LANGUAGE plpgsql STABLE AS $$
BEGIN
  RETURN QUERY
  SELECT s.id, s.object_id, s.grating, s.fits_path, s.file_hash, s.file_size,
         s.signal_to_noise, s.reduction_version
  FROM spectra s
  JOIN objects o ON o.object_id = s.object_id
  WHERE o.observation = p_obs_name
    AND o.program_id = ANY(p_program_ids)
  ORDER BY s.object_id, s.grating;
END;
$$;
