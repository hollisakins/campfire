-- =============================================================================
-- Migration: Tag sharing (issue #450)
-- =============================================================================
-- Adds per-user view/edit sharing of tags (object_lists):
--
-- 1. New table object_list_shares (list_id, user_id, role viewer|editor):
--    a share makes the list visible to the grantee regardless of its
--    visibility setting; role=editor additionally allows adding/removing
--    members (same scope as public_edit — list metadata stays owner-only).
--    Owners manage shares; grantees can delete their own share (leave).
-- 2. New SECURITY DEFINER helpers viewable_list_ids() /
--    member_editable_list_ids() — the single authority for "which lists can
--    the caller see / edit members of" — now used by the object_lists,
--    object_list_members and list_audit_log RLS policies.
-- 3. The four RPCs that re-embed the list-visibility predicate
--    (get_objects_for_sync, get_lists_for_sync, get_csv_export_spectra,
--    get_csv_export_objects) learn the share clause.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. Table
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS "public"."object_list_shares" (
    "id" integer NOT NULL,
    "list_id" integer NOT NULL,
    "user_id" "uuid" NOT NULL,
    "role" "text" DEFAULT 'viewer'::"text" NOT NULL,
    "granted_by" "uuid",
    "granted_at" timestamp with time zone DEFAULT "now"(),
    CONSTRAINT "object_list_shares_role_check" CHECK (("role" = ANY (ARRAY['viewer'::"text", 'editor'::"text"])))
);

ALTER TABLE "public"."object_list_shares" OWNER TO "postgres";

COMMENT ON TABLE "public"."object_list_shares" IS 'Per-user sharing grants on object_lists (issue #450). A share gives the grantee visibility of the list regardless of its visibility setting; role=editor additionally allows adding/removing members (same scope as public_edit — list metadata stays owner-only). Owner manages shares; grantees can remove their own share (leave).';

CREATE SEQUENCE IF NOT EXISTS "public"."object_list_shares_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE "public"."object_list_shares_id_seq" OWNER TO "postgres";

ALTER SEQUENCE "public"."object_list_shares_id_seq" OWNED BY "public"."object_list_shares"."id";

ALTER TABLE ONLY "public"."object_list_shares" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."object_list_shares_id_seq"'::"regclass");

ALTER TABLE ONLY "public"."object_list_shares"
    ADD CONSTRAINT "object_list_shares_pkey" PRIMARY KEY ("id");

ALTER TABLE ONLY "public"."object_list_shares"
    ADD CONSTRAINT "object_list_shares_list_id_user_id_key" UNIQUE ("list_id", "user_id");

ALTER TABLE ONLY "public"."object_list_shares"
    ADD CONSTRAINT "object_list_shares_list_id_fkey" FOREIGN KEY ("list_id") REFERENCES "public"."object_lists"("id") ON DELETE CASCADE;

ALTER TABLE ONLY "public"."object_list_shares"
    ADD CONSTRAINT "object_list_shares_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;

ALTER TABLE ONLY "public"."object_list_shares"
    ADD CONSTRAINT "object_list_shares_granted_by_fkey" FOREIGN KEY ("granted_by") REFERENCES "auth"."users"("id");

GRANT ALL ON TABLE "public"."object_list_shares" TO "anon";
GRANT ALL ON TABLE "public"."object_list_shares" TO "authenticated";
GRANT ALL ON TABLE "public"."object_list_shares" TO "service_role";

GRANT ALL ON SEQUENCE "public"."object_list_shares_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."object_list_shares_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."object_list_shares_id_seq" TO "service_role";

CREATE INDEX IF NOT EXISTS idx_list_shares_user_id
    ON public.object_list_shares USING btree (user_id);


-- -----------------------------------------------------------------------------
-- 2. RLS helper functions
-- -----------------------------------------------------------------------------


-- Tag sharing (issue #450). These two helpers are the single authority for
-- "which lists can the caller see / edit members of": owner + public visibility
-- + per-user object_list_shares grants. SECURITY DEFINER so RLS policies on
-- object_lists / object_list_members / list_audit_log can call them without
-- recursing into each table's own policies.
CREATE OR REPLACE FUNCTION public.viewable_list_ids()
RETURNS SETOF integer
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT id FROM object_lists
  WHERE created_by = auth.uid()
     OR visibility IN ('public_read', 'public_edit')
     OR id IN (SELECT list_id FROM object_list_shares WHERE user_id = auth.uid());
$$;

GRANT EXECUTE ON FUNCTION public.viewable_list_ids() TO authenticated;

-- Lists whose MEMBERS the caller may add/remove (list metadata stays
-- owner-only): owner + public_edit + editor-role shares.
CREATE OR REPLACE FUNCTION public.member_editable_list_ids()
RETURNS SETOF integer
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT id FROM object_lists
  WHERE created_by = auth.uid()
     OR visibility = 'public_edit'
     OR id IN (SELECT list_id FROM object_list_shares WHERE user_id = auth.uid() AND role = 'editor');
$$;

GRANT EXECUTE ON FUNCTION public.member_editable_list_ids() TO authenticated;


-- -----------------------------------------------------------------------------
-- 3. RPCs that embed the list-visibility predicate
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.get_objects_for_sync(
  p_program_slugs TEXT[],
  p_user_id UUID DEFAULT NULL,
  p_updated_since TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_offset INTEGER DEFAULT 0,
  p_include_counts BOOLEAN DEFAULT TRUE,
  p_include_unpublished BOOLEAN DEFAULT false,
  -- Keyset cursor (#103): the object_id of the last row of the previous page.
  -- When non-NULL the scan seeks straight to the next id via the
  -- objects_object_id_key UNIQUE btree, so each page costs O(log N + limit)
  -- instead of OFFSET's O(offset + limit). p_offset is kept for old clients.
  p_after_object_id TEXT DEFAULT NULL
)
RETURNS TABLE(objects JSONB, total_count BIGINT, total_accessible_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
-- Keyset clients (p_after_object_id) never touch this timeout: each page is a
-- shallow index range scan. It is retained only for legacy OFFSET clients,
-- whose deep pages must materialize the ordered scan up to `offset` plus run
-- three aggregate CTEs and were tipping past the default service_role timeout
-- around page ~29 of a 30k-object --full sync. Drop this SET once offset
-- clients are gone (see #103 follow-up).
SET statement_timeout = '120s'
AS $$
BEGIN
  RETURN QUERY
  -- matched is MATERIALIZED so the three aggregate CTEs below each see the
  -- same ~p_limit-row set without re-evaluating the WHERE/ORDER/LIMIT.
  WITH matched AS MATERIALIZED (
    SELECT o.id, o.object_id, o.field, o.ra, o.dec,
           o.n_targets, o.n_spectra, o.programs, o.gratings,
           o.max_snr, o.max_exposure_time,
           o.redshift, o.redshift_quality,
           o.redshift_inspected, o.redshift_auto,
           o.inspected_used_auto,
           o.last_inspected_at, o.last_inspected_by,
           o.last_data_change_at, o.staleness_reason,
           o.version, o.is_active,
           o.has_photometry, o.photo_z, o.photo_z_err_lo, o.photo_z_err_hi,
           o.created_at, o.updated_at
    FROM objects o
    WHERE o.programs && p_program_slugs
      -- Phase D: hide soft-deleted objects from sync. Reactivation rewrites
      -- updated_at, so re-activated rows get re-synced naturally on next pull.
      AND o.is_active = true
      -- B1: drop objects with no published spectrum (fail-closed).
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
      -- Keyset (#103): seek past the previous page's last object_id. object_id
      -- is UNIQUE, so a strict > needs no (sort_col, id) tiebreaker. Any future
      -- change to this ORDER BY must keep the ordering column UNIQUE (or switch
      -- to a row-value cursor) or keyset will skip/duplicate rows.
      AND (p_after_object_id IS NULL OR o.object_id > p_after_object_id)
    ORDER BY o.object_id
    LIMIT p_limit OFFSET p_offset
  ),
  member_targets_agg AS (
    SELECT t.object_id,
           jsonb_agg(t.target_id ORDER BY t.target_id) AS target_ids
    FROM targets t
    WHERE t.object_id IN (SELECT id FROM matched)
      AND t.program_slug = ANY(p_program_slugs)
    GROUP BY t.object_id
  ),
  -- Phase D: per-spectrum payload (per design doc) so the Python client
  -- can render redshift_auto and dq_flags per grating without a second
  -- round-trip.
  spectra_agg AS (
    SELECT t.object_id,
           jsonb_agg(jsonb_build_object(
             'id', s.id,
             'target_id', s.target_id,
             'grating', s.grating,
             'signal_to_noise', s.signal_to_noise,
             'exposure_time', s.exposure_time,
             'redshift_auto', s.redshift_auto,
             'dq_flags', s.dq_flags
           ) ORDER BY s.target_id, s.grating) AS spectra
    FROM spectra s
    JOIN targets t ON t.target_id = s.target_id
    WHERE t.object_id IN (SELECT id FROM matched)
      AND t.program_slug = ANY(p_program_slugs)
      AND (p_include_unpublished OR s.deploy_status = 'published')
    GROUP BY t.object_id
  ),
  lists_agg AS (
    SELECT olm.object_id,
           jsonb_agg(ol.slug ORDER BY ol.slug) AS list_slugs
    FROM object_list_members olm
    JOIN object_lists ol ON ol.id = olm.list_id
    WHERE olm.object_id IN (SELECT id FROM matched)
      AND (ol.created_by = p_user_id
           OR ol.visibility IN ('public_read', 'public_edit')
           OR ol.id IN (SELECT list_id FROM object_list_shares WHERE user_id = p_user_id))
    GROUP BY olm.object_id
  ),
  -- Count CTEs are gated on p_include_counts; when FALSE the planner
  -- collapses them to One-Time Filter: false and skips the scan.
  total AS (
    SELECT COUNT(*) AS cnt
    FROM objects o
    WHERE p_include_counts
      AND o.programs && p_program_slugs
      AND o.is_active = true
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt
    FROM objects o
    WHERE p_include_counts
      AND o.programs && p_program_slugs
      AND o.is_active = true
      AND (p_include_unpublished OR o.has_published_spectrum)
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'object_id', m.object_id,
        'field', m.field,
        'ra', m.ra,
        'dec', m.dec,
        -- Aggregates scoped to the caller's accessible programs so a sync that
        -- pulls a mixed-program object doesn't leak proprietary member metadata
        -- into the Python catalog. See object_scoped_aggregates().
        'n_targets', sa.n_targets,
        'n_spectra', sa.n_spectra,
        'programs', sa.programs,
        'gratings', sa.gratings,
        'max_snr', sa.max_snr,
        'max_exposure_time', sa.max_exposure_time,
        'redshift', m.redshift,
        'redshift_quality', m.redshift_quality,
        'redshift_inspected', m.redshift_inspected,
        'redshift_auto', m.redshift_auto,
        'inspected_used_auto', m.inspected_used_auto,
        'last_inspected_at', m.last_inspected_at,
        'last_inspected_by', m.last_inspected_by,
        'last_data_change_at', m.last_data_change_at,
        'staleness_reason', m.staleness_reason,
        'version', m.version,
        'is_active', m.is_active,
        'has_photometry', m.has_photometry,
        'photo_z', m.photo_z,
        'photo_z_err_lo', m.photo_z_err_lo,
        'photo_z_err_hi', m.photo_z_err_hi,
        'created_at', m.created_at,
        'updated_at', m.updated_at,
        'member_target_ids', COALESCE(mt.target_ids, '[]'::jsonb),
        'spectra',           COALESCE(sp.spectra,    '[]'::jsonb),
        'lists',             COALESCE(la.list_slugs, '[]'::jsonb)
      )
      -- Keyset (#103): the client uses the LAST element's object_id as the next
      -- page's cursor, so the page array MUST be in object_id order. matched is
      -- ORDER BY object_id, but the LEFT JOINs below can reorder it, so pin the
      -- aggregate order explicitly.
      ORDER BY m.object_id
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m
  LEFT JOIN member_targets_agg mt ON mt.object_id = m.id
  LEFT JOIN spectra_agg         sp ON sp.object_id = m.id
  LEFT JOIN lists_agg           la ON la.object_id = m.id
  LEFT JOIN LATERAL public.object_scoped_aggregates(m.id, p_program_slugs, p_include_unpublished) sa ON true;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN, TEXT) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN, TEXT) TO service_role;


DROP FUNCTION IF EXISTS public.get_lists_for_sync(UUID);

CREATE OR REPLACE FUNCTION public.get_lists_for_sync(
  p_user_id UUID DEFAULT NULL,
  p_include_unpublished BOOLEAN DEFAULT false
)
RETURNS JSONB
LANGUAGE plpgsql STABLE
AS $$
BEGIN
  RETURN COALESCE(
    (SELECT jsonb_agg(jsonb_build_object(
      'id', ol.id,
      'slug', ol.slug,
      'name', ol.name,
      'description', ol.description,
      'visibility', ol.visibility,
      'is_system', ol.is_system,
      'created_by', ol.created_by,
      'created_at', ol.created_at,
      'updated_at', ol.updated_at,
      -- B1: count only members whose object has a published spectrum (unlinked
      -- coordinate-keyed members, object_id IS NULL, still count). Fail-closed.
      'member_count', (
        SELECT COUNT(*) FROM object_list_members olm
        LEFT JOIN objects o ON o.id = olm.object_id
        WHERE olm.list_id = ol.id
          AND (p_include_unpublished OR olm.object_id IS NULL OR o.has_published_spectrum)
      )
    ) ORDER BY ol.is_system DESC, ol.name)
    FROM object_lists ol
    WHERE ol.created_by = p_user_id
       OR ol.visibility IN ('public_read', 'public_edit')
       OR ol.id IN (SELECT list_id FROM object_list_shares WHERE user_id = p_user_id)),
    '[]'::jsonb
  );
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_lists_for_sync(UUID, BOOLEAN) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_lists_for_sync(UUID, BOOLEAN) TO service_role;


CREATE OR REPLACE FUNCTION public.get_csv_export_spectra(
  p_program_slugs TEXT[], p_filter_programs TEXT[] DEFAULT NULL,
  p_fields TEXT[] DEFAULT NULL, p_gratings TEXT[] DEFAULT NULL,
  p_gratings_mode TEXT DEFAULT 'any', p_observations TEXT[] DEFAULT NULL,
  p_redshift_quality INTEGER[] DEFAULT NULL,
  p_redshift_min DOUBLE PRECISION DEFAULT NULL, p_redshift_max DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_min DOUBLE PRECISION DEFAULT NULL, p_max_snr_max DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_min DOUBLE PRECISION DEFAULT NULL, p_max_exposure_time_max DOUBLE PRECISION DEFAULT NULL,
  p_dq_flags_include_any INTEGER DEFAULT NULL, p_dq_flags_include_all INTEGER DEFAULT NULL,
  p_dq_flags_exclude INTEGER DEFAULT NULL,
  p_list_ids INTEGER[] DEFAULT NULL,
  p_list_ids_mode TEXT DEFAULT 'any',
  p_search TEXT DEFAULT NULL, p_inspected_only BOOLEAN DEFAULT NULL,
  p_needs_review BOOLEAN DEFAULT NULL,
  p_has_photometry BOOLEAN DEFAULT NULL,
  p_comment_search TEXT DEFAULT NULL, p_comment_search_scope TEXT DEFAULT NULL,
  p_comment_user_id UUID DEFAULT NULL,
  p_coord_ra DOUBLE PRECISION DEFAULT NULL, p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_include_unpublished BOOLEAN DEFAULT false,
  p_after_id INTEGER DEFAULT NULL, p_page_size INTEGER DEFAULT 5000
)
RETURNS TABLE(
  id INTEGER, spectrum_id TEXT, target_id TEXT, grating TEXT, field TEXT, observation TEXT,
  ra DOUBLE PRECISION, "dec" DOUBLE PRECISION,
  redshift NUMERIC, redshift_quality INTEGER, redshift_auto DOUBLE PRECISION,
  signal_to_noise DOUBLE PRECISION,
  exposure_time DOUBLE PRECISION, fits_path TEXT, program_slug TEXT, program_name TEXT,
  last_inspected_at TIMESTAMPTZ, last_inspected_by TEXT, distance DOUBLE PRECISION,
  dq_flags INTEGER,
  lists TEXT
)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
SET statement_timeout = '120s'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
  v_page_size INTEGER;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (p_comment_search IS NOT NULL AND p_comment_search != '' AND p_comment_search_scope IN ('just_me', 'everyone'));
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN v_list_ids_mode := 'any'; END IF;
  v_page_size := LEAST(GREATEST(COALESCE(p_page_size, 5000), 1), 10000);
  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs)) INTO v_filtered_program_slugs;
  ELSE v_filtered_program_slugs := p_program_slugs; END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN RETURN; END IF;

  RETURN QUERY
  WITH visible_lists AS (
    SELECT olm.object_id, string_agg(ol.slug, ';' ORDER BY ol.slug) AS lists
    FROM object_list_members olm
    JOIN object_lists ol ON ol.id = olm.list_id
    WHERE ol.created_by = auth.uid() OR ol.visibility IN ('public_read', 'public_edit')
       OR ol.id IN (SELECT list_id FROM object_list_shares WHERE user_id = auth.uid())
    GROUP BY olm.object_id
  ),
  filtered_spectra AS (
    SELECT s.id, s.spectrum_id, t.target_id, s.grating, t.field, t.ra, t.dec,
      o.redshift, o.redshift_quality,
      s.redshift_auto,
      s.signal_to_noise, s.exposure_time, s.fits_path, t.program_slug, t.observation,
      o.last_inspected_at, o.last_inspected_by,
      CASE WHEN v_coord_search_active THEN
        2 * DEGREES(ASIN(SQRT(POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) + COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) * POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2))))
      ELSE NULL END AS distance,
      COALESCE(s.dq_flags, 0) AS dq_flags,
      vl.lists
    FROM targets t
    JOIN spectra s ON s.target_id = t.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    LEFT JOIN visible_lists vl ON vl.object_id = t.object_id
    WHERE t.program_slug = ANY(v_filtered_program_slugs)
      AND (p_after_id IS NULL OR s.id > p_after_id)
      AND (o.id IS NULL OR o.is_active = true)
      AND (NOT v_grating_filter_active OR s.grating = ANY(p_gratings))
      -- B1: hide unpublished spectra (fail-closed; admin opt-in only).
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min) AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR s.signal_to_noise >= p_max_snr_min) AND (p_max_snr_max IS NULL OR s.signal_to_noise <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR s.exposure_time >= p_max_exposure_time_min) AND (p_max_exposure_time_max IS NULL OR s.exposure_time <= p_max_exposure_time_max)
      AND (p_dq_flags_include_any IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_include_any) != 0)
      AND (p_dq_flags_include_all IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
      AND (p_dq_flags_exclude IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_exclude) = 0)
      AND (
        NOT v_list_filter_active
        OR (v_list_ids_mode = 'any' AND t.object_id IN (
            SELECT olm.object_id FROM object_list_members olm WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
        OR (v_list_ids_mode = 'all' AND (
            SELECT COUNT(DISTINCT olm.list_id) FROM object_list_members olm
            WHERE olm.object_id = t.object_id AND olm.list_id = ANY(p_list_ids)
        ) = (SELECT COUNT(DISTINCT __list_id) FROM unnest(p_list_ids) __list_id))
        OR (v_list_ids_mode = 'none' AND (t.object_id IS NULL OR t.object_id NOT IN (
            SELECT olm.object_id FROM object_list_members olm WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        )))
      )
      AND (p_search IS NULL OR s.id IN (SELECT __s.id FROM public.spectra __s WHERE __s.search_text ILIKE '%' || p_search || '%'))
      AND (p_inspected_only IS NULL OR (p_inspected_only = TRUE AND o.redshift_quality > 0) OR (p_inspected_only = FALSE AND COALESCE(o.redshift_quality, 0) = 0))
      AND (p_needs_review IS NULL
        OR (p_needs_review = TRUE
            AND o.staleness_reason IS NOT NULL
            AND o.last_inspected_at IS NOT NULL
            AND (o.last_data_change_at IS NULL OR o.last_data_change_at > o.last_inspected_at))
        OR (p_needs_review = FALSE
            AND (o.staleness_reason IS NULL
                 OR o.last_inspected_at IS NULL
                 OR (o.last_data_change_at IS NOT NULL AND o.last_data_change_at <= o.last_inspected_at))))
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (NOT v_comment_search_active OR t.id IN (
        SELECT c.target_id FROM comments c WHERE c.target_id IS NOT NULL AND c.is_deleted = false
          AND c.content ILIKE '%' || p_comment_search || '%'
          AND (p_comment_search_scope = 'everyone' OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id))))
      AND (NOT v_coord_search_active OR (
        t.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
        AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)))
  ),
  distance_filtered AS (SELECT fs.* FROM filtered_spectra fs WHERE NOT v_coord_search_active OR fs.distance <= p_radius_degrees)
  SELECT df.id, df.spectrum_id, df.target_id, df.grating, df.field, df.observation,
    df.ra, df.dec, df.redshift, df.redshift_quality, df.redshift_auto,
    df.signal_to_noise, df.exposure_time, df.fits_path, df.program_slug,
    pr.program_name, df.last_inspected_at, up.full_name AS last_inspected_by,
    df.distance, df.dq_flags, df.lists
  FROM distance_filtered df
  LEFT JOIN programs pr ON pr.slug = df.program_slug
  LEFT JOIN user_profiles up ON up.user_id = df.last_inspected_by
  ORDER BY df.id ASC
  LIMIT v_page_size;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_csv_export_spectra TO authenticated;


CREATE OR REPLACE FUNCTION public.get_csv_export_objects(
  p_program_slugs TEXT[], p_filter_programs TEXT[] DEFAULT NULL,
  p_fields TEXT[] DEFAULT NULL, p_gratings TEXT[] DEFAULT NULL,
  p_gratings_mode TEXT DEFAULT 'any',
  p_redshift_quality INTEGER[] DEFAULT NULL,
  p_redshift_min DOUBLE PRECISION DEFAULT NULL, p_redshift_max DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_min DOUBLE PRECISION DEFAULT NULL, p_max_snr_max DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_min DOUBLE PRECISION DEFAULT NULL, p_max_exposure_time_max DOUBLE PRECISION DEFAULT NULL,
  p_search TEXT DEFAULT NULL, p_inspected_only BOOLEAN DEFAULT NULL,
  p_needs_review BOOLEAN DEFAULT NULL,
  p_list_ids INTEGER[] DEFAULT NULL,
  p_list_ids_mode TEXT DEFAULT 'any',
  p_coord_ra DOUBLE PRECISION DEFAULT NULL, p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_has_photometry BOOLEAN DEFAULT NULL,
  p_photo_z_min DOUBLE PRECISION DEFAULT NULL, p_photo_z_max DOUBLE PRECISION DEFAULT NULL,
  p_comment_search TEXT DEFAULT NULL, p_comment_search_scope TEXT DEFAULT NULL,
  p_comment_user_id UUID DEFAULT NULL,
  p_include_unpublished BOOLEAN DEFAULT false,
  p_after_object_id TEXT DEFAULT NULL, p_page_size INTEGER DEFAULT 5000
)
RETURNS TABLE(
  object_id TEXT, field TEXT, ra DOUBLE PRECISION, "dec" DOUBLE PRECISION,
  redshift NUMERIC, redshift_quality INTEGER,
  redshift_inspected NUMERIC, redshift_auto DOUBLE PRECISION,
  last_inspected_at TIMESTAMPTZ, last_inspected_by TEXT,
  last_data_change_at TIMESTAMPTZ, staleness_reason TEXT, version INTEGER,
  n_targets INTEGER, n_spectra INTEGER,
  programs TEXT, gratings TEXT,
  max_snr DOUBLE PRECISION, max_exposure_time DOUBLE PRECISION,
  member_target_ids TEXT, distance DOUBLE PRECISION,
  lists TEXT,
  has_photometry BOOLEAN, photo_z DOUBLE PRECISION,
  photo_z_err_lo DOUBLE PRECISION, photo_z_err_hi DOUBLE PRECISION,
  photometry JSONB
)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
SET statement_timeout = '120s'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
  v_page_size INTEGER;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (
    p_comment_search IS NOT NULL
    AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN v_gratings_mode := 'any'; END IF;
  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN v_list_ids_mode := 'any'; END IF;
  v_page_size := LEAST(GREATEST(COALESCE(p_page_size, 5000), 1), 10000);

  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs)) INTO v_filtered_program_slugs;
  ELSE v_filtered_program_slugs := p_program_slugs; END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN RETURN; END IF;

  RETURN QUERY
  WITH member_targets AS (
    SELECT t.object_id, string_agg(t.target_id, ';' ORDER BY t.target_id) AS member_target_ids
    FROM targets t
    WHERE t.program_slug = ANY(v_filtered_program_slugs)
    GROUP BY t.object_id
  ),
  visible_lists AS (
    SELECT olm.object_id, string_agg(ol.slug, ';' ORDER BY ol.slug) AS lists
    FROM object_list_members olm
    JOIN object_lists ol ON ol.id = olm.list_id
    WHERE ol.created_by = auth.uid() OR ol.visibility IN ('public_read', 'public_edit')
       OR ol.id IN (SELECT list_id FROM object_list_shares WHERE user_id = auth.uid())
    GROUP BY olm.object_id
  ),
  filtered_objects AS (
    SELECT o.object_id, o.field, o.ra, o.dec,
      o.redshift, o.redshift_quality,
      o.redshift_inspected, o.redshift_auto,
      o.last_inspected_at, up.full_name AS last_inspected_by,
      o.last_data_change_at, o.staleness_reason, o.version,
      -- Aggregates scoped to accessible (+ filtered) programs so mixed-program
      -- objects don't export proprietary member metadata. See
      -- object_scoped_aggregates().
      sa.n_targets, sa.n_spectra,
      array_to_string(sa.programs, ';') AS programs,
      array_to_string(sa.gratings, ';') AS gratings,
      sa.max_snr, sa.max_exposure_time,
      mt.member_target_ids,
      CASE WHEN v_coord_search_active THEN
        2 * DEGREES(ASIN(SQRT(POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) + COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) * POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2))))
      ELSE NULL END AS distance,
      vl.lists,
      o.has_photometry, o.photo_z, o.photo_z_err_lo, o.photo_z_err_hi,
      phot.photometry
    FROM objects o
    LEFT JOIN member_targets mt ON mt.object_id = o.id
    LEFT JOIN visible_lists vl ON vl.object_id = o.id
    LEFT JOIN user_profiles up ON up.user_id = o.last_inspected_by
    LEFT JOIN LATERAL public.object_scoped_aggregates(o.id, v_filtered_program_slugs, p_include_unpublished) sa ON true
    LEFT JOIN LATERAL (
      SELECT op.photometry FROM object_photometry op
      WHERE op.object_id = o.id ORDER BY op.updated_at DESC LIMIT 1
    ) phot ON true
    WHERE o.programs && v_filtered_program_slugs
      AND (p_after_object_id IS NULL OR o.object_id > p_after_object_id)
      AND o.is_active = true
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (
        NOT v_grating_filter_active
        OR (v_gratings_mode = 'any' AND o.gratings && p_gratings)
        OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings)
        OR (v_gratings_mode = 'none' AND NOT o.gratings && p_gratings)
      )
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
      AND (p_inspected_only IS NULL OR (p_inspected_only = TRUE AND o.redshift_quality > 0) OR (p_inspected_only = FALSE AND o.redshift_quality = 0))
      AND (p_needs_review IS NULL
        OR (p_needs_review = TRUE
            AND o.staleness_reason IS NOT NULL
            AND o.last_inspected_at IS NOT NULL
            AND (o.last_data_change_at IS NULL OR o.last_data_change_at > o.last_inspected_at))
        OR (p_needs_review = FALSE
            AND (o.staleness_reason IS NULL
                 OR o.last_inspected_at IS NULL
                 OR (o.last_data_change_at IS NOT NULL AND o.last_data_change_at <= o.last_inspected_at))))
      AND (NOT v_coord_search_active OR (
        o.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
        AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
      ))
      AND (
        NOT v_list_filter_active
        OR (v_list_ids_mode = 'any' AND o.id IN (
            SELECT olm.object_id FROM object_list_members olm
            WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
        OR (v_list_ids_mode = 'all' AND (
            SELECT COUNT(DISTINCT olm.list_id) FROM object_list_members olm
            WHERE olm.object_id = o.id AND olm.list_id = ANY(p_list_ids)
        ) = (SELECT COUNT(DISTINCT __list_id) FROM unnest(p_list_ids) __list_id))
        OR (v_list_ids_mode = 'none' AND o.id NOT IN (
            SELECT olm.object_id FROM object_list_members olm
            WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
      )
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (p_photo_z_min IS NULL OR o.photo_z >= p_photo_z_min)
      AND (p_photo_z_max IS NULL OR o.photo_z <= p_photo_z_max)
      AND (
        NOT v_comment_search_active
        -- Uncorrelated semijoin; see get_filtered_objects_paginated for rationale.
        OR o.id IN (
          SELECT c.object_id FROM comments c
          WHERE c.object_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
          UNION
          SELECT t.object_id FROM comments c
          JOIN targets t ON t.id = c.target_id
          WHERE c.target_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
        )
      )
  ),
  distance_filtered AS (SELECT fo.* FROM filtered_objects fo WHERE NOT v_coord_search_active OR fo.distance <= p_radius_degrees)
  SELECT df.object_id, df.field, df.ra, df.dec,
    df.redshift, df.redshift_quality,
    df.redshift_inspected, df.redshift_auto,
    df.last_inspected_at, df.last_inspected_by,
    df.last_data_change_at, df.staleness_reason, df.version,
    df.n_targets, df.n_spectra,
    df.programs, df.gratings,
    df.max_snr, df.max_exposure_time,
    df.member_target_ids, df.distance, df.lists,
    df.has_photometry, df.photo_z, df.photo_z_err_lo, df.photo_z_err_hi,
    df.photometry
  FROM distance_filtered df
  ORDER BY df.object_id ASC
  LIMIT v_page_size;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_csv_export_objects TO authenticated;


-- -----------------------------------------------------------------------------
-- 4. RLS policies
-- -----------------------------------------------------------------------------

-- Users can see: their own lists + public lists + public_edit lists + lists
-- shared with them (issue #450). viewable_list_ids() (SECURITY DEFINER,
-- functions.sql) is the single authority for that predicate.
DROP POLICY IF EXISTS "select_lists" ON object_lists;
CREATE POLICY "select_lists"
  ON object_lists FOR SELECT TO authenticated
  USING (id IN (SELECT public.viewable_list_ids()));



-- Members visible if:
--   1. The list is visible to the user (owner / public / shared), AND
--   2. The matched object (if any) has at least one accessible program
-- Members with NULL object_id (orphaned) are visible to anyone who can edit
-- the list's members (owner, public_edit, editor-role share) — co-editors
-- need to see orphans; read-only viewers don't.
DROP POLICY IF EXISTS "select_list_members" ON object_list_members;
CREATE POLICY "select_list_members"
  ON object_list_members FOR SELECT TO authenticated
  USING (
    list_id IN (SELECT public.viewable_list_ids())
    AND (
      (object_id IS NULL AND list_id IN (SELECT public.member_editable_list_ids()))
      OR object_id IN (
        SELECT o.id FROM objects o
        WHERE o.programs && (SELECT public.accessible_program_slugs())
          -- B1 (#217): list members matched to an unpublished-only object are
          -- hidden from non-admins (the object itself is invisible).
          AND (o.has_published_spectrum OR (SELECT public.is_admin()))
      )
    )
  );

-- can_comment users can add members to own lists + public_edit lists +
-- editor-role shared lists.
DROP POLICY IF EXISTS "insert_list_members" ON object_list_members;
CREATE POLICY "insert_list_members"
  ON object_list_members FOR INSERT TO authenticated
  WITH CHECK (
    (SELECT public.can_comment())
    AND list_id IN (SELECT public.member_editable_list_ids())
  );

-- can_comment users can update members in member-editable lists
-- (needed for upsert ON CONFLICT DO UPDATE when re-linking coordinate entries).
DROP POLICY IF EXISTS "update_list_members" ON object_list_members;
CREATE POLICY "update_list_members"
  ON object_list_members FOR UPDATE TO authenticated
  USING (
    (SELECT public.can_comment())
    AND list_id IN (SELECT public.member_editable_list_ids())
  )
  WITH CHECK (
    (SELECT public.can_comment())
    AND list_id IN (SELECT public.member_editable_list_ids())
  );

-- can_comment users can remove members from member-editable lists.
DROP POLICY IF EXISTS "delete_list_members" ON object_list_members;
CREATE POLICY "delete_list_members"
  ON object_list_members FOR DELETE TO authenticated
  USING (
    (SELECT public.can_comment())
    AND list_id IN (SELECT public.member_editable_list_ids())
  );

-- Audit log visible if the parent list is visible (owner / public / shared).
DROP POLICY IF EXISTS "select_list_audit" ON list_audit_log;
CREATE POLICY "select_list_audit"
  ON list_audit_log FOR SELECT TO authenticated
  USING (list_id IN (SELECT public.viewable_list_ids()));

-- =============================================================================
-- object_list_shares  (tag sharing, issue #450)
-- =============================================================================
-- NOTE: policies here reference object_lists via plain subqueries; that is
-- safe from RLS recursion because the object_lists policies reach shares only
-- through the SECURITY DEFINER helpers (viewable_list_ids et al.), which do
-- not re-enter policy evaluation.

ALTER TABLE object_list_shares ENABLE ROW LEVEL SECURITY;

-- Grantees see their own share rows; list owners see all shares on their lists.
DROP POLICY IF EXISTS "select_list_shares" ON object_list_shares;
CREATE POLICY "select_list_shares"
  ON object_list_shares FOR SELECT TO authenticated
  USING (
    user_id = (SELECT auth.uid())
    OR list_id IN (
      SELECT id FROM object_lists WHERE created_by = (SELECT auth.uid())
    )
  );

-- Only the list owner grants shares, on their own non-system lists. granted_by
-- is stamped with the grantor; self-shares are pointless and disallowed.
DROP POLICY IF EXISTS "insert_list_shares" ON object_list_shares;
CREATE POLICY "insert_list_shares"
  ON object_list_shares FOR INSERT TO authenticated
  WITH CHECK (
    granted_by = (SELECT auth.uid())
    AND user_id <> (SELECT auth.uid())
    AND list_id IN (
      SELECT id FROM object_lists
      WHERE created_by = (SELECT auth.uid()) AND is_system = false
    )
  );

-- Only the list owner changes a share's role.
DROP POLICY IF EXISTS "update_list_shares" ON object_list_shares;
CREATE POLICY "update_list_shares"
  ON object_list_shares FOR UPDATE TO authenticated
  USING (
    list_id IN (
      SELECT id FROM object_lists WHERE created_by = (SELECT auth.uid())
    )
  )
  WITH CHECK (
    list_id IN (
      SELECT id FROM object_lists WHERE created_by = (SELECT auth.uid())
    )
  );

-- The list owner revokes shares; grantees can remove their own share (leave).
DROP POLICY IF EXISTS "delete_list_shares" ON object_list_shares;
CREATE POLICY "delete_list_shares"
  ON object_list_shares FOR DELETE TO authenticated
  USING (
    user_id = (SELECT auth.uid())
    OR list_id IN (
      SELECT id FROM object_lists WHERE created_by = (SELECT auth.uid())
    )
  );

-- Admins can manage all shares.
DROP POLICY IF EXISTS "admin_manage_list_shares" ON object_list_shares;
CREATE POLICY "admin_manage_list_shares"
  ON object_list_shares
  USING ((SELECT public.is_admin()));

