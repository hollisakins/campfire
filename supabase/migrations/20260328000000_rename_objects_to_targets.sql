-- Rename objects → targets, object_id → target_id
--
-- Phase 2a of the Objects / Targets / Spectra hierarchy.
-- This is the structural rename — table, columns, constraints, indexes,
-- triggers, views, and all RPC functions.
--
-- Every function body references table/column names as text strings,
-- so they must all be dropped and recreated. Views and materialized views
-- track references by OID and auto-update, but their output column names
-- and the view names themselves need renaming.

BEGIN;

-- ============================================================
-- 1. DROP ALL FUNCTIONS (they reference old table/column names)
-- ============================================================

-- RPC functions (renamed)
DROP FUNCTION IF EXISTS public.get_filtered_object_ids;
DROP FUNCTION IF EXISTS public.get_filtered_objects_paginated;
DROP FUNCTION IF EXISTS public.get_adjacent_objects;
DROP FUNCTION IF EXISTS public.get_objects_in_viewport;
DROP FUNCTION IF EXISTS public.get_objects_for_sync;

-- RPC functions (name stays, body changes)
DROP FUNCTION IF EXISTS public.get_csv_export;
DROP FUNCTION IF EXISTS public.get_csv_export_spectra;
DROP FUNCTION IF EXISTS public.get_filtered_spectra_paginated;
DROP FUNCTION IF EXISTS public.get_observation_stats;
DROP FUNCTION IF EXISTS public.get_observation_manifest;
DROP FUNCTION IF EXISTS public.get_programs_overview;
DROP FUNCTION IF EXISTS public.get_program_stats;

-- RPC functions (missed in initial list)
DROP FUNCTION IF EXISTS public.propagate_crossmatch_inspection;
DROP FUNCTION IF EXISTS public.get_user_profile_stats;
DROP FUNCTION IF EXISTS public.get_download_stats;

-- Trigger functions
DROP FUNCTION IF EXISTS public.log_flag_changes CASCADE;
DROP FUNCTION IF EXISTS public.update_object_max_snr CASCADE;
DROP FUNCTION IF EXISTS public.update_object_max_exposure_time CASCADE;

-- CASCADE on trigger functions also drops the triggers themselves.


-- ============================================================
-- 2. STRUCTURAL RENAMES
-- ============================================================

-- Table
ALTER TABLE public.objects RENAME TO targets;

-- Columns on targets (formerly objects)
ALTER TABLE public.targets RENAME COLUMN object_id TO target_id;

-- Columns on dependent tables
ALTER TABLE public.spectra RENAME COLUMN object_id TO target_id;
ALTER TABLE public.comments RENAME COLUMN object_id TO target_id;
ALTER TABLE public.flag_audit_log RENAME COLUMN object_id TO target_id;
ALTER TABLE public.download_log RENAME COLUMN object_ids TO target_ids;
ALTER TABLE public.download_log RENAME COLUMN object_count TO target_count;

-- Sequence
ALTER SEQUENCE public.objects_id_seq RENAME TO targets_id_seq;

-- Constraints on targets
ALTER TABLE public.targets RENAME CONSTRAINT objects_pkey TO targets_pkey;
ALTER TABLE public.targets RENAME CONSTRAINT objects_object_id_key TO targets_target_id_key;
ALTER TABLE public.targets RENAME CONSTRAINT fk_objects_program TO fk_targets_program;
ALTER TABLE public.targets RENAME CONSTRAINT fk_objects_observation TO fk_targets_observation;
ALTER TABLE public.targets RENAME CONSTRAINT objects_last_inspected_by_fkey TO targets_last_inspected_by_fkey;

-- Constraints on dependent tables
ALTER TABLE public.spectra RENAME CONSTRAINT spectra_object_id_fkey TO spectra_target_id_fkey;
ALTER TABLE public.comments RENAME CONSTRAINT comments_object_id_fkey TO comments_target_id_fkey;
ALTER TABLE public.flag_audit_log RENAME CONSTRAINT flag_audit_log_object_id_fkey TO flag_audit_log_target_id_fkey;

-- Indexes on targets
ALTER INDEX public.idx_objects_coords RENAME TO idx_targets_coords;
ALTER INDEX public.idx_objects_field RENAME TO idx_targets_field;
ALTER INDEX public.idx_objects_field_observation RENAME TO idx_targets_field_observation;
ALTER INDEX public.idx_objects_has_sed_plot RENAME TO idx_targets_has_sed_plot;
ALTER INDEX public.idx_objects_max_snr RENAME TO idx_targets_max_snr;
ALTER INDEX public.idx_objects_max_exposure_time RENAME TO idx_targets_max_exposure_time;
ALTER INDEX public.idx_objects_object_id_trgm RENAME TO idx_targets_target_id_trgm;
ALTER INDEX public.idx_objects_program_slug RENAME TO idx_targets_program_slug;
ALTER INDEX public.idx_objects_program_slug_field RENAME TO idx_targets_program_slug_field;
ALTER INDEX public.idx_objects_program_slug_quality RENAME TO idx_targets_program_slug_quality;
ALTER INDEX public.idx_objects_observation RENAME TO idx_targets_observation;
ALTER INDEX public.idx_objects_updated_at RENAME TO idx_targets_updated_at;

-- Indexes on dependent tables
ALTER INDEX public.idx_spectra_object_id RENAME TO idx_spectra_target_id;
ALTER INDEX public.idx_spectra_object_grating RENAME TO idx_spectra_target_grating;
ALTER INDEX public.idx_comments_object RENAME TO idx_comments_target;
ALTER INDEX public.idx_audit_object RENAME TO idx_audit_target;
ALTER INDEX public.idx_flag_audit_log_object_id RENAME TO idx_flag_audit_log_target_id;
ALTER INDEX public.idx_download_log_object_ids RENAME TO idx_download_log_target_ids;

-- RLS policies on targets (renamed from objects)
ALTER POLICY "select_objects_by_access" ON public.targets RENAME TO "select_targets_by_access";
ALTER POLICY "update_objects_by_access" ON public.targets RENAME TO "update_targets_by_access";

-- Views
ALTER VIEW public.object_flag_summary RENAME TO target_flag_summary;
ALTER VIEW public.objects_with_flags RENAME TO targets_with_flags;
ALTER MATERIALIZED VIEW public.mv_programs_overview RENAME COLUMN object_count TO target_count;


-- ============================================================
-- 3. TRIGGER FUNCTIONS
-- ============================================================

CREATE OR REPLACE FUNCTION public.log_flag_changes() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
    IF OLD.redshift_quality IS DISTINCT FROM NEW.redshift_quality THEN
        INSERT INTO flag_audit_log (target_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'redshift_quality', OLD.redshift_quality, NEW.redshift_quality);
    END IF;
    IF OLD.spectral_features IS DISTINCT FROM NEW.spectral_features THEN
        INSERT INTO flag_audit_log (target_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'spectral_features', OLD.spectral_features, NEW.spectral_features);
    END IF;
    IF OLD.object_flags IS DISTINCT FROM NEW.object_flags THEN
        INSERT INTO flag_audit_log (target_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'object_flags', OLD.object_flags, NEW.object_flags);
    END IF;
    IF OLD.dq_flags IS DISTINCT FROM NEW.dq_flags THEN
        INSERT INTO flag_audit_log (target_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'dq_flags', OLD.dq_flags, NEW.dq_flags);
    END IF;
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.update_target_max_snr() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  v_target_id TEXT;
BEGIN
  IF TG_OP = 'DELETE' THEN
    v_target_id := OLD.target_id;
  ELSE
    v_target_id := NEW.target_id;
  END IF;
  UPDATE targets
  SET max_snr = (
    SELECT MAX(signal_to_noise)
    FROM spectra
    WHERE spectra.target_id = v_target_id
  )
  WHERE targets.target_id = v_target_id;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.update_target_max_exposure_time() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  v_target_id TEXT;
BEGIN
  IF TG_OP = 'DELETE' THEN
    v_target_id := OLD.target_id;
  ELSE
    v_target_id := NEW.target_id;
  END IF;
  UPDATE targets
  SET max_exposure_time = (
    SELECT MAX(exposure_time)
    FROM spectra
    WHERE spectra.target_id = v_target_id
  )
  WHERE targets.target_id = v_target_id;
  RETURN NEW;
END;
$$;


-- ============================================================
-- 4. TRIGGERS
-- ============================================================

CREATE TRIGGER track_flag_changes
  BEFORE UPDATE ON public.targets
  FOR EACH ROW EXECUTE FUNCTION public.log_flag_changes();

CREATE TRIGGER update_max_snr_trigger
  AFTER INSERT OR DELETE OR UPDATE ON public.spectra
  FOR EACH ROW EXECUTE FUNCTION public.update_target_max_snr();

CREATE TRIGGER update_max_exposure_time_trigger
  AFTER INSERT OR DELETE OR UPDATE ON public.spectra
  FOR EACH ROW EXECUTE FUNCTION public.update_target_max_exposure_time();


-- ============================================================
-- 5. RPC: get_filtered_target_ids (renamed from get_filtered_object_ids)
-- ============================================================

CREATE OR REPLACE FUNCTION public.get_filtered_target_ids(
  p_program_slugs TEXT[],
  p_filter_programs TEXT[] DEFAULT NULL,
  p_fields TEXT[] DEFAULT NULL,
  p_gratings TEXT[] DEFAULT NULL,
  p_gratings_mode TEXT DEFAULT 'any',
  p_observations TEXT[] DEFAULT NULL,
  p_redshift_quality INTEGER[] DEFAULT NULL,
  p_redshift_min DOUBLE PRECISION DEFAULT NULL,
  p_redshift_max DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_min DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_max DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_min DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_max DOUBLE PRECISION DEFAULT NULL,
  p_spectral_features_include_any INTEGER DEFAULT NULL,
  p_spectral_features_include_all INTEGER DEFAULT NULL,
  p_spectral_features_exclude INTEGER DEFAULT NULL,
  p_object_flags_include_any INTEGER DEFAULT NULL,
  p_object_flags_include_all INTEGER DEFAULT NULL,
  p_object_flags_exclude INTEGER DEFAULT NULL,
  p_dq_flags_include_any INTEGER DEFAULT NULL,
  p_dq_flags_include_all INTEGER DEFAULT NULL,
  p_dq_flags_exclude INTEGER DEFAULT NULL,
  p_search TEXT DEFAULT NULL,
  p_inspected_only BOOLEAN DEFAULT NULL,
  p_comment_search TEXT DEFAULT NULL,
  p_comment_search_scope TEXT DEFAULT NULL,
  p_comment_user_id UUID DEFAULT NULL,
  p_coord_ra DOUBLE PRECISION DEFAULT NULL,
  p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_sort_column TEXT DEFAULT 'target_id',
  p_sort_direction TEXT DEFAULT 'asc',
  p_page INTEGER DEFAULT NULL,
  p_page_size INTEGER DEFAULT NULL,
  p_updated_since TIMESTAMP WITHOUT TIME ZONE DEFAULT NULL
)
RETURNS TABLE(target_id TEXT, distance DOUBLE PRECISION, row_num BIGINT, total_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_paginate BOOLEAN;
  v_offset INTEGER;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (
    p_comment_search IS NOT NULL
    AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN
    v_gratings_mode := 'any';
  END IF;
  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;
  IF NOT (p_sort_column IN ('target_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'max_snr', 'max_exposure_time')
       OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'target_id';
  END IF;
  IF v_coord_search_active AND p_sort_column = 'target_id' AND p_sort_direction = 'asc' THEN
    p_sort_column := 'distance';
  END IF;
  v_paginate := (p_page IS NOT NULL AND p_page_size IS NOT NULL);
  IF v_paginate THEN
    v_offset := (p_page - 1) * p_page_size;
  END IF;
  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(
      SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs)
    ) INTO v_filtered_program_slugs;
  ELSE
    v_filtered_program_slugs := p_program_slugs;
  END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN
    RETURN;
  END IF;

  -- === Paginated path ===
  IF v_paginate THEN
    RETURN QUERY
    WITH filtered_targets AS (
      SELECT
        t.target_id,
        CASE WHEN v_coord_search_active THEN
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) *
            POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL END AS distance,
        t.field, t.observation, t.ra, t.dec, t.redshift, t.redshift_quality, t.max_snr, t.max_exposure_time
      FROM targets t
      WHERE
        t.program_slug = ANY(v_filtered_program_slugs)
        AND (p_updated_since IS NULL OR t.updated_at > p_updated_since)
        AND (
          NOT v_grating_filter_active
          OR (v_gratings_mode = 'any' AND EXISTS (
            SELECT 1 FROM spectra gs WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)
          ))
          OR (v_gratings_mode = 'all' AND (
            SELECT COUNT(DISTINCT gs.grating) FROM spectra gs
            WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)
          ) = array_length(p_gratings, 1))
          OR (v_gratings_mode = 'none' AND NOT EXISTS (
            SELECT 1 FROM spectra gs WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)
          ))
        )
        AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
        AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
        AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR t.redshift_quality = ANY(p_redshift_quality))
        AND (p_redshift_min IS NULL OR t.redshift >= p_redshift_min)
        AND (p_redshift_max IS NULL OR t.redshift <= p_redshift_max)
        AND (p_max_snr_min IS NULL OR t.max_snr >= p_max_snr_min)
        AND (p_max_snr_max IS NULL OR t.max_snr <= p_max_snr_max)
        AND (p_max_exposure_time_min IS NULL OR t.max_exposure_time >= p_max_exposure_time_min)
        AND (p_max_exposure_time_max IS NULL OR t.max_exposure_time <= p_max_exposure_time_max)
        AND (p_spectral_features_include_any IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_any) != 0)
        AND (p_spectral_features_include_all IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_all) = p_spectral_features_include_all)
        AND (p_spectral_features_exclude IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_exclude) = 0)
        AND (p_object_flags_include_any IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_any) != 0)
        AND (p_object_flags_include_all IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_all) = p_object_flags_include_all)
        AND (p_object_flags_exclude IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_exclude) = 0)
        AND (p_dq_flags_include_any IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_any) != 0)
        AND (p_dq_flags_include_all IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
        AND (p_dq_flags_exclude IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_exclude) = 0)
        AND (p_search IS NULL OR t.target_id ILIKE '%' || p_search || '%')
        AND (
          p_inspected_only IS NULL
          OR (p_inspected_only = TRUE AND t.redshift_quality > 0)
          OR (p_inspected_only = FALSE AND t.redshift_quality = 0)
        )
        AND (
          NOT v_comment_search_active
          OR EXISTS (
            SELECT 1 FROM comments c
            WHERE c.target_id = t.id AND c.is_deleted = false
              AND c.content ILIKE '%' || p_comment_search || '%'
              AND (p_comment_search_scope = 'everyone'
                   OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id))
          )
        )
        AND (
          NOT v_coord_search_active
          OR (t.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
              AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees))
        )
    ),
    distance_filtered AS (
      SELECT ft.* FROM filtered_targets ft
      WHERE NOT v_coord_search_active OR ft.distance <= p_radius_degrees
    )
    SELECT
      df.target_id, df.distance,
      ROW_NUMBER() OVER (
        ORDER BY
          CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'asc' THEN df.distance END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'desc' THEN df.distance END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'asc' THEN df.target_id END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'desc' THEN df.target_id END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN df.observation END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN df.observation END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN df.ra END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN df.ra END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN df.dec END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN df.dec END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN df.redshift END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN df.redshift END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN df.redshift_quality END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN df.redshift_quality END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN df.max_snr END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN df.max_snr END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN df.max_exposure_time END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN df.max_exposure_time END DESC NULLS LAST,
          df.target_id ASC
      ) AS row_num,
      (SELECT COUNT(*) FROM distance_filtered)::BIGINT AS total_count
    FROM distance_filtered df
    ORDER BY
      CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'asc' THEN df.distance END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'desc' THEN df.distance END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'asc' THEN df.target_id END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'desc' THEN df.target_id END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN df.observation END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN df.observation END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN df.ra END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN df.ra END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN df.dec END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN df.dec END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN df.redshift END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN df.redshift END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN df.redshift_quality END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN df.redshift_quality END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN df.max_snr END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN df.max_snr END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN df.max_exposure_time END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN df.max_exposure_time END DESC NULLS LAST,
      df.target_id ASC
    LIMIT p_page_size OFFSET v_offset;

  -- === Full path (all rows) ===
  ELSE
    RETURN QUERY
    WITH filtered_targets AS (
      SELECT
        t.target_id,
        CASE WHEN v_coord_search_active THEN
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) *
            POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL END AS distance,
        t.field, t.observation, t.ra, t.dec, t.redshift, t.redshift_quality, t.max_snr, t.max_exposure_time
      FROM targets t
      WHERE
        t.program_slug = ANY(v_filtered_program_slugs)
        AND (p_updated_since IS NULL OR t.updated_at > p_updated_since)
        AND (
          NOT v_grating_filter_active
          OR (v_gratings_mode = 'any' AND EXISTS (
            SELECT 1 FROM spectra gs WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)
          ))
          OR (v_gratings_mode = 'all' AND (
            SELECT COUNT(DISTINCT gs.grating) FROM spectra gs
            WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)
          ) = array_length(p_gratings, 1))
          OR (v_gratings_mode = 'none' AND NOT EXISTS (
            SELECT 1 FROM spectra gs WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)
          ))
        )
        AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
        AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
        AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR t.redshift_quality = ANY(p_redshift_quality))
        AND (p_redshift_min IS NULL OR t.redshift >= p_redshift_min)
        AND (p_redshift_max IS NULL OR t.redshift <= p_redshift_max)
        AND (p_max_snr_min IS NULL OR t.max_snr >= p_max_snr_min)
        AND (p_max_snr_max IS NULL OR t.max_snr <= p_max_snr_max)
        AND (p_max_exposure_time_min IS NULL OR t.max_exposure_time >= p_max_exposure_time_min)
        AND (p_max_exposure_time_max IS NULL OR t.max_exposure_time <= p_max_exposure_time_max)
        AND (p_spectral_features_include_any IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_any) != 0)
        AND (p_spectral_features_include_all IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_all) = p_spectral_features_include_all)
        AND (p_spectral_features_exclude IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_exclude) = 0)
        AND (p_object_flags_include_any IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_any) != 0)
        AND (p_object_flags_include_all IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_all) = p_object_flags_include_all)
        AND (p_object_flags_exclude IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_exclude) = 0)
        AND (p_dq_flags_include_any IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_any) != 0)
        AND (p_dq_flags_include_all IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
        AND (p_dq_flags_exclude IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_exclude) = 0)
        AND (p_search IS NULL OR t.target_id ILIKE '%' || p_search || '%')
        AND (
          p_inspected_only IS NULL
          OR (p_inspected_only = TRUE AND t.redshift_quality > 0)
          OR (p_inspected_only = FALSE AND t.redshift_quality = 0)
        )
        AND (
          NOT v_comment_search_active
          OR EXISTS (
            SELECT 1 FROM comments c
            WHERE c.target_id = t.id AND c.is_deleted = false
              AND c.content ILIKE '%' || p_comment_search || '%'
              AND (p_comment_search_scope = 'everyone'
                   OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id))
          )
        )
        AND (
          NOT v_coord_search_active
          OR (t.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
              AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees))
        )
    ),
    distance_filtered AS (
      SELECT ft.* FROM filtered_targets ft
      WHERE NOT v_coord_search_active OR ft.distance <= p_radius_degrees
    )
    SELECT
      df.target_id, df.distance,
      ROW_NUMBER() OVER (
        ORDER BY
          CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'asc' THEN df.distance END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'desc' THEN df.distance END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'asc' THEN df.target_id END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'desc' THEN df.target_id END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN df.observation END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN df.observation END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN df.ra END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN df.ra END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN df.dec END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN df.dec END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN df.redshift END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN df.redshift END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN df.redshift_quality END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN df.redshift_quality END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN df.max_snr END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN df.max_snr END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN df.max_exposure_time END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN df.max_exposure_time END DESC NULLS LAST,
          df.target_id ASC
      ) AS row_num,
      COUNT(*) OVER () AS total_count
    FROM distance_filtered df;
  END IF;
END;
$$;


-- ============================================================
-- 6. RPC: get_filtered_targets_paginated
-- ============================================================

CREATE OR REPLACE FUNCTION public.get_filtered_targets_paginated(
  p_program_slugs TEXT[],
  p_filter_programs TEXT[] DEFAULT NULL,
  p_fields TEXT[] DEFAULT NULL,
  p_gratings TEXT[] DEFAULT NULL,
  p_gratings_mode TEXT DEFAULT 'any',
  p_observations TEXT[] DEFAULT NULL,
  p_redshift_quality INTEGER[] DEFAULT NULL,
  p_redshift_min DOUBLE PRECISION DEFAULT NULL,
  p_redshift_max DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_min DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_max DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_min DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_max DOUBLE PRECISION DEFAULT NULL,
  p_spectral_features INTEGER DEFAULT NULL,
  p_object_flags INTEGER DEFAULT NULL,
  p_dq_flags INTEGER DEFAULT NULL,
  p_spectral_features_include_any INTEGER DEFAULT NULL,
  p_spectral_features_include_all INTEGER DEFAULT NULL,
  p_spectral_features_exclude INTEGER DEFAULT NULL,
  p_object_flags_include_any INTEGER DEFAULT NULL,
  p_object_flags_include_all INTEGER DEFAULT NULL,
  p_object_flags_exclude INTEGER DEFAULT NULL,
  p_dq_flags_include_any INTEGER DEFAULT NULL,
  p_dq_flags_include_all INTEGER DEFAULT NULL,
  p_dq_flags_exclude INTEGER DEFAULT NULL,
  p_search TEXT DEFAULT NULL,
  p_inspected_only BOOLEAN DEFAULT NULL,
  p_comment_search TEXT DEFAULT NULL,
  p_comment_search_scope TEXT DEFAULT NULL,
  p_comment_user_id UUID DEFAULT NULL,
  p_coord_ra DOUBLE PRECISION DEFAULT NULL,
  p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_sort_column TEXT DEFAULT 'target_id',
  p_sort_direction TEXT DEFAULT 'asc',
  p_page INTEGER DEFAULT 1,
  p_page_size INTEGER DEFAULT 50,
  p_include_thumbnails BOOLEAN DEFAULT false,
  p_updated_since TIMESTAMP WITHOUT TIME ZONE DEFAULT NULL
)
RETURNS TABLE(targets JSONB, total_count BIGINT, page INTEGER, page_size INTEGER)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_sf_include_any INTEGER;
  v_sf_include_all INTEGER;
  v_sf_exclude INTEGER;
  v_of_include_any INTEGER;
  v_of_include_all INTEGER;
  v_of_exclude INTEGER;
  v_dq_include_any INTEGER;
  v_dq_include_all INTEGER;
  v_dq_exclude INTEGER;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_coord_search_active BOOLEAN;
BEGIN
  v_sf_include_any := COALESCE(p_spectral_features_include_any, p_spectral_features);
  v_sf_include_all := p_spectral_features_include_all;
  v_sf_exclude := p_spectral_features_exclude;
  v_of_include_any := COALESCE(p_object_flags_include_any, p_object_flags);
  v_of_include_all := p_object_flags_include_all;
  v_of_exclude := p_object_flags_exclude;
  v_dq_include_any := COALESCE(p_dq_flags_include_any, p_dq_flags);
  v_dq_include_all := p_dq_flags_include_all;
  v_dq_exclude := p_dq_flags_exclude;
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);

  RETURN QUERY
  WITH ids AS (
    SELECT *
    FROM public.get_filtered_target_ids(
      p_program_slugs, p_filter_programs, p_fields, p_gratings, p_gratings_mode,
      p_observations, p_redshift_quality, p_redshift_min, p_redshift_max,
      p_max_snr_min, p_max_snr_max, p_max_exposure_time_min, p_max_exposure_time_max,
      v_sf_include_any, v_sf_include_all, v_sf_exclude,
      v_of_include_any, v_of_include_all, v_of_exclude,
      v_dq_include_any, v_dq_include_all, v_dq_exclude,
      p_search, p_inspected_only, p_comment_search, p_comment_search_scope, p_comment_user_id,
      p_coord_ra, p_coord_dec, p_radius_degrees,
      p_sort_column, p_sort_direction,
      p_page, p_page_size,
      p_updated_since
    )
  ),
  with_relations AS (
    SELECT
      jsonb_build_object(
        'id', t.id,
        'target_id', t.target_id,
        'program_slug', t.program_slug,
        'program_name', pr.program_name,
        'field', t.field,
        'observation', t.observation,
        'ra', t.ra,
        'dec', t.dec,
        'redshift', t.redshift,
        'redshift_auto', t.redshift_auto,
        'redshift_inspected', t.redshift_inspected,
        'redshift_quality', t.redshift_quality,
        'spectral_features', t.spectral_features,
        'object_flags', t.object_flags,
        'dq_flags', t.dq_flags,
        'max_snr', t.max_snr,
        'max_exposure_time', t.max_exposure_time,
        'last_inspected_at', t.last_inspected_at,
        'last_inspected_by', t.last_inspected_by,
        'created_at', t.created_at,
        'updated_at', t.updated_at,
        'distance', CASE WHEN v_coord_search_active THEN ids.distance ELSE NULL END,
        'spectra', COALESCE(
          (SELECT jsonb_agg(
            jsonb_build_object(
              'id', s.id,
              'target_id', s.target_id,
              'grating', s.grating,
              'fits_path', s.fits_path,
              'signal_to_noise', s.signal_to_noise,
              'file_hash', s.file_hash,
              'file_size', s.file_size,
              'thumbnail_svg_fnu', CASE WHEN p_include_thumbnails THEN s.thumbnail_svg_fnu ELSE NULL END,
              'thumbnail_svg_flambda', CASE WHEN p_include_thumbnails THEN s.thumbnail_svg_flambda ELSE NULL END
            )
          )
          FROM spectra s
          WHERE s.target_id = t.target_id
            AND (NOT v_grating_filter_active OR v_gratings_mode = 'none' OR s.grating = ANY(p_gratings))
          ),
          '[]'::jsonb
        ),
        'row_num', ids.row_num
      ) AS obj_json,
      ids.total_count,
      ids.row_num
    FROM ids
    JOIN targets t ON t.target_id = ids.target_id
    LEFT JOIN programs pr ON t.program_slug = pr.slug
  )
  SELECT
    COALESCE(jsonb_agg(wr.obj_json ORDER BY wr.row_num), '[]'::jsonb),
    COALESCE(MAX(wr.total_count), 0)::BIGINT,
    p_page,
    p_page_size
  FROM with_relations wr;
END;
$$;


-- ============================================================
-- 7. RPC: get_filtered_spectra_paginated (name stays, body updated)
-- ============================================================

CREATE OR REPLACE FUNCTION public.get_filtered_spectra_paginated(
  p_program_slugs TEXT[],
  p_filter_programs TEXT[] DEFAULT NULL,
  p_fields TEXT[] DEFAULT NULL,
  p_gratings TEXT[] DEFAULT NULL,
  p_gratings_mode TEXT DEFAULT 'any',
  p_observations TEXT[] DEFAULT NULL,
  p_redshift_quality INTEGER[] DEFAULT NULL,
  p_redshift_min DOUBLE PRECISION DEFAULT NULL,
  p_redshift_max DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_min DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_max DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_min DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_max DOUBLE PRECISION DEFAULT NULL,
  p_spectral_features_include_any INTEGER DEFAULT NULL,
  p_spectral_features_include_all INTEGER DEFAULT NULL,
  p_spectral_features_exclude INTEGER DEFAULT NULL,
  p_object_flags_include_any INTEGER DEFAULT NULL,
  p_object_flags_include_all INTEGER DEFAULT NULL,
  p_object_flags_exclude INTEGER DEFAULT NULL,
  p_dq_flags_include_any INTEGER DEFAULT NULL,
  p_dq_flags_include_all INTEGER DEFAULT NULL,
  p_dq_flags_exclude INTEGER DEFAULT NULL,
  p_search TEXT DEFAULT NULL,
  p_inspected_only BOOLEAN DEFAULT NULL,
  p_comment_search TEXT DEFAULT NULL,
  p_comment_search_scope TEXT DEFAULT NULL,
  p_comment_user_id UUID DEFAULT NULL,
  p_coord_ra DOUBLE PRECISION DEFAULT NULL,
  p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_sort_column TEXT DEFAULT 'target_id',
  p_sort_direction TEXT DEFAULT 'asc',
  p_page INTEGER DEFAULT 1,
  p_page_size INTEGER DEFAULT 50,
  p_include_thumbnails BOOLEAN DEFAULT false
)
RETURNS TABLE(targets JSONB, total_count BIGINT, page INTEGER, page_size INTEGER)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_offset INTEGER;
  v_total_count BIGINT;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (
    p_comment_search IS NOT NULL AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN v_gratings_mode := 'any'; END IF;
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'asc'; END IF;
  IF NOT (p_sort_column IN (
    'target_id', 'field', 'observation', 'ra', 'dec', 'redshift',
    'redshift_quality', 'signal_to_noise', 'exposure_time', 'grating'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'target_id';
  END IF;
  IF v_coord_search_active AND p_sort_column = 'target_id' AND p_sort_direction = 'asc' THEN
    p_sort_column := 'distance';
  END IF;
  v_offset := (COALESCE(p_page, 1) - 1) * COALESCE(p_page_size, 50);
  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs))
    INTO v_filtered_program_slugs;
  ELSE
    v_filtered_program_slugs := p_program_slugs;
  END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN
    RETURN QUERY SELECT '[]'::jsonb, 0::BIGINT, p_page, p_page_size;
    RETURN;
  END IF;

  -- Count query (separate from page query for performance)
  SELECT COUNT(*) INTO v_total_count
  FROM targets t
  JOIN spectra s ON s.target_id = t.target_id
  WHERE
    t.program_slug = ANY(v_filtered_program_slugs)
    AND (NOT v_grating_filter_active OR s.grating = ANY(p_gratings))
    AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
    AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
    AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR t.redshift_quality = ANY(p_redshift_quality))
    AND (p_redshift_min IS NULL OR t.redshift >= p_redshift_min)
    AND (p_redshift_max IS NULL OR t.redshift <= p_redshift_max)
    AND (p_max_snr_min IS NULL OR t.max_snr >= p_max_snr_min)
    AND (p_max_snr_max IS NULL OR t.max_snr <= p_max_snr_max)
    AND (p_max_exposure_time_min IS NULL OR t.max_exposure_time >= p_max_exposure_time_min)
    AND (p_max_exposure_time_max IS NULL OR t.max_exposure_time <= p_max_exposure_time_max)
    AND (p_spectral_features_include_any IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_any) != 0)
    AND (p_spectral_features_include_all IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_all) = p_spectral_features_include_all)
    AND (p_spectral_features_exclude IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_exclude) = 0)
    AND (p_object_flags_include_any IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_any) != 0)
    AND (p_object_flags_include_all IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_all) = p_object_flags_include_all)
    AND (p_object_flags_exclude IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_exclude) = 0)
    AND (p_dq_flags_include_any IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_any) != 0)
    AND (p_dq_flags_include_all IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
    AND (p_dq_flags_exclude IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_exclude) = 0)
    AND (p_search IS NULL OR t.target_id ILIKE '%' || p_search || '%')
    AND (p_inspected_only IS NULL
      OR (p_inspected_only = TRUE AND t.redshift_quality > 0)
      OR (p_inspected_only = FALSE AND t.redshift_quality = 0))
    AND (NOT v_comment_search_active OR EXISTS (
      SELECT 1 FROM comments c WHERE c.target_id = t.id AND c.is_deleted = false
        AND c.content ILIKE '%' || p_comment_search || '%'
        AND (p_comment_search_scope = 'everyone' OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id))
    ))
    AND (NOT v_coord_search_active OR (
      t.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
      AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
      AND 2 * DEGREES(ASIN(SQRT(
        POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) +
        COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) *
        POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2)
      ))) <= p_radius_degrees
    ));

  -- Page query
  RETURN QUERY
  WITH filtered_spectra AS (
    SELECT
      t.id AS tgt_db_id, t.target_id, t.program_slug, t.field, t.observation,
      t.ra, t.dec, t.redshift, t.redshift_auto, t.redshift_inspected, t.redshift_quality,
      COALESCE(t.spectral_features, 0) AS spectral_features,
      COALESCE(t.object_flags, 0) AS object_flags,
      COALESCE(t.dq_flags, 0) AS dq_flags,
      t.max_snr, t.max_exposure_time, t.last_inspected_at, t.last_inspected_by,
      t.created_at, t.updated_at,
      s.id AS spectrum_id, s.grating, s.fits_path, s.signal_to_noise, s.exposure_time,
      s.file_hash, s.file_size, s.thumbnail_svg_fnu, s.thumbnail_svg_flambda,
      CASE WHEN v_coord_search_active THEN
        2 * DEGREES(ASIN(SQRT(
          POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) +
          COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) *
          POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2)
        )))
      ELSE NULL END AS distance
    FROM targets t
    JOIN spectra s ON s.target_id = t.target_id
    WHERE
      t.program_slug = ANY(v_filtered_program_slugs)
      AND (NOT v_grating_filter_active OR s.grating = ANY(p_gratings))
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR t.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR t.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR t.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR t.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR t.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR t.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR t.max_exposure_time <= p_max_exposure_time_max)
      AND (p_spectral_features_include_any IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_any) != 0)
      AND (p_spectral_features_include_all IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_all) = p_spectral_features_include_all)
      AND (p_spectral_features_exclude IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_exclude) = 0)
      AND (p_object_flags_include_any IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_any) != 0)
      AND (p_object_flags_include_all IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_all) = p_object_flags_include_all)
      AND (p_object_flags_exclude IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_exclude) = 0)
      AND (p_dq_flags_include_any IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_any) != 0)
      AND (p_dq_flags_include_all IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
      AND (p_dq_flags_exclude IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_exclude) = 0)
      AND (p_search IS NULL OR t.target_id ILIKE '%' || p_search || '%')
      AND (p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND t.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND t.redshift_quality = 0))
      AND (NOT v_comment_search_active OR EXISTS (
        SELECT 1 FROM comments c WHERE c.target_id = t.id AND c.is_deleted = false
          AND c.content ILIKE '%' || p_comment_search || '%'
          AND (p_comment_search_scope = 'everyone' OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id))
      ))
      AND (NOT v_coord_search_active OR (
        t.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
        AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
      ))
  ),
  distance_filtered AS (
    SELECT fs.* FROM filtered_spectra fs
    WHERE NOT v_coord_search_active OR fs.distance <= p_radius_degrees
  ),
  page_rows AS (
    SELECT * FROM distance_filtered
    ORDER BY
      CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'asc' THEN distance END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'desc' THEN distance END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'asc' THEN target_id END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'desc' THEN target_id END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN field END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN field END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN observation END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN observation END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN ra END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN ra END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN "dec" END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN "dec" END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN redshift END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN redshift END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN redshift_quality END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN redshift_quality END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'signal_to_noise' AND p_sort_direction = 'asc' THEN signal_to_noise END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'signal_to_noise' AND p_sort_direction = 'desc' THEN signal_to_noise END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'exposure_time' AND p_sort_direction = 'asc' THEN exposure_time END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'exposure_time' AND p_sort_direction = 'desc' THEN exposure_time END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'grating' AND p_sort_direction = 'asc' THEN grating END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'grating' AND p_sort_direction = 'desc' THEN grating END DESC NULLS LAST,
      target_id ASC, grating ASC
    LIMIT p_page_size OFFSET v_offset
  )
  SELECT
    COALESCE(jsonb_agg(jsonb_build_object(
      'id', r.tgt_db_id,
      'target_id', r.target_id,
      'program_slug', r.program_slug,
      'program_name', pr.program_name,
      'field', r.field,
      'observation', r.observation,
      'ra', r.ra,
      'dec', r.dec,
      'redshift', r.redshift,
      'redshift_auto', r.redshift_auto,
      'redshift_inspected', r.redshift_inspected,
      'redshift_quality', r.redshift_quality,
      'spectral_features', r.spectral_features,
      'object_flags', r.object_flags,
      'dq_flags', r.dq_flags,
      'max_snr', r.max_snr,
      'max_exposure_time', r.max_exposure_time,
      'last_inspected_at', r.last_inspected_at,
      'last_inspected_by', r.last_inspected_by,
      'created_at', r.created_at,
      'updated_at', r.updated_at,
      'distance', CASE WHEN v_coord_search_active THEN r.distance ELSE NULL END,
      'spectra', jsonb_build_array(jsonb_build_object(
        'id', r.spectrum_id,
        'target_id', r.target_id,
        'grating', r.grating,
        'fits_path', r.fits_path,
        'signal_to_noise', r.signal_to_noise,
        'exposure_time', r.exposure_time,
        'file_hash', r.file_hash,
        'file_size', r.file_size,
        'thumbnail_svg_fnu', CASE WHEN p_include_thumbnails THEN r.thumbnail_svg_fnu ELSE NULL END,
        'thumbnail_svg_flambda', CASE WHEN p_include_thumbnails THEN r.thumbnail_svg_flambda ELSE NULL END
      ))
    )), '[]'::jsonb),
    v_total_count,
    p_page,
    p_page_size
  FROM page_rows r
  LEFT JOIN programs pr ON pr.slug = r.program_slug;
END;
$$;


-- ============================================================
-- 8. RPC: get_adjacent_targets (renamed from get_adjacent_objects)
-- ============================================================

CREATE OR REPLACE FUNCTION public.get_adjacent_targets(
  p_current_target_id TEXT,
  p_program_slugs TEXT[],
  p_filter_programs TEXT[] DEFAULT NULL,
  p_fields TEXT[] DEFAULT NULL,
  p_gratings TEXT[] DEFAULT NULL,
  p_gratings_mode TEXT DEFAULT 'any',
  p_observations TEXT[] DEFAULT NULL,
  p_redshift_quality INTEGER[] DEFAULT NULL,
  p_redshift_min DOUBLE PRECISION DEFAULT NULL,
  p_redshift_max DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_min DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_max DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_min DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_max DOUBLE PRECISION DEFAULT NULL,
  p_spectral_features INTEGER DEFAULT NULL,
  p_object_flags INTEGER DEFAULT NULL,
  p_dq_flags INTEGER DEFAULT NULL,
  p_spectral_features_include_any INTEGER DEFAULT NULL,
  p_spectral_features_include_all INTEGER DEFAULT NULL,
  p_spectral_features_exclude INTEGER DEFAULT NULL,
  p_object_flags_include_any INTEGER DEFAULT NULL,
  p_object_flags_include_all INTEGER DEFAULT NULL,
  p_object_flags_exclude INTEGER DEFAULT NULL,
  p_dq_flags_include_any INTEGER DEFAULT NULL,
  p_dq_flags_include_all INTEGER DEFAULT NULL,
  p_dq_flags_exclude INTEGER DEFAULT NULL,
  p_search TEXT DEFAULT NULL,
  p_inspected_only BOOLEAN DEFAULT NULL,
  p_comment_search TEXT DEFAULT NULL,
  p_comment_search_scope TEXT DEFAULT NULL,
  p_comment_user_id UUID DEFAULT NULL,
  p_coord_ra DOUBLE PRECISION DEFAULT NULL,
  p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_sort_column TEXT DEFAULT 'target_id',
  p_sort_direction TEXT DEFAULT 'asc'
)
RETURNS TABLE(prev_target_id TEXT, next_target_id TEXT, current_index BIGINT, total_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_sort_is_text BOOLEAN;
  v_sf_include_any INTEGER;
  v_sf_include_all INTEGER;
  v_sf_exclude INTEGER;
  v_of_include_any INTEGER;
  v_of_include_all INTEGER;
  v_of_exclude INTEGER;
  v_dq_include_any INTEGER;
  v_dq_include_all INTEGER;
  v_dq_exclude INTEGER;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (
    p_comment_search IS NOT NULL AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN v_gratings_mode := 'any'; END IF;
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'asc'; END IF;
  IF NOT (p_sort_column IN ('target_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'max_snr', 'max_exposure_time')
       OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'target_id';
  END IF;
  IF v_coord_search_active THEN p_sort_column := 'distance'; p_sort_direction := 'asc'; END IF;
  v_sort_is_text := p_sort_column IN ('target_id', 'field', 'observation');
  v_sf_include_any := COALESCE(p_spectral_features_include_any, p_spectral_features);
  v_sf_include_all := p_spectral_features_include_all;
  v_sf_exclude := p_spectral_features_exclude;
  v_of_include_any := COALESCE(p_object_flags_include_any, p_object_flags);
  v_of_include_all := p_object_flags_include_all;
  v_of_exclude := p_object_flags_exclude;
  v_dq_include_any := COALESCE(p_dq_flags_include_any, p_dq_flags);
  v_dq_include_all := p_dq_flags_include_all;
  v_dq_exclude := p_dq_flags_exclude;
  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs))
    INTO v_filtered_program_slugs;
  ELSE
    v_filtered_program_slugs := p_program_slugs;
  END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN
    RETURN QUERY SELECT NULL::TEXT, NULL::TEXT, 0::BIGINT, 0::BIGINT;
    RETURN;
  END IF;

  RETURN QUERY
  WITH filtered_targets AS MATERIALIZED (
    SELECT
      t.target_id,
      CASE WHEN v_coord_search_active THEN
        2 * DEGREES(ASIN(SQRT(
          POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) +
          COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) *
          POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2)
        )))
      ELSE NULL END AS distance,
      t.field, t.observation, t.ra, t.dec, t.redshift, t.redshift_quality, t.max_snr, t.max_exposure_time
    FROM targets t
    WHERE
      t.program_slug = ANY(v_filtered_program_slugs)
      AND (
        NOT v_grating_filter_active
        OR (v_gratings_mode = 'any' AND EXISTS (SELECT 1 FROM spectra gs WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)))
        OR (v_gratings_mode = 'all' AND (SELECT COUNT(DISTINCT gs.grating) FROM spectra gs WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)) = array_length(p_gratings, 1))
        OR (v_gratings_mode = 'none' AND NOT EXISTS (SELECT 1 FROM spectra gs WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)))
      )
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR t.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR t.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR t.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR t.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR t.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR t.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR t.max_exposure_time <= p_max_exposure_time_max)
      AND (v_sf_include_any IS NULL OR (COALESCE(t.spectral_features, 0) & v_sf_include_any) != 0)
      AND (v_sf_include_all IS NULL OR (COALESCE(t.spectral_features, 0) & v_sf_include_all) = v_sf_include_all)
      AND (v_sf_exclude IS NULL OR (COALESCE(t.spectral_features, 0) & v_sf_exclude) = 0)
      AND (v_of_include_any IS NULL OR (COALESCE(t.object_flags, 0) & v_of_include_any) != 0)
      AND (v_of_include_all IS NULL OR (COALESCE(t.object_flags, 0) & v_of_include_all) = v_of_include_all)
      AND (v_of_exclude IS NULL OR (COALESCE(t.object_flags, 0) & v_of_exclude) = 0)
      AND (v_dq_include_any IS NULL OR (COALESCE(t.dq_flags, 0) & v_dq_include_any) != 0)
      AND (v_dq_include_all IS NULL OR (COALESCE(t.dq_flags, 0) & v_dq_include_all) = v_dq_include_all)
      AND (v_dq_exclude IS NULL OR (COALESCE(t.dq_flags, 0) & v_dq_exclude) = 0)
      AND (p_search IS NULL OR t.target_id ILIKE '%' || p_search || '%')
      AND (p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND t.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND t.redshift_quality = 0))
      AND (NOT v_comment_search_active OR EXISTS (
        SELECT 1 FROM comments c WHERE c.target_id = t.id AND c.is_deleted = false
          AND c.content ILIKE '%' || p_comment_search || '%'
          AND (p_comment_search_scope = 'everyone' OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id))
      ))
      AND (NOT v_coord_search_active OR (
        t.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
        AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
      ))
  ),
  distance_filtered AS MATERIALIZED (
    SELECT
      ft.*,
      CASE p_sort_column
        WHEN 'target_id' THEN ft.target_id WHEN 'field' THEN ft.field WHEN 'observation' THEN ft.observation ELSE NULL
      END AS sort_text,
      CASE p_sort_column
        WHEN 'ra' THEN ft.ra WHEN 'dec' THEN ft.dec WHEN 'redshift' THEN ft.redshift::DOUBLE PRECISION
        WHEN 'redshift_quality' THEN ft.redshift_quality::DOUBLE PRECISION
        WHEN 'max_snr' THEN ft.max_snr WHEN 'max_exposure_time' THEN ft.max_exposure_time
        WHEN 'distance' THEN ft.distance ELSE NULL
      END AS sort_num
    FROM filtered_targets ft
    WHERE NOT v_coord_search_active OR ft.distance <= p_radius_degrees
  ),
  current_tgt AS (
    SELECT df.sort_text, df.sort_num, df.target_id FROM distance_filtered df WHERE df.target_id = p_current_target_id
  )
  SELECT
    (SELECT df.target_id FROM distance_filtered df, current_tgt c
     WHERE CASE WHEN v_sort_is_text THEN
       (CASE WHEN p_sort_direction = 'asc' THEN df.sort_text < c.sort_text ELSE df.sort_text > c.sort_text END)
       OR (df.sort_text IS NOT DISTINCT FROM c.sort_text AND df.target_id < c.target_id)
       OR (df.sort_text IS NOT NULL AND c.sort_text IS NULL)
     ELSE
       (CASE WHEN p_sort_direction = 'asc' THEN df.sort_num < c.sort_num ELSE df.sort_num > c.sort_num END)
       OR (df.sort_num IS NOT DISTINCT FROM c.sort_num AND df.target_id < c.target_id)
       OR (df.sort_num IS NOT NULL AND c.sort_num IS NULL)
     END
     ORDER BY
       CASE WHEN v_sort_is_text AND p_sort_direction = 'asc' THEN df.sort_text END DESC NULLS FIRST,
       CASE WHEN v_sort_is_text AND p_sort_direction = 'desc' THEN df.sort_text END ASC NULLS FIRST,
       CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'asc' THEN df.sort_num END DESC NULLS FIRST,
       CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'desc' THEN df.sort_num END ASC NULLS FIRST,
       df.target_id DESC
     LIMIT 1
    ) AS prev_target_id,
    (SELECT df.target_id FROM distance_filtered df, current_tgt c
     WHERE CASE WHEN v_sort_is_text THEN
       (CASE WHEN p_sort_direction = 'asc' THEN df.sort_text > c.sort_text ELSE df.sort_text < c.sort_text END)
       OR (df.sort_text IS NOT DISTINCT FROM c.sort_text AND df.target_id > c.target_id)
       OR (c.sort_text IS NOT NULL AND df.sort_text IS NULL)
     ELSE
       (CASE WHEN p_sort_direction = 'asc' THEN df.sort_num > c.sort_num ELSE df.sort_num < c.sort_num END)
       OR (df.sort_num IS NOT DISTINCT FROM c.sort_num AND df.target_id > c.target_id)
       OR (c.sort_num IS NOT NULL AND df.sort_num IS NULL)
     END
     ORDER BY
       CASE WHEN v_sort_is_text AND p_sort_direction = 'asc' THEN df.sort_text END ASC NULLS LAST,
       CASE WHEN v_sort_is_text AND p_sort_direction = 'desc' THEN df.sort_text END DESC NULLS LAST,
       CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'asc' THEN df.sort_num END ASC NULLS LAST,
       CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'desc' THEN df.sort_num END DESC NULLS LAST,
       df.target_id ASC
     LIMIT 1
    ) AS next_target_id,
    CASE WHEN EXISTS (SELECT 1 FROM current_tgt) THEN (
      SELECT COUNT(*) + 1
      FROM distance_filtered df, current_tgt c
      WHERE CASE WHEN v_sort_is_text THEN
        (CASE WHEN p_sort_direction = 'asc' THEN df.sort_text < c.sort_text ELSE df.sort_text > c.sort_text END)
        OR (df.sort_text IS NOT DISTINCT FROM c.sort_text AND df.target_id < c.target_id)
        OR (df.sort_text IS NOT NULL AND c.sort_text IS NULL)
      ELSE
        (CASE WHEN p_sort_direction = 'asc' THEN df.sort_num < c.sort_num ELSE df.sort_num > c.sort_num END)
        OR (df.sort_num IS NOT DISTINCT FROM c.sort_num AND df.target_id < c.target_id)
        OR (df.sort_num IS NOT NULL AND c.sort_num IS NULL)
      END
    )::BIGINT ELSE 0::BIGINT END AS current_index,
    (SELECT COUNT(*) FROM distance_filtered)::BIGINT AS total_count;
END;
$$;


-- ============================================================
-- 9. RPC: get_csv_export
-- ============================================================

CREATE OR REPLACE FUNCTION public.get_csv_export(
  p_program_slugs TEXT[], p_filter_programs TEXT[] DEFAULT NULL,
  p_fields TEXT[] DEFAULT NULL, p_gratings TEXT[] DEFAULT NULL,
  p_gratings_mode TEXT DEFAULT 'any', p_observations TEXT[] DEFAULT NULL,
  p_redshift_quality INTEGER[] DEFAULT NULL,
  p_redshift_min DOUBLE PRECISION DEFAULT NULL, p_redshift_max DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_min DOUBLE PRECISION DEFAULT NULL, p_max_snr_max DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_min DOUBLE PRECISION DEFAULT NULL, p_max_exposure_time_max DOUBLE PRECISION DEFAULT NULL,
  p_spectral_features_include_any INTEGER DEFAULT NULL, p_spectral_features_include_all INTEGER DEFAULT NULL,
  p_spectral_features_exclude INTEGER DEFAULT NULL,
  p_object_flags_include_any INTEGER DEFAULT NULL, p_object_flags_include_all INTEGER DEFAULT NULL,
  p_object_flags_exclude INTEGER DEFAULT NULL,
  p_dq_flags_include_any INTEGER DEFAULT NULL, p_dq_flags_include_all INTEGER DEFAULT NULL,
  p_dq_flags_exclude INTEGER DEFAULT NULL,
  p_search TEXT DEFAULT NULL, p_inspected_only BOOLEAN DEFAULT NULL,
  p_comment_search TEXT DEFAULT NULL, p_comment_search_scope TEXT DEFAULT NULL,
  p_comment_user_id UUID DEFAULT NULL,
  p_coord_ra DOUBLE PRECISION DEFAULT NULL, p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_sort_column TEXT DEFAULT 'target_id', p_sort_direction TEXT DEFAULT 'asc'
)
RETURNS TABLE(
  target_id TEXT, field TEXT, ra DOUBLE PRECISION, "dec" DOUBLE PRECISION,
  redshift NUMERIC, redshift_quality INTEGER, max_snr DOUBLE PRECISION,
  max_exposure_time DOUBLE PRECISION, num_gratings INTEGER,
  program_slug TEXT, program_name TEXT, last_inspected_at TIMESTAMPTZ,
  last_inspected_by TEXT, distance DOUBLE PRECISION,
  spectral_features INTEGER, object_flags INTEGER, dq_flags INTEGER
)
LANGUAGE plpgsql STABLE SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (p_comment_search IS NOT NULL AND p_comment_search != '' AND p_comment_search_scope IN ('just_me', 'everyone'));
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN v_gratings_mode := 'any'; END IF;
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'asc'; END IF;
  IF NOT (p_sort_column IN ('target_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'max_snr', 'max_exposure_time')
       OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'target_id';
  END IF;
  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs)) INTO v_filtered_program_slugs;
  ELSE v_filtered_program_slugs := p_program_slugs; END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN RETURN; END IF;

  RETURN QUERY
  WITH filtered_targets AS (
    SELECT t.target_id, t.field, t.ra, t.dec, t.redshift, t.redshift_quality,
      t.max_snr, t.max_exposure_time,
      (SELECT COUNT(*)::INTEGER FROM spectra s WHERE s.target_id = t.target_id) AS num_gratings,
      t.program_slug, t.observation, t.last_inspected_at, t.last_inspected_by,
      CASE WHEN v_coord_search_active THEN
        2 * DEGREES(ASIN(SQRT(POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) + COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) * POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2))))
      ELSE NULL END AS distance,
      COALESCE(t.spectral_features, 0) AS spectral_features,
      COALESCE(t.object_flags, 0) AS object_flags,
      COALESCE(t.dq_flags, 0) AS dq_flags
    FROM targets t
    WHERE t.program_slug = ANY(v_filtered_program_slugs)
      AND (NOT v_grating_filter_active
        OR (v_gratings_mode = 'any' AND EXISTS (SELECT 1 FROM spectra gs WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)))
        OR (v_gratings_mode = 'all' AND (SELECT COUNT(DISTINCT gs.grating) FROM spectra gs WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)) = array_length(p_gratings, 1))
        OR (v_gratings_mode = 'none' AND NOT EXISTS (SELECT 1 FROM spectra gs WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings))))
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR t.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR t.redshift >= p_redshift_min) AND (p_redshift_max IS NULL OR t.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR t.max_snr >= p_max_snr_min) AND (p_max_snr_max IS NULL OR t.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR t.max_exposure_time >= p_max_exposure_time_min) AND (p_max_exposure_time_max IS NULL OR t.max_exposure_time <= p_max_exposure_time_max)
      AND (p_spectral_features_include_any IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_any) != 0)
      AND (p_spectral_features_include_all IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_all) = p_spectral_features_include_all)
      AND (p_spectral_features_exclude IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_exclude) = 0)
      AND (p_object_flags_include_any IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_any) != 0)
      AND (p_object_flags_include_all IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_all) = p_object_flags_include_all)
      AND (p_object_flags_exclude IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_exclude) = 0)
      AND (p_dq_flags_include_any IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_any) != 0)
      AND (p_dq_flags_include_all IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
      AND (p_dq_flags_exclude IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_exclude) = 0)
      AND (p_search IS NULL OR t.target_id ILIKE '%' || p_search || '%')
      AND (p_inspected_only IS NULL OR (p_inspected_only = TRUE AND t.redshift_quality > 0) OR (p_inspected_only = FALSE AND t.redshift_quality = 0))
      AND (NOT v_comment_search_active OR EXISTS (
        SELECT 1 FROM comments c WHERE c.target_id = t.id AND c.is_deleted = false
          AND c.content ILIKE '%' || p_comment_search || '%'
          AND (p_comment_search_scope = 'everyone' OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id))))
      AND (NOT v_coord_search_active OR (
        t.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
        AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)))
  ),
  distance_filtered AS (SELECT ft.* FROM filtered_targets ft WHERE NOT v_coord_search_active OR ft.distance <= p_radius_degrees)
  SELECT df.target_id, df.field, df.ra, df.dec, df.redshift, df.redshift_quality,
    df.max_snr, df.max_exposure_time, df.num_gratings, df.program_slug,
    pr.program_name, df.last_inspected_at, up.full_name AS last_inspected_by,
    df.distance, df.spectral_features, df.object_flags, df.dq_flags
  FROM distance_filtered df
  LEFT JOIN programs pr ON pr.slug = df.program_slug
  LEFT JOIN user_profiles up ON up.user_id = df.last_inspected_by
  ORDER BY
    CASE WHEN v_coord_search_active THEN df.distance END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'target_id' AND p_sort_direction = 'asc' THEN df.target_id END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'target_id' AND p_sort_direction = 'desc' THEN df.target_id END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN df.observation END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN df.observation END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN df.ra END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN df.ra END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN df.dec END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN df.dec END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN df.redshift END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN df.redshift END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN df.redshift_quality END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN df.redshift_quality END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN df.max_snr END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN df.max_snr END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN df.max_exposure_time END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN df.max_exposure_time END DESC NULLS LAST,
    df.target_id ASC;
END;
$$;


-- ============================================================
-- 10. RPC: get_csv_export_spectra
-- ============================================================

CREATE OR REPLACE FUNCTION public.get_csv_export_spectra(
  p_program_slugs TEXT[], p_filter_programs TEXT[] DEFAULT NULL,
  p_fields TEXT[] DEFAULT NULL, p_gratings TEXT[] DEFAULT NULL,
  p_gratings_mode TEXT DEFAULT 'any', p_observations TEXT[] DEFAULT NULL,
  p_redshift_quality INTEGER[] DEFAULT NULL,
  p_redshift_min DOUBLE PRECISION DEFAULT NULL, p_redshift_max DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_min DOUBLE PRECISION DEFAULT NULL, p_max_snr_max DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_min DOUBLE PRECISION DEFAULT NULL, p_max_exposure_time_max DOUBLE PRECISION DEFAULT NULL,
  p_spectral_features_include_any INTEGER DEFAULT NULL, p_spectral_features_include_all INTEGER DEFAULT NULL,
  p_spectral_features_exclude INTEGER DEFAULT NULL,
  p_object_flags_include_any INTEGER DEFAULT NULL, p_object_flags_include_all INTEGER DEFAULT NULL,
  p_object_flags_exclude INTEGER DEFAULT NULL,
  p_dq_flags_include_any INTEGER DEFAULT NULL, p_dq_flags_include_all INTEGER DEFAULT NULL,
  p_dq_flags_exclude INTEGER DEFAULT NULL,
  p_search TEXT DEFAULT NULL, p_inspected_only BOOLEAN DEFAULT NULL,
  p_comment_search TEXT DEFAULT NULL, p_comment_search_scope TEXT DEFAULT NULL,
  p_comment_user_id UUID DEFAULT NULL,
  p_coord_ra DOUBLE PRECISION DEFAULT NULL, p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_sort_column TEXT DEFAULT 'target_id', p_sort_direction TEXT DEFAULT 'asc'
)
RETURNS TABLE(
  target_id TEXT, grating TEXT, field TEXT, ra DOUBLE PRECISION, "dec" DOUBLE PRECISION,
  redshift NUMERIC, redshift_quality INTEGER, signal_to_noise DOUBLE PRECISION,
  exposure_time DOUBLE PRECISION, fits_path TEXT, program_slug TEXT, program_name TEXT,
  last_inspected_at TIMESTAMPTZ, last_inspected_by TEXT, distance DOUBLE PRECISION,
  spectral_features INTEGER, object_flags INTEGER, dq_flags INTEGER
)
LANGUAGE plpgsql STABLE SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (p_comment_search IS NOT NULL AND p_comment_search != '' AND p_comment_search_scope IN ('just_me', 'everyone'));
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'asc'; END IF;
  IF NOT (p_sort_column IN ('target_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'signal_to_noise', 'exposure_time', 'grating')
       OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'target_id';
  END IF;
  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs)) INTO v_filtered_program_slugs;
  ELSE v_filtered_program_slugs := p_program_slugs; END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN RETURN; END IF;

  RETURN QUERY
  WITH filtered_spectra AS (
    SELECT t.target_id, s.grating, t.field, t.ra, t.dec, t.redshift, t.redshift_quality,
      s.signal_to_noise, s.exposure_time, s.fits_path, t.program_slug, t.observation,
      t.last_inspected_at, t.last_inspected_by,
      CASE WHEN v_coord_search_active THEN
        2 * DEGREES(ASIN(SQRT(POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) + COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) * POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2))))
      ELSE NULL END AS distance,
      COALESCE(t.spectral_features, 0) AS spectral_features,
      COALESCE(t.object_flags, 0) AS object_flags,
      COALESCE(t.dq_flags, 0) AS dq_flags
    FROM targets t JOIN spectra s ON s.target_id = t.target_id
    WHERE t.program_slug = ANY(v_filtered_program_slugs)
      AND (NOT v_grating_filter_active OR s.grating = ANY(p_gratings))
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR t.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR t.redshift >= p_redshift_min) AND (p_redshift_max IS NULL OR t.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR t.max_snr >= p_max_snr_min) AND (p_max_snr_max IS NULL OR t.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR t.max_exposure_time >= p_max_exposure_time_min) AND (p_max_exposure_time_max IS NULL OR t.max_exposure_time <= p_max_exposure_time_max)
      AND (p_spectral_features_include_any IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_any) != 0)
      AND (p_spectral_features_include_all IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_all) = p_spectral_features_include_all)
      AND (p_spectral_features_exclude IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_exclude) = 0)
      AND (p_object_flags_include_any IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_any) != 0)
      AND (p_object_flags_include_all IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_all) = p_object_flags_include_all)
      AND (p_object_flags_exclude IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_exclude) = 0)
      AND (p_dq_flags_include_any IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_any) != 0)
      AND (p_dq_flags_include_all IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
      AND (p_dq_flags_exclude IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_exclude) = 0)
      AND (p_search IS NULL OR t.target_id ILIKE '%' || p_search || '%')
      AND (p_inspected_only IS NULL OR (p_inspected_only = TRUE AND t.redshift_quality > 0) OR (p_inspected_only = FALSE AND t.redshift_quality = 0))
      AND (NOT v_comment_search_active OR EXISTS (
        SELECT 1 FROM comments c WHERE c.target_id = t.id AND c.is_deleted = false
          AND c.content ILIKE '%' || p_comment_search || '%'
          AND (p_comment_search_scope = 'everyone' OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id))))
      AND (NOT v_coord_search_active OR (
        t.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
        AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)))
  ),
  distance_filtered AS (SELECT fs.* FROM filtered_spectra fs WHERE NOT v_coord_search_active OR fs.distance <= p_radius_degrees)
  SELECT df.target_id, df.grating, df.field, df.ra, df.dec, df.redshift, df.redshift_quality,
    df.signal_to_noise, df.exposure_time, df.fits_path, df.program_slug,
    pr.program_name, df.last_inspected_at, up.full_name AS last_inspected_by,
    df.distance, df.spectral_features, df.object_flags, df.dq_flags
  FROM distance_filtered df
  LEFT JOIN programs pr ON pr.slug = df.program_slug
  LEFT JOIN user_profiles up ON up.user_id = df.last_inspected_by
  ORDER BY
    CASE WHEN v_coord_search_active THEN df.distance END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'target_id' AND p_sort_direction = 'asc' THEN df.target_id END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'target_id' AND p_sort_direction = 'desc' THEN df.target_id END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN df.observation END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN df.observation END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN df.ra END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN df.ra END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN df.dec END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN df.dec END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN df.redshift END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN df.redshift END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN df.redshift_quality END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN df.redshift_quality END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'signal_to_noise' AND p_sort_direction = 'asc' THEN df.signal_to_noise END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'signal_to_noise' AND p_sort_direction = 'desc' THEN df.signal_to_noise END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'exposure_time' AND p_sort_direction = 'asc' THEN df.exposure_time END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'exposure_time' AND p_sort_direction = 'desc' THEN df.exposure_time END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'grating' AND p_sort_direction = 'asc' THEN df.grating END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'grating' AND p_sort_direction = 'desc' THEN df.grating END DESC NULLS LAST,
    df.target_id ASC, df.grating ASC;
END;
$$;


-- ============================================================
-- 11. Smaller RPCs
-- ============================================================

CREATE OR REPLACE FUNCTION public.get_observation_stats(p_program_slugs text[])
RETURNS TABLE(
  observation text, program_slug text, program_name text, field text,
  target_count bigint, spectrum_count bigint, total_size_bytes bigint
) LANGUAGE sql STABLE AS $$
  SELECT t.observation, t.program_slug, p.program_name, t.field,
    COUNT(DISTINCT t.target_id) AS target_count,
    COUNT(s.id) AS spectrum_count,
    COALESCE(SUM(s.file_size), 0)::bigint AS total_size_bytes
  FROM targets t
  JOIN programs p ON p.slug = t.program_slug
  LEFT JOIN spectra s ON s.target_id = t.target_id
  WHERE t.program_slug = ANY(p_program_slugs)
  GROUP BY t.observation, t.program_slug, p.program_name, t.field
  ORDER BY t.observation;
$$;

CREATE OR REPLACE FUNCTION public.get_observation_manifest(p_obs_name text, p_program_slugs text[])
RETURNS TABLE(
  spectra_id integer, target_id text, grating text, fits_path text,
  file_hash text, file_size bigint, signal_to_noise double precision, reduction_version text
) LANGUAGE plpgsql STABLE AS $$
BEGIN
  RETURN QUERY
  SELECT s.id, s.target_id, s.grating, s.fits_path, s.file_hash, s.file_size,
         s.signal_to_noise, s.reduction_version
  FROM spectra s
  JOIN targets t ON t.target_id = s.target_id
  WHERE t.observation = p_obs_name AND t.program_slug = ANY(p_program_slugs)
  ORDER BY s.target_id, s.grating;
END;
$$;

CREATE OR REPLACE FUNCTION public.get_targets_in_viewport(
  p_ra_min double precision, p_ra_max double precision,
  p_dec_min double precision, p_dec_max double precision,
  p_field text DEFAULT NULL, p_limit integer DEFAULT 5000
)
RETURNS TABLE(
  "target_id" text, "ra" double precision, "dec" double precision,
  "redshift" double precision, "redshift_quality" integer, "field" text, "program_slug" text
) LANGUAGE plpgsql STABLE AS $$
BEGIN
  RETURN QUERY
  SELECT t.target_id, t.ra, t.dec, t.redshift::double precision, t.redshift_quality, t.field, t.program_slug
  FROM public.targets t
  WHERE t.ra BETWEEN p_ra_min AND p_ra_max AND t.dec BETWEEN p_dec_min AND p_dec_max
    AND (p_field IS NULL OR t.field = p_field)
  ORDER BY t.ra LIMIT p_limit;
END;
$$;

CREATE OR REPLACE FUNCTION public.get_targets_for_sync(
  p_program_slugs TEXT[],
  p_updated_since TIMESTAMP WITHOUT TIME ZONE DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_offset INTEGER DEFAULT 0
)
RETURNS TABLE(targets JSONB, total_count BIGINT, total_accessible_count BIGINT)
LANGUAGE plpgsql STABLE SET plan_cache_mode = 'force_custom_plan'
AS $$
BEGIN
  RETURN QUERY
  WITH matched AS (
    SELECT t.id, t.target_id, t.program_slug, t.field, t.observation,
           t.ra, t.dec, t.redshift, t.redshift_auto, t.redshift_inspected,
           t.redshift_quality, t.spectral_features, t.object_flags, t.dq_flags,
           t.max_snr, t.max_exposure_time, t.last_inspected_at, t.created_at, t.updated_at
    FROM targets t
    WHERE t.program_slug = ANY(p_program_slugs)
      AND (p_updated_since IS NULL OR t.updated_at > p_updated_since)
    ORDER BY t.target_id LIMIT p_limit OFFSET p_offset
  ),
  total AS (SELECT COUNT(*) AS cnt FROM targets t WHERE t.program_slug = ANY(p_program_slugs) AND (p_updated_since IS NULL OR t.updated_at > p_updated_since)),
  accessible AS (SELECT COUNT(*) AS cnt FROM targets t WHERE t.program_slug = ANY(p_program_slugs))
  SELECT
    COALESCE(jsonb_agg(jsonb_build_object(
      'id', m.id, 'target_id', m.target_id, 'program_slug', m.program_slug,
      'program_name', pr.program_name, 'field', m.field, 'observation', m.observation,
      'ra', m.ra, 'dec', m.dec, 'redshift', m.redshift,
      'redshift_auto', m.redshift_auto, 'redshift_inspected', m.redshift_inspected,
      'redshift_quality', m.redshift_quality, 'spectral_features', m.spectral_features,
      'object_flags', m.object_flags, 'dq_flags', m.dq_flags,
      'max_snr', m.max_snr, 'max_exposure_time', m.max_exposure_time,
      'last_inspected_at', m.last_inspected_at, 'created_at', m.created_at, 'updated_at', m.updated_at,
      'spectra', COALESCE(
        (SELECT jsonb_agg(jsonb_build_object(
          'id', s.id, 'target_id', s.target_id, 'grating', s.grating,
          'fits_path', s.fits_path, 'file_hash', s.file_hash, 'file_size', s.file_size,
          'signal_to_noise', s.signal_to_noise, 'exposure_time', s.exposure_time,
          'reduction_version', s.reduction_version
        )) FROM spectra s WHERE s.target_id = m.target_id),
        '[]'::jsonb)
    )), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m LEFT JOIN programs pr ON m.program_slug = pr.slug;
END;
$$;

CREATE OR REPLACE FUNCTION public.get_programs_overview()
RETURNS TABLE(
  slug text, program_name text, pi_name text, description text,
  is_public boolean, cycle integer, target_count bigint,
  gratings text[], fields text[], observations text[], jwst_pids integer[]
) LANGUAGE sql STABLE AS $$
  SELECT mv.slug, mv.program_name, mv.pi_name, mv.description, mv.is_public, mv.cycle,
    mv.target_count, mv.gratings, mv.fields, mv.observations, mv.jwst_pids
  FROM public.mv_programs_overview mv ORDER BY mv.program_name;
$$;

CREATE OR REPLACE FUNCTION public.get_program_stats()
RETURNS TABLE(slug text, target_count bigint, user_access_count bigint)
LANGUAGE sql STABLE SECURITY DEFINER
AS $$
  SELECT p.slug,
    COALESCE(tc.cnt, 0) AS target_count,
    COALESCE(a.cnt, 0) AS user_access_count
  FROM programs p
  LEFT JOIN (SELECT program_slug, COUNT(*) AS cnt FROM targets GROUP BY program_slug) tc ON p.slug = tc.program_slug
  LEFT JOIN (SELECT program_slug, COUNT(*) AS cnt FROM user_program_access GROUP BY program_slug) a ON p.slug = a.program_slug;
$$;


-- ============================================================
-- 11b. RECREATE MISSED FUNCTIONS
-- ============================================================

CREATE OR REPLACE FUNCTION public.propagate_crossmatch_inspection(
    p_target_id INTEGER,
    p_radius_arcsec DOUBLE PRECISION DEFAULT 0.1,
    p_redshift_tolerance DOUBLE PRECISION DEFAULT 0.01
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_source RECORD;
    v_radius_deg DOUBLE PRECISION;
    v_ra_radius_deg DOUBLE PRECISION;
    v_updated_count INTEGER := 0;
BEGIN
    SELECT id, ra, dec, redshift, redshift_auto, redshift_quality
    INTO v_source
    FROM targets
    WHERE id = p_target_id;

    IF NOT FOUND THEN
        RETURN 0;
    END IF;

    v_radius_deg := p_radius_arcsec / 3600.0;
    v_ra_radius_deg := v_radius_deg / COS(RADIANS(v_source.dec));

    IF v_source.redshift_quality = 4 AND v_source.redshift IS NOT NULL THEN
        WITH matches AS (
            SELECT t.id
            FROM targets t
            WHERE t.id != v_source.id
              AND t.redshift_quality = 0
              AND t.redshift_auto IS NOT NULL
              AND ABS(t.redshift_auto - v_source.redshift) < p_redshift_tolerance
              AND t.ra BETWEEN (v_source.ra - v_ra_radius_deg) AND (v_source.ra + v_ra_radius_deg)
              AND t.dec BETWEEN (v_source.dec - v_radius_deg) AND (v_source.dec + v_radius_deg)
              AND (2 * DEGREES(ASIN(SQRT(
                  POWER(SIN(RADIANS(t.dec - v_source.dec) / 2), 2) +
                  COS(RADIANS(v_source.dec)) * COS(RADIANS(t.dec)) *
                  POWER(SIN(RADIANS(t.ra - v_source.ra) / 2), 2)
              )))) <= v_radius_deg
        )
        UPDATE targets
        SET redshift_quality = 4,
            last_inspected_at = NOW()
        FROM matches
        WHERE targets.id = matches.id;

        GET DIAGNOSTICS v_updated_count = ROW_COUNT;

    ELSIF v_source.redshift_quality = 0 AND v_source.redshift_auto IS NOT NULL THEN
        PERFORM 1
        FROM targets t
        WHERE t.id != v_source.id
          AND t.redshift_quality = 4
          AND t.redshift IS NOT NULL
          AND ABS(v_source.redshift_auto - t.redshift) < p_redshift_tolerance
          AND t.ra BETWEEN (v_source.ra - v_ra_radius_deg) AND (v_source.ra + v_ra_radius_deg)
          AND t.dec BETWEEN (v_source.dec - v_radius_deg) AND (v_source.dec + v_radius_deg)
          AND (2 * DEGREES(ASIN(SQRT(
              POWER(SIN(RADIANS(t.dec - v_source.dec) / 2), 2) +
              COS(RADIANS(v_source.dec)) * COS(RADIANS(t.dec)) *
              POWER(SIN(RADIANS(t.ra - v_source.ra) / 2), 2)
          )))) <= v_radius_deg
        LIMIT 1;

        IF FOUND THEN
            UPDATE targets
            SET redshift_quality = 4,
                last_inspected_at = NOW()
            WHERE id = v_source.id;

            v_updated_count := 1;
        END IF;
    END IF;

    RETURN v_updated_count;
END;
$$;

CREATE OR REPLACE FUNCTION public.get_user_profile_stats(p_user_id uuid)
RETURNS json
LANGUAGE plpgsql STABLE SECURITY DEFINER
AS $$
DECLARE
  result JSON;
  targets_inspected BIGINT;
  comments_posted BIGINT;
  last_comment_at TIMESTAMPTZ;
  last_inspection_at TIMESTAMPTZ;
  last_activity TIMESTAMPTZ;
BEGIN
  SELECT COUNT(DISTINCT target_id) INTO targets_inspected
  FROM flag_audit_log
  WHERE user_id = p_user_id;

  SELECT COUNT(*) INTO comments_posted
  FROM comments
  WHERE user_id = p_user_id AND is_deleted = false;

  SELECT created_at INTO last_comment_at
  FROM comments
  WHERE user_id = p_user_id
  ORDER BY created_at DESC
  LIMIT 1;

  SELECT changed_at INTO last_inspection_at
  FROM flag_audit_log
  WHERE user_id = p_user_id
  ORDER BY changed_at DESC
  LIMIT 1;

  last_activity := GREATEST(
    COALESCE(last_comment_at, '1970-01-01'::timestamptz),
    COALESCE(last_inspection_at, '1970-01-01'::timestamptz)
  );
  IF last_activity = '1970-01-01'::timestamptz THEN
    last_activity := NULL;
  END IF;

  result := json_build_object(
    'targets_inspected', COALESCE(targets_inspected, 0),
    'comments_posted', COALESCE(comments_posted, 0),
    'last_activity', last_activity
  );

  RETURN result;
END;
$$;

CREATE OR REPLACE FUNCTION public.get_download_stats(p_days integer DEFAULT 30)
RETURNS json
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  result JSON;
  is_admin BOOLEAN;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO is_admin
  FROM user_profiles up
  WHERE up.user_id = auth.uid();

  IF NOT is_admin THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  SELECT json_build_object(
    'total_downloads', (
      SELECT COUNT(*) FROM download_log
      WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
    ),
    'unique_users', (
      SELECT COUNT(DISTINCT user_id) FROM download_log
      WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
    ),
    'by_type', (
      SELECT json_object_agg(download_type, count)
      FROM (
        SELECT download_type, COUNT(*) as count
        FROM download_log
        WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
        GROUP BY download_type
      ) t
    ),
    'total_files', (
      SELECT COALESCE(SUM(file_count), 0) FROM download_log
      WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
    ),
    'total_targets', (
      SELECT COALESCE(SUM(target_count), 0) FROM download_log
      WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
    ),
    'recent_downloads', (
      SELECT json_agg(t)
      FROM (
        SELECT
          dl.id,
          dl.download_type,
          dl.target_count,
          dl.file_count,
          dl.requested_at,
          au.email,
          up.full_name
        FROM download_log dl
        LEFT JOIN auth.users au ON dl.user_id = au.id
        LEFT JOIN user_profiles up ON dl.user_id = up.user_id
        WHERE dl.requested_at >= NOW() - (p_days || ' days')::INTERVAL
        ORDER BY dl.requested_at DESC
        LIMIT 50
      ) t
    ),
    'most_downloaded_targets', (
      SELECT json_agg(t)
      FROM (
        SELECT
          target_id,
          COUNT(*) as download_count
        FROM download_log, unnest(target_ids) as target_id
        WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
        GROUP BY target_id
        ORDER BY download_count DESC
        LIMIT 20
      ) t
    ),
    'downloads_by_day', (
      SELECT json_agg(t ORDER BY day)
      FROM (
        SELECT
          DATE(requested_at) as day,
          COUNT(*) as count
        FROM download_log
        WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
        GROUP BY DATE(requested_at)
      ) t
    )
  ) INTO result;

  RETURN result;
END;
$$;


-- ============================================================
-- 12. GRANTS
-- ============================================================

GRANT EXECUTE ON FUNCTION public.get_filtered_target_ids TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_filtered_target_ids TO service_role;
GRANT EXECUTE ON FUNCTION public.get_filtered_targets_paginated TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_filtered_targets_paginated TO service_role;
GRANT EXECUTE ON FUNCTION public.get_filtered_spectra_paginated TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_filtered_spectra_paginated TO service_role;
GRANT EXECUTE ON FUNCTION public.get_adjacent_targets TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_adjacent_targets TO service_role;
GRANT EXECUTE ON FUNCTION public.get_csv_export TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_csv_export_spectra TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_observation_stats TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_observation_manifest TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_targets_in_viewport TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_targets_for_sync TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_targets_for_sync TO service_role;
GRANT EXECUTE ON FUNCTION public.get_programs_overview TO authenticated;
GRANT ALL ON FUNCTION public.get_program_stats TO anon;
GRANT ALL ON FUNCTION public.get_program_stats TO authenticated;
GRANT ALL ON FUNCTION public.get_program_stats TO service_role;
GRANT EXECUTE ON FUNCTION public.propagate_crossmatch_inspection TO authenticated;
GRANT EXECUTE ON FUNCTION public.propagate_crossmatch_inspection TO service_role;
GRANT EXECUTE ON FUNCTION public.get_user_profile_stats TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_download_stats TO authenticated;

-- Refresh materialized views so cached data reflects renamed tables
REFRESH MATERIALIZED VIEW public.mv_filter_options;
REFRESH MATERIALIZED VIEW public.mv_programs_overview;

COMMIT;
