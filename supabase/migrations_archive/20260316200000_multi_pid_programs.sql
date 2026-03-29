-- Multi-PID program refactor.
--
-- Replaces integer program_id primary key with text slug, adds observations
-- table for per-PID grouping, and migrates all foreign keys, RLS policies,
-- materialized views, and RPC functions.
--
-- PID → slug mapping:
--   6368→capers  7076→ember  7417→zenith  6585→cosmos_ddt  5224→mom
--   4233→rubies  1345→ceers  2750→ceers_ddt  9214→spurs  2561→uncover
--   1214→gto_wide  1213→gto_wide  8018→diver  8410→oceans  5997→oasis
--   3543→excels  4287→egs_bubbles  3215→jades  1433→macs0647jd_coe

BEGIN;

-- =============================================================================
-- 0. Drop materialized view BEFORE dropping old programs table
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_programs_overview;

-- =============================================================================
-- 1. Create new programs table (slug PK)
-- =============================================================================

ALTER TABLE programs RENAME TO programs_old;

CREATE TABLE programs (
    slug text PRIMARY KEY,
    program_name text NOT NULL,
    pi_name text,
    description text,
    cycle integer,
    is_public boolean DEFAULT false,
    created_at timestamptz DEFAULT now()
);

INSERT INTO programs (slug, program_name, pi_name, description, cycle, is_public, created_at)
SELECT
    CASE program_id
        WHEN 6368 THEN 'capers' WHEN 7076 THEN 'ember' WHEN 7417 THEN 'zenith'
        WHEN 6585 THEN 'cosmos_ddt' WHEN 5224 THEN 'mom' WHEN 4233 THEN 'rubies'
        WHEN 1345 THEN 'ceers' WHEN 2750 THEN 'ceers_ddt' WHEN 9214 THEN 'spurs'
        WHEN 2561 THEN 'uncover' WHEN 1214 THEN 'gto_wide' WHEN 1213 THEN 'gto_wide'
        WHEN 8018 THEN 'diver' WHEN 8410 THEN 'oceans' WHEN 5997 THEN 'oasis'
        WHEN 3543 THEN 'excels' WHEN 4287 THEN 'egs_bubbles' WHEN 3215 THEN 'jades' WHEN 1433 THEN 'macs0647jd_coe'
    END,
    program_name, pi_name, description, cycle, is_public, created_at
FROM programs_old
ON CONFLICT (slug) DO NOTHING;

-- =============================================================================
-- 2. Create observations table
-- =============================================================================

CREATE TABLE observations (
    name text PRIMARY KEY,
    program_slug text NOT NULL REFERENCES programs(slug),
    jwst_program_id integer NOT NULL,
    field text NOT NULL,
    created_at timestamptz DEFAULT now()
);

INSERT INTO observations (name, program_slug, jwst_program_id, field)
SELECT DISTINCT
    o.observation,
    CASE o.program_id
        WHEN 6368 THEN 'capers' WHEN 7076 THEN 'ember' WHEN 7417 THEN 'zenith'
        WHEN 6585 THEN 'cosmos_ddt' WHEN 5224 THEN 'mom' WHEN 4233 THEN 'rubies'
        WHEN 1345 THEN 'ceers' WHEN 2750 THEN 'ceers_ddt' WHEN 9214 THEN 'spurs'
        WHEN 2561 THEN 'uncover' WHEN 1214 THEN 'gto_wide' WHEN 1213 THEN 'gto_wide'
        WHEN 8018 THEN 'diver' WHEN 8410 THEN 'oceans' WHEN 5997 THEN 'oasis'
        WHEN 3543 THEN 'excels' WHEN 4287 THEN 'egs_bubbles' WHEN 3215 THEN 'jades' WHEN 1433 THEN 'macs0647jd_coe'
    END,
    o.program_id,
    o.field
FROM objects o
WHERE o.observation IS NOT NULL;

CREATE INDEX idx_observations_program_slug ON observations(program_slug);
CREATE INDEX idx_observations_jwst_pid ON observations(jwst_program_id);

-- =============================================================================
-- 3. Migrate objects table
-- =============================================================================

-- Add program_slug column and populate
ALTER TABLE objects ADD COLUMN program_slug text;
UPDATE objects o SET program_slug = CASE o.program_id
    WHEN 6368 THEN 'capers' WHEN 7076 THEN 'ember' WHEN 7417 THEN 'zenith'
    WHEN 6585 THEN 'cosmos_ddt' WHEN 5224 THEN 'mom' WHEN 4233 THEN 'rubies'
    WHEN 1345 THEN 'ceers' WHEN 2750 THEN 'ceers_ddt' WHEN 9214 THEN 'spurs'
    WHEN 2561 THEN 'uncover' WHEN 1214 THEN 'gto_wide' WHEN 1213 THEN 'gto_wide'
    WHEN 8018 THEN 'diver' WHEN 8410 THEN 'oceans' WHEN 5997 THEN 'oasis'
    WHEN 3543 THEN 'excels' WHEN 4287 THEN 'egs_bubbles' WHEN 3215 THEN 'jades'
END;
ALTER TABLE objects ALTER COLUMN program_slug SET NOT NULL;

-- Replace generated observation column with regular column + FK
-- Can't ALTER generated → regular in PG, must drop and recreate
-- Must drop mv_filter_options first (depends on observation column)
DROP MATERIALIZED VIEW IF EXISTS public.mv_filter_options;

ALTER TABLE objects ADD COLUMN observation_name text;
UPDATE objects SET observation_name = observation;
ALTER TABLE objects DROP COLUMN observation;
ALTER TABLE objects RENAME COLUMN observation_name TO observation;
ALTER TABLE objects ALTER COLUMN observation SET NOT NULL;

-- Recreate mv_filter_options (same definition, observation is now a regular column)
CREATE MATERIALIZED VIEW public.mv_filter_options AS
SELECT 1 AS id,
    ARRAY(SELECT DISTINCT objects.field FROM public.objects ORDER BY objects.field) AS fields,
    ARRAY(SELECT DISTINCT objects.observation FROM public.objects WHERE objects.observation IS NOT NULL ORDER BY objects.observation) AS observations,
    ARRAY(SELECT DISTINCT spectra.grating FROM public.spectra ORDER BY spectra.grating) AS gratings
WITH DATA;
CREATE UNIQUE INDEX mv_filter_options_id ON public.mv_filter_options USING btree (id);
GRANT ALL ON TABLE public.mv_filter_options TO anon;
GRANT ALL ON TABLE public.mv_filter_options TO authenticated;
GRANT ALL ON TABLE public.mv_filter_options TO service_role;

-- Add FK constraints
ALTER TABLE objects ADD CONSTRAINT fk_objects_program FOREIGN KEY (program_slug) REFERENCES programs(slug);
ALTER TABLE objects ADD CONSTRAINT fk_objects_observation FOREIGN KEY (observation) REFERENCES observations(name);

-- Drop dependencies on program_id before dropping the column
-- (views, RLS policies — they'll be recreated in step 8)
DROP VIEW IF EXISTS public.objects_with_flags;
DROP POLICY IF EXISTS "select_objects_by_access" ON objects;
DROP POLICY IF EXISTS "update_objects_by_access" ON objects;
DROP POLICY IF EXISTS "select_spectra_by_access" ON spectra;
DROP POLICY IF EXISTS "select_comments_by_access" ON comments;
DROP POLICY IF EXISTS "insert_comments_by_access" ON comments;
DROP POLICY IF EXISTS "select_audit_by_access" ON flag_audit_log;
DROP POLICY IF EXISTS "insert_audit_by_access" ON flag_audit_log;

-- Drop old program_id column (must drop FK first)
ALTER TABLE objects DROP CONSTRAINT IF EXISTS objects_program_id_fkey;
ALTER TABLE objects DROP COLUMN program_id;

-- Rebuild indexes
DROP INDEX IF EXISTS idx_objects_program;
DROP INDEX IF EXISTS idx_objects_program_field;
DROP INDEX IF EXISTS idx_objects_program_quality;
DROP INDEX IF EXISTS idx_objects_observation;
DROP INDEX IF EXISTS idx_objects_field_observation;

CREATE INDEX idx_objects_program_slug ON objects(program_slug);
CREATE INDEX idx_objects_program_slug_field ON objects(program_slug, field);
CREATE INDEX idx_objects_program_slug_quality ON objects(program_slug, redshift_quality);
CREATE INDEX idx_objects_observation ON objects(observation);
CREATE INDEX idx_objects_field_observation ON objects(field, observation);

-- =============================================================================
-- 4. Migrate user_program_access
-- =============================================================================

ALTER TABLE user_program_access ADD COLUMN program_slug text;

UPDATE user_program_access upa SET program_slug = CASE upa.program_id
    WHEN 6368 THEN 'capers' WHEN 7076 THEN 'ember' WHEN 7417 THEN 'zenith'
    WHEN 6585 THEN 'cosmos_ddt' WHEN 5224 THEN 'mom' WHEN 4233 THEN 'rubies'
    WHEN 1345 THEN 'ceers' WHEN 2750 THEN 'ceers_ddt' WHEN 9214 THEN 'spurs'
    WHEN 2561 THEN 'uncover' WHEN 1214 THEN 'gto_wide' WHEN 1213 THEN 'gto_wide'
    WHEN 8018 THEN 'diver' WHEN 8410 THEN 'oceans' WHEN 5997 THEN 'oasis'
    WHEN 3543 THEN 'excels' WHEN 4287 THEN 'egs_bubbles' WHEN 3215 THEN 'jades'
END;

-- Deduplicate (users with access to both 1213 and 1214 now have duplicate gto_wide rows)
DELETE FROM user_program_access a
USING user_program_access b
WHERE a.ctid < b.ctid
  AND a.user_id = b.user_id
  AND a.program_slug = b.program_slug;

ALTER TABLE user_program_access ALTER COLUMN program_slug SET NOT NULL;
ALTER TABLE user_program_access DROP CONSTRAINT user_program_access_pkey;
ALTER TABLE user_program_access ADD PRIMARY KEY (user_id, program_slug);
ALTER TABLE user_program_access DROP COLUMN program_id;
ALTER TABLE user_program_access ADD CONSTRAINT fk_upa_program FOREIGN KEY (program_slug) REFERENCES programs(slug);

-- =============================================================================
-- 5. Migrate access_codes, account_requests, pending_invites
-- =============================================================================

-- access_codes
ALTER TABLE access_codes ADD COLUMN program_slugs text[];
UPDATE access_codes SET program_slugs = (
    SELECT ARRAY_AGG(DISTINCT CASE pid
        WHEN 6368 THEN 'capers' WHEN 7076 THEN 'ember' WHEN 7417 THEN 'zenith'
        WHEN 6585 THEN 'cosmos_ddt' WHEN 5224 THEN 'mom' WHEN 4233 THEN 'rubies'
        WHEN 1345 THEN 'ceers' WHEN 2750 THEN 'ceers_ddt' WHEN 9214 THEN 'spurs'
        WHEN 2561 THEN 'uncover' WHEN 1214 THEN 'gto_wide' WHEN 1213 THEN 'gto_wide'
        WHEN 8018 THEN 'diver' WHEN 8410 THEN 'oceans' WHEN 5997 THEN 'oasis'
        WHEN 3543 THEN 'excels' WHEN 4287 THEN 'egs_bubbles' WHEN 3215 THEN 'jades' WHEN 1433 THEN 'macs0647jd_coe'
    END)
    FROM unnest(program_ids) AS pid
) WHERE program_ids IS NOT NULL;
ALTER TABLE access_codes DROP COLUMN program_ids;

-- account_requests
ALTER TABLE account_requests ADD COLUMN program_slugs text[];
UPDATE account_requests SET program_slugs = (
    SELECT ARRAY_AGG(DISTINCT CASE pid
        WHEN 6368 THEN 'capers' WHEN 7076 THEN 'ember' WHEN 7417 THEN 'zenith'
        WHEN 6585 THEN 'cosmos_ddt' WHEN 5224 THEN 'mom' WHEN 4233 THEN 'rubies'
        WHEN 1345 THEN 'ceers' WHEN 2750 THEN 'ceers_ddt' WHEN 9214 THEN 'spurs'
        WHEN 2561 THEN 'uncover' WHEN 1214 THEN 'gto_wide' WHEN 1213 THEN 'gto_wide'
        WHEN 8018 THEN 'diver' WHEN 8410 THEN 'oceans' WHEN 5997 THEN 'oasis'
        WHEN 3543 THEN 'excels' WHEN 4287 THEN 'egs_bubbles' WHEN 3215 THEN 'jades' WHEN 1433 THEN 'macs0647jd_coe'
    END)
    FROM unnest(program_ids) AS pid
) WHERE program_ids IS NOT NULL;
ALTER TABLE account_requests DROP COLUMN program_ids;

-- pending_invites
ALTER TABLE pending_invites ADD COLUMN program_slugs text[];
UPDATE pending_invites SET program_slugs = (
    SELECT ARRAY_AGG(DISTINCT CASE pid
        WHEN 6368 THEN 'capers' WHEN 7076 THEN 'ember' WHEN 7417 THEN 'zenith'
        WHEN 6585 THEN 'cosmos_ddt' WHEN 5224 THEN 'mom' WHEN 4233 THEN 'rubies'
        WHEN 1345 THEN 'ceers' WHEN 2750 THEN 'ceers_ddt' WHEN 9214 THEN 'spurs'
        WHEN 2561 THEN 'uncover' WHEN 1214 THEN 'gto_wide' WHEN 1213 THEN 'gto_wide'
        WHEN 8018 THEN 'diver' WHEN 8410 THEN 'oceans' WHEN 5997 THEN 'oasis'
        WHEN 3543 THEN 'excels' WHEN 4287 THEN 'egs_bubbles' WHEN 3215 THEN 'jades' WHEN 1433 THEN 'macs0647jd_coe'
    END)
    FROM unnest(program_ids) AS pid
) WHERE program_ids IS NOT NULL;
ALTER TABLE pending_invites DROP COLUMN program_ids;

-- =============================================================================
-- 6. Drop old programs table
-- =============================================================================

DROP TABLE programs_old;

-- =============================================================================
-- 7. Add FK to shutters and slit_regions
-- =============================================================================

ALTER TABLE shutters ADD CONSTRAINT fk_shutters_observation FOREIGN KEY (observation) REFERENCES observations(name);
ALTER TABLE slit_regions ADD CONSTRAINT fk_slit_regions_observation FOREIGN KEY (observation) REFERENCES observations(name);

-- =============================================================================
-- 8. Update RLS policies
-- =============================================================================

-- objects SELECT
DROP POLICY IF EXISTS "select_objects_by_access" ON objects;
CREATE POLICY "select_objects_by_access" ON objects FOR SELECT USING (
    program_slug IN (SELECT program_slug FROM user_program_access WHERE user_id = auth.uid())
    OR program_slug IN (SELECT slug FROM programs WHERE is_public = true)
);

-- objects UPDATE
DROP POLICY IF EXISTS "update_objects_by_access" ON objects;
DROP POLICY IF EXISTS "Allow authenticated users to update objects" ON objects;
CREATE POLICY "update_objects_by_access" ON objects FOR UPDATE USING (
    program_slug IN (SELECT program_slug FROM user_program_access WHERE user_id = auth.uid())
    AND (SELECT can_comment FROM user_profiles WHERE user_id = auth.uid()) = true
);

-- spectra SELECT
DROP POLICY IF EXISTS "select_spectra_by_access" ON spectra;
CREATE POLICY "select_spectra_by_access" ON spectra FOR SELECT USING (
    object_id IN (
        SELECT objects.object_id FROM objects
        WHERE objects.program_slug IN (SELECT program_slug FROM user_program_access WHERE user_id = auth.uid())
           OR objects.program_slug IN (SELECT slug FROM programs WHERE is_public = true)
    )
);

-- comments SELECT
DROP POLICY IF EXISTS "select_comments_by_access" ON comments;
CREATE POLICY "select_comments_by_access" ON comments FOR SELECT USING (
    object_id IN (
        SELECT objects.id FROM objects
        WHERE objects.program_slug IN (SELECT program_slug FROM user_program_access WHERE user_id = auth.uid())
    )
);

-- comments INSERT
DROP POLICY IF EXISTS "insert_comments_by_access" ON comments;
CREATE POLICY "insert_comments_by_access" ON comments FOR INSERT WITH CHECK (
    object_id IN (
        SELECT objects.id FROM objects
        WHERE objects.program_slug IN (SELECT program_slug FROM user_program_access WHERE user_id = auth.uid())
    )
    AND (SELECT can_comment FROM user_profiles WHERE user_id = auth.uid()) = true
);

-- flag_audit_log SELECT
DROP POLICY IF EXISTS "select_audit_by_access" ON flag_audit_log;
CREATE POLICY "select_audit_by_access" ON flag_audit_log FOR SELECT USING (
    object_id IN (
        SELECT objects.id FROM objects
        WHERE objects.program_slug IN (SELECT program_slug FROM user_program_access WHERE user_id = auth.uid())
    )
);

-- flag_audit_log INSERT
DROP POLICY IF EXISTS "insert_audit_by_access" ON flag_audit_log;
CREATE POLICY "insert_audit_by_access" ON flag_audit_log FOR INSERT TO authenticated WITH CHECK (
    object_id IN (
        SELECT objects.id FROM objects
        WHERE objects.program_slug IN (SELECT program_slug FROM user_program_access WHERE user_id = auth.uid())
           OR objects.program_slug IN (SELECT slug FROM programs WHERE is_public = true)
    )
);

-- Recreate objects_with_flags view (was dropped in step 3 due to program_id dependency)
CREATE OR REPLACE VIEW public.objects_with_flags AS
SELECT o.id,
    o.object_id,
    o.program_slug,
    o.field,
    o.ra,
    o.dec,
    o.redshift_auto AS redshift,
    o.redshift_quality,
    o.spectral_features,
    o.object_flags,
    o.dq_flags,
    o.created_at,
    o.updated_at,
    rq.label AS redshift_quality_label,
    rq.icon AS redshift_quality_icon,
    rq.color AS redshift_quality_color
FROM public.objects o
LEFT JOIN public.flag_definitions rq ON (rq.category = 'redshift_quality' AND rq.value = o.redshift_quality);

-- =============================================================================
-- 9. Rewrite RPC functions
-- =============================================================================

-- ---- get_filtered_object_ids ----

DROP FUNCTION IF EXISTS public.get_filtered_object_ids(
  INTEGER[], INTEGER[], TEXT[], TEXT[], TEXT, TEXT[], INTEGER[],
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION,
  DOUBLE PRECISION, DOUBLE PRECISION,
  INTEGER, INTEGER, INTEGER, INTEGER, INTEGER, INTEGER, INTEGER, INTEGER, INTEGER,
  TEXT, BOOLEAN, TEXT, TEXT, UUID,
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION,
  TEXT, TEXT, INTEGER, INTEGER
);

CREATE OR REPLACE FUNCTION public.get_filtered_object_ids(
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
  p_sort_column TEXT DEFAULT 'object_id',
  p_sort_direction TEXT DEFAULT 'asc',
  p_page INTEGER DEFAULT NULL,
  p_page_size INTEGER DEFAULT NULL
)
RETURNS TABLE(object_id TEXT, distance DOUBLE PRECISION, row_num BIGINT, total_count BIGINT)
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

  IF NOT (p_sort_column IN ('object_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'max_snr', 'max_exposure_time')
       OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'object_id';
  END IF;

  -- Determine pagination mode
  v_paginate := (p_page IS NOT NULL AND p_page_size IS NOT NULL);
  IF v_paginate THEN
    v_offset := (p_page - 1) * p_page_size;
  END IF;

  -- Determine which programs to query
  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(
      SELECT unnest(p_program_slugs)
      INTERSECT
      SELECT unnest(p_filter_programs)
    ) INTO v_filtered_program_slugs;
  ELSE
    v_filtered_program_slugs := p_program_slugs;
  END IF;

  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN
    RETURN;
  END IF;

  -- =========================================================================
  -- Paginated path: LIMIT/OFFSET with separate COUNT(*)
  -- Enables top-N heapsort and emits only the requested page.
  -- =========================================================================
  IF v_paginate THEN
    RETURN QUERY
    WITH filtered_objects AS (
      SELECT
        o.object_id,
        CASE
          WHEN v_coord_search_active THEN
            2 * DEGREES(ASIN(SQRT(
              POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
              COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
              POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
            )))
          ELSE NULL
        END AS distance,
        o.field, o.observation, o.ra, o.dec, o.redshift, o.redshift_quality, o.max_snr, o.max_exposure_time
      FROM objects o
      WHERE
        o.program_slug = ANY(v_filtered_program_slugs)
        AND (
          NOT v_grating_filter_active
          OR (v_gratings_mode = 'any' AND EXISTS (
            SELECT 1 FROM spectra gs WHERE gs.object_id = o.object_id AND gs.grating = ANY(p_gratings)
          ))
          OR (v_gratings_mode = 'all' AND (
            SELECT COUNT(DISTINCT gs.grating) FROM spectra gs
            WHERE gs.object_id = o.object_id AND gs.grating = ANY(p_gratings)
          ) = array_length(p_gratings, 1))
          OR (v_gratings_mode = 'none' AND NOT EXISTS (
            SELECT 1 FROM spectra gs WHERE gs.object_id = o.object_id AND gs.grating = ANY(p_gratings)
          ))
        )
        AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
        AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observation = ANY(p_observations))
        AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
        AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
        AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
        AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
        AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
        AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
        AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
        AND (p_spectral_features_include_any IS NULL OR (COALESCE(o.spectral_features, 0) & p_spectral_features_include_any) != 0)
        AND (p_spectral_features_include_all IS NULL OR (COALESCE(o.spectral_features, 0) & p_spectral_features_include_all) = p_spectral_features_include_all)
        AND (p_spectral_features_exclude IS NULL OR (COALESCE(o.spectral_features, 0) & p_spectral_features_exclude) = 0)
        AND (p_object_flags_include_any IS NULL OR (COALESCE(o.object_flags, 0) & p_object_flags_include_any) != 0)
        AND (p_object_flags_include_all IS NULL OR (COALESCE(o.object_flags, 0) & p_object_flags_include_all) = p_object_flags_include_all)
        AND (p_object_flags_exclude IS NULL OR (COALESCE(o.object_flags, 0) & p_object_flags_exclude) = 0)
        AND (p_dq_flags_include_any IS NULL OR (COALESCE(o.dq_flags, 0) & p_dq_flags_include_any) != 0)
        AND (p_dq_flags_include_all IS NULL OR (COALESCE(o.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
        AND (p_dq_flags_exclude IS NULL OR (COALESCE(o.dq_flags, 0) & p_dq_flags_exclude) = 0)
        AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%')
        AND (
          p_inspected_only IS NULL
          OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
          OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
        )
        AND (
          NOT v_comment_search_active
          OR EXISTS (
            SELECT 1 FROM comments c
            WHERE c.object_id = o.id
              AND c.is_deleted = false
              AND c.content ILIKE '%' || p_comment_search || '%'
              AND (
                p_comment_search_scope = 'everyone'
                OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
              )
          )
        )
        AND (
          NOT v_coord_search_active
          OR (
            o.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
            AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
          )
        )
    ),
    distance_filtered AS (
      SELECT fo.*
      FROM filtered_objects fo
      WHERE NOT v_coord_search_active OR fo.distance <= p_radius_degrees
    )
    SELECT
      df.object_id,
      df.distance,
      ROW_NUMBER() OVER (
        ORDER BY
          CASE WHEN v_coord_search_active THEN df.distance END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN df.object_id END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN df.object_id END DESC NULLS LAST,
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
          df.object_id ASC
      ) AS row_num,
      (SELECT COUNT(*) FROM distance_filtered)::BIGINT AS total_count
    FROM distance_filtered df
    ORDER BY
      CASE WHEN v_coord_search_active THEN df.distance END ASC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN df.object_id END ASC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN df.object_id END DESC NULLS LAST,
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
      df.object_id ASC
    LIMIT p_page_size OFFSET v_offset;

  -- =========================================================================
  -- Full path: existing behavior (all rows with ROW_NUMBER + COUNT(*) OVER)
  -- Used by: map markers, inspection queue, CSV export, adjacent objects
  -- =========================================================================
  ELSE
    RETURN QUERY
    WITH filtered_objects AS (
      SELECT
        o.object_id,
        CASE
          WHEN v_coord_search_active THEN
            2 * DEGREES(ASIN(SQRT(
              POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
              COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
              POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
            )))
          ELSE NULL
        END AS distance,
        o.field, o.observation, o.ra, o.dec, o.redshift, o.redshift_quality, o.max_snr, o.max_exposure_time
      FROM objects o
      WHERE
        o.program_slug = ANY(v_filtered_program_slugs)
        AND (
          NOT v_grating_filter_active
          OR (v_gratings_mode = 'any' AND EXISTS (
            SELECT 1 FROM spectra gs WHERE gs.object_id = o.object_id AND gs.grating = ANY(p_gratings)
          ))
          OR (v_gratings_mode = 'all' AND (
            SELECT COUNT(DISTINCT gs.grating) FROM spectra gs
            WHERE gs.object_id = o.object_id AND gs.grating = ANY(p_gratings)
          ) = array_length(p_gratings, 1))
          OR (v_gratings_mode = 'none' AND NOT EXISTS (
            SELECT 1 FROM spectra gs WHERE gs.object_id = o.object_id AND gs.grating = ANY(p_gratings)
          ))
        )
        AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
        AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observation = ANY(p_observations))
        AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
        AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
        AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
        AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
        AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
        AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
        AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
        AND (p_spectral_features_include_any IS NULL OR (COALESCE(o.spectral_features, 0) & p_spectral_features_include_any) != 0)
        AND (p_spectral_features_include_all IS NULL OR (COALESCE(o.spectral_features, 0) & p_spectral_features_include_all) = p_spectral_features_include_all)
        AND (p_spectral_features_exclude IS NULL OR (COALESCE(o.spectral_features, 0) & p_spectral_features_exclude) = 0)
        AND (p_object_flags_include_any IS NULL OR (COALESCE(o.object_flags, 0) & p_object_flags_include_any) != 0)
        AND (p_object_flags_include_all IS NULL OR (COALESCE(o.object_flags, 0) & p_object_flags_include_all) = p_object_flags_include_all)
        AND (p_object_flags_exclude IS NULL OR (COALESCE(o.object_flags, 0) & p_object_flags_exclude) = 0)
        AND (p_dq_flags_include_any IS NULL OR (COALESCE(o.dq_flags, 0) & p_dq_flags_include_any) != 0)
        AND (p_dq_flags_include_all IS NULL OR (COALESCE(o.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
        AND (p_dq_flags_exclude IS NULL OR (COALESCE(o.dq_flags, 0) & p_dq_flags_exclude) = 0)
        AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%')
        AND (
          p_inspected_only IS NULL
          OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
          OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
        )
        AND (
          NOT v_comment_search_active
          OR EXISTS (
            SELECT 1 FROM comments c
            WHERE c.object_id = o.id
              AND c.is_deleted = false
              AND c.content ILIKE '%' || p_comment_search || '%'
              AND (
                p_comment_search_scope = 'everyone'
                OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
              )
          )
        )
        AND (
          NOT v_coord_search_active
          OR (
            o.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
            AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
          )
        )
    ),
    distance_filtered AS (
      SELECT fo.*
      FROM filtered_objects fo
      WHERE NOT v_coord_search_active OR fo.distance <= p_radius_degrees
    )
    SELECT
      df.object_id,
      df.distance,
      ROW_NUMBER() OVER (
        ORDER BY
          CASE WHEN v_coord_search_active THEN df.distance END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN df.object_id END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN df.object_id END DESC NULLS LAST,
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
          df.object_id ASC
      ) AS row_num,
      COUNT(*) OVER () AS total_count
    FROM distance_filtered df;
  END IF;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_filtered_object_ids TO authenticated;

-- ---- get_filtered_objects_paginated ----

DROP FUNCTION IF EXISTS public.get_filtered_objects_paginated;

CREATE OR REPLACE FUNCTION public.get_filtered_objects_paginated(
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
  p_sort_column TEXT DEFAULT 'object_id',
  p_sort_direction TEXT DEFAULT 'asc',
  p_page INTEGER DEFAULT 1,
  p_page_size INTEGER DEFAULT 50,
  p_include_thumbnails BOOLEAN DEFAULT false
)
RETURNS TABLE(objects JSONB, total_count BIGINT, page INTEGER, page_size INTEGER)
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
  -- Backward-compat: normalize old single-integer flag params into new _include_any
  v_sf_include_any := COALESCE(p_spectral_features_include_any, p_spectral_features);
  v_sf_include_all := p_spectral_features_include_all;
  v_sf_exclude := p_spectral_features_exclude;
  v_of_include_any := COALESCE(p_object_flags_include_any, p_object_flags);
  v_of_include_all := p_object_flags_include_all;
  v_of_exclude := p_object_flags_exclude;
  v_dq_include_any := COALESCE(p_dq_flags_include_any, p_dq_flags);
  v_dq_include_all := p_dq_flags_include_all;
  v_dq_exclude := p_dq_flags_exclude;

  -- Need these for spectra subquery filtering in JSONB output
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);

  RETURN QUERY
  WITH ids AS (
    SELECT *
    FROM public.get_filtered_object_ids(
      p_program_slugs, p_filter_programs, p_fields, p_gratings, p_gratings_mode,
      p_observations, p_redshift_quality, p_redshift_min, p_redshift_max,
      p_max_snr_min, p_max_snr_max, p_max_exposure_time_min, p_max_exposure_time_max,
      v_sf_include_any, v_sf_include_all, v_sf_exclude,
      v_of_include_any, v_of_include_all, v_of_exclude,
      v_dq_include_any, v_dq_include_all, v_dq_exclude,
      p_search, p_inspected_only, p_comment_search, p_comment_search_scope, p_comment_user_id,
      p_coord_ra, p_coord_dec, p_radius_degrees,
      p_sort_column, p_sort_direction,
      p_page, p_page_size
    )
  ),
  with_relations AS (
    SELECT
      jsonb_build_object(
        'id', o.id,
        'object_id', o.object_id,
        'program_slug', o.program_slug,
        'field', o.field,
        'observation', o.observation,
        'ra', o.ra,
        'dec', o.dec,
        'redshift', o.redshift,
        'redshift_auto', o.redshift_auto,
        'redshift_inspected', o.redshift_inspected,
        'redshift_quality', o.redshift_quality,
        'spectral_features', o.spectral_features,
        'object_flags', o.object_flags,
        'dq_flags', o.dq_flags,
        'max_snr', o.max_snr,
        'max_exposure_time', o.max_exposure_time,
        'last_inspected_at', o.last_inspected_at,
        'last_inspected_by', o.last_inspected_by,
        'created_at', o.created_at,
        'updated_at', o.updated_at,
        'program_name', pr.program_name,
        'distance', CASE WHEN v_coord_search_active THEN i.distance ELSE NULL END,
        'spectra', COALESCE(
          (
            SELECT jsonb_agg(
              jsonb_build_object(
                'id', s.id,
                'object_id', s.object_id,
                'grating', s.grating,
                'fits_path', s.fits_path,
                'reduction_version', s.reduction_version,
                'signal_to_noise', s.signal_to_noise,
                'created_at', s.created_at,
                'thumbnail_svg_fnu', CASE WHEN p_include_thumbnails THEN s.thumbnail_svg_fnu ELSE NULL END,
                'thumbnail_svg_flambda', CASE WHEN p_include_thumbnails THEN s.thumbnail_svg_flambda ELSE NULL END
              )
              ORDER BY s.grating
            )
            FROM spectra s
            WHERE s.object_id = o.object_id
              AND (NOT v_grating_filter_active OR v_gratings_mode = 'none' OR s.grating = ANY(p_gratings))
          ),
          '[]'::jsonb
        )
      ) as obj,
      i.row_num
    FROM ids i
    JOIN objects o ON o.object_id = i.object_id
    LEFT JOIN programs pr ON pr.slug = o.program_slug
  )
  SELECT
    COALESCE(
      (SELECT jsonb_agg(wr.obj ORDER BY wr.row_num) FROM with_relations wr),
      '[]'::jsonb
    ) as objects,
    COALESCE((SELECT i.total_count FROM ids i LIMIT 1), 0::BIGINT) as total_count,
    p_page as page,
    p_page_size as page_size;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_filtered_objects_paginated TO authenticated;

-- ---- get_adjacent_objects ----

DROP FUNCTION IF EXISTS public.get_adjacent_objects;

CREATE OR REPLACE FUNCTION public.get_adjacent_objects(
  p_current_object_id TEXT,
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
  p_sort_column TEXT DEFAULT 'object_id',
  p_sort_direction TEXT DEFAULT 'asc'
)
RETURNS TABLE(
  prev_object_id TEXT,
  next_object_id TEXT,
  current_index BIGINT,
  total_count BIGINT
)
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
  -- Backward-compat flag normalization
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

  IF NOT (p_sort_column IN ('object_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'max_snr', 'max_exposure_time')
       OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'object_id';
  END IF;

  -- Coord search always sorts by distance ASC
  IF v_coord_search_active THEN
    p_sort_column := 'distance';
    p_sort_direction := 'asc';
  END IF;

  v_sort_is_text := p_sort_column IN ('object_id', 'field', 'observation');

  -- Backward-compat: normalize old single-integer flag params
  v_sf_include_any := COALESCE(p_spectral_features_include_any, p_spectral_features);
  v_sf_include_all := p_spectral_features_include_all;
  v_sf_exclude := p_spectral_features_exclude;
  v_of_include_any := COALESCE(p_object_flags_include_any, p_object_flags);
  v_of_include_all := p_object_flags_include_all;
  v_of_exclude := p_object_flags_exclude;
  v_dq_include_any := COALESCE(p_dq_flags_include_any, p_dq_flags);
  v_dq_include_all := p_dq_flags_include_all;
  v_dq_exclude := p_dq_flags_exclude;

  -- Determine which programs to query
  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(
      SELECT unnest(p_program_slugs)
      INTERSECT
      SELECT unnest(p_filter_programs)
    ) INTO v_filtered_program_slugs;
  ELSE
    v_filtered_program_slugs := p_program_slugs;
  END IF;

  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN
    RETURN QUERY SELECT NULL::TEXT, NULL::TEXT, 0::BIGINT, 0::BIGINT;
    RETURN;
  END IF;

  RETURN QUERY
  WITH filtered_objects AS MATERIALIZED (
    SELECT
      o.object_id,
      CASE
        WHEN v_coord_search_active THEN
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
            POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL
      END AS distance,
      o.field, o.observation, o.ra, o.dec, o.redshift, o.redshift_quality, o.max_snr, o.max_exposure_time
    FROM objects o
    WHERE
      o.program_slug = ANY(v_filtered_program_slugs)
      -- Grating filter
      AND (
        NOT v_grating_filter_active
        OR (v_gratings_mode = 'any' AND EXISTS (
          SELECT 1 FROM spectra gs WHERE gs.object_id = o.object_id AND gs.grating = ANY(p_gratings)
        ))
        OR (v_gratings_mode = 'all' AND (
          SELECT COUNT(DISTINCT gs.grating) FROM spectra gs
          WHERE gs.object_id = o.object_id AND gs.grating = ANY(p_gratings)
        ) = array_length(p_gratings, 1))
        OR (v_gratings_mode = 'none' AND NOT EXISTS (
          SELECT 1 FROM spectra gs WHERE gs.object_id = o.object_id AND gs.grating = ANY(p_gratings)
        ))
      )
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      -- Spectral features filter (three modes)
      AND (v_sf_include_any IS NULL OR (COALESCE(o.spectral_features, 0) & v_sf_include_any) != 0)
      AND (v_sf_include_all IS NULL OR (COALESCE(o.spectral_features, 0) & v_sf_include_all) = v_sf_include_all)
      AND (v_sf_exclude IS NULL OR (COALESCE(o.spectral_features, 0) & v_sf_exclude) = 0)
      -- Object flags filter (three modes)
      AND (v_of_include_any IS NULL OR (COALESCE(o.object_flags, 0) & v_of_include_any) != 0)
      AND (v_of_include_all IS NULL OR (COALESCE(o.object_flags, 0) & v_of_include_all) = v_of_include_all)
      AND (v_of_exclude IS NULL OR (COALESCE(o.object_flags, 0) & v_of_exclude) = 0)
      -- DQ flags filter (three modes)
      AND (v_dq_include_any IS NULL OR (COALESCE(o.dq_flags, 0) & v_dq_include_any) != 0)
      AND (v_dq_include_all IS NULL OR (COALESCE(o.dq_flags, 0) & v_dq_include_all) = v_dq_include_all)
      AND (v_dq_exclude IS NULL OR (COALESCE(o.dq_flags, 0) & v_dq_exclude) = 0)
      -- Object ID text search
      AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%')
      -- Inspected only filter
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
      )
      -- Comment search filter
      AND (
        NOT v_comment_search_active
        OR EXISTS (
          SELECT 1 FROM comments c
          WHERE c.object_id = o.id
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
        )
      )
      -- Coordinate search bounding box pre-filter
      AND (
        NOT v_coord_search_active
        OR (
          o.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
          AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
        )
      )
  ),
  distance_filtered AS MATERIALIZED (
    SELECT
      fo.*,
      -- Normalize sort value into typed columns for cursor comparisons
      CASE p_sort_column
        WHEN 'object_id' THEN fo.object_id
        WHEN 'field' THEN fo.field
        WHEN 'observation' THEN fo.observation
        ELSE NULL
      END AS sort_text,
      CASE p_sort_column
        WHEN 'ra' THEN fo.ra
        WHEN 'dec' THEN fo.dec
        WHEN 'redshift' THEN fo.redshift::DOUBLE PRECISION
        WHEN 'redshift_quality' THEN fo.redshift_quality::DOUBLE PRECISION
        WHEN 'max_snr' THEN fo.max_snr
        WHEN 'max_exposure_time' THEN fo.max_exposure_time
        WHEN 'distance' THEN fo.distance
        ELSE NULL
      END AS sort_num
    FROM filtered_objects fo
    WHERE NOT v_coord_search_active OR fo.distance <= p_radius_degrees
  ),
  current_obj AS (
    SELECT df.sort_text, df.sort_num, df.object_id
    FROM distance_filtered df
    WHERE df.object_id = p_current_object_id
  )
  SELECT
    -- prev: last row that sorts before current (reversed ORDER BY, LIMIT 1)
    (SELECT df.object_id
     FROM distance_filtered df, current_obj c
     WHERE
       CASE WHEN v_sort_is_text THEN
         (CASE WHEN p_sort_direction = 'asc' THEN df.sort_text < c.sort_text
               ELSE df.sort_text > c.sort_text END)
         OR (df.sort_text IS NOT DISTINCT FROM c.sort_text AND df.object_id < c.object_id)
         OR (df.sort_text IS NOT NULL AND c.sort_text IS NULL)
       ELSE
         (CASE WHEN p_sort_direction = 'asc' THEN df.sort_num < c.sort_num
               ELSE df.sort_num > c.sort_num END)
         OR (df.sort_num IS NOT DISTINCT FROM c.sort_num AND df.object_id < c.object_id)
         OR (df.sort_num IS NOT NULL AND c.sort_num IS NULL)
       END
     ORDER BY
       CASE WHEN v_sort_is_text AND p_sort_direction = 'asc' THEN df.sort_text END DESC NULLS FIRST,
       CASE WHEN v_sort_is_text AND p_sort_direction = 'desc' THEN df.sort_text END ASC NULLS FIRST,
       CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'asc' THEN df.sort_num END DESC NULLS FIRST,
       CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'desc' THEN df.sort_num END ASC NULLS FIRST,
       df.object_id DESC
     LIMIT 1
    ) AS prev_object_id,

    -- next: first row that sorts after current (forward ORDER BY, LIMIT 1)
    (SELECT df.object_id
     FROM distance_filtered df, current_obj c
     WHERE
       CASE WHEN v_sort_is_text THEN
         (CASE WHEN p_sort_direction = 'asc' THEN df.sort_text > c.sort_text
               ELSE df.sort_text < c.sort_text END)
         OR (df.sort_text IS NOT DISTINCT FROM c.sort_text AND df.object_id > c.object_id)
         OR (c.sort_text IS NOT NULL AND df.sort_text IS NULL)
       ELSE
         (CASE WHEN p_sort_direction = 'asc' THEN df.sort_num > c.sort_num
               ELSE df.sort_num < c.sort_num END)
         OR (df.sort_num IS NOT DISTINCT FROM c.sort_num AND df.object_id > c.object_id)
         OR (c.sort_num IS NOT NULL AND df.sort_num IS NULL)
       END
     ORDER BY
       CASE WHEN v_sort_is_text AND p_sort_direction = 'asc' THEN df.sort_text END ASC NULLS LAST,
       CASE WHEN v_sort_is_text AND p_sort_direction = 'desc' THEN df.sort_text END DESC NULLS LAST,
       CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'asc' THEN df.sort_num END ASC NULLS LAST,
       CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'desc' THEN df.sort_num END DESC NULLS LAST,
       df.object_id ASC
     LIMIT 1
    ) AS next_object_id,

    -- current_index: count of rows before current + 1 (0 if current not found)
    CASE WHEN EXISTS (SELECT 1 FROM current_obj)
      THEN (
        SELECT COUNT(*) + 1
        FROM distance_filtered df, current_obj c
        WHERE
          CASE WHEN v_sort_is_text THEN
            (CASE WHEN p_sort_direction = 'asc' THEN df.sort_text < c.sort_text
                  ELSE df.sort_text > c.sort_text END)
            OR (df.sort_text IS NOT DISTINCT FROM c.sort_text AND df.object_id < c.object_id)
            OR (df.sort_text IS NOT NULL AND c.sort_text IS NULL)
          ELSE
            (CASE WHEN p_sort_direction = 'asc' THEN df.sort_num < c.sort_num
                  ELSE df.sort_num > c.sort_num END)
            OR (df.sort_num IS NOT DISTINCT FROM c.sort_num AND df.object_id < c.object_id)
            OR (df.sort_num IS NOT NULL AND c.sort_num IS NULL)
          END
      )::BIGINT
      ELSE 0::BIGINT
    END AS current_index,

    -- total_count
    (SELECT COUNT(*) FROM distance_filtered)::BIGINT AS total_count;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_adjacent_objects TO authenticated;

-- ---- get_csv_export ----

DROP FUNCTION IF EXISTS public.get_csv_export;

CREATE OR REPLACE FUNCTION public.get_csv_export(
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
  p_sort_column TEXT DEFAULT 'object_id',
  p_sort_direction TEXT DEFAULT 'asc'
)
RETURNS TABLE(
  object_id TEXT,
  field TEXT,
  ra DOUBLE PRECISION,
  "dec" DOUBLE PRECISION,
  redshift NUMERIC,
  redshift_quality INTEGER,
  max_snr DOUBLE PRECISION,
  max_exposure_time DOUBLE PRECISION,
  num_gratings INTEGER,
  program_slug TEXT,
  program_name TEXT,
  last_inspected_at TIMESTAMPTZ,
  last_inspected_by UUID,
  distance DOUBLE PRECISION
)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
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

  IF NOT (p_sort_column IN ('object_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'max_snr', 'max_exposure_time')
       OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'object_id';
  END IF;

  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(
      SELECT unnest(p_program_slugs)
      INTERSECT
      SELECT unnest(p_filter_programs)
    ) INTO v_filtered_program_slugs;
  ELSE
    v_filtered_program_slugs := p_program_slugs;
  END IF;

  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN
    RETURN;
  END IF;

  RETURN QUERY
  WITH filtered_objects AS (
    SELECT
      o.object_id,
      o.field,
      o.ra,
      o.dec,
      o.redshift,
      o.redshift_quality,
      o.max_snr,
      o.max_exposure_time,
      (SELECT COUNT(*)::INTEGER FROM spectra s WHERE s.object_id = o.object_id) AS num_gratings,
      o.program_slug,
      o.last_inspected_at,
      o.last_inspected_by,
      CASE
        WHEN v_coord_search_active THEN
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
            POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL
      END AS distance
    FROM objects o
    WHERE
      o.program_slug = ANY(v_filtered_program_slugs)
      AND (
        NOT v_grating_filter_active
        OR (v_gratings_mode = 'any' AND EXISTS (
          SELECT 1 FROM spectra gs WHERE gs.object_id = o.object_id AND gs.grating = ANY(p_gratings)
        ))
        OR (v_gratings_mode = 'all' AND (
          SELECT COUNT(DISTINCT gs.grating) FROM spectra gs
          WHERE gs.object_id = o.object_id AND gs.grating = ANY(p_gratings)
        ) = array_length(p_gratings, 1))
        OR (v_gratings_mode = 'none' AND NOT EXISTS (
          SELECT 1 FROM spectra gs WHERE gs.object_id = o.object_id AND gs.grating = ANY(p_gratings)
        ))
      )
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      AND (p_spectral_features_include_any IS NULL OR (COALESCE(o.spectral_features, 0) & p_spectral_features_include_any) != 0)
      AND (p_spectral_features_include_all IS NULL OR (COALESCE(o.spectral_features, 0) & p_spectral_features_include_all) = p_spectral_features_include_all)
      AND (p_spectral_features_exclude IS NULL OR (COALESCE(o.spectral_features, 0) & p_spectral_features_exclude) = 0)
      AND (p_object_flags_include_any IS NULL OR (COALESCE(o.object_flags, 0) & p_object_flags_include_any) != 0)
      AND (p_object_flags_include_all IS NULL OR (COALESCE(o.object_flags, 0) & p_object_flags_include_all) = p_object_flags_include_all)
      AND (p_object_flags_exclude IS NULL OR (COALESCE(o.object_flags, 0) & p_object_flags_exclude) = 0)
      AND (p_dq_flags_include_any IS NULL OR (COALESCE(o.dq_flags, 0) & p_dq_flags_include_any) != 0)
      AND (p_dq_flags_include_all IS NULL OR (COALESCE(o.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
      AND (p_dq_flags_exclude IS NULL OR (COALESCE(o.dq_flags, 0) & p_dq_flags_exclude) = 0)
      AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%')
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
      )
      AND (
        NOT v_comment_search_active
        OR EXISTS (
          SELECT 1 FROM comments c
          WHERE c.object_id = o.id
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
        )
      )
      AND (
        NOT v_coord_search_active
        OR (
          o.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
          AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
        )
      )
  ),
  distance_filtered AS (
    SELECT fo.*
    FROM filtered_objects fo
    WHERE NOT v_coord_search_active OR fo.distance <= p_radius_degrees
  )
  SELECT
    df.object_id,
    df.field,
    df.ra,
    df.dec,
    df.redshift,
    df.redshift_quality,
    df.max_snr,
    df.max_exposure_time,
    df.num_gratings,
    df.program_slug,
    pr.program_name,
    df.last_inspected_at,
    df.last_inspected_by,
    df.distance
  FROM distance_filtered df
  LEFT JOIN programs pr ON pr.slug = df.program_slug
  ORDER BY
    CASE WHEN v_coord_search_active THEN df.distance END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN df.object_id END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN df.object_id END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
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
    df.object_id ASC;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_csv_export TO authenticated;

-- ---- mv_programs_overview (must exist before get_programs_overview function) ----

CREATE MATERIALIZED VIEW mv_programs_overview AS
SELECT
    p.slug,
    p.program_name,
    p.pi_name,
    p.description,
    p.is_public,
    p.cycle,
    COALESCE(stats.object_count, 0)::bigint AS object_count,
    COALESCE(stats.gratings, ARRAY[]::text[]) AS gratings,
    COALESCE(stats.fields, ARRAY[]::text[]) AS fields,
    COALESCE(stats.observations, ARRAY[]::text[]) AS observations,
    COALESCE(pids.jwst_pids, ARRAY[]::integer[]) AS jwst_pids
FROM programs p
LEFT JOIN (
    SELECT o.program_slug,
        COUNT(DISTINCT o.object_id) AS object_count,
        ARRAY_AGG(DISTINCT s.grating ORDER BY s.grating)
            FILTER (WHERE s.grating IS NOT NULL) AS gratings,
        ARRAY_AGG(DISTINCT o.field ORDER BY o.field) AS fields,
        ARRAY_AGG(DISTINCT o.observation ORDER BY o.observation) AS observations
    FROM objects o
    LEFT JOIN spectra s ON s.object_id = o.object_id
    GROUP BY o.program_slug
) stats ON p.slug = stats.program_slug
LEFT JOIN (
    SELECT program_slug,
        ARRAY_AGG(DISTINCT jwst_program_id ORDER BY jwst_program_id) AS jwst_pids
    FROM observations
    GROUP BY program_slug
) pids ON p.slug = pids.program_slug
WITH DATA;

CREATE UNIQUE INDEX mv_programs_overview_slug ON mv_programs_overview(slug);
GRANT SELECT ON mv_programs_overview TO authenticated;

-- ---- get_programs_overview ----

DROP FUNCTION IF EXISTS public.get_programs_overview();

CREATE OR REPLACE FUNCTION public.get_programs_overview()
RETURNS TABLE(
  slug text,
  program_name text,
  pi_name text,
  description text,
  is_public boolean,
  cycle integer,
  object_count bigint,
  gratings text[],
  fields text[],
  observations text[],
  jwst_pids integer[]
) LANGUAGE sql STABLE AS $$
  SELECT
    mv.slug,
    mv.program_name,
    mv.pi_name,
    mv.description,
    mv.is_public,
    mv.cycle,
    mv.object_count,
    mv.gratings,
    mv.fields,
    mv.observations,
    mv.jwst_pids
  FROM public.mv_programs_overview mv
  ORDER BY mv.program_name;
$$;

GRANT EXECUTE ON FUNCTION public.get_programs_overview TO authenticated;

-- ---- get_observation_stats ----

DROP FUNCTION IF EXISTS public.get_observation_stats(integer[]);

CREATE OR REPLACE FUNCTION public.get_observation_stats(p_program_slugs text[])
RETURNS TABLE(
  observation text,
  program_slug text,
  program_name text,
  field text,
  object_count bigint,
  spectrum_count bigint,
  total_size_bytes bigint
) LANGUAGE sql STABLE AS $$
  SELECT
    o.observation,
    o.program_slug,
    p.program_name,
    o.field,
    COUNT(DISTINCT o.object_id) AS object_count,
    COUNT(s.id) AS spectrum_count,
    COALESCE(SUM(s.file_size), 0)::bigint AS total_size_bytes
  FROM objects o
  JOIN programs p ON p.slug = o.program_slug
  LEFT JOIN spectra s ON s.object_id = o.object_id
  WHERE o.program_slug = ANY(p_program_slugs)
  GROUP BY o.observation, o.program_slug, p.program_name, o.field
  ORDER BY o.observation;
$$;

GRANT EXECUTE ON FUNCTION public.get_observation_stats TO authenticated;

-- ---- get_observation_manifest ----

DROP FUNCTION IF EXISTS public.get_observation_manifest(text, integer[]);

CREATE OR REPLACE FUNCTION public.get_observation_manifest(
  p_obs_name text,
  p_program_slugs text[]
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
    AND o.program_slug = ANY(p_program_slugs)
  ORDER BY s.object_id, s.grating;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_observation_manifest TO authenticated;

-- ---- get_objects_in_viewport ----

DROP FUNCTION IF EXISTS public.get_objects_in_viewport;

CREATE OR REPLACE FUNCTION public.get_objects_in_viewport(
    p_ra_min double precision,
    p_ra_max double precision,
    p_dec_min double precision,
    p_dec_max double precision,
    p_field text DEFAULT NULL,
    p_limit integer DEFAULT 5000
)
RETURNS TABLE (
    "object_id" text,
    "ra" double precision,
    "dec" double precision,
    "redshift" double precision,
    "redshift_quality" integer,
    "field" text,
    "program_slug" text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
AS $$
BEGIN
    RETURN QUERY
    SELECT
        o.object_id,
        o.ra,
        o.dec,
        o.redshift::double precision,
        o.redshift_quality,
        o.field,
        o.program_slug
    FROM public.objects o
    WHERE
        o.ra BETWEEN p_ra_min AND p_ra_max
        AND o.dec BETWEEN p_dec_min AND p_dec_max
        AND (p_field IS NULL OR o.field = p_field)
    ORDER BY o.ra
    LIMIT p_limit;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_objects_in_viewport TO authenticated;

-- ---- get_program_stats ----

DROP FUNCTION IF EXISTS public.get_program_stats();

CREATE OR REPLACE FUNCTION public.get_program_stats()
RETURNS TABLE(slug text, object_count bigint, user_access_count bigint)
LANGUAGE sql STABLE SECURITY DEFINER
AS $$
  SELECT
    p.slug,
    COALESCE(o.cnt, 0) AS object_count,
    COALESCE(a.cnt, 0) AS user_access_count
  FROM programs p
  LEFT JOIN (
    SELECT program_slug, COUNT(*) AS cnt
    FROM objects
    GROUP BY program_slug
  ) o ON p.slug = o.program_slug
  LEFT JOIN (
    SELECT program_slug, COUNT(*) AS cnt
    FROM user_program_access
    GROUP BY program_slug
  ) a ON p.slug = a.program_slug;
$$;

GRANT ALL ON FUNCTION public.get_program_stats() TO anon;
GRANT ALL ON FUNCTION public.get_program_stats() TO authenticated;
GRANT ALL ON FUNCTION public.get_program_stats() TO service_role;

-- ---- refresh_programs_overview (no changes needed, just recreate after MV) ----

CREATE OR REPLACE FUNCTION public.refresh_programs_overview()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
  REFRESH MATERIALIZED VIEW CONCURRENTLY mv_programs_overview;
END;
$$;

GRANT EXECUTE ON FUNCTION public.refresh_programs_overview TO authenticated;

COMMIT;
