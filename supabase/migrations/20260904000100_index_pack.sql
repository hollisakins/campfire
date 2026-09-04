-- Perf T1-5 (#501, epic #515): June index pack + drop the dead indexes.
--
-- Hand-authored: no local Docker for `supabase db diff`. Matches
-- supabase/schemas/indexes.sql.
--
-- Plain CREATE INDEX (not CONCURRENTLY): the Supabase CLI migration runner
-- wraps each file in a transaction (see 20260417184613). These tables are
-- small enough (spectra 80 k, shutters 243 k rows) that the build is
-- sub-second; reads continue, only writes (deploys) wait.
--
-- Dropped indexes had 0–3 scans in pg_stat_user_indexes on 2026-09-03
-- (idx_storage_objects_content_hash: 92 MB, 0 scans; the two *_trgm on
-- object_id / spectrum_id were superseded by the search_text indexes).

CREATE INDEX IF NOT EXISTS idx_spectra_signal_to_noise
    ON public.spectra USING btree (signal_to_noise DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_spectra_exposure_time
    ON public.spectra USING btree (exposure_time DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_objects_observations
    ON public.objects USING gin (observations);

CREATE INDEX IF NOT EXISTS idx_shutters_observation
    ON public.shutters USING btree (observation);

CREATE INDEX IF NOT EXISTS idx_slit_regions_observation
    ON public.slit_regions USING btree (observation);

CREATE INDEX IF NOT EXISTS idx_shutters_field_object_id
    ON public.shutters USING btree (field, object_id);

DROP INDEX IF EXISTS public.idx_storage_objects_content_hash;
DROP INDEX IF EXISTS public.idx_object_photometry_coords;
DROP INDEX IF EXISTS public.idx_comments_content_trgm;
DROP INDEX IF EXISTS public.idx_objects_object_id_trgm;
DROP INDEX IF EXISTS public.idx_spectra_spectrum_id_trgm;
