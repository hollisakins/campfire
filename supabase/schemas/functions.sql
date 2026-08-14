-- =============================================================================
-- CAMPFIRE Supabase Schema: Functions
-- =============================================================================
-- Canonical source of truth for all RPC and helper functions.
-- Do NOT read migration files to understand current signatures or behavior.
--
-- Workflow: edit here → run apply.sh → supabase db diff → commit migration
-- =============================================================================


-- =============================================================================
-- RLS Helper Functions
-- =============================================================================

CREATE OR REPLACE FUNCTION public.is_admin()
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE(
    (SELECT is_admin FROM user_profiles WHERE user_id = auth.uid()),
    false
  );
$$;

GRANT EXECUTE ON FUNCTION public.is_admin() TO authenticated;

CREATE OR REPLACE FUNCTION public.can_comment()
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE(
    (SELECT can_comment FROM user_profiles WHERE user_id = auth.uid()),
    false
  );
$$;

GRANT EXECUTE ON FUNCTION public.can_comment() TO authenticated;

-- Gates write access to inspection state (object redshift/quality, target
-- inspection fields, per-spectrum DQ flags). Distinct from can_comment, which
-- gates comments and tag/list editing. Self-registered users default to
-- can_inspect = false and request the privilege from an admin.
CREATE OR REPLACE FUNCTION public.can_inspect()
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE(
    (SELECT can_inspect FROM user_profiles WHERE user_id = auth.uid()),
    false
  );
$$;

GRANT EXECUTE ON FUNCTION public.can_inspect() TO authenticated;

CREATE OR REPLACE FUNCTION public.is_group_account()
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE(
    (SELECT is_group_account FROM user_profiles WHERE user_id = auth.uid()),
    false
  );
$$;

GRANT EXECUTE ON FUNCTION public.is_group_account() TO authenticated;

-- =============================================================================
-- Share links (docs/design-public-mirror.md)
-- =============================================================================
-- A share link is backed by a synthetic authenticated principal (a "link
-- account"). Because it authenticates like any other user, every reader in the
-- portal works unchanged -- and the entire security story is the narrowing
-- conjuncts these four helpers feed throughout policies.sql.
--
-- All four are argument-free on purpose. A policy conjunct written as
--     (SELECT public.link_observation()) = t.observation
-- evaluates the helper ONCE PER QUERY (Postgres treats the scalar subquery as
-- an InitPlan), whereas a row-varying call like link_ok(t.observation) would
-- run per row. `targets` and `spectra` back the big paginated table queries, so
-- that difference is the hot path of the whole portal. This is the same reason
-- every policy in this schema wraps is_admin() as `(SELECT public.is_admin())`.
--
-- SECURITY DEFINER because a link account cannot read share_links (RLS there is
-- admin-only) and must not be able to.

CREATE OR REPLACE FUNCTION public.is_link_account()
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE(
    (SELECT is_link_account FROM user_profiles WHERE user_id = auth.uid()),
    false
  );
$$;

GRANT EXECUTE ON FUNCTION public.is_link_account() TO authenticated;


-- The scoped observation for the calling link account, or NULL for everyone
-- else (and for a field-scoped link). A revoked or expired link resolves to
-- NULL on BOTH axes, so its session reads nothing even before the account is
-- deleted -- revocation takes effect on the next query, not the next refresh.
CREATE OR REPLACE FUNCTION public.link_observation()
RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT sl.observation
  FROM share_links sl
  WHERE sl.link_user_id = auth.uid()
    AND sl.revoked_at IS NULL
    AND (sl.expires_at IS NULL OR sl.expires_at > now());
$$;

GRANT EXECUTE ON FUNCTION public.link_observation() TO authenticated;


CREATE OR REPLACE FUNCTION public.link_field()
RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT sl.field
  FROM share_links sl
  WHERE sl.link_user_id = auth.uid()
    AND sl.revoked_at IS NULL
    AND (sl.expires_at IS NULL OR sl.expires_at > now());
$$;

GRANT EXECUTE ON FUNCTION public.link_field() TO authenticated;


-- Whether this link may also see draft (in-prep) rows inside its scope. This is
-- what makes "reduce it for a colleague without publishing it" work: deploy
-- with `campfire deploy --in-prep`, then mint a link with include_drafts.
--
-- NOTE this widens an invariant that used to be absolute: before share links,
-- deploy_status='draft' meant "admins only", full stop. It now means "admins,
-- plus a link account scoped to that row". Still narrow -- link_sees_drafts()
-- only ever appears ANDed with a scope match -- but the next person reading
-- these policies deserves to know the rule has an exception.
CREATE OR REPLACE FUNCTION public.link_sees_drafts()
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE(
    (SELECT sl.include_drafts
     FROM share_links sl
     WHERE sl.link_user_id = auth.uid()
       AND sl.revoked_at IS NULL
       AND (sl.expires_at IS NULL OR sl.expires_at > now())),
    false
  );
$$;

GRANT EXECUTE ON FUNCTION public.link_sees_drafts() TO authenticated;


-- Whether this link may download FITS. Gates the whole storage_objects select
-- policy, which every download path in the portal and the API runs through.
--
-- MUST be a SECURITY DEFINER helper rather than an inline subquery over
-- share_links: that table is admin-only under RLS, so a policy reading it
-- directly evaluates to no-rows for the very link account it is deciding
-- about -- silently denying every download instead of honouring the flag.
CREATE OR REPLACE FUNCTION public.link_allows_download()
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE(
    (SELECT sl.allow_download
     FROM share_links sl
     WHERE sl.link_user_id = auth.uid()
       AND sl.revoked_at IS NULL
       AND (sl.expires_at IS NULL OR sl.expires_at > now())),
    false
  );
$$;

GRANT EXECUTE ON FUNCTION public.link_allows_download() TO authenticated;


CREATE OR REPLACE FUNCTION public.accessible_program_slugs()
RETURNS text[]
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  -- Share links (docs/design-public-mirror.md §5.1): a link account gets ONLY
  -- the program of its scoped observation. No user_program_access union (it has
  -- no grants), no is_public union, no admin union.
  --
  -- Dropping is_public is the load-bearing part. Without it, handing someone a
  -- share link would hand them every public program in the archive -- and
  -- because ~20 policies route through this function, that one omission would
  -- leak through all of them at once. A field-scoped link gets '{}': NIRCam is
  -- not program-gated at all, so its narrowing lives on the field axis instead.
  SELECT CASE WHEN public.is_link_account() THEN
    COALESCE(
      (SELECT array_agg(DISTINCT o.program_slug)
       FROM observations o
       WHERE o.name = public.link_observation()),
      '{}'::text[]
    )
  ELSE
    COALESCE(
      (SELECT array_agg(DISTINCT slug)
       FROM (
         SELECT program_slug AS slug
         FROM user_program_access
         WHERE user_id = auth.uid()
         UNION
         SELECT slug
         FROM programs
         WHERE is_public = true
         UNION
         -- Admins (the operators) inherit access to every program. This is the
         -- single point where admin access is granted to all program-gated data:
         -- every RLS SELECT policy routes through accessible_program_slugs(), so
         -- this lets admins read back rows they deploy for private programs
         -- (otherwise an upsert's RETURNING fails the SELECT policy -> 42501).
         SELECT slug
         FROM programs
         WHERE public.is_admin()
       ) sub),
      '{}'::text[]
    )
  END;
$$;

GRANT EXECUTE ON FUNCTION public.accessible_program_slugs() TO authenticated;

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


-- Whether a FitsGL dataset (epic #337, Phase 3) is public: every backing mosaic it
-- was built from is published. The pyramid in the public tiles bucket is built from
-- ALL of the dataset's on-disk mosaics, so a composite that mixes published + draft
-- mosaics must stay hidden until they ALL publish (a plain EXISTS-any-published would
-- leak the draft imagery). SECURITY DEFINER so it sees draft nircam_images rows the
-- caller's own RLS would hide — otherwise the "NOT EXISTS unpublished" check can never
-- fire for a non-admin. Requires ≥1 backing mosaic present AND none unpublished.
CREATE OR REPLACE FUNCTION public.fitsgl_dataset_is_public(
  p_field text, p_tiles text[], p_bands text[], p_pixel_scale text
) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT EXISTS (
    SELECT 1 FROM nircam_images ni
    WHERE ni.field = p_field AND ni.tile = ANY (p_tiles)
      AND ni.filter = ANY (p_bands) AND ni.pixel_scale = p_pixel_scale
      AND ni.epoch = '' AND ni.deploy_status = 'published'
  ) AND NOT EXISTS (
    SELECT 1 FROM nircam_images ni
    WHERE ni.field = p_field AND ni.tile = ANY (p_tiles)
      AND ni.filter = ANY (p_bands) AND ni.pixel_scale = p_pixel_scale
      AND ni.epoch = '' AND ni.deploy_status <> 'published'
  );
$$;

GRANT EXECUTE ON FUNCTION public.fitsgl_dataset_is_public(text, text[], text[], text) TO authenticated;


-- =============================================================================
-- NIRCam field summaries  (NIRCam page redesign; issue #303 fields table)
-- =============================================================================
-- Both are SECURITY INVOKER: nircam_images RLS (deploy_status = 'published' OR
-- is_admin()) does the visibility gating, so non-admins get published-only
-- aggregates and admins see everything with no manual deploy_status filter.
-- Driven by nircam_images so only fields with visible mosaics appear; the fields
-- table (LEFT JOIN) supplies display_name / center / coverage_area, and
-- storage_objects supplies the deployed layout-plot key for the card preview.

-- Landing grid: one row per field the caller can see any mosaic of.
CREATE OR REPLACE FUNCTION public.get_nircam_fields()
RETURNS TABLE(
  field text,
  display_name text,
  center_ra double precision,
  center_dec double precision,
  coverage_area_arcmin2 double precision,
  coverage_area_deg2 double precision,
  n_filters integer,
  n_tiles integer,
  n_files bigint,
  total_bytes bigint,
  last_updated timestamp without time zone,
  layout_key text
)
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = public
AS $$
  SELECT
    ni.field,
    COALESCE(f.display_name, upper(ni.field)) AS display_name,
    f.center_ra,
    f.center_dec,
    f.coverage_area_arcmin2,
    f.coverage_area_deg2,
    COUNT(DISTINCT ni.filter)::integer AS n_filters,
    COUNT(DISTINCT ni.tile)::integer   AS n_tiles,
    COUNT(*)::bigint                    AS n_files,
    COALESCE(SUM(ni.file_size), 0)::bigint AS total_bytes,
    MAX(ni.created_at)                  AS last_updated,
    (SELECT so.storage_key FROM storage_objects so
       WHERE so.product_type = 'nircam_layout'
         AND so.field = ni.field
         AND so.status = 'active'
       LIMIT 1)                         AS layout_key
  FROM nircam_images ni
  LEFT JOIN fields f ON f.name = ni.field
  GROUP BY ni.field, f.display_name, f.center_ra, f.center_dec,
           f.coverage_area_arcmin2, f.coverage_area_deg2
  ORDER BY ni.field;
$$;

GRANT EXECUTE ON FUNCTION public.get_nircam_fields() TO authenticated;


-- Field detail page: the single-field version with the facet arrays.
CREATE OR REPLACE FUNCTION public.get_nircam_field_summary(p_field text)
RETURNS TABLE(
  field text,
  display_name text,
  center_ra double precision,
  center_dec double precision,
  coverage_area_arcmin2 double precision,
  coverage_area_deg2 double precision,
  filters text[],
  tiles text[],
  pixel_scales text[],
  extensions text[],
  epochs text[],
  n_files bigint,
  total_bytes bigint,
  last_updated timestamp without time zone,
  layout_key text
)
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = public
AS $$
  SELECT
    ni.field,
    COALESCE(f.display_name, upper(ni.field)) AS display_name,
    f.center_ra,
    f.center_dec,
    f.coverage_area_arcmin2,
    f.coverage_area_deg2,
    array_agg(DISTINCT ni.filter ORDER BY ni.filter)           AS filters,
    array_agg(DISTINCT ni.tile ORDER BY ni.tile)               AS tiles,
    array_agg(DISTINCT ni.pixel_scale ORDER BY ni.pixel_scale) AS pixel_scales,
    array_agg(DISTINCT ni.extension ORDER BY ni.extension)     AS extensions,
    array_agg(DISTINCT ni.epoch ORDER BY ni.epoch)             AS epochs,
    COUNT(*)::bigint                       AS n_files,
    COALESCE(SUM(ni.file_size), 0)::bigint AS total_bytes,
    MAX(ni.created_at)                     AS last_updated,
    (SELECT so.storage_key FROM storage_objects so
       WHERE so.product_type = 'nircam_layout'
         AND so.field = ni.field
         AND so.status = 'active'
       LIMIT 1)                            AS layout_key
  FROM nircam_images ni
  LEFT JOIN fields f ON f.name = ni.field
  WHERE ni.field = p_field
  GROUP BY ni.field, f.display_name, f.center_ra, f.center_dec,
           f.coverage_area_arcmin2, f.coverage_area_deg2;
$$;

GRANT EXECUTE ON FUNCTION public.get_nircam_field_summary(text) TO authenticated;


-- =============================================================================
-- object_scoped_aggregates
-- =============================================================================
-- Viewer-scoped recompute of an object's aggregate columns.
--
-- The objects table stores aggregate columns (programs, gratings, observations,
-- n_targets, n_spectra, max_snr, max_exposure_time) computed across ALL member
-- targets at deploy time (see python/campfire/deploy/objects.py). Object row
-- visibility is granted by array overlap (policies.sql: programs && accessible),
-- so a viewer who can access only SOME member programs still sees the row — and
-- the stored aggregates would leak the existence/metadata of proprietary members
-- they cannot access (programs[] names them; counts/snr/exposure quantify them).
--
-- This helper recomputes those aggregates restricted to p_program_slugs, using
-- the SAME semantics as the deploy-time builder so that a full-access viewer
-- (p_program_slugs ⊇ the object's programs) gets values identical to the stored
-- columns. Member-level payloads (member_targets, spectra) are already filtered
-- in each read RPC; this covers the object's own columns.
--
-- Callers MUST pass an already-access-checked slug array (the caller's accessible
-- set, optionally narrowed by an active program filter). Do not rely on RLS
-- inside this function: it is SECURITY INVOKER, but when called from a
-- SECURITY DEFINER context it would run as the owner with RLS bypassed — the
-- explicit p_program_slugs filter is the access gate.
DROP FUNCTION IF EXISTS public.object_scoped_aggregates(INTEGER, TEXT[]);

CREATE OR REPLACE FUNCTION public.object_scoped_aggregates(
  p_object_id INTEGER,
  p_program_slugs TEXT[],
  p_include_unpublished BOOLEAN DEFAULT false
)
RETURNS TABLE(
  programs          TEXT[],
  gratings          TEXT[],
  observations      TEXT[],
  n_targets         INTEGER,
  n_spectra         INTEGER,
  max_snr           DOUBLE PRECISION,
  max_exposure_time DOUBLE PRECISION
)
LANGUAGE plpgsql STABLE
AS $$
BEGIN
  -- Fast path (perf, issue #103): when the caller can access every program this
  -- object belongs to AND unpublished members are excluded, the viewer-scoped
  -- recompute below is provably identical to the aggregate columns already
  -- stored on the object -- both are the deploy-time builder's aggregation over
  -- published members (reconcile_field_objects keeps the stored columns in
  -- lockstep with the object row, and targets/spectra only change at deploy).
  -- Restricting the recompute to a superset of the object's programs drops
  -- nothing, so `o.programs <@ p_program_slugs` is exactly the "recompute ==
  -- stored" condition. Returning the stored columns via a single PK lookup skips
  -- the per-row targets+spectra scans that dominated get_objects_for_sync (and
  -- the catalog list RPCs) at scale. Partial-access or draft-inclusive callers
  -- fall through to the recompute, preserving the anti-leak scoping (see the
  -- header comment and supabase/tests/check_object_aggregate_scoping.sql).
  IF NOT p_include_unpublished THEN
    RETURN QUERY
    SELECT o.programs, o.gratings, o.observations,
           o.n_targets, o.n_spectra, o.max_snr, o.max_exposure_time
    FROM objects o
    WHERE o.id = p_object_id
      AND o.programs <@ p_program_slugs;
    IF FOUND THEN
      RETURN;
    END IF;
  END IF;

  RETURN QUERY
  WITH m AS (
    SELECT t.target_id, t.program_slug, t.observation
    FROM targets t
    WHERE t.object_id = p_object_id
      AND t.program_slug = ANY(p_program_slugs)
      -- B1: only count targets that contribute a published spectrum so
      -- n_targets / programs / observations don't include draft-only members.
      AND (p_include_unpublished OR t.has_published_spectrum)
  ),
  sp AS (
    SELECT s.grating, s.signal_to_noise, s.exposure_time
    FROM spectra s
    WHERE s.target_id IN (SELECT target_id FROM m)
      AND (p_include_unpublished OR s.deploy_status = 'published')
  )
  SELECT
    COALESCE((SELECT array_agg(DISTINCT m.program_slug ORDER BY m.program_slug) FROM m), '{}')::text[],
    COALESCE((SELECT array_agg(DISTINCT sp.grating ORDER BY sp.grating) FROM sp WHERE sp.grating IS NOT NULL), '{}')::text[],
    COALESCE((SELECT array_agg(DISTINCT m.observation ORDER BY m.observation) FROM m WHERE m.observation IS NOT NULL), '{}')::text[],
    (SELECT COUNT(*) FROM m)::integer,
    (SELECT COUNT(*) FROM sp)::integer,
    (SELECT MAX(sp.signal_to_noise) FROM sp),
    (SELECT MAX(sp.exposure_time) FROM sp);
END;
$$;

GRANT EXECUTE ON FUNCTION public.object_scoped_aggregates(INTEGER, TEXT[], BOOLEAN) TO authenticated;
GRANT EXECUTE ON FUNCTION public.object_scoped_aggregates(INTEGER, TEXT[], BOOLEAN) TO service_role;


-- =============================================================================
-- Device Code Auth
-- =============================================================================

CREATE OR REPLACE FUNCTION public.authorize_device_code(p_user_code text, p_user_id uuid)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  updated_rows INTEGER;
BEGIN
  -- A JWT-bearing caller can only authorize a code for THEMSELVES: p_user_id
  -- is caller-supplied and this function is EXECUTEable by `authenticated`, so
  -- without the binding a session could mint an API credential for an
  -- arbitrary user id. Service-role callers (the web routes) pass through.
  IF (SELECT auth.role()) <> 'service_role'
     AND (auth.uid() IS NULL OR p_user_id IS DISTINCT FROM auth.uid()) THEN
    RETURN false;
  END IF;

  -- Share links (docs/design-public-mirror.md §5.5): a link account must never
  -- mint a durable API credential. Its cookie session is scoped by RLS, but a
  -- device-flow token would outlive revocation and be honoured by API-layer
  -- authorization paths. Fail closed at the source of the grant.
  IF EXISTS (
    SELECT 1 FROM public.user_profiles up
    WHERE up.user_id = p_user_id AND up.is_link_account
  ) THEN
    RETURN false;
  END IF;

  UPDATE device_codes
  SET
    status = 'authorized',
    user_id = p_user_id,
    authorized_at = NOW()
  WHERE
    user_code = p_user_code
    AND status = 'pending'
    AND expires_at > NOW();

  GET DIAGNOSTICS updated_rows = ROW_COUNT;
  RETURN updated_rows > 0;
END;
$$;

GRANT ALL ON FUNCTION public.authorize_device_code(text, uuid) TO anon;
GRANT ALL ON FUNCTION public.authorize_device_code(text, uuid) TO authenticated;
GRANT ALL ON FUNCTION public.authorize_device_code(text, uuid) TO service_role;

CREATE OR REPLACE FUNCTION public.check_device_code_status(p_device_code text)
RETURNS TABLE(status text, user_id uuid, is_expired boolean)
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
  RETURN QUERY
  SELECT
    dc.status,
    dc.user_id,
    (dc.expires_at < NOW())::BOOLEAN AS is_expired
  FROM device_codes dc
  WHERE dc.device_code = p_device_code;
END;
$$;

GRANT ALL ON FUNCTION public.check_device_code_status(text) TO anon;
GRANT ALL ON FUNCTION public.check_device_code_status(text) TO authenticated;
GRANT ALL ON FUNCTION public.check_device_code_status(text) TO service_role;




-- =============================================================================
-- Access Code Redemption
-- =============================================================================

-- Redeems an access code for the calling user in a single transaction.
-- SECURITY DEFINER so codes never need to be readable by non-admins (the
-- access_codes SELECT policy is admin-only); the row lock (FOR UPDATE) makes
-- the max_uses check + use_count increment atomic under concurrent redemptions.
-- Returns a jsonb object whose 'status' key the API route maps to HTTP codes.
CREATE OR REPLACE FUNCTION public.redeem_access_code(p_code text)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_uid uuid := auth.uid();
  v_code access_codes%ROWTYPE;
  v_slugs text[];
BEGIN
  IF v_uid IS NULL THEN
    RETURN jsonb_build_object('status', 'unauthenticated');
  END IF;

  IF public.is_group_account() THEN
    RETURN jsonb_build_object('status', 'group_account');
  END IF;

  SELECT * INTO v_code
  FROM access_codes
  WHERE code = p_code AND is_active = true
  FOR UPDATE;

  IF NOT FOUND THEN
    RETURN jsonb_build_object('status', 'invalid');
  END IF;

  IF v_code.expires_at IS NOT NULL AND v_code.expires_at < NOW() THEN
    RETURN jsonb_build_object('status', 'expired');
  END IF;

  IF v_code.max_uses IS NOT NULL AND v_code.use_count >= v_code.max_uses THEN
    RETURN jsonb_build_object('status', 'exhausted');
  END IF;

  IF EXISTS (
    SELECT 1 FROM code_redemptions
    WHERE code_id = v_code.id AND user_id = v_uid
  ) THEN
    RETURN jsonb_build_object('status', 'already_redeemed');
  END IF;

  IF v_code.grants_all_programs THEN
    SELECT array_agg(slug) INTO v_slugs FROM programs;
  ELSE
    v_slugs := v_code.program_slugs;
  END IF;

  IF v_slugs IS NULL OR array_length(v_slugs, 1) IS NULL THEN
    RETURN jsonb_build_object('status', 'no_programs');
  END IF;

  INSERT INTO user_program_access (user_id, program_slug, granted_by)
  SELECT v_uid, slug, v_code.created_by
  FROM unnest(v_slugs) AS slug
  ON CONFLICT (user_id, program_slug) DO NOTHING;

  INSERT INTO code_redemptions (code_id, user_id)
  VALUES (v_code.id, v_uid);

  UPDATE access_codes
  SET use_count = use_count + 1
  WHERE id = v_code.id;

  RETURN jsonb_build_object(
    'status', 'ok',
    'grants_all_programs', v_code.grants_all_programs,
    'programs_granted', COALESCE(array_length(v_slugs, 1), 0)
  );
END;
$$;

REVOKE ALL ON FUNCTION public.redeem_access_code(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.redeem_access_code(text) FROM anon;
GRANT EXECUTE ON FUNCTION public.redeem_access_code(text) TO authenticated;
GRANT EXECUTE ON FUNCTION public.redeem_access_code(text) TO service_role;




-- =============================================================================
-- get_objects_for_sync
-- (lightweight bulk fetch for Python client objects catalog sync)
-- =============================================================================

DROP FUNCTION IF EXISTS public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN);
DROP FUNCTION IF EXISTS public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN);

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


-- =============================================================================
-- get_spectra_for_sync
-- (bulk fetch of per-spectrum download-relevant metadata for the Python
-- client; complements get_objects_for_sync which carries display-level
-- spectra fields only)
-- =============================================================================

DROP FUNCTION IF EXISTS public.get_spectra_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN);
DROP FUNCTION IF EXISTS public.get_spectra_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN);

CREATE OR REPLACE FUNCTION public.get_spectra_for_sync(
  p_program_slugs TEXT[],
  p_user_id UUID DEFAULT NULL,
  p_updated_since TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_offset INTEGER DEFAULT 0,
  p_include_counts BOOLEAN DEFAULT TRUE,
  p_include_unpublished BOOLEAN DEFAULT false,
  -- Keyset cursor (#103): the spectrum_id of the last row of the previous page,
  -- seeked via the idx_spectra_spectrum_id UNIQUE btree. See
  -- get_objects_for_sync for the design. p_offset is kept for old clients.
  p_after_spectrum_id TEXT DEFAULT NULL
)
RETURNS TABLE(spectra JSONB, total_count BIGINT, total_accessible_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
-- Mirrors get_objects_for_sync: retained only for legacy OFFSET clients, whose
-- deep --full-sync pages can tip past the default timeout. Keyset clients
-- (p_after_spectrum_id) never reach it. Drop once offset clients are gone (#103).
SET statement_timeout = '120s'
AS $$
BEGIN
  RETURN QUERY
  WITH matched AS MATERIALIZED (
    SELECT s.id, s.spectrum_id, s.target_id, o.object_id AS object_id,
           s.grating, s.fits_path, s.file_hash, s.file_size,
           s.signal_to_noise, s.exposure_time,
           s.cfpipe_version, s.crds_context, s.jwst_version, s.date_obs, s.reduced_at,
           s.redshift_auto, s.dq_flags,
           t.program_slug, t.observation, t.field,
           s.created_at, s.updated_at
    FROM spectra s
    JOIN targets t ON t.target_id = s.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE t.program_slug = ANY(p_program_slugs)
      AND (o.id IS NULL OR o.is_active = true)
      -- B1: fail-closed publish gate (this RPC always bypasses RLS).
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND (p_updated_since IS NULL OR s.updated_at > p_updated_since)
      -- Keyset (#103): spectrum_id is UNIQUE (idx_spectra_spectrum_id), so a
      -- strict > needs no tiebreaker; keep the ordering column UNIQUE.
      AND (p_after_spectrum_id IS NULL OR s.spectrum_id > p_after_spectrum_id)
    ORDER BY s.spectrum_id
    LIMIT p_limit OFFSET p_offset
  ),
  -- Count CTEs are gated on p_include_counts; when FALSE the planner
  -- collapses them to One-Time Filter: false and skips the scan/join.
  total AS (
    SELECT COUNT(*) AS cnt
    FROM spectra s
    JOIN targets t ON t.target_id = s.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE p_include_counts
      AND t.program_slug = ANY(p_program_slugs)
      AND (o.id IS NULL OR o.is_active = true)
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND (p_updated_since IS NULL OR s.updated_at > p_updated_since)
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt
    FROM spectra s
    JOIN targets t ON t.target_id = s.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE p_include_counts
      AND t.program_slug = ANY(p_program_slugs)
      AND (o.id IS NULL OR o.is_active = true)
      AND (p_include_unpublished OR s.deploy_status = 'published')
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'spectrum_id', m.spectrum_id,
        'target_id', m.target_id,
        'object_id', m.object_id,
        'grating', m.grating,
        'fits_path', m.fits_path,
        'file_hash', m.file_hash,
        'file_size', m.file_size,
        'signal_to_noise', m.signal_to_noise,
        'exposure_time', m.exposure_time,
        'cfpipe_version', m.cfpipe_version,
        'crds_context', m.crds_context,
        'jwst_version', m.jwst_version,
        'date_obs', m.date_obs,
        'reduced_at', m.reduced_at,
        'redshift_auto', m.redshift_auto,
        'dq_flags', m.dq_flags,
        'program_slug', m.program_slug,
        'observation', m.observation,
        'field', m.field,
        'created_at', m.created_at,
        'updated_at', m.updated_at
      )
      -- Keyset (#103): page array must be spectrum_id-ordered (client cursors on
      -- the last element). matched has no post-ORDER joins, but pin it anyway.
      ORDER BY m.spectrum_id
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_spectra_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN, TEXT) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_spectra_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN, TEXT) TO service_role;


-- =============================================================================
-- get_photometry_for_sync
-- (bulk fetch for Python client photometry sync)
-- =============================================================================

DROP FUNCTION IF EXISTS public.get_photometry_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, INTEGER);
DROP FUNCTION IF EXISTS public.get_photometry_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN);

CREATE OR REPLACE FUNCTION public.get_photometry_for_sync(
  p_program_slugs TEXT[],
  p_updated_since TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_offset INTEGER DEFAULT 0,
  p_include_unpublished BOOLEAN DEFAULT false,
  -- Count gating (#103): only the keyset first page needs the count; skip the
  -- COUNT(*) scan on every subsequent page, matching the other /sync/* RPCs.
  p_include_counts BOOLEAN DEFAULT TRUE,
  -- Keyset cursor (#103): the id of the last row of the previous page, seeked
  -- via the object_photometry PK btree. p_offset is kept for old clients.
  p_after_id INTEGER DEFAULT NULL
)
RETURNS TABLE(photometry_records JSONB, total_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
-- Mirrors get_objects_for_sync: retained only for legacy OFFSET clients whose
-- deep --full-sync pages can tip past the default timeout. Keyset clients
-- (p_after_id) never reach it. Drop once offset clients are gone (#103).
SET statement_timeout = '120s'
AS $$
BEGIN
  RETURN QUERY
  WITH matched AS (
    SELECT op.id, o.object_id, op.field, op.catalog_name, op.catalog_id,
           op.match_distance_arcsec, op.photometry, op.photo_z,
           op.photo_z_err_lo, op.photo_z_err_hi, op.has_pz,
           op.created_at, op.updated_at
    FROM object_photometry op
    JOIN objects o ON o.id = op.object_id
    WHERE o.programs && p_program_slugs
      -- B1: fail-closed publish gate (this RPC always bypasses RLS).
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_updated_since IS NULL OR op.updated_at > p_updated_since)
      -- Keyset (#103): op.id is the PK, so a strict > needs no tiebreaker.
      AND (p_after_id IS NULL OR op.id > p_after_id)
    ORDER BY op.id
    LIMIT p_limit OFFSET p_offset
  ),
  -- Count CTE gated on p_include_counts; when FALSE the planner collapses it to
  -- One-Time Filter: false and skips the scan/join.
  total AS (
    SELECT COUNT(*) AS cnt
    FROM object_photometry op
    JOIN objects o ON o.id = op.object_id
    WHERE p_include_counts
      AND o.programs && p_program_slugs
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_updated_since IS NULL OR op.updated_at > p_updated_since)
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'object_id', m.object_id,
        'field', m.field,
        'catalog_name', m.catalog_name,
        'catalog_id', m.catalog_id,
        'match_distance_arcsec', m.match_distance_arcsec,
        'photometry', m.photometry,
        'photo_z', m.photo_z,
        'photo_z_err_lo', m.photo_z_err_lo,
        'photo_z_err_hi', m.photo_z_err_hi,
        'has_pz', m.has_pz,
        'created_at', m.created_at,
        'updated_at', m.updated_at
      )
      -- Keyset (#103): page array must be id-ordered (client cursors on the
      -- last element).
      ORDER BY m.id
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT
  FROM matched m;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_photometry_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN, INTEGER) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_photometry_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN, INTEGER) TO service_role;


-- =============================================================================
-- get_lists_for_sync
-- (returns all list metadata for Python client sync)
-- =============================================================================

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


-- =============================================================================
-- get_filtered_spectra_paginated
-- (final version: per-spectrum S/N and exposure_time filtering)
-- =============================================================================

-- Phase D: spectra rows now read inspection state through their parent object
-- (targets are stateless provenance). The redshift_quality / redshift filters
-- query objects.redshift_quality / objects.redshift via the targets→objects
-- FK; DQ filters operate on the per-spectrum spectra.dq_flags. The
-- spectral_features parameters are gone (Phase E drop). Parameter list
-- shrunk, so drop the old signature first.
DROP FUNCTION IF EXISTS public.get_filtered_spectra_paginated(
  TEXT[], TEXT[], TEXT[], TEXT[], TEXT, TEXT[], INTEGER[],
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION,
  DOUBLE PRECISION, DOUBLE PRECISION,
  INTEGER, INTEGER, INTEGER,
  INTEGER, INTEGER, INTEGER,
  INTEGER[], TEXT, BOOLEAN, BOOLEAN, TEXT, TEXT, UUID,
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, TEXT, TEXT, INTEGER, INTEGER, BOOLEAN
);

DROP FUNCTION IF EXISTS public.get_filtered_spectra_paginated;

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
  p_dq_flags_include_any INTEGER DEFAULT NULL,
  p_dq_flags_include_all INTEGER DEFAULT NULL,
  p_dq_flags_exclude INTEGER DEFAULT NULL,
  p_list_ids INTEGER[] DEFAULT NULL,
  p_list_ids_mode TEXT DEFAULT 'any',
  p_search TEXT DEFAULT NULL,
  p_inspected_only BOOLEAN DEFAULT NULL,
  p_needs_review BOOLEAN DEFAULT NULL,
  p_has_photometry BOOLEAN DEFAULT NULL,
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
  p_include_unpublished BOOLEAN DEFAULT false
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
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
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

  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN
    v_list_ids_mode := 'any';
  END IF;

  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  IF NOT (p_sort_column IN (
    'target_id', 'spectrum_id', 'field', 'observation', 'program_slug', 'ra', 'dec', 'redshift',
    'redshift_quality', 'redshift_auto', 'signal_to_noise', 'exposure_time', 'grating'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'spectrum_id';
  END IF;

  IF v_coord_search_active AND p_sort_column IN ('target_id', 'spectrum_id') AND p_sort_direction = 'asc' THEN
    p_sort_column := 'distance';
  END IF;

  v_offset := (COALESCE(p_page, 1) - 1) * COALESCE(p_page_size, 50);

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
    RETURN QUERY SELECT '[]'::jsonb, 0::BIGINT, p_page, p_page_size;
    RETURN;
  END IF;

  -- Single-pass CTE: filtered_spectra is referenced by both distance_filtered
  -- and the count subquery, so PostgreSQL materializes it once.
  RETURN QUERY
  WITH filtered_spectra AS (
    SELECT
      t.id AS tgt_db_id,
      t.target_id,
      t.program_slug,
      t.field,
      t.observation,
      t.ra,
      t.dec,
      -- Phase D: redshift / redshift_quality / inspected flags now live on the
      -- parent object. LEFT JOIN so spectra whose target has no object FK
      -- (shouldn't happen post-reconcile, but safe) still appear.
      o.redshift,
      o.redshift_quality,
      o.redshift_inspected,
      o.last_inspected_at,
      o.last_inspected_by,
      o.is_active AS object_is_active,
      o.has_photometry AS object_has_photometry,
      o.object_id AS parent_object_id,
      t.max_snr,
      t.max_exposure_time,
      t.created_at,
      t.updated_at,
      s.id AS spectrum_pk,
      s.spectrum_id,
      s.grating,
      s.fits_path,
      s.signal_to_noise,
      s.exposure_time,
      s.redshift_auto,
      COALESCE(s.dq_flags, 0) AS dq_flags,
      s.file_hash,
      s.file_size,
      s.thumbnail_svg_fnu,
      s.thumbnail_svg_flambda,
      CASE
        WHEN v_coord_search_active THEN
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) *
            POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL
      END AS distance
    FROM targets t
    JOIN spectra s ON s.target_id = t.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE
      t.program_slug = ANY(v_filtered_program_slugs)
      -- Hide spectra whose parent object was soft-deleted.
      AND (o.id IS NULL OR o.is_active = true)
      AND (NOT v_grating_filter_active OR s.grating = ANY(p_gratings))
      -- B1: hide unpublished spectra (fail-closed; admin opt-in only).
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR s.signal_to_noise >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR s.signal_to_noise <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR s.exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR s.exposure_time <= p_max_exposure_time_max)
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
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND COALESCE(o.redshift_quality, 0) = 0)
      )
      AND (
        p_needs_review IS NULL
        OR (p_needs_review = TRUE
            AND o.staleness_reason IS NOT NULL
            AND o.last_inspected_at IS NOT NULL
            AND (o.last_data_change_at IS NULL OR o.last_data_change_at > o.last_inspected_at))
        OR (p_needs_review = FALSE
            AND (o.staleness_reason IS NULL
                 OR o.last_inspected_at IS NULL
                 OR (o.last_data_change_at IS NOT NULL AND o.last_data_change_at <= o.last_inspected_at)))
      )
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (
        NOT v_comment_search_active
        -- Uncorrelated semijoin: build the set of matching target_ids ONCE
        -- (trgm/seq scan over the tiny comments table) instead of re-probing
        -- comments per outer row. Correlated EXISTS-inside-OR can't be pulled
        -- up and re-executes per spectrum -> timeouts on broad access. See the
        -- objects path below for the object-level analogue.
        OR t.id IN (
          SELECT c.target_id FROM comments c
          WHERE c.target_id IS NOT NULL
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
          t.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
          AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
        )
      )
  ),
  distance_filtered AS (
    SELECT fs.*
    FROM filtered_spectra fs
    WHERE NOT v_coord_search_active OR fs.distance <= p_radius_degrees
  ),
  page_rows AS (
    SELECT *, ROW_NUMBER() OVER () as row_num
    FROM (
      SELECT * FROM distance_filtered
      ORDER BY
        CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'asc' THEN distance END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'desc' THEN distance END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'asc' THEN target_id END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'desc' THEN target_id END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'spectrum_id' AND p_sort_direction = 'asc' THEN spectrum_id END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'spectrum_id' AND p_sort_direction = 'desc' THEN spectrum_id END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN field END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN field END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN observation END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN observation END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'program_slug' AND p_sort_direction = 'asc' THEN program_slug END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'program_slug' AND p_sort_direction = 'desc' THEN program_slug END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN ra END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN ra END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN "dec" END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN "dec" END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN redshift END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN redshift END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN redshift_quality END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN redshift_quality END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'redshift_auto' AND p_sort_direction = 'asc' THEN redshift_auto END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'redshift_auto' AND p_sort_direction = 'desc' THEN redshift_auto END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'signal_to_noise' AND p_sort_direction = 'asc' THEN signal_to_noise END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'signal_to_noise' AND p_sort_direction = 'desc' THEN signal_to_noise END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'exposure_time' AND p_sort_direction = 'asc' THEN exposure_time END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'exposure_time' AND p_sort_direction = 'desc' THEN exposure_time END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'grating' AND p_sort_direction = 'asc' THEN grating END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'grating' AND p_sort_direction = 'desc' THEN grating END DESC NULLS LAST,
        target_id ASC, grating ASC
      LIMIT p_page_size OFFSET v_offset
    ) sorted_page
  )
  SELECT
    COALESCE(jsonb_agg(jsonb_build_object(
      'id', r.tgt_db_id,
      'target_id', r.target_id,
      'parent_object_id', r.parent_object_id,
      'program_slug', r.program_slug,
      'program_name', pr.program_name,
      'field', r.field,
      'observation', r.observation,
      'ra', r.ra,
      'dec', r.dec,
      -- Phase D: redshift fields are object-level reads
      'redshift', r.redshift,
      'redshift_inspected', r.redshift_inspected,
      'redshift_quality', r.redshift_quality,
      'last_inspected_at', r.last_inspected_at,
      'last_inspected_by', r.last_inspected_by,
      'max_snr', r.max_snr,
      'max_exposure_time', r.max_exposure_time,
      'created_at', r.created_at,
      'updated_at', r.updated_at,
      'distance', CASE WHEN v_coord_search_active THEN r.distance ELSE NULL END,
      'spectra', jsonb_build_array(jsonb_build_object(
        'id', r.spectrum_pk,
        'spectrum_id', r.spectrum_id,
        'target_id', r.target_id,
        'grating', r.grating,
        'fits_path', r.fits_path,
        'signal_to_noise', r.signal_to_noise,
        'exposure_time', r.exposure_time,
        -- Phase D: per-spectrum auto-z and DQ
        'redshift_auto', r.redshift_auto,
        'dq_flags', r.dq_flags,
        'file_hash', r.file_hash,
        'file_size', r.file_size,
        'thumbnail_svg_fnu', CASE WHEN p_include_thumbnails THEN r.thumbnail_svg_fnu ELSE NULL END,
        'thumbnail_svg_flambda', CASE WHEN p_include_thumbnails THEN r.thumbnail_svg_flambda ELSE NULL END
      ))
    ) ORDER BY r.row_num), '[]'::jsonb),
    (SELECT COUNT(*) FROM distance_filtered),
    p_page,
    p_page_size
  FROM page_rows r
  LEFT JOIN programs pr ON pr.slug = r.program_slug;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_filtered_spectra_paginated TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_filtered_spectra_paginated TO service_role;


-- =============================================================================
-- get_filtered_objects_paginated
-- (one row per unique sky position, cross-matched across programs)
-- =============================================================================

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
  p_search TEXT DEFAULT NULL,
  p_inspected_only BOOLEAN DEFAULT NULL,
  p_needs_review BOOLEAN DEFAULT NULL,
  p_list_ids INTEGER[] DEFAULT NULL,
  p_list_ids_mode TEXT DEFAULT 'any',
  p_coord_ra DOUBLE PRECISION DEFAULT NULL,
  p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_has_photometry BOOLEAN DEFAULT NULL,
  p_photo_z_min DOUBLE PRECISION DEFAULT NULL,
  p_photo_z_max DOUBLE PRECISION DEFAULT NULL,
  p_comment_search TEXT DEFAULT NULL,
  p_comment_search_scope TEXT DEFAULT NULL,
  p_comment_user_id UUID DEFAULT NULL,
  p_sort_column TEXT DEFAULT 'object_id',
  p_sort_direction TEXT DEFAULT 'asc',
  p_page INTEGER DEFAULT 1,
  p_page_size INTEGER DEFAULT 50,
  p_include_unpublished BOOLEAN DEFAULT false
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
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
  v_offset INTEGER;
  v_total_count BIGINT;
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
  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN
    v_list_ids_mode := 'any';
  END IF;

  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  IF NOT (p_sort_column IN (
    'object_id', 'field', 'ra', 'dec', 'redshift', 'redshift_quality',
    'n_targets', 'n_spectra', 'max_snr', 'max_exposure_time', 'photo_z'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'object_id';
  END IF;

  IF v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN
    p_sort_column := 'distance';
  END IF;

  v_offset := (COALESCE(p_page, 1) - 1) * COALESCE(p_page_size, 50);

  -- Intersect user-accessible programs with filter selection
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
    RETURN QUERY SELECT '[]'::jsonb, 0::BIGINT, p_page, p_page_size;
    RETURN;
  END IF;

  -- Step 1: count
  SELECT COUNT(*) INTO v_total_count
  FROM objects o
  WHERE
    -- Access control: object must have at least one accessible program
    o.programs && v_filtered_program_slugs
    AND o.is_active = true
    -- B1: hide objects with no published spectrum (fail-closed).
    AND (p_include_unpublished OR o.has_published_spectrum)
    AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
    AND (
      NOT v_grating_filter_active
      OR (v_gratings_mode = 'any' AND o.gratings && p_gratings)
      OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings)
      OR (v_gratings_mode = 'none' AND NOT o.gratings && p_gratings)
    )
    AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observations && p_observations)
    AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
    AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
    AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
    AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
    AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
    AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
    AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
    AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
    AND (
      p_inspected_only IS NULL
      OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
      OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
    )
    AND (
      p_needs_review IS NULL
      OR (p_needs_review = TRUE
          AND o.staleness_reason IS NOT NULL
          AND o.last_inspected_at IS NOT NULL
          AND (o.last_data_change_at IS NULL OR o.last_data_change_at > o.last_inspected_at))
      OR (p_needs_review = FALSE
          AND (o.staleness_reason IS NULL
               OR o.last_inspected_at IS NULL
               OR (o.last_data_change_at IS NOT NULL AND o.last_data_change_at <= o.last_inspected_at)))
    )
    AND (
      NOT v_coord_search_active
      OR (
        o.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
        AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
        AND 2 * DEGREES(ASIN(SQRT(
          POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
          COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
          POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
        ))) <= p_radius_degrees
      )
    )
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
      -- Uncorrelated semijoin: collect the object_ids that have a matching
      -- comment ONCE (object-level comments directly + target-level comments
      -- mapped through their parent object), then probe o.id IN (...). The old
      -- correlated EXISTS-inside-OR re-ran a per-object targets subquery for
      -- every (object x matching-comment) pair -> 271k subplan executions /
      -- ~870ms here, multi-second on broad terms or cold cache.
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
    );

  -- Step 2: fetch page
  RETURN QUERY
  WITH filtered_objects AS (
    SELECT
      o.id,
      o.object_id,
      o.field,
      o.ra,
      o.dec,
      o.n_targets,
      o.n_spectra,
      o.programs,
      o.gratings,
      o.max_snr,
      o.max_exposure_time,
      o.redshift,
      o.redshift_quality,
      o.redshift_inspected,
      o.redshift_auto,
      o.inspected_used_auto,
      o.last_inspected_at,
      o.last_inspected_by,
      o.last_data_change_at,
      o.staleness_reason,
      o.version,
      o.is_active,
      o.photo_z,
      o.has_photometry,
      o.created_at,
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
      o.programs && v_filtered_program_slugs
      AND o.is_active = true
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (
        NOT v_grating_filter_active
        OR (v_gratings_mode = 'any' AND o.gratings && p_gratings)
        OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings)
        OR (v_gratings_mode = 'none' AND NOT o.gratings && p_gratings)
      )
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observations && p_observations)
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
      )
      AND (
        p_needs_review IS NULL
        OR (p_needs_review = TRUE
            AND o.staleness_reason IS NOT NULL
            AND o.last_inspected_at IS NOT NULL
            AND (o.last_data_change_at IS NULL OR o.last_data_change_at > o.last_inspected_at))
        OR (p_needs_review = FALSE
            AND (o.staleness_reason IS NULL
                 OR o.last_inspected_at IS NULL
                 OR (o.last_data_change_at IS NOT NULL AND o.last_data_change_at <= o.last_inspected_at)))
      )
      AND (
        NOT v_coord_search_active
        OR (
          o.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
          AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
          AND 2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
            POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
          ))) <= p_radius_degrees
        )
      )
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
        -- Uncorrelated semijoin; see the count query above for the rationale.
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
    ORDER BY
      CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'asc' THEN
        2 * DEGREES(ASIN(SQRT(
          POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
          COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
          POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
        ))) END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'desc' THEN
        2 * DEGREES(ASIN(SQRT(
          POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
          COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
          POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
        ))) END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN o.object_id END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN o.object_id END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN o.field END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN o.field END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN o.ra END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN o.ra END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN o.dec END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN o.dec END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN o.redshift END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN o.redshift END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN o.redshift_quality END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN o.redshift_quality END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'n_targets' AND p_sort_direction = 'asc' THEN o.n_targets END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'n_targets' AND p_sort_direction = 'desc' THEN o.n_targets END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'n_spectra' AND p_sort_direction = 'asc' THEN o.n_spectra END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'n_spectra' AND p_sort_direction = 'desc' THEN o.n_spectra END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN o.max_snr END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN o.max_snr END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN o.max_exposure_time END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN o.max_exposure_time END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'photo_z' AND p_sort_direction = 'asc' THEN o.photo_z END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'photo_z' AND p_sort_direction = 'desc' THEN o.photo_z END DESC NULLS LAST,
      o.object_id ASC
    LIMIT p_page_size OFFSET v_offset
  ),
  with_members AS (
    SELECT
      jsonb_build_object(
        'id', fo.id,
        'object_id', fo.object_id,
        'field', fo.field,
        'ra', fo.ra,
        'dec', fo.dec,
        -- Aggregates scoped to the viewer's accessible programs so mixed-program
        -- objects don't leak proprietary member metadata. Deliberately NOT
        -- narrowed by p_filter_programs: a program filter selects which objects
        -- appear (overlap test above), but each row still shows the object's
        -- full accessible programs/observations. Filter and sort above run on
        -- the global o.* columns; the substitution happens only on the
        -- paginated result set.
        'n_targets', sa.n_targets,
        'n_spectra', sa.n_spectra,
        'programs', sa.programs,
        'gratings', sa.gratings,
        'max_snr', sa.max_snr,
        'max_exposure_time', sa.max_exposure_time,
        'redshift', fo.redshift,
        'redshift_quality', fo.redshift_quality,
        'redshift_inspected', fo.redshift_inspected,
        'redshift_auto', fo.redshift_auto,
        'inspected_used_auto', fo.inspected_used_auto,
        'last_inspected_at', fo.last_inspected_at,
        'last_inspected_by', fo.last_inspected_by,
        'last_data_change_at', fo.last_data_change_at,
        'staleness_reason', fo.staleness_reason,
        'version', fo.version,
        'is_active', fo.is_active,
        'photo_z', fo.photo_z,
        'has_photometry', fo.has_photometry,
        'created_at', fo.created_at,
        'distance', fo.distance,
        -- Phase D: member_targets becomes provenance only (target_id, program,
        -- observation). Inspection state lives on the object now; redshift_auto
        -- on targets is retained for transitional UI display until Phase E.
        'member_targets', COALESCE(
          (SELECT jsonb_agg(
            jsonb_build_object(
              'target_id', t.target_id,
              'program_slug', t.program_slug,
              'observation', t.observation,
              'redshift_auto', t.redshift_auto
            )
          )
          FROM targets t
          WHERE t.object_id = fo.id
            AND t.program_slug = ANY(p_program_slugs)
            -- Same publication gate as object_scoped_aggregates: the RPC is
            -- reached via the service-role client (/api/v1/objects), so RLS
            -- won't hide draft-only members here.
            AND (p_include_unpublished OR t.has_published_spectrum)
          ),
          '[]'::jsonb
        ),
        'lists', COALESCE(
          (SELECT jsonb_agg(
            jsonb_build_object(
              'id', ol.id,
              'name', ol.name,
              'slug', ol.slug,
              'icon', ol.icon,
              'color', ol.color
            ) ORDER BY ol.name
          )
          FROM object_list_members olm
          JOIN object_lists ol ON ol.id = olm.list_id
          WHERE olm.object_id = fo.id),
          '[]'::jsonb
        )
      ) AS obj_json
    FROM filtered_objects fo
    LEFT JOIN LATERAL public.object_scoped_aggregates(fo.id, p_program_slugs, p_include_unpublished) sa ON true
  )
  SELECT
    COALESCE(jsonb_agg(wm.obj_json), '[]'::jsonb),
    v_total_count,
    p_page,
    p_page_size
  FROM with_members wm;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_filtered_objects_paginated TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_filtered_objects_paginated TO service_role;


-- =============================================================================
-- get_filtered_object_ids
-- (lightweight: returns only object_id strings for map marker filtering)
-- =============================================================================

DROP FUNCTION IF EXISTS public.get_filtered_object_ids;

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
  p_search TEXT DEFAULT NULL,
  p_inspected_only BOOLEAN DEFAULT NULL,
  p_needs_review BOOLEAN DEFAULT NULL,
  p_list_ids INTEGER[] DEFAULT NULL,
  p_list_ids_mode TEXT DEFAULT 'any',
  p_coord_ra DOUBLE PRECISION DEFAULT NULL,
  p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_has_photometry BOOLEAN DEFAULT NULL,
  p_photo_z_min DOUBLE PRECISION DEFAULT NULL,
  p_photo_z_max DOUBLE PRECISION DEFAULT NULL,
  p_comment_search TEXT DEFAULT NULL,
  p_comment_search_scope TEXT DEFAULT NULL,
  p_comment_user_id UUID DEFAULT NULL,
  p_sort_column TEXT DEFAULT 'object_id',
  p_sort_direction TEXT DEFAULT 'asc',
  p_include_unpublished BOOLEAN DEFAULT false
)
RETURNS TABLE(object_id TEXT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
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
  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN
    v_list_ids_mode := 'any';
  END IF;

  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  IF NOT (p_sort_column IN (
    'object_id', 'field', 'ra', 'dec', 'redshift', 'redshift_quality',
    'n_targets', 'n_spectra', 'max_snr', 'max_exposure_time', 'photo_z'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'object_id';
  END IF;

  -- Intersect user-accessible programs with filter selection
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
  SELECT o.object_id
  FROM objects o
  WHERE
    o.programs && v_filtered_program_slugs
    AND o.is_active = true
    AND (p_include_unpublished OR o.has_published_spectrum)
    AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
    AND (
      NOT v_grating_filter_active
      OR (v_gratings_mode = 'any' AND o.gratings && p_gratings)
      OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings)
      OR (v_gratings_mode = 'none' AND NOT o.gratings && p_gratings)
    )
    AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observations && p_observations)
    AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
    AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
    AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
    AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
    AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
    AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
    AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
    AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
    AND (
      p_inspected_only IS NULL
      OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
      OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
    )
    AND (
      p_needs_review IS NULL
      OR (p_needs_review = TRUE
          AND o.staleness_reason IS NOT NULL
          AND o.last_inspected_at IS NOT NULL
          AND (o.last_data_change_at IS NULL OR o.last_data_change_at > o.last_inspected_at))
      OR (p_needs_review = FALSE
          AND (o.staleness_reason IS NULL
               OR o.last_inspected_at IS NULL
               OR (o.last_data_change_at IS NOT NULL AND o.last_data_change_at <= o.last_inspected_at)))
    )
    AND (
      NOT v_coord_search_active
      OR (
        o.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
        AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
        AND 2 * DEGREES(ASIN(SQRT(
          POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
          COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
          POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
        ))) <= p_radius_degrees
      )
    )
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
  ORDER BY
    CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'asc' THEN
      2 * DEGREES(ASIN(SQRT(
        POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
        COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
        POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
      ))) END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'desc' THEN
      2 * DEGREES(ASIN(SQRT(
        POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
        COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
        POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
      ))) END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN o.object_id END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN o.object_id END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN o.field END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN o.field END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN o.ra END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN o.ra END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN o.dec END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN o.dec END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN o.redshift END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN o.redshift END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN o.redshift_quality END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN o.redshift_quality END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'n_targets' AND p_sort_direction = 'asc' THEN o.n_targets END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'n_targets' AND p_sort_direction = 'desc' THEN o.n_targets END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'n_spectra' AND p_sort_direction = 'asc' THEN o.n_spectra END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'n_spectra' AND p_sort_direction = 'desc' THEN o.n_spectra END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN o.max_snr END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN o.max_snr END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN o.max_exposure_time END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN o.max_exposure_time END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'photo_z' AND p_sort_direction = 'asc' THEN o.photo_z END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'photo_z' AND p_sort_direction = 'desc' THEN o.photo_z END DESC NULLS LAST,
    o.object_id ASC;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_filtered_object_ids TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_filtered_object_ids TO service_role;



-- =============================================================================
-- get_adjacent_objects
-- =============================================================================

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
  p_search TEXT DEFAULT NULL,
  p_inspected_only BOOLEAN DEFAULT NULL,
  p_needs_review BOOLEAN DEFAULT NULL,
  p_list_ids INTEGER[] DEFAULT NULL,
  p_list_ids_mode TEXT DEFAULT 'any',
  p_coord_ra DOUBLE PRECISION DEFAULT NULL,
  p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_sort_column TEXT DEFAULT 'object_id',
  p_sort_direction TEXT DEFAULT 'asc',
  p_has_photometry BOOLEAN DEFAULT NULL,
  p_photo_z_min DOUBLE PRECISION DEFAULT NULL,
  p_photo_z_max DOUBLE PRECISION DEFAULT NULL,
  p_comment_search TEXT DEFAULT NULL,
  p_comment_search_scope TEXT DEFAULT NULL,
  p_comment_user_id UUID DEFAULT NULL,
  p_include_unpublished BOOLEAN DEFAULT false
)
RETURNS TABLE(prev_object_id TEXT, next_object_id TEXT, current_index BIGINT, total_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
  v_sort_is_text BOOLEAN;
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
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'asc'; END IF;
  IF NOT (p_sort_column IN (
    'object_id', 'field', 'ra', 'dec', 'redshift', 'redshift_quality',
    'n_targets', 'n_spectra', 'max_snr', 'max_exposure_time'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'object_id';
  END IF;
  IF v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN
    p_sort_column := 'distance';
    p_sort_direction := 'asc';
  END IF;
  v_sort_is_text := p_sort_column IN ('object_id', 'field');

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
  WITH filtered_objects AS MATERIALIZED (
    SELECT
      o.object_id,
      CASE WHEN v_coord_search_active THEN
        2 * DEGREES(ASIN(SQRT(
          POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
          COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
          POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
        )))
      ELSE NULL END AS distance,
      o.field, o.ra, o.dec, o.redshift, o.redshift_quality,
      o.n_targets, o.n_spectra, o.max_snr, o.max_exposure_time
    FROM objects o
    WHERE
      o.programs && v_filtered_program_slugs
      AND o.is_active = true
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (
        NOT v_grating_filter_active
        OR (v_gratings_mode = 'any' AND o.gratings && p_gratings)
        OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings)
        OR (v_gratings_mode = 'none' AND NOT o.gratings && p_gratings)
      )
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observations && p_observations)
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
      AND (p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND o.redshift_quality = 0))
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
  distance_filtered AS MATERIALIZED (
    SELECT
      fo.*,
      CASE p_sort_column
        WHEN 'object_id' THEN fo.object_id WHEN 'field' THEN fo.field ELSE NULL
      END AS sort_text,
      CASE p_sort_column
        WHEN 'ra' THEN fo.ra WHEN 'dec' THEN fo.dec
        WHEN 'redshift' THEN fo.redshift
        WHEN 'redshift_quality' THEN fo.redshift_quality::DOUBLE PRECISION
        WHEN 'n_targets' THEN fo.n_targets::DOUBLE PRECISION
        WHEN 'n_spectra' THEN fo.n_spectra::DOUBLE PRECISION
        WHEN 'max_snr' THEN fo.max_snr WHEN 'max_exposure_time' THEN fo.max_exposure_time
        WHEN 'distance' THEN fo.distance ELSE NULL
      END AS sort_num
    FROM filtered_objects fo
    WHERE NOT v_coord_search_active OR fo.distance <= p_radius_degrees
  ),
  current_obj AS (
    SELECT df.sort_text, df.sort_num, df.object_id FROM distance_filtered df WHERE df.object_id = p_current_object_id
  )
  SELECT
    (SELECT df.object_id FROM distance_filtered df, current_obj c
     WHERE CASE WHEN v_sort_is_text THEN
       (CASE WHEN p_sort_direction = 'asc' THEN df.sort_text < c.sort_text ELSE df.sort_text > c.sort_text END)
       OR (df.sort_text IS NOT DISTINCT FROM c.sort_text AND df.object_id < c.object_id)
       OR (df.sort_text IS NOT NULL AND c.sort_text IS NULL)
     ELSE
       (CASE WHEN p_sort_direction = 'asc' THEN df.sort_num < c.sort_num ELSE df.sort_num > c.sort_num END)
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
    (SELECT df.object_id FROM distance_filtered df, current_obj c
     WHERE CASE WHEN v_sort_is_text THEN
       (CASE WHEN p_sort_direction = 'asc' THEN df.sort_text > c.sort_text ELSE df.sort_text < c.sort_text END)
       OR (df.sort_text IS NOT DISTINCT FROM c.sort_text AND df.object_id > c.object_id)
       OR (c.sort_text IS NOT NULL AND df.sort_text IS NULL)
     ELSE
       (CASE WHEN p_sort_direction = 'asc' THEN df.sort_num > c.sort_num ELSE df.sort_num < c.sort_num END)
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
    CASE WHEN EXISTS (SELECT 1 FROM current_obj) THEN (
      SELECT COUNT(*) + 1
      FROM distance_filtered df, current_obj c
      WHERE CASE WHEN v_sort_is_text THEN
        (CASE WHEN p_sort_direction = 'asc' THEN df.sort_text < c.sort_text ELSE df.sort_text > c.sort_text END)
        OR (df.sort_text IS NOT DISTINCT FROM c.sort_text AND df.object_id < c.object_id)
        OR (df.sort_text IS NOT NULL AND c.sort_text IS NULL)
      ELSE
        (CASE WHEN p_sort_direction = 'asc' THEN df.sort_num < c.sort_num ELSE df.sort_num > c.sort_num END)
        OR (df.sort_num IS NOT DISTINCT FROM c.sort_num AND df.object_id < c.object_id)
        OR (df.sort_num IS NOT NULL AND c.sort_num IS NULL)
      END
    )::BIGINT ELSE 0::BIGINT END AS current_index,
    (SELECT COUNT(*) FROM distance_filtered)::BIGINT AS total_count;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_adjacent_objects TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_adjacent_objects TO service_role;



-- =============================================================================
-- get_csv_export_spectra
-- =============================================================================

-- Issue #412: keyset pagination. PostgREST applies .range() LIMIT/OFFSET
-- OUTSIDE a set-returning function, so the old offset-paged export fully
-- materialized and sorted the WHOLE filtered result set on every page —
-- pages × O(N log N) — and blew through the authenticated role's 8s
-- statement_timeout at ~16k rows. The caller now passes p_after_id (spectra.id
-- PK cursor; spectrum_id is not uniquely constrained) and p_page_size; the
-- LIMIT lives inside the query, each page is one index-bounded scan in id
-- order, and total work across an export stays O(N). Rows come back in
-- spectra.id order — the web action re-sorts in JS for cosmetic CSV ordering,
-- so p_sort_column/p_sort_direction are gone. RETURNS gains id + observation;
-- signature/RETURNS changes require DROP first.
DROP FUNCTION IF EXISTS public.get_csv_export_spectra(
  TEXT[], TEXT[], TEXT[], TEXT[], TEXT, TEXT[], INTEGER[],
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION,
  DOUBLE PRECISION, DOUBLE PRECISION,
  INTEGER, INTEGER, INTEGER,
  INTEGER[], TEXT, BOOLEAN, BOOLEAN, BOOLEAN, TEXT, TEXT, UUID,
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, TEXT, TEXT, BOOLEAN
);

DROP FUNCTION IF EXISTS public.get_csv_export_spectra;

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


-- =============================================================================
-- get_csv_export_objects
-- (one row per sky-object for CSV download in objects view mode)
-- =============================================================================

-- Issue #412: keyset pagination — same rationale as get_csv_export_spectra
-- above. Cursor is objects.object_id (UNIQUE, objects_object_id_key); the
-- LIMIT lives inside the query so each page is one index-bounded scan and an
-- export's total work stays O(N). Sort params removed (the web action
-- re-sorts in JS). Signature change requires DROP first.
DROP FUNCTION IF EXISTS public.get_csv_export_objects(
  TEXT[], TEXT[], TEXT[], TEXT[], TEXT, INTEGER[],
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION,
  DOUBLE PRECISION, DOUBLE PRECISION,
  TEXT, BOOLEAN, BOOLEAN, INTEGER[],
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION,
  BOOLEAN, DOUBLE PRECISION, DOUBLE PRECISION, TEXT, TEXT, UUID, TEXT, TEXT, BOOLEAN
);

DROP FUNCTION IF EXISTS public.get_csv_export_objects;

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


-- =============================================================================
-- get_programs_overview (reads from mv_programs_overview)
-- =============================================================================

DROP FUNCTION IF EXISTS public.get_programs_overview();

CREATE OR REPLACE FUNCTION public.get_programs_overview()
RETURNS TABLE(
  slug text, program_name text, pi_name text, description text,
  is_public boolean, cycle integer, target_count bigint,
  gratings text[], fields text[], observations text[], jwst_pids integer[],
  n_observations bigint, last_reduced_at timestamptz
) LANGUAGE sql STABLE AS $$
  SELECT mv.slug, mv.program_name, mv.pi_name, mv.description, mv.is_public, mv.cycle,
    mv.target_count, mv.gratings, mv.fields, mv.observations, mv.jwst_pids,
    mv.n_observations, mv.last_reduced_at
  FROM public.mv_programs_overview mv ORDER BY mv.program_name;
$$;

GRANT EXECUTE ON FUNCTION public.get_programs_overview TO authenticated;


-- =============================================================================
-- refresh_programs_overview
-- =============================================================================

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


-- =============================================================================
-- get_observation_stats
-- =============================================================================

-- Aggregate stats first, then LEFT JOIN observations once for the JSONB
-- payload. Keeps the GROUP BY key as cheap text/uuid columns so adding more
-- per-observation metadata (additional JSONB or array columns) doesn't drag
-- through the targets x spectra cross product. Provenance fields come from
-- the most recent FULL deployment (source_ids_filter IS NULL); patch deployments
-- contribute only to n_patches_since_full so per-source re-reductions don't
-- masquerade as observation-level reductions.
DROP FUNCTION IF EXISTS public.get_observation_stats(text[]);

CREATE OR REPLACE FUNCTION public.get_observation_stats(p_program_slugs text[], p_include_unpublished boolean DEFAULT false)
RETURNS TABLE(
  observation text, program_slug text, program_name text, field text,
  target_count bigint, spectrum_count bigint, total_size_bytes bigint,
  pointings jsonb,
  crds_context text, cfpipe_version text, jwst_version text,
  reduced_at timestamptz, deployed_at timestamptz,
  deployed_by_username text, deployed_by_full_name text,
  n_patches_since_full integer, last_patch_at timestamptz
) LANGUAGE sql STABLE AS $$
  WITH stats AS (
    SELECT t.observation, t.program_slug, p.program_name, t.field,
      COUNT(DISTINCT t.target_id) AS target_count,
      COUNT(s.id) AS spectrum_count,
      COALESCE(SUM(s.file_size), 0)::bigint AS total_size_bytes
    FROM targets t
    JOIN programs p ON p.slug = t.program_slug
    LEFT JOIN spectra s ON s.target_id = t.target_id
      -- B1: unpublished spectra don't contribute to counts/size (targets still appear).
      AND (p_include_unpublished OR s.deploy_status = 'published')
    WHERE t.program_slug = ANY(p_program_slugs)
    GROUP BY t.observation, t.program_slug, p.program_name, t.field
  )
  SELECT s.observation, s.program_slug, s.program_name, s.field,
    s.target_count, s.spectrum_count, s.total_size_bytes,
    o.pointings,
    full_dep.crds_context,
    full_dep.cfpipe_version, full_dep.jwst_version,
    full_dep.reduced_at, full_dep.deployed_at,
    full_dep.deployed_by_username, full_dep.deployed_by_full_name,
    COALESCE(patches.n_patches, 0)::integer AS n_patches_since_full,
    patches.last_patch_at
  FROM stats s
  LEFT JOIN observations o ON o.name = s.observation
  LEFT JOIN LATERAL (
    SELECT d.crds_context, d.cfpipe_version, d.jwst_version,
           d.reduced_at, d.deployed_at,
           up.username AS deployed_by_username,
           up.full_name AS deployed_by_full_name
    FROM public.deployments d
    LEFT JOIN public.user_profiles up ON up.user_id = d.deployed_by
    WHERE d.observation = s.observation AND d.source_ids_filter IS NULL
    ORDER BY d.deployed_at DESC
    LIMIT 1
  ) full_dep ON true
  LEFT JOIN LATERAL (
    SELECT COUNT(*)::integer AS n_patches, MAX(d.deployed_at) AS last_patch_at
    FROM public.deployments d
    WHERE d.observation = s.observation
      AND d.source_ids_filter IS NOT NULL
      AND (full_dep.deployed_at IS NULL OR d.deployed_at > full_dep.deployed_at)
  ) patches ON true
  ORDER BY s.observation;
$$;

GRANT EXECUTE ON FUNCTION public.get_observation_stats TO authenticated;


-- =============================================================================
-- get_observations_overview
-- =============================================================================
-- Flat list of observations (scoped to the caller's accessible programs) with
-- provenance + patch counts. Powers the /nirspec/metadata page Observations
-- tab. Caller passes the accessible program slug list (public + explicit
-- access), matching the get_observation_stats pattern; filtering happens in
-- SQL so the targets/spectra aggregate doesn't scan inaccessible rows.
--
-- Gratings are derived from the spectra table (the actual deployed data),
-- with observations.gratings as a fallback when no spectra exist yet — the
-- observations.gratings column is populated from observations.toml at deploy
-- time and is empty for observations that haven't gone through that path.
--
-- deployed_by_username / deployed_by_full_name come from user_profiles via
-- the latest full deployment so the metadata page can show who reduced each
-- observation without an extra client-side join.
DROP FUNCTION IF EXISTS public.get_observations_overview();
DROP FUNCTION IF EXISTS public.get_observations_overview(text[]);

CREATE OR REPLACE FUNCTION public.get_observations_overview(p_program_slugs text[], p_include_unpublished boolean DEFAULT false)
RETURNS TABLE(
  observation text, program_slug text, program_name text, field text,
  cycle integer, gratings text[], pointing_count integer, pointings jsonb,
  target_count bigint, spectrum_count bigint, total_size_bytes bigint,
  crds_context text, cfpipe_version text, jwst_version text,
  reduced_at timestamptz, deployed_at timestamptz,
  deployed_by_username text, deployed_by_full_name text,
  n_patches_since_full integer, last_patch_at timestamptz
) LANGUAGE sql STABLE AS $$
  WITH stats AS (
    SELECT t.observation, t.program_slug,
      COUNT(DISTINCT t.target_id) AS target_count,
      COUNT(s.id) AS spectrum_count,
      COALESCE(SUM(s.file_size), 0)::bigint AS total_size_bytes,
      ARRAY_AGG(DISTINCT s.grating ORDER BY s.grating)
        FILTER (WHERE s.grating IS NOT NULL) AS gratings
    FROM public.targets t
    LEFT JOIN public.spectra s ON s.target_id = t.target_id
      -- B1: unpublished spectra don't contribute to counts/size/gratings.
      AND (p_include_unpublished OR s.deploy_status = 'published')
    WHERE t.program_slug = ANY(p_program_slugs)
    GROUP BY t.observation, t.program_slug
  )
  SELECT
    o.name AS observation,
    o.program_slug,
    p.program_name,
    o.field,
    p.cycle,
    CASE
      WHEN COALESCE(array_length(s.gratings, 1), 0) > 0 THEN s.gratings
      ELSE COALESCE(o.gratings, ARRAY[]::text[])
    END AS gratings,
    COALESCE(jsonb_array_length(o.pointings), 0) AS pointing_count,
    o.pointings,
    COALESCE(s.target_count, 0)::bigint AS target_count,
    COALESCE(s.spectrum_count, 0)::bigint AS spectrum_count,
    COALESCE(s.total_size_bytes, 0)::bigint AS total_size_bytes,
    full_dep.crds_context,
    full_dep.cfpipe_version, full_dep.jwst_version,
    full_dep.reduced_at, full_dep.deployed_at,
    full_dep.deployed_by_username, full_dep.deployed_by_full_name,
    COALESCE(patches.n_patches, 0)::integer AS n_patches_since_full,
    patches.last_patch_at
  FROM public.observations o
  JOIN public.programs p ON p.slug = o.program_slug
  LEFT JOIN stats s ON s.observation = o.name AND s.program_slug = o.program_slug
  LEFT JOIN LATERAL (
    SELECT d.crds_context, d.cfpipe_version, d.jwst_version,
           d.reduced_at, d.deployed_at,
           up.username AS deployed_by_username,
           up.full_name AS deployed_by_full_name
    FROM public.deployments d
    LEFT JOIN public.user_profiles up ON up.user_id = d.deployed_by
    WHERE d.observation = o.name AND d.source_ids_filter IS NULL
    ORDER BY d.deployed_at DESC
    LIMIT 1
  ) full_dep ON true
  LEFT JOIN LATERAL (
    SELECT COUNT(*)::integer AS n_patches, MAX(d.deployed_at) AS last_patch_at
    FROM public.deployments d
    WHERE d.observation = o.name
      AND d.source_ids_filter IS NOT NULL
      AND (full_dep.deployed_at IS NULL OR d.deployed_at > full_dep.deployed_at)
  ) patches ON true
  WHERE o.program_slug = ANY(p_program_slugs)
  ORDER BY o.program_slug, o.name;
$$;

GRANT EXECUTE ON FUNCTION public.get_observations_overview TO authenticated;


-- =============================================================================
-- get_database_overview
-- =============================================================================
-- Single-row scope summary for the metadata page header.
CREATE OR REPLACE FUNCTION public.get_database_overview(p_include_unpublished boolean DEFAULT false)
RETURNS TABLE(
  n_programs bigint, n_observations bigint, n_pointings bigint,
  n_targets bigint, n_spectra bigint, total_size_bytes bigint,
  latest_deployed_at timestamptz, latest_cfpipe_version text
) LANGUAGE sql STABLE AS $$
  WITH latest AS (
    SELECT d.deployed_at, d.cfpipe_version
    FROM public.deployments d
    WHERE d.source_ids_filter IS NULL
    ORDER BY d.deployed_at DESC
    LIMIT 1
  )
  SELECT
    (SELECT COUNT(*)::bigint FROM public.programs) AS n_programs,
    (SELECT COUNT(*)::bigint FROM public.observations) AS n_observations,
    (SELECT COALESCE(SUM(jsonb_array_length(pointings)), 0)::bigint
       FROM public.observations
       WHERE pointings IS NOT NULL) AS n_pointings,
    (SELECT COUNT(*)::bigint FROM public.targets) AS n_targets,
    -- B1: gate spectra count/size on publish status (no alias param available).
    (SELECT COUNT(*)::bigint FROM public.spectra
       WHERE p_include_unpublished OR deploy_status = 'published') AS n_spectra,
    (SELECT COALESCE(SUM(file_size), 0)::bigint FROM public.spectra
       WHERE p_include_unpublished OR deploy_status = 'published') AS total_size_bytes,
    (SELECT deployed_at FROM latest) AS latest_deployed_at,
    (SELECT cfpipe_version FROM latest) AS latest_cfpipe_version;
$$;

GRANT EXECUTE ON FUNCTION public.get_database_overview TO authenticated;


-- =============================================================================
-- get_observation_manifest
-- =============================================================================

DROP FUNCTION IF EXISTS public.get_observation_manifest(TEXT, TEXT[]);

CREATE OR REPLACE FUNCTION public.get_observation_manifest(p_obs_name text, p_program_slugs text[], p_include_unpublished boolean DEFAULT false)
RETURNS TABLE(
  spectra_id integer, spectrum_id text, target_id text, grating text, fits_path text,
  file_hash text, file_size bigint, signal_to_noise double precision, cfpipe_version text
) LANGUAGE plpgsql STABLE AS $$
BEGIN
  RETURN QUERY
  SELECT s.id, s.spectrum_id, s.target_id, s.grating, s.fits_path, s.file_hash, s.file_size,
         s.signal_to_noise, s.cfpipe_version
  FROM spectra s
  JOIN targets t ON t.target_id = s.target_id
  WHERE t.observation = p_obs_name AND t.program_slug = ANY(p_program_slugs)
    -- B1: fail-closed; admin sync passes p_include_unpublished => true.
    AND (p_include_unpublished OR s.deploy_status = 'published')
  ORDER BY s.spectrum_id;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_observation_manifest TO authenticated;


-- =============================================================================
-- get_storage_objects_for_sync (epic #210)
-- =============================================================================
-- Catalog-sync endpoint for the Python client's local `storage_objects` mirror,
-- which is the single download/availability layer (finals + intermediates +
-- future NIRCam share one engine). Mirrors get_spectra_for_sync: returns a JSONB
-- page + counts in one row, paginated, incremental via p_updated_since.
--
-- Scope (matches the storage_objects RLS SELECT policy, restated here because the
-- route calls this under service_role with RLS bypassed):
--   * status = 'active' (current rows only — superseded/revoked are GC state).
--   * Admins (p_include_unpublished, set only when isAdminUser) get every active
--     row — a faithful full mirror, including drafts and field-only products.
--   * Non-admins get rows whose observation is in an accessible program AND that
--     are published: spectrum-family rows follow their spectrum's deploy_status;
--     exposure/object-level rows follow their deployment's status. Drafts/revoked
--     and out-of-program rows are excluded. Field-only products (NULL observation,
--     e.g. NIRCam) are admin-only here until NIRCam client download lands.
DROP FUNCTION IF EXISTS public.get_storage_objects_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN);

CREATE OR REPLACE FUNCTION public.get_storage_objects_for_sync(
  p_program_slugs TEXT[],
  p_updated_since TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_offset INTEGER DEFAULT 0,
  p_include_counts BOOLEAN DEFAULT TRUE,
  p_include_unpublished BOOLEAN DEFAULT FALSE,
  -- Keyset cursor (#103): the id of the last row of the previous page, seeked
  -- via the storage_objects_pkey btree. storage_key is NOT usable as a cursor
  -- (only UNIQUE as (backend, bucket, storage_key)), and sync order is
  -- irrelevant to the client (it upserts by key), so this orders by the PK.
  -- p_offset is kept for old clients.
  p_after_id BIGINT DEFAULT NULL
)
RETURNS TABLE(objects JSONB, total_count BIGINT, total_accessible_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
-- Retained only for legacy OFFSET clients; keyset clients (p_after_id) seek the
-- PK index and never reach it. Unlike the other /sync RPCs, the scope predicate
-- (published EXISTS checks) is itself an O(N) floor for OFFSET clients — keyset
-- lets a page seek straight to id > cursor and evaluate scope on only ~p_limit
-- rows. Drop this SET once offset clients are gone (#103).
SET statement_timeout = '120s'
AS $$
BEGIN
  RETURN QUERY
  -- `scoped` carries the full published-scope filter and feeds the count CTEs
  -- only (gated on p_include_counts, so it is skipped entirely on keyset pages
  -- 2+). `matched` re-states the SAME scope inline against the base table so its
  -- keyset seek uses the PK index instead of reading a materialized full scan.
  -- The two copies must stay in sync (same house pattern as get_objects_for_sync's
  -- matched vs. total/accessible).
  WITH scoped AS (
    SELECT so.updated_at
    FROM storage_objects so
    WHERE so.status = 'active'
      AND (
        p_include_unpublished
        OR (so.spectrum_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM spectra s
              JOIN targets t ON t.target_id = s.target_id
              WHERE s.spectrum_id = so.spectrum_id
                AND s.deploy_status = 'published'
                AND t.program_slug = ANY(p_program_slugs)))
        OR (so.spectrum_id IS NULL AND so.deployment_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM deployments d
              LEFT JOIN observations o ON o.name = d.observation
              WHERE d.id = so.deployment_id
                AND d.status = 'published'
                -- NIRCam field deploy (epic #261, N1): multi-program, public to all
                -- when published. NIRSpec obs deploy stays program-scoped.
                AND (d.field IS NOT NULL OR o.program_slug = ANY(p_program_slugs))))
      )
  ),
  matched AS MATERIALIZED (
    SELECT so.*
    FROM storage_objects so
    WHERE so.status = 'active'
      AND (
        p_include_unpublished
        OR (so.spectrum_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM spectra s
              JOIN targets t ON t.target_id = s.target_id
              WHERE s.spectrum_id = so.spectrum_id
                AND s.deploy_status = 'published'
                AND t.program_slug = ANY(p_program_slugs)))
        OR (so.spectrum_id IS NULL AND so.deployment_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM deployments d
              LEFT JOIN observations o ON o.name = d.observation
              WHERE d.id = so.deployment_id
                AND d.status = 'published'
                AND (d.field IS NOT NULL OR o.program_slug = ANY(p_program_slugs))))
      )
      AND (p_updated_since IS NULL OR so.updated_at > p_updated_since)
      -- Keyset (#103): id is the PK, so a strict > needs no tiebreaker.
      AND (p_after_id IS NULL OR so.id > p_after_id)
    ORDER BY so.id
    LIMIT p_limit OFFSET p_offset
  ),
  total AS (
    SELECT COUNT(*) AS cnt FROM scoped
    WHERE p_include_counts
      AND (p_updated_since IS NULL OR scoped.updated_at > p_updated_since)
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt FROM scoped
    WHERE p_include_counts
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'backend', m.backend,
        'bucket', m.bucket,
        'storage_key', m.storage_key,
        'content_hash', m.content_hash,
        'sci_dq_hash', m.sci_dq_hash,
        'size_bytes', m.size_bytes,
        'content_type', m.content_type,
        'product_type', m.product_type,
        'instrument', m.instrument,
        'status', m.status,
        'observation', m.observation,
        'field', m.field,
        'filter', m.filter,
        'spectrum_id', m.spectrum_id,
        'exposure_ref', m.exposure_ref,
        'deployment_id', m.deployment_id,
        'cfpipe_version', m.cfpipe_version,
        'created_at', m.created_at,
        'updated_at', m.updated_at
      )
      -- Keyset (#103): page array must be id-ordered (client cursors on the
      -- last element).
      ORDER BY m.id
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_storage_objects_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN, BIGINT) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_storage_objects_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN, BIGINT) TO service_role;


-- =============================================================================
-- filter_accessible_storage_keys (epic #210)
-- =============================================================================
-- Per-key authorization for the /api/v1/storage/presign endpoint. Returns the
-- subset of p_keys the caller may download, applying the same scope as
-- get_storage_objects_for_sync (active + admin-all OR program-accessible &
-- published). The route presigns only the returned keys; anything omitted is
-- silently denied. Called under service_role (RLS bypassed), so scope is in SQL.
CREATE OR REPLACE FUNCTION public.filter_accessible_storage_keys(
  p_keys TEXT[],
  p_program_slugs TEXT[],
  p_include_unpublished BOOLEAN DEFAULT FALSE
)
RETURNS TABLE(storage_key TEXT)
LANGUAGE sql STABLE
AS $$
  SELECT so.storage_key
  FROM storage_objects so
  WHERE so.storage_key = ANY(p_keys)
    AND so.status = 'active'
    AND (
      p_include_unpublished
      OR (so.spectrum_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM spectra s
            JOIN targets t ON t.target_id = s.target_id
            WHERE s.spectrum_id = so.spectrum_id
              AND s.deploy_status = 'published'
              AND t.program_slug = ANY(p_program_slugs)))
      OR (so.spectrum_id IS NULL AND so.deployment_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM deployments d
            LEFT JOIN observations o ON o.name = d.observation
            WHERE d.id = so.deployment_id
              AND d.status = 'published'
              -- NIRCam field deploy (epic #261, N1): multi-program, public to all
              -- when published. NIRSpec obs deploy stays program-scoped.
              AND (d.field IS NOT NULL OR o.program_slug = ANY(p_program_slugs))))
    );
$$;

GRANT EXECUTE ON FUNCTION public.filter_accessible_storage_keys(TEXT[], TEXT[], BOOLEAN) TO authenticated;
GRANT EXECUTE ON FUNCTION public.filter_accessible_storage_keys(TEXT[], TEXT[], BOOLEAN) TO service_role;


-- =============================================================================
-- get_targets_in_viewport
-- =============================================================================

CREATE OR REPLACE FUNCTION public.get_targets_in_viewport(
  p_ra_min double precision, p_ra_max double precision,
  p_dec_min double precision, p_dec_max double precision,
  p_field text DEFAULT NULL, p_limit integer DEFAULT 5000,
  p_include_unpublished boolean DEFAULT false
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
    -- B1: hide targets with no published spectrum (fail-closed).
    AND (p_include_unpublished OR t.has_published_spectrum)
  ORDER BY t.ra LIMIT p_limit;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_targets_in_viewport TO authenticated;


-- =============================================================================
-- get_nearby_shutters
-- =============================================================================

CREATE OR REPLACE FUNCTION public.get_nearby_shutters(
  p_ra double precision,
  p_dec double precision,
  p_radius_arcsec double precision DEFAULT 5.0,
  p_field text DEFAULT NULL
)
RETURNS TABLE (
  object_id text,
  source_id integer,
  center_ra double precision,
  center_dec double precision,
  position_angle double precision,
  shutter_idx smallint,
  dither_id smallint,
  shutter_state text,
  observation text,
  aperture_name text,
  aperture_width_arcsec double precision,
  aperture_height_arcsec double precision
)
LANGUAGE sql STABLE AS $$
  SELECT s.object_id, s.source_id, s.center_ra, s.center_dec,
         s.position_angle, s.shutter_idx, s.dither_id, s.shutter_state, s.observation,
         s.aperture_name, s.aperture_width_arcsec, s.aperture_height_arcsec
  FROM shutters s
  WHERE (p_field IS NULL OR s.field = p_field)
    AND s.center_ra BETWEEN p_ra - p_radius_arcsec / 3600.0 / COS(RADIANS(p_dec))
                        AND p_ra + p_radius_arcsec / 3600.0 / COS(RADIANS(p_dec))
    AND s.center_dec BETWEEN p_dec - p_radius_arcsec / 3600.0
                         AND p_dec + p_radius_arcsec / 3600.0;
$$;


-- =============================================================================
-- get_field_object_markers
-- =============================================================================
-- Single-shot fetch of every object in a field for the map viewer. Replaces
-- the paginated PostgREST select that capped at 1000 rows/page and embedded
-- targets(target_id) for the slit-filter bridge — both very expensive on
-- COSMOS-sized fields. RLS on objects still applies (SECURITY INVOKER).

CREATE OR REPLACE FUNCTION public.get_field_object_markers(p_field TEXT, p_include_unpublished BOOLEAN DEFAULT false)
RETURNS TABLE (
  object_id           TEXT,
  ra                  DOUBLE PRECISION,
  "dec"               DOUBLE PRECISION,
  redshift            DOUBLE PRECISION,
  redshift_quality    INTEGER,
  field               TEXT,
  n_targets           INTEGER,
  n_spectra           INTEGER,
  programs            TEXT[],
  member_target_ids   TEXT[]
)
LANGUAGE sql STABLE
AS $$
  -- programs, n_targets, n_spectra and member_target_ids are scoped to the
  -- caller's accessible programs so mixed-program objects don't leak proprietary
  -- member metadata on the map. redshift / redshift_quality stay visible (object
  -- science, per the access policy). Object row visibility is enforced by RLS
  -- (programs && accessible); this function is SECURITY INVOKER and additionally
  -- gates the member CTEs by an explicit accessible_program_slugs() filter.
  -- Set-based (not the per-row object_scoped_aggregates helper) because a field
  -- can return up to ~5000 objects; the logic mirrors that helper.
  WITH acc AS (
    SELECT public.accessible_program_slugs() AS slugs
  ),
  mt AS (
    SELECT t.object_id,
           array_agg(t.target_id ORDER BY t.target_id)              AS member_target_ids,
           array_agg(DISTINCT t.program_slug ORDER BY t.program_slug) AS programs,
           COUNT(*)::int                                            AS n_targets
    FROM public.targets t
    CROSS JOIN acc
    WHERE t.field = p_field
      AND t.program_slug = ANY(acc.slugs)
      -- B1: mirror object_scoped_aggregates -- only count targets that
      -- contribute a published spectrum so draft-only members vanish (the
      -- final JOIN mt is inner, so objects with zero published members drop out).
      AND (p_include_unpublished OR t.has_published_spectrum)
    GROUP BY t.object_id
  ),
  sp AS (
    SELECT t.object_id, COUNT(*)::int AS n_spectra
    FROM public.spectra s
    JOIN public.targets t ON t.target_id = s.target_id
    CROSS JOIN acc
    WHERE t.field = p_field
      AND t.program_slug = ANY(acc.slugs)
      AND (p_include_unpublished OR s.deploy_status = 'published')
    GROUP BY t.object_id
  )
  SELECT
    o.object_id,
    o.ra,
    o.dec,
    o.redshift::double precision,
    o.redshift_quality,
    o.field,
    COALESCE(mt.n_targets, 0)                      AS n_targets,
    COALESCE(sp.n_spectra, 0)                      AS n_spectra,
    COALESCE(mt.programs, ARRAY[]::TEXT[])         AS programs,
    COALESCE(mt.member_target_ids, ARRAY[]::TEXT[]) AS member_target_ids
  FROM public.objects o
  JOIN mt ON mt.object_id = o.id
  LEFT JOIN sp ON sp.object_id = o.id
  WHERE o.field = p_field
    AND o.is_active
  ORDER BY o.object_id;
$$;

GRANT EXECUTE ON FUNCTION public.get_field_object_markers TO authenticated;


-- =============================================================================
-- get_field_shutters
-- =============================================================================
-- Single-shot fetch of every shutter in a field for the map viewer. Shutters
-- are public to authenticated users, so SECURITY INVOKER is fine.

CREATE OR REPLACE FUNCTION public.get_field_shutters(p_field TEXT)
RETURNS TABLE (
  object_id        TEXT,
  source_id        INTEGER,
  center_ra        DOUBLE PRECISION,
  center_dec       DOUBLE PRECISION,
  position_angle   DOUBLE PRECISION,
  shutter_idx      SMALLINT,
  dither_id        SMALLINT,
  shutter_state    TEXT,
  observation      TEXT,
  aperture_name           TEXT,
  aperture_width_arcsec    DOUBLE PRECISION,
  aperture_height_arcsec   DOUBLE PRECISION
)
LANGUAGE sql STABLE
AS $$
  SELECT s.object_id, s.source_id, s.center_ra, s.center_dec,
         s.position_angle, s.shutter_idx, s.dither_id, s.shutter_state, s.observation,
         s.aperture_name, s.aperture_width_arcsec, s.aperture_height_arcsec
  FROM public.shutters s
  WHERE s.field = p_field
  ORDER BY s.object_id;
$$;

GRANT EXECUTE ON FUNCTION public.get_field_shutters TO authenticated;


-- =============================================================================
-- increment_tile_version
-- =============================================================================

CREATE OR REPLACE FUNCTION public.increment_tile_version(
    p_field text,
    p_filter text
)
RETURNS void
LANGUAGE sql
AS $$
    UPDATE public.map_layers
    SET tile_version = tile_version + 1
    WHERE field = p_field AND filter = p_filter;
$$;

GRANT EXECUTE ON FUNCTION public.increment_tile_version TO service_role;



-- =============================================================================
-- get_program_stats
-- =============================================================================

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

GRANT ALL ON FUNCTION public.get_program_stats TO anon;
GRANT ALL ON FUNCTION public.get_program_stats TO authenticated;
GRANT ALL ON FUNCTION public.get_program_stats TO service_role;


-- =============================================================================
-- get_user_profile_stats
-- =============================================================================

CREATE OR REPLACE FUNCTION public.get_user_profile_stats(p_user_id uuid)
RETURNS json
LANGUAGE plpgsql STABLE SECURITY DEFINER
AS $$
DECLARE
  result JSON;
  objects_inspected BIGINT;
  comments_posted BIGINT;
  last_comment_at TIMESTAMPTZ;
  last_inspection_at TIMESTAMPTZ;
  last_activity TIMESTAMPTZ;
BEGIN
  -- Phase D: count distinct *objects* (not targets) — inspection state lives
  -- on objects now. Union of post-D rows (object_id NOT NULL) and pre-D rows
  -- mapped via targets.object_id so historical activity counts stay intact.
  SELECT COUNT(DISTINCT obj_id) INTO objects_inspected FROM (
    SELECT object_id AS obj_id
    FROM flag_audit_log
    WHERE user_id = p_user_id AND object_id IS NOT NULL
    UNION
    SELECT t.object_id AS obj_id
    FROM flag_audit_log fal
    JOIN targets t ON t.id = fal.target_id
    WHERE fal.user_id = p_user_id
      AND fal.target_id IS NOT NULL
      AND t.object_id IS NOT NULL
  ) sub;

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
    -- Key remains 'targets_inspected' for back-compat with the API contract;
    -- semantically the value now means objects-inspected.
    'targets_inspected', COALESCE(objects_inspected, 0),
    'comments_posted', COALESCE(comments_posted, 0),
    'last_activity', last_activity
  );

  RETURN result;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_user_profile_stats TO authenticated;


-- =============================================================================
-- get_download_stats
-- =============================================================================

CREATE OR REPLACE FUNCTION public.get_download_stats(p_days integer DEFAULT 30)
RETURNS json
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
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

GRANT EXECUTE ON FUNCTION public.get_download_stats TO authenticated;


-- =============================================================================
-- get_activity_feed / get_activity_users (admin activity feed)
-- =============================================================================
-- Server-side replacement for the /api/admin/activity route's fetch-everything-
-- then-sort-in-JS approach: UNION the two activity sources (comments +
-- flag_audit_log), filter/sort/paginate in one scan, and return the page total
-- via a window count. Semantics mirror the route exactly:
--   * comments are joined to targets (inner — object-level comments are not
--     surfaced in the feed, matching the previous targets!inner embed);
--   * inspection rows label their subject as target -> object -> spectrum
--     (spectra label = target_id/grating), degrading to '' if all FKs are NULL;
--   * the user filter matches rows whose user_id is in p_user_ids, plus (for
--     inspections) NULL-user system rows when p_include_system. No user filter
--     at all (empty p_user_ids, p_include_system=false) means everything.
-- SECURITY DEFINER because flag_audit_log's RLS is access-scoped, not
-- admin-scoped; the admin gate here is the authorization boundary.

CREATE OR REPLACE FUNCTION public.get_activity_feed(
  p_include_comments boolean DEFAULT true,
  p_include_inspections boolean DEFAULT true,
  p_user_ids uuid[] DEFAULT NULL,
  p_include_system boolean DEFAULT false,
  p_field_names text[] DEFAULT NULL,
  p_page integer DEFAULT 1,
  p_page_size integer DEFAULT 50
)
RETURNS TABLE (
  id text,
  type text,
  target_db_id integer,
  target_display_id text,
  user_id uuid,
  ts timestamp without time zone,
  content text,
  edited_at timestamp without time zone,
  field_name text,
  old_value integer,
  new_value integer,
  user_full_name text,
  user_is_group_account boolean,
  total_count bigint
)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_has_user_filter boolean :=
    (p_user_ids IS NOT NULL AND array_length(p_user_ids, 1) > 0) OR p_include_system;
  v_limit integer := LEAST(GREATEST(COALESCE(p_page_size, 50), 1), 100);
  v_offset integer := GREATEST(COALESCE(p_page, 1) - 1, 0) * LEAST(GREATEST(COALESCE(p_page_size, 50), 1), 100);
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  RETURN QUERY
  WITH feed AS (
    SELECT
      'comment-' || c.id AS id,
      'comment'::text AS type,
      c.target_id AS target_db_id,
      t.target_id AS target_display_id,
      c.user_id,
      c.created_at AS ts,
      c.content,
      c.edited_at,
      NULL::text AS field_name,
      NULL::integer AS old_value,
      NULL::integer AS new_value
    FROM comments c
    JOIN targets t ON t.id = c.target_id
    WHERE p_include_comments
      AND NOT c.is_deleted
      AND (NOT v_has_user_filter
           OR c.user_id = ANY(COALESCE(p_user_ids, '{}'::uuid[])))

    UNION ALL

    SELECT
      'audit-' || f.id,
      'inspection'::text,
      COALESCE(f.target_id, f.object_id, f.spectrum_id, 0),
      COALESCE(
        t.target_id,
        o.object_id,
        CASE WHEN s.id IS NOT NULL THEN s.target_id || '/' || s.grating END,
        ''
      ),
      f.user_id,
      f.changed_at,
      NULL::text,
      NULL::timestamp without time zone,
      f.field_name,
      f.old_value,
      f.new_value
    FROM flag_audit_log f
    LEFT JOIN targets t ON t.id = f.target_id
    LEFT JOIN objects o ON o.id = f.object_id
    LEFT JOIN spectra s ON s.id = f.spectrum_id
    WHERE p_include_inspections
      AND (p_field_names IS NULL OR f.field_name = ANY(p_field_names))
      AND (NOT v_has_user_filter
           OR f.user_id = ANY(COALESCE(p_user_ids, '{}'::uuid[]))
           OR (p_include_system AND f.user_id IS NULL))
  )
  SELECT
    feed.id,
    feed.type,
    feed.target_db_id,
    feed.target_display_id,
    feed.user_id,
    feed.ts,
    feed.content,
    feed.edited_at,
    feed.field_name,
    feed.old_value,
    feed.new_value,
    up.full_name,
    COALESCE(up.is_group_account, false),
    count(*) OVER ()
  FROM feed
  LEFT JOIN user_profiles up ON up.user_id = feed.user_id
  ORDER BY feed.ts DESC, feed.id DESC
  OFFSET v_offset
  LIMIT v_limit;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_activity_feed TO authenticated;

-- The activity page's user-filter dropdown: distinct users with any activity.
-- A NULL user_id row signals system-generated activity (NULL-user audit rows);
-- the route maps it to its synthetic "System" entry.
CREATE OR REPLACE FUNCTION public.get_activity_users()
RETURNS TABLE (
  user_id uuid,
  full_name text
)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  RETURN QUERY
  WITH active AS (
    SELECT DISTINCT c.user_id FROM comments c WHERE NOT c.is_deleted
    UNION
    SELECT DISTINCT f.user_id FROM flag_audit_log f
  )
  SELECT a.user_id, up.full_name
  FROM active a
  LEFT JOIN user_profiles up ON up.user_id = a.user_id
  ORDER BY (a.user_id IS NULL) DESC, up.full_name ASC NULLS LAST;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_activity_users TO authenticated;


-- =============================================================================
-- Admin list RPCs (admin audit 2026-07-03, Phase 1)
-- =============================================================================
-- One RPC per admin list, replacing PostgREST count:'exact' + hardcoded sort:
-- whitelisted sort column/direction (CASE-based ORDER BY, the
-- get_filtered_objects_paginated house pattern), windowed count(*) OVER()
-- total, and page-size clamping. All are STABLE **invoker-rights** functions
-- (the underlying tables are admin-readable via RLS; no definer needed) with
-- an explicit is_admin() gate for clean errors, SET search_path per the
-- definer-hardening convention.
--
-- get_admin_exposures and get_admin_exposure_neighbors MUST keep identical
-- filter predicates and ORDER BY expressions (including the e.id tiebreak):
-- the neighbors window feeds the detail page's prev/next + prefetch, which
-- must walk the exact order the list shows.

CREATE OR REPLACE FUNCTION public.get_admin_deployments(
  p_status text DEFAULT NULL,
  p_instrument text DEFAULT NULL,       -- 'nirspec' | 'nircam' | NULL
  p_sort_column text DEFAULT 'deployed_at',
  p_sort_direction text DEFAULT 'desc',
  p_page integer DEFAULT 1,
  p_page_size integer DEFAULT 50
)
RETURNS TABLE (
  id integer,
  observation text,
  field text,
  status text,
  n_targets integer,
  n_spectra integer,
  cfpipe_version text,
  deployed_at timestamptz,
  published_at timestamptz,
  revoked_at timestamptz,
  total_count bigint
)
LANGUAGE plpgsql STABLE
SET search_path = public, pg_temp
AS $$
DECLARE
  v_limit integer := LEAST(GREATEST(COALESCE(p_page_size, 50), 1), 200);
  v_offset integer := GREATEST(COALESCE(p_page, 1) - 1, 0) * LEAST(GREATEST(COALESCE(p_page_size, 50), 1), 200);
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'desc'; END IF;
  IF p_sort_column NOT IN ('id', 'deployed_at', 'status', 'scope') THEN
    p_sort_column := 'deployed_at';
  END IF;

  RETURN QUERY
  SELECT d.id, d.observation, d.field, d.status, d.n_targets, d.n_spectra,
         d.cfpipe_version, d.deployed_at, d.published_at, d.revoked_at,
         count(*) OVER ()
  FROM deployments d
  WHERE (p_status IS NULL OR d.status = p_status)
    AND (p_instrument IS NULL
         OR (p_instrument = 'nirspec' AND d.observation IS NOT NULL)
         OR (p_instrument = 'nircam'  AND d.field IS NOT NULL))
  ORDER BY
    CASE WHEN p_sort_column = 'deployed_at' AND p_sort_direction = 'desc' THEN d.deployed_at END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'deployed_at' AND p_sort_direction = 'asc'  THEN d.deployed_at END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'status' AND p_sort_direction = 'desc' THEN d.status END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'status' AND p_sort_direction = 'asc'  THEN d.status END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'scope' AND p_sort_direction = 'desc' THEN COALESCE(d.observation, d.field) END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'scope' AND p_sort_direction = 'asc'  THEN COALESCE(d.observation, d.field) END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'id' AND p_sort_direction = 'asc' THEN d.id END ASC,
    d.id DESC
  OFFSET v_offset LIMIT v_limit;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_admin_deployments TO authenticated;

-- Audit-log browse. Reads the first-class deploy_events.field column (Phase 3;
-- backfilled from the legacy metadata->>'field') and folds the actor display
-- name (full_name, falling back to username) into the row, replacing two extra
-- client round-trips.
CREATE OR REPLACE FUNCTION public.get_admin_deploy_events(
  p_action text DEFAULT NULL,
  p_observation text DEFAULT NULL,
  p_field text DEFAULT NULL,
  p_sort_column text DEFAULT 'occurred_at',
  p_sort_direction text DEFAULT 'desc',
  p_page integer DEFAULT 1,
  p_page_size integer DEFAULT 50
)
RETURNS TABLE (
  id uuid,
  action text,
  observation text,
  field text,
  deployment_id integer,
  status_to text,
  affected_count integer,
  occurred_at timestamptz,
  actor uuid,
  actor_name text,
  total_count bigint
)
LANGUAGE plpgsql STABLE
SET search_path = public, pg_temp
AS $$
DECLARE
  v_limit integer := LEAST(GREATEST(COALESCE(p_page_size, 50), 1), 200);
  v_offset integer := GREATEST(COALESCE(p_page, 1) - 1, 0) * LEAST(GREATEST(COALESCE(p_page_size, 50), 1), 200);
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'desc'; END IF;
  IF p_sort_column NOT IN ('occurred_at', 'action') THEN
    p_sort_column := 'occurred_at';
  END IF;

  RETURN QUERY
  SELECT e.id, e.action, e.observation,
         e.field,
         e.deployment_id, e.status_to, e.affected_count, e.occurred_at,
         e.actor,
         COALESCE(up.full_name, up.username) AS actor_name,
         count(*) OVER ()
  FROM deploy_events e
  LEFT JOIN user_profiles up ON up.user_id = e.actor
  WHERE (p_action IS NULL OR e.action = p_action)
    AND (p_observation IS NULL OR e.observation = p_observation)
    AND (p_field IS NULL OR e.field = p_field)
  ORDER BY
    CASE WHEN p_sort_column = 'occurred_at' AND p_sort_direction = 'desc' THEN e.occurred_at END DESC,
    CASE WHEN p_sort_column = 'occurred_at' AND p_sort_direction = 'asc'  THEN e.occurred_at END ASC,
    CASE WHEN p_sort_column = 'action' AND p_sort_direction = 'desc' THEN e.action END DESC,
    CASE WHEN p_sort_column = 'action' AND p_sort_direction = 'asc'  THEN e.action END ASC,
    e.occurred_at DESC
  OFFSET v_offset LIMIT v_limit;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_admin_deploy_events TO authenticated;

CREATE OR REPLACE FUNCTION public.get_admin_storage_objects(
  p_product_type text DEFAULT NULL,
  p_status text DEFAULT NULL,
  p_field text DEFAULT NULL,
  p_observation text DEFAULT NULL,
  p_backend text DEFAULT NULL,
  p_sort_column text DEFAULT 'created_at',
  p_sort_direction text DEFAULT 'desc',
  p_page integer DEFAULT 1,
  p_page_size integer DEFAULT 50,
  p_search text DEFAULT NULL              -- substring ILIKE on storage_key
)
RETURNS TABLE (
  id bigint,
  storage_key text,
  product_type text,
  instrument text,
  observation text,
  field text,
  exposure_ref text,
  size_bytes bigint,
  content_hash text,
  backend text,
  status text,
  cfpipe_version text,
  created_at timestamptz,
  total_count bigint
)
LANGUAGE plpgsql STABLE
SET search_path = public, pg_temp
AS $$
DECLARE
  v_limit integer := LEAST(GREATEST(COALESCE(p_page_size, 50), 1), 200);
  v_offset integer := GREATEST(COALESCE(p_page, 1) - 1, 0) * LEAST(GREATEST(COALESCE(p_page_size, 50), 1), 200);
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'desc'; END IF;
  IF p_sort_column NOT IN ('created_at', 'size_bytes', 'product_type', 'storage_key',
                           'observation', 'field', 'status') THEN
    p_sort_column := 'created_at';
  END IF;

  RETURN QUERY
  SELECT so.id, so.storage_key, so.product_type, so.instrument, so.observation,
         so.field, so.exposure_ref, so.size_bytes, so.content_hash, so.backend,
         so.status, so.cfpipe_version, so.created_at,
         count(*) OVER ()
  FROM storage_objects so
  WHERE (p_product_type IS NULL OR so.product_type = p_product_type)
    AND (p_status IS NULL OR so.status = p_status)
    AND (p_field IS NULL OR so.field = p_field)
    AND (p_observation IS NULL OR so.observation = p_observation)
    AND (p_backend IS NULL OR so.backend = p_backend)
    AND (p_search IS NULL OR so.storage_key ILIKE '%' || p_search || '%')
  ORDER BY
    CASE WHEN p_sort_column = 'created_at' AND p_sort_direction = 'desc' THEN so.created_at END DESC,
    CASE WHEN p_sort_column = 'created_at' AND p_sort_direction = 'asc'  THEN so.created_at END ASC,
    CASE WHEN p_sort_column = 'size_bytes' AND p_sort_direction = 'desc' THEN so.size_bytes END DESC,
    CASE WHEN p_sort_column = 'size_bytes' AND p_sort_direction = 'asc'  THEN so.size_bytes END ASC,
    CASE WHEN p_sort_column = 'product_type' AND p_sort_direction = 'desc' THEN so.product_type END DESC,
    CASE WHEN p_sort_column = 'product_type' AND p_sort_direction = 'asc'  THEN so.product_type END ASC,
    CASE WHEN p_sort_column = 'storage_key' AND p_sort_direction = 'desc' THEN so.storage_key END DESC,
    CASE WHEN p_sort_column = 'storage_key' AND p_sort_direction = 'asc'  THEN so.storage_key END ASC,
    CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN so.observation END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'asc'  THEN so.observation END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN so.field END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc'  THEN so.field END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'status' AND p_sort_direction = 'desc' THEN so.status END DESC,
    CASE WHEN p_sort_column = 'status' AND p_sort_direction = 'asc'  THEN so.status END ASC,
    so.id DESC
  OFFSET v_offset LIMIT v_limit;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_admin_storage_objects TO authenticated;

CREATE OR REPLACE FUNCTION public.get_admin_exposures(
  p_field text DEFAULT NULL,
  p_filter text DEFAULT NULL,
  p_detector text DEFAULT NULL,
  p_review_status text DEFAULT NULL,
  p_stage text DEFAULT NULL,
  p_correction text DEFAULT NULL,
  p_sort_column text DEFAULT 'filename',   -- 'filename' = the compound (field, filter, filename) list order
  p_sort_direction text DEFAULT 'asc',
  p_page integer DEFAULT 1,
  p_page_size integer DEFAULT 50
)
RETURNS TABLE (
  id integer,
  field text,
  filter text,
  detector text,
  filename text,
  visit text,
  date_obs timestamp without time zone,
  ra_center double precision,
  dec_center double precision,
  stage text,
  review_status text,
  correction text,
  png_path text,
  full_png_path text,
  image_width integer,
  image_height integer,
  mask_regions jsonb,
  notes text,
  created_at timestamp without time zone,
  updated_at timestamp without time zone,
  total_count bigint
)
LANGUAGE plpgsql STABLE
SET search_path = public, pg_temp
AS $$
DECLARE
  v_limit integer := LEAST(GREATEST(COALESCE(p_page_size, 50), 1), 200);
  v_offset integer := GREATEST(COALESCE(p_page, 1) - 1, 0) * LEAST(GREATEST(COALESCE(p_page_size, 50), 1), 200);
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'asc'; END IF;
  IF p_sort_column NOT IN ('filename', 'field', 'filter', 'detector', 'stage',
                           'review_status', 'date_obs', 'updated_at') THEN
    p_sort_column := 'filename';
  END IF;

  RETURN QUERY
  SELECT e.id, e.field, e.filter, e.detector, e.filename, e.visit, e.date_obs,
         e.ra_center, e.dec_center, e.stage, e.review_status,
         e.correction, e.png_path, e.full_png_path, e.image_width,
         e.image_height, e.mask_regions, e.notes, e.created_at, e.updated_at,
         count(*) OVER ()
  FROM nircam_exposures e
  WHERE (p_field IS NULL OR e.field = p_field)
    AND (p_filter IS NULL OR e.filter = p_filter)
    AND (p_detector IS NULL OR e.detector = p_detector)
    AND (p_review_status IS NULL OR e.review_status = p_review_status)
    AND (p_stage IS NULL OR e.stage = p_stage)
    AND (p_correction IS NULL OR e.correction = p_correction)
  ORDER BY
    -- Keep in lockstep with get_admin_exposure_neighbors.
    CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'asc'  THEN e.field END ASC,
    CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'asc'  THEN e.filter END ASC,
    CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'asc'  THEN e.filename END ASC,
    CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'desc' THEN e.field END DESC,
    CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'desc' THEN e.filter END DESC,
    CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'desc' THEN e.filename END DESC,
    CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc'  THEN e.field END ASC,
    CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN e.field END DESC,
    CASE WHEN p_sort_column = 'filter' AND p_sort_direction = 'asc'  THEN e.filter END ASC,
    CASE WHEN p_sort_column = 'filter' AND p_sort_direction = 'desc' THEN e.filter END DESC,
    CASE WHEN p_sort_column = 'detector' AND p_sort_direction = 'asc'  THEN e.detector END ASC,
    CASE WHEN p_sort_column = 'detector' AND p_sort_direction = 'desc' THEN e.detector END DESC,
    CASE WHEN p_sort_column = 'stage' AND p_sort_direction = 'asc'  THEN e.stage END ASC,
    CASE WHEN p_sort_column = 'stage' AND p_sort_direction = 'desc' THEN e.stage END DESC,
    CASE WHEN p_sort_column = 'review_status' AND p_sort_direction = 'asc'  THEN e.review_status END ASC,
    CASE WHEN p_sort_column = 'review_status' AND p_sort_direction = 'desc' THEN e.review_status END DESC,
    CASE WHEN p_sort_column = 'date_obs' AND p_sort_direction = 'asc'  THEN e.date_obs END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'date_obs' AND p_sort_direction = 'desc' THEN e.date_obs END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'updated_at' AND p_sort_direction = 'asc'  THEN e.updated_at END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'updated_at' AND p_sort_direction = 'desc' THEN e.updated_at END DESC NULLS LAST,
    e.id ASC
  OFFSET v_offset LIMIT v_limit;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_admin_exposures TO authenticated;

-- The detail page's prev/next nav: absolute positions of the current exposure
-- and its ±p_window neighbors within the SAME filtered, ordered set the list
-- shows. Bounded transfer (≤ 2*window+1 rows) — replaces the unbounded
-- fetch-every-matching-id nav cache. Returns zero rows if p_current_id does
-- not match the filters.
CREATE OR REPLACE FUNCTION public.get_admin_exposure_neighbors(
  p_current_id integer,
  p_field text DEFAULT NULL,
  p_filter text DEFAULT NULL,
  p_detector text DEFAULT NULL,
  p_review_status text DEFAULT NULL,
  p_stage text DEFAULT NULL,
  p_correction text DEFAULT NULL,
  p_sort_column text DEFAULT 'filename',
  p_sort_direction text DEFAULT 'asc',
  p_window integer DEFAULT 3
)
RETURNS TABLE (
  id integer,
  nav_position bigint,   -- 1-based rank in the filtered set ("position" is reserved)
  total_count bigint
)
LANGUAGE plpgsql STABLE
SET search_path = public, pg_temp
AS $$
DECLARE
  v_window integer := LEAST(GREATEST(COALESCE(p_window, 3), 1), 25);
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'asc'; END IF;
  IF p_sort_column NOT IN ('filename', 'field', 'filter', 'detector', 'stage',
                           'review_status', 'date_obs', 'updated_at') THEN
    p_sort_column := 'filename';
  END IF;

  RETURN QUERY
  WITH ranked AS (
    SELECT e.id AS exp_id,
           row_number() OVER (ORDER BY
             -- Keep in lockstep with get_admin_exposures.
             CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'asc'  THEN e.field END ASC,
             CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'asc'  THEN e.filter END ASC,
             CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'asc'  THEN e.filename END ASC,
             CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'desc' THEN e.field END DESC,
             CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'desc' THEN e.filter END DESC,
             CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'desc' THEN e.filename END DESC,
             CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc'  THEN e.field END ASC,
             CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN e.field END DESC,
             CASE WHEN p_sort_column = 'filter' AND p_sort_direction = 'asc'  THEN e.filter END ASC,
             CASE WHEN p_sort_column = 'filter' AND p_sort_direction = 'desc' THEN e.filter END DESC,
             CASE WHEN p_sort_column = 'detector' AND p_sort_direction = 'asc'  THEN e.detector END ASC,
             CASE WHEN p_sort_column = 'detector' AND p_sort_direction = 'desc' THEN e.detector END DESC,
             CASE WHEN p_sort_column = 'stage' AND p_sort_direction = 'asc'  THEN e.stage END ASC,
             CASE WHEN p_sort_column = 'stage' AND p_sort_direction = 'desc' THEN e.stage END DESC,
             CASE WHEN p_sort_column = 'review_status' AND p_sort_direction = 'asc'  THEN e.review_status END ASC,
             CASE WHEN p_sort_column = 'review_status' AND p_sort_direction = 'desc' THEN e.review_status END DESC,
             CASE WHEN p_sort_column = 'date_obs' AND p_sort_direction = 'asc'  THEN e.date_obs END ASC NULLS LAST,
             CASE WHEN p_sort_column = 'date_obs' AND p_sort_direction = 'desc' THEN e.date_obs END DESC NULLS LAST,
             CASE WHEN p_sort_column = 'updated_at' AND p_sort_direction = 'asc'  THEN e.updated_at END ASC NULLS LAST,
             CASE WHEN p_sort_column = 'updated_at' AND p_sort_direction = 'desc' THEN e.updated_at END DESC NULLS LAST,
             e.id ASC
           ) AS rn,
           count(*) OVER () AS n
    FROM nircam_exposures e
    WHERE (p_field IS NULL OR e.field = p_field)
      AND (p_filter IS NULL OR e.filter = p_filter)
      AND (p_detector IS NULL OR e.detector = p_detector)
      AND (p_review_status IS NULL OR e.review_status = p_review_status)
      AND (p_stage IS NULL OR e.stage = p_stage)
        AND (p_correction IS NULL OR e.correction = p_correction)
  ),
  cur AS (
    SELECT r.rn AS rn0 FROM ranked r WHERE r.exp_id = p_current_id
  )
  SELECT r.exp_id, r.rn, r.n
  FROM ranked r, cur
  WHERE r.rn BETWEEN cur.rn0 - v_window AND cur.rn0 + v_window
  ORDER BY r.rn;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_admin_exposure_neighbors TO authenticated;

-- Distinct facet values for the admin filter dropdowns, one grouped scan per
-- facet — replaces the fetch-every-row-then-Set() option builders.
CREATE OR REPLACE FUNCTION public.get_admin_exposure_facets()
RETURNS TABLE (kind text, value text)
LANGUAGE plpgsql STABLE
SET search_path = public, pg_temp
AS $$
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  RETURN QUERY
  SELECT 'field'::text, e.field FROM nircam_exposures e GROUP BY e.field
  UNION ALL
  SELECT 'filter'::text, e.filter FROM nircam_exposures e GROUP BY e.filter
  UNION ALL
  SELECT 'detector'::text, e.detector FROM nircam_exposures e GROUP BY e.detector
  UNION ALL
  SELECT 'stage'::text, e.stage FROM nircam_exposures e GROUP BY e.stage
  ORDER BY 1, 2;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_admin_exposure_facets() TO authenticated;

CREATE OR REPLACE FUNCTION public.get_admin_storage_facets()
RETURNS TABLE (kind text, value text)
LANGUAGE plpgsql STABLE
SET search_path = public, pg_temp
AS $$
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  RETURN QUERY
  SELECT 'product_type'::text, so.product_type FROM storage_objects so GROUP BY so.product_type
  UNION ALL
  SELECT 'status'::text, so.status FROM storage_objects so GROUP BY so.status
  UNION ALL
  SELECT 'backend'::text, so.backend FROM storage_objects so GROUP BY so.backend
  UNION ALL
  SELECT 'field'::text, so.field FROM storage_objects so WHERE so.field IS NOT NULL GROUP BY so.field
  UNION ALL
  SELECT 'observation'::text, so.observation FROM storage_objects so WHERE so.observation IS NOT NULL GROUP BY so.observation
  ORDER BY 1, 2;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_admin_storage_facets() TO authenticated;


-- =============================================================================
-- get_storage_budget (epic #210, F1)
-- =============================================================================
-- Bytes-at-rest budget against the 20 TB cap. Egress is free (OSN academic
-- service), so the budget tracks storage only. Sums active storage_objects rows
-- (data bucket) plus aggregated tile bytes from map_layers (tiles are kept on R2
-- and intentionally not indexed per-object). SECURITY DEFINER + admin gate so a
-- non-admin cannot enumerate the registry. Used by `campfire deploy registry budget`.

CREATE OR REPLACE FUNCTION public.get_storage_budget()
RETURNS json
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  result JSON;
  is_admin BOOLEAN;
  cap_bytes BIGINT := 20::BIGINT * 1024 * 1024 * 1024 * 1024;  -- 20 TB
  registry_bytes BIGINT;
  tile_bytes BIGINT;
  total_bytes BIGINT;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO is_admin
  FROM user_profiles up
  WHERE up.user_id = auth.uid();

  -- Admin (web/CLI login) OR service_role (CLI --local / headless deploy). Both are
  -- trusted callers of the registry; everyone else is denied. NULL is_admin (no uid /
  -- no profile) coalesces to false so the gate is fail-closed — stronger than the
  -- get_download_stats pattern, which relies on authenticated-only EXECUTE to mask it.
  IF NOT (COALESCE(is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  SELECT COALESCE(SUM(size_bytes), 0) INTO registry_bytes
  FROM storage_objects
  WHERE status = 'active';

  SELECT COALESCE(SUM(total_size_bytes), 0) INTO tile_bytes
  FROM map_layers;

  total_bytes := registry_bytes + tile_bytes;

  SELECT json_build_object(
    'cap_bytes', cap_bytes,
    'total_bytes', total_bytes,
    'pct_used', ROUND((total_bytes::NUMERIC / NULLIF(cap_bytes, 0)) * 100, 2),
    'registry_bytes', registry_bytes,
    'tile_bytes', tile_bytes,
    'by_backend', (
      SELECT COALESCE(json_object_agg(backend, bytes), '{}'::json)
      FROM (
        SELECT backend, SUM(size_bytes) AS bytes
        FROM storage_objects WHERE status = 'active'
        GROUP BY backend
      ) t
    ),
    'by_bucket', (
      SELECT COALESCE(json_object_agg(bucket, bytes), '{}'::json)
      FROM (
        SELECT bucket, SUM(size_bytes) AS bytes
        FROM storage_objects WHERE status = 'active'
        GROUP BY bucket
      ) t
    ),
    'by_product_type', (
      SELECT COALESCE(json_object_agg(product_type, bytes), '{}'::json)
      FROM (
        SELECT product_type, SUM(size_bytes) AS bytes
        FROM storage_objects WHERE status = 'active'
        GROUP BY product_type
      ) t
    ),
    'by_status', (
      SELECT COALESCE(json_object_agg(status, json_build_object('count', cnt, 'bytes', bytes)), '{}'::json)
      FROM (
        SELECT status, COUNT(*) AS cnt, SUM(size_bytes) AS bytes
        FROM storage_objects
        GROUP BY status
      ) t
    )
  ) INTO result;

  RETURN result;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_storage_budget TO authenticated, service_role;


-- =============================================================================
-- Intermediate-product lifecycle (epic #210, B2/B3)
-- =============================================================================
-- The publish/revoke/recover flow for draft science. spectra.deploy_status is
-- the user-facing visibility gate (B1); these RPCs are the only sanctioned way
-- to transition it, and they keep targets/objects.has_published_spectrum and the
-- deploy_events audit log in lockstep. All are SECURITY DEFINER + admin/service_role
-- gated (the same gate as get_storage_budget): callers are the deploy CLI
-- (service_role / admin token) and the B3 admin web actions (admin session).

-- get_lifecycle_status: the capability marker the deploy CLI checks before any
-- `--in-prep` upload. It introspects the LIVE catalog to confirm B1 (#217) is
-- applied to *this* database: the deploy_status column exists AND the reader RPCs
-- actually carry the p_include_unpublished predicate. Returns enabled=false (not
-- an error) when B1 is absent, so `deploy --in-prep` can abort cleanly; if even
-- this function is missing, the client catches the RPC-not-found and aborts too.
CREATE OR REPLACE FUNCTION public.get_lifecycle_status()
RETURNS json
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_is_admin boolean;
  v_has_status_col boolean;
  v_has_target_flag boolean;
  v_reader_threaded boolean;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'spectra' AND column_name = 'deploy_status'
  ) INTO v_has_status_col;

  SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'targets' AND column_name = 'has_published_spectrum'
  ) INTO v_has_target_flag;

  -- A representative reader RPC must carry the predicate parameter — proof that
  -- B1's reader-threading (not just the column) is deployed.
  SELECT EXISTS (
    SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public' AND p.proname = 'get_filtered_object_ids'
      AND pg_get_function_arguments(p.oid) LIKE '%p_include_unpublished%'
  ) INTO v_reader_threaded;

  RETURN json_build_object(
    'enabled', (v_has_status_col AND v_has_target_flag AND v_reader_threaded),
    'version', 1,
    'checks', json_build_object(
      'spectra_deploy_status', v_has_status_col,
      'targets_has_published_spectrum', v_has_target_flag,
      'reader_p_include_unpublished', v_reader_threaded
    )
  );
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_lifecycle_status() TO authenticated, service_role;


-- recompute_has_published_spectrum: refresh targets/objects.has_published_spectrum
-- (the B1 object/target visibility gate) from the current spectra.deploy_status.
-- Scope by p_target_ids (publish/revoke) or p_field (reconcile, after membership
-- changes). A target is published iff a member spectrum is; an object is published
-- iff a member target is. Fail-closed default is TRUE, so this MUST run after any
-- status change or membership relink — forgetting it leaves rows over-visible.
CREATE OR REPLACE FUNCTION public.recompute_has_published_spectrum(
  p_target_ids text[] DEFAULT NULL,
  p_field text DEFAULT NULL
)
RETURNS json
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_is_admin boolean;
  v_targets text[];
  v_n_targets int := 0;
  v_n_objects int := 0;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  IF p_target_ids IS NOT NULL THEN
    v_targets := p_target_ids;
  ELSIF p_field IS NOT NULL THEN
    SELECT array_agg(t.target_id) INTO v_targets FROM targets t WHERE t.field = p_field;
  ELSE
    RAISE EXCEPTION 'recompute_has_published_spectrum requires p_target_ids or p_field';
  END IF;

  IF v_targets IS NULL OR array_length(v_targets, 1) IS NULL THEN
    RETURN json_build_object('targets_updated', 0, 'objects_updated', 0);
  END IF;

  UPDATE targets t
  SET has_published_spectrum = EXISTS (
        SELECT 1 FROM spectra s
        WHERE s.target_id = t.target_id AND s.deploy_status = 'published'
      )
  WHERE t.target_id = ANY(v_targets);
  GET DIAGNOSTICS v_n_targets = ROW_COUNT;

  UPDATE objects o
  SET has_published_spectrum = EXISTS (
        SELECT 1 FROM targets t WHERE t.object_id = o.id AND t.has_published_spectrum
      )
  WHERE o.id IN (
    SELECT DISTINCT t2.object_id FROM targets t2
    WHERE t2.target_id = ANY(v_targets) AND t2.object_id IS NOT NULL
  );
  GET DIAGNOSTICS v_n_objects = ROW_COUNT;

  RETURN json_build_object('targets_updated', v_n_targets, 'objects_updated', v_n_objects);
END;
$$;

GRANT EXECUTE ON FUNCTION public.recompute_has_published_spectrum(text[], text) TO authenticated, service_role;


-- log_deploy_event: append one row to the deploy_events audit log. The only
-- sanctioned write path (deploy_events has no INSERT policy, so direct client
-- inserts are denied). Used by the deploy CLI for 'upload' events; the set_*
-- RPCs write their own rows inline.
CREATE OR REPLACE FUNCTION public.log_deploy_event(
  p_action text,
  p_actor uuid DEFAULT NULL,
  p_deployment_id integer DEFAULT NULL,
  p_observation text DEFAULT NULL,
  p_field text DEFAULT NULL,
  p_affected_count integer DEFAULT NULL,
  p_metadata jsonb DEFAULT NULL,
  p_host text DEFAULT NULL
)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_is_admin boolean;
  v_id uuid;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  IF p_action NOT IN ('upload', 'publish', 'revoke', 'recover', 'supersede', 'delete', 'config_sync') THEN
    RAISE EXCEPTION 'Invalid deploy_event action: %', p_action;
  END IF;

  INSERT INTO deploy_events(actor, action, deployment_id, observation, field, affected_count, metadata, host)
  VALUES (p_actor, p_action, p_deployment_id, p_observation, p_field, p_affected_count, p_metadata, p_host)
  RETURNING id INTO v_id;
  RETURN v_id;
END;
$$;

GRANT EXECUTE ON FUNCTION public.log_deploy_event(text, uuid, integer, text, text, integer, jsonb, text) TO authenticated, service_role;


-- set_spectra_deploy_status: the publish/revoke/recover primitive. Transitions a
-- set of spectra (by PK) to p_to, recomputes has_published_spectrum for their
-- targets/objects, and writes one audit row. p_action labels intent (publish vs
-- recover both land 'published'); defaulted from p_to when omitted.
CREATE OR REPLACE FUNCTION public.set_spectra_deploy_status(
  p_spectrum_db_ids integer[],
  p_to text,
  p_action text DEFAULT NULL,
  p_actor uuid DEFAULT NULL,
  p_deployment_id integer DEFAULT NULL,
  p_host text DEFAULT NULL
)
RETURNS json
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_is_admin boolean;
  v_action text;
  v_updated int := 0;
  v_target_ids text[];
  v_recompute json;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  IF p_to NOT IN ('draft', 'published', 'revoked') THEN
    RAISE EXCEPTION 'Invalid deploy_status: %', p_to;
  END IF;

  v_action := COALESCE(p_action,
    CASE p_to WHEN 'published' THEN 'publish' WHEN 'revoked' THEN 'revoke' ELSE 'upload' END);
  IF v_action NOT IN ('upload', 'publish', 'revoke', 'recover', 'supersede', 'delete') THEN
    RAISE EXCEPTION 'Invalid action: %', v_action;
  END IF;

  IF p_spectrum_db_ids IS NULL OR array_length(p_spectrum_db_ids, 1) IS NULL THEN
    RETURN json_build_object('updated', 0, 'action', v_action);
  END IF;

  SELECT array_agg(DISTINCT s.target_id) INTO v_target_ids
  FROM spectra s WHERE s.id = ANY(p_spectrum_db_ids);

  UPDATE spectra s SET deploy_status = p_to
  WHERE s.id = ANY(p_spectrum_db_ids) AND s.deploy_status <> p_to;
  GET DIAGNOSTICS v_updated = ROW_COUNT;

  IF v_target_ids IS NOT NULL THEN
    v_recompute := public.recompute_has_published_spectrum(p_target_ids := v_target_ids);
  END IF;

  INSERT INTO deploy_events(actor, action, deployment_id, status_to, affected_count, host, metadata)
  VALUES (p_actor, v_action, p_deployment_id, p_to, v_updated, p_host,
          jsonb_build_object(
            'instrument', 'nirspec',
            'counts', jsonb_build_object(
              'succeeded', v_updated,
              'targets', COALESCE(array_length(v_target_ids, 1), 0)),
            'flags', jsonb_build_object('lifecycle', true)));

  RETURN json_build_object('updated', v_updated, 'action', v_action, 'recompute', v_recompute);
END;
$$;

GRANT EXECUTE ON FUNCTION public.set_spectra_deploy_status(integer[], text, text, uuid, integer, text) TO authenticated, service_role;


-- set_deployment_status: deployment-scoped publish/revoke. Resolves the
-- deployment's observation, transitions its spectra (draft->published on
-- publish; published->revoked on revoke), stamps the deployment lifecycle, and
-- delegates the per-spectrum work (+ audit + recompute) to set_spectra_deploy_status.
CREATE OR REPLACE FUNCTION public.set_deployment_status(
  p_deployment_id integer,
  p_to text,
  p_actor uuid DEFAULT NULL,
  p_host text DEFAULT NULL
)
RETURNS json
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_is_admin boolean;
  v_obs text;
  v_field text;
  v_action text;
  v_spectrum_ids integer[];
  v_n_images integer;
  v_result json;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  IF p_to NOT IN ('draft', 'published', 'revoked') THEN
    RAISE EXCEPTION 'Invalid status: %', p_to;
  END IF;

  SELECT observation, field INTO v_obs, v_field FROM deployments WHERE id = p_deployment_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Deployment % not found', p_deployment_id;
  END IF;

  -- NIRCam field-scoped deployment (epic #261, N1/N2): exposure/mosaic FITS
  -- visibility rides deployment.status via the storage_objects gate; the public
  -- mosaic index (nircam_images) carries its own deploy_status, flipped here to
  -- match (mirrors how the observation path flips spectra.deploy_status).
  IF v_field IS NOT NULL THEN
    v_action := CASE WHEN p_to = 'revoked' THEN 'revoke'
                     WHEN p_to = 'draft' THEN 'upload' ELSE 'publish' END;
    UPDATE deployments SET
      status = p_to,
      published_at = CASE WHEN p_to = 'published' THEN now() ELSE published_at END,
      revoked_at = CASE WHEN p_to = 'revoked' THEN now() ELSE revoked_at END
    WHERE id = p_deployment_id;
    WITH flipped AS (
      UPDATE nircam_images SET deploy_status = p_to
      WHERE deployment_id = p_deployment_id AND deploy_status <> p_to
      RETURNING 1)
    SELECT count(*) INTO v_n_images FROM flipped;
    INSERT INTO deploy_events (actor, action, deployment_id, field, status_to, host, affected_count, metadata)
      VALUES (p_actor, v_action, p_deployment_id, v_field, p_to, p_host, v_n_images,
              jsonb_build_object(
                'instrument', 'nircam',
                'scope', jsonb_build_object('field', v_field),
                'counts', jsonb_build_object('succeeded', v_n_images),
                'flags', jsonb_build_object('lifecycle', true)));
    RETURN json_build_object(
      'deployment_id', p_deployment_id, 'field', v_field, 'status', p_to,
      'nircam_images', json_build_object('updated', v_n_images, 'action', v_action));
  END IF;

  -- Which current statuses transition to p_to:
  --   p_to='published'  -> draft (first publish) OR revoked (recover) become visible
  --   p_to='revoked'    -> published spectra are hidden
  --   p_to='draft'    -> published spectra go back to draft
  -- The prior version matched only 'draft' for the published case, so recovering
  -- a REVOKED deployment flipped the deployment row but left its spectra revoked
  -- and hidden ("0 updated", silently inconsistent) — #233 review.
  SELECT array_agg(s.id) INTO v_spectrum_ids
  FROM spectra s JOIN targets t ON s.target_id = t.target_id
  WHERE t.observation = v_obs
    AND s.deploy_status = ANY (
      CASE p_to WHEN 'published' THEN ARRAY['draft', 'revoked']
                WHEN 'revoked'   THEN ARRAY['published']
                ELSE                  ARRAY['published'] END);

  -- Audit label: publishing previously-revoked spectra is a 'recover', not a
  -- first 'publish'. Computed before the transition (spectra still hold old status).
  v_action := CASE
    WHEN p_to = 'revoked' THEN 'revoke'
    WHEN p_to = 'draft' THEN 'upload'
    WHEN EXISTS (SELECT 1 FROM spectra s JOIN targets t ON s.target_id = t.target_id
                 WHERE t.observation = v_obs AND s.deploy_status = 'revoked')
      THEN 'recover'
    ELSE 'publish'
  END;

  UPDATE deployments SET
    status = p_to,
    published_at = CASE WHEN p_to = 'published' THEN now() ELSE published_at END,
    revoked_at = CASE WHEN p_to = 'revoked' THEN now() ELSE revoked_at END
  WHERE id = p_deployment_id;

  IF v_spectrum_ids IS NOT NULL THEN
    v_result := public.set_spectra_deploy_status(
      p_spectrum_db_ids := v_spectrum_ids, p_to := p_to, p_action := v_action,
      p_actor := p_actor, p_deployment_id := p_deployment_id, p_host := p_host);
  ELSE
    v_result := json_build_object('updated', 0, 'action', v_action);
  END IF;

  RETURN json_build_object(
    'deployment_id', p_deployment_id, 'observation', v_obs,
    'status', p_to, 'spectra', v_result);
END;
$$;

GRANT EXECUTE ON FUNCTION public.set_deployment_status(integer, text, uuid, text) TO authenticated, service_role;


-- =============================================================================
-- Multi-reducer safety (epic #210, B4)
-- =============================================================================
-- Optimistic concurrency over deploy scopes. A deploy reads the scope version
-- when it starts (get_deploy_scope_version) and compare-and-sets at finalize
-- (claim_deploy_scope). A version mismatch means another reducer deployed the
-- same scope concurrently — the clobber is detected and surfaced, not silent.

-- get_deploy_scope_version: the scope's current version (0 if it has never been
-- deployed). SECURITY DEFINER so it reads uniformly under admin and service_role.
CREATE OR REPLACE FUNCTION public.get_deploy_scope_version(
  p_scope_type text,
  p_scope_key text
)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_is_admin boolean;
  v_version integer;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  SELECT version INTO v_version FROM deploy_scope_state
  WHERE scope_type = p_scope_type AND scope_key = p_scope_key;
  RETURN COALESCE(v_version, 0);
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_deploy_scope_version(text, text) TO authenticated, service_role;


-- claim_deploy_scope: atomic compare-and-set. Bumps the scope version iff it
-- still equals p_expected_version (the value the deploy read at start). Returns
-- {claimed, version, current, conflict}: claimed=false + conflict=true means a
-- concurrent deploy advanced the scope (the caller should surface the clobber).
CREATE OR REPLACE FUNCTION public.claim_deploy_scope(
  p_scope_type text,
  p_scope_key text,
  p_expected_version integer,
  p_actor uuid DEFAULT NULL
)
RETURNS json
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_is_admin boolean;
  v_new_version integer;
  v_current integer;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  IF p_scope_type NOT IN ('observation', 'field') THEN
    RAISE EXCEPTION 'Invalid scope_type: %', p_scope_type;
  END IF;

  -- CAS: insert at version 1 if the scope is new (expected 0); otherwise bump
  -- only when the stored version still matches what the caller read at start.
  INSERT INTO deploy_scope_state AS d (scope_type, scope_key, version, last_actor, last_deploy_at, updated_at)
  VALUES (p_scope_type, p_scope_key, 1, p_actor, now(), now())
  ON CONFLICT (scope_type, scope_key) DO UPDATE
    SET version = d.version + 1, last_actor = p_actor, last_deploy_at = now(), updated_at = now()
    WHERE d.version = p_expected_version
  RETURNING d.version INTO v_new_version;

  IF v_new_version IS NOT NULL THEN
    RETURN json_build_object('claimed', true, 'version', v_new_version, 'conflict', false);
  END IF;

  -- No row returned: an existing row's version != expected (concurrent deploy).
  SELECT version INTO v_current FROM deploy_scope_state
  WHERE scope_type = p_scope_type AND scope_key = p_scope_key;
  RETURN json_build_object('claimed', false, 'conflict', true,
                           'current', COALESCE(v_current, 0), 'expected', p_expected_version);
END;
$$;

GRANT EXECUTE ON FUNCTION public.claim_deploy_scope(text, text, integer, uuid) TO authenticated, service_role;


-- =============================================================================
-- Device Auth, API Keys, and Refresh Tokens
-- =============================================================================

CREATE OR REPLACE FUNCTION public.cleanup_expired_device_codes()
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  deleted_count INTEGER;
BEGIN
  DELETE FROM device_codes
  WHERE expires_at < NOW()
  RETURNING 1 INTO deleted_count;

  GET DIAGNOSTICS deleted_count = ROW_COUNT;
  RETURN deleted_count;
END;
$$;

GRANT ALL ON FUNCTION public.cleanup_expired_device_codes() TO anon;
GRANT ALL ON FUNCTION public.cleanup_expired_device_codes() TO authenticated;
GRANT ALL ON FUNCTION public.cleanup_expired_device_codes() TO service_role;

CREATE OR REPLACE FUNCTION public.cleanup_expired_refresh_tokens()
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  deleted_count INTEGER;
BEGIN
  -- Delete tokens that expired more than 30 days ago (keep recent for audit)
  DELETE FROM refresh_tokens
  WHERE expires_at < NOW() - INTERVAL '30 days';

  GET DIAGNOSTICS deleted_count = ROW_COUNT;
  RETURN deleted_count;
END;
$$;

GRANT ALL ON FUNCTION public.cleanup_expired_refresh_tokens() TO anon;
GRANT ALL ON FUNCTION public.cleanup_expired_refresh_tokens() TO authenticated;
GRANT ALL ON FUNCTION public.cleanup_expired_refresh_tokens() TO service_role;

CREATE OR REPLACE FUNCTION public.consume_device_code(p_device_code text)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_user_id UUID;
BEGIN
  UPDATE device_codes
  SET status = 'consumed'
  WHERE
    device_code = p_device_code
    AND status = 'authorized'
    AND expires_at > NOW()
  RETURNING user_id INTO v_user_id;

  RETURN v_user_id;
END;
$$;

GRANT ALL ON FUNCTION public.consume_device_code(text) TO anon;
GRANT ALL ON FUNCTION public.consume_device_code(text) TO authenticated;
GRANT ALL ON FUNCTION public.consume_device_code(text) TO service_role;

-- Phase D: counts distinct objects a user has inspected. Replaces
-- count_distinct_inspected_targets — inspection state lives on objects now.
-- The query unions audit rows that targeted an object directly with rows
-- that pre-date Phase D (which targeted the parent target); the latter map
-- back to objects via targets.object_id so historical activity counts stay
-- intact across the migration boundary.
CREATE OR REPLACE FUNCTION public.count_distinct_inspected_objects(p_user_id uuid)
RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER
AS $$
  SELECT COUNT(DISTINCT obj_id)::INTEGER FROM (
    SELECT object_id AS obj_id
    FROM flag_audit_log
    WHERE user_id = p_user_id AND object_id IS NOT NULL
    UNION
    SELECT t.object_id AS obj_id
    FROM flag_audit_log fal
    JOIN targets t ON t.id = fal.target_id
    WHERE fal.user_id = p_user_id
      AND fal.target_id IS NOT NULL
      AND t.object_id IS NOT NULL
  ) sub;
$$;

COMMENT ON FUNCTION public.count_distinct_inspected_objects(uuid) IS
  'Returns the count of distinct objects a user has inspected (object-level audit rows ∪ pre-Phase-D target-level rows mapped via targets.object_id).';

GRANT ALL ON FUNCTION public.count_distinct_inspected_objects(uuid) TO anon;
GRANT ALL ON FUNCTION public.count_distinct_inspected_objects(uuid) TO authenticated;
GRANT ALL ON FUNCTION public.count_distinct_inspected_objects(uuid) TO service_role;

CREATE OR REPLACE FUNCTION public.deny_device_code(p_user_code text)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  updated_rows INTEGER;
BEGIN
  UPDATE device_codes
  SET status = 'denied'
  WHERE
    user_code = p_user_code
    AND status = 'pending';

  GET DIAGNOSTICS updated_rows = ROW_COUNT;
  RETURN updated_rows > 0;
END;
$$;

GRANT ALL ON FUNCTION public.deny_device_code(text) TO anon;
GRANT ALL ON FUNCTION public.deny_device_code(text) TO authenticated;
GRANT ALL ON FUNCTION public.deny_device_code(text) TO service_role;

CREATE OR REPLACE FUNCTION public.refresh_filter_options()
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
  REFRESH MATERIALIZED VIEW CONCURRENTLY mv_filter_options;
END;
$$;

GRANT ALL ON FUNCTION public.refresh_filter_options() TO anon;
GRANT ALL ON FUNCTION public.refresh_filter_options() TO authenticated;
GRANT ALL ON FUNCTION public.refresh_filter_options() TO service_role;

CREATE OR REPLACE FUNCTION public.revoke_all_user_refresh_tokens(p_user_id uuid)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  updated_rows INTEGER;
BEGIN
  UPDATE refresh_tokens
  SET
    is_revoked = TRUE,
    revoked_at = NOW()
  WHERE
    user_id = p_user_id
    AND is_revoked = FALSE;

  GET DIAGNOSTICS updated_rows = ROW_COUNT;
  RETURN updated_rows;
END;
$$;

GRANT ALL ON FUNCTION public.revoke_all_user_refresh_tokens(uuid) TO anon;
GRANT ALL ON FUNCTION public.revoke_all_user_refresh_tokens(uuid) TO authenticated;
GRANT ALL ON FUNCTION public.revoke_all_user_refresh_tokens(uuid) TO service_role;

CREATE OR REPLACE FUNCTION public.revoke_refresh_token(p_token_id uuid, p_user_id uuid)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  updated_rows INTEGER;
BEGIN
  UPDATE refresh_tokens
  SET
    is_revoked = TRUE,
    revoked_at = NOW()
  WHERE
    id = p_token_id
    AND user_id = p_user_id
    AND is_revoked = FALSE;

  GET DIAGNOSTICS updated_rows = ROW_COUNT;
  RETURN updated_rows > 0;
END;
$$;

GRANT ALL ON FUNCTION public.revoke_refresh_token(uuid, uuid) TO anon;
GRANT ALL ON FUNCTION public.revoke_refresh_token(uuid, uuid) TO authenticated;
GRANT ALL ON FUNCTION public.revoke_refresh_token(uuid, uuid) TO service_role;

CREATE OR REPLACE FUNCTION public.rotate_refresh_token(
  p_old_token_hash text,
  p_new_token_hash text,
  p_expires_at timestamptz,
  p_client_ip text DEFAULT NULL,
  p_user_agent text DEFAULT NULL
)
RETURNS TABLE(success boolean, user_id uuid, new_token_id uuid)
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_user_id UUID;
  v_old_token_id UUID;
  v_new_token_id UUID;
  v_device_name TEXT;
BEGIN
  -- First, validate and get the old token info
  SELECT rt.user_id, rt.id, rt.device_name
  INTO v_user_id, v_old_token_id, v_device_name
  FROM refresh_tokens rt
  WHERE rt.token_hash = p_old_token_hash
    AND rt.is_revoked = FALSE
    AND rt.expires_at > NOW();

  IF v_user_id IS NULL THEN
    -- Token not found or invalid
    RETURN QUERY SELECT FALSE, NULL::UUID, NULL::UUID;
    RETURN;
  END IF;

  -- Create new token
  INSERT INTO refresh_tokens (
    token_hash,
    user_id,
    device_name,
    expires_at,
    client_ip,
    user_agent
  ) VALUES (
    p_new_token_hash,
    v_user_id,
    v_device_name,
    p_expires_at,
    p_client_ip,
    p_user_agent
  )
  RETURNING id INTO v_new_token_id;

  -- Revoke old token and link to new one
  UPDATE refresh_tokens
  SET
    is_revoked = TRUE,
    revoked_at = NOW(),
    replaced_by = v_new_token_id
  WHERE id = v_old_token_id;

  RETURN QUERY SELECT TRUE, v_user_id, v_new_token_id;
END;
$$;

GRANT ALL ON FUNCTION public.rotate_refresh_token(text, text, timestamptz, text, text) TO anon;
GRANT ALL ON FUNCTION public.rotate_refresh_token(text, text, timestamptz, text, text) TO authenticated;
GRANT ALL ON FUNCTION public.rotate_refresh_token(text, text, timestamptz, text, text) TO service_role;

CREATE OR REPLACE FUNCTION public.update_api_key_last_used(key_hash_input text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
  UPDATE api_keys
  SET last_used_at = NOW()
  WHERE key_hash = key_hash_input;
END;
$$;

GRANT ALL ON FUNCTION public.update_api_key_last_used(text) TO anon;
GRANT ALL ON FUNCTION public.update_api_key_last_used(text) TO authenticated;
GRANT ALL ON FUNCTION public.update_api_key_last_used(text) TO service_role;

CREATE OR REPLACE FUNCTION public.validate_api_key(key_hash_input text)
RETURNS TABLE(user_id uuid, is_valid boolean)
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
  RETURN QUERY
  SELECT
    ak.user_id,
    (ak.is_active AND (ak.expires_at IS NULL OR ak.expires_at > NOW()))::BOOLEAN AS is_valid
  FROM api_keys ak
  WHERE ak.key_hash = key_hash_input;
END;
$$;

GRANT ALL ON FUNCTION public.validate_api_key(text) TO anon;
GRANT ALL ON FUNCTION public.validate_api_key(text) TO authenticated;
GRANT ALL ON FUNCTION public.validate_api_key(text) TO service_role;

CREATE OR REPLACE FUNCTION public.validate_refresh_token(p_token_hash text)
RETURNS TABLE(is_valid boolean, user_id uuid, token_id uuid)
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
  -- Also update last_used_at when validating
  UPDATE refresh_tokens
  SET last_used_at = NOW()
  WHERE token_hash = p_token_hash
    AND is_revoked = FALSE
    AND expires_at > NOW();

  RETURN QUERY
  SELECT
    (rt.is_revoked = FALSE AND rt.expires_at > NOW())::BOOLEAN AS is_valid,
    rt.user_id,
    rt.id AS token_id
  FROM refresh_tokens rt
  WHERE rt.token_hash = p_token_hash;
END;
$$;

GRANT ALL ON FUNCTION public.validate_refresh_token(text) TO anon;
GRANT ALL ON FUNCTION public.validate_refresh_token(text) TO authenticated;
GRANT ALL ON FUNCTION public.validate_refresh_token(text) TO service_role;


-- =============================================================================
-- Bulk set target object FK references
-- =============================================================================
-- Used by cfdeploy objects rebuild to set targets.object_id in bulk,
-- avoiding per-object HTTP round-trips through PostgREST.

-- A typed jsonb_to_recordset (not jsonb_array_elements + per-row ->>/cast)
-- gives the planner real column types and a sane row estimate, so it
-- hash-joins against targets_pkey instead of a misestimated nested loop.
-- statement_timeout matches the other heavy reconcile RPCs: without it the
-- function ran at the short role default, and large fields tripped 57014
-- (canceling statement due to statement timeout) mid-apply — see #184.
CREATE OR REPLACE FUNCTION public.bulk_set_target_object_fks(
  p_pairs JSONB,
  p_updated_at TIMESTAMPTZ DEFAULT now()
)
RETURNS void
LANGUAGE plpgsql
SET statement_timeout = '300s'
AS $$
BEGIN
  UPDATE targets t SET
    object_id = pair.object_id,
    updated_at = p_updated_at
  FROM jsonb_to_recordset(p_pairs) AS pair(target_id integer, object_id integer)
  WHERE t.id = pair.target_id;
END;
$$;

GRANT EXECUTE ON FUNCTION public.bulk_set_target_object_fks(JSONB, TIMESTAMPTZ) TO service_role;


-- =============================================================================
-- Atomic object-reconciliation apply (deploy path)
-- =============================================================================
-- Applies the deploy-path reconciliation proposal set — inserts, revivals,
-- updates, orphan soft-deletes, and ALL target->object FK assignments — in a
-- SINGLE transaction. Replaces the previous Python apply_proposals(), which
-- issued each step as an independent, auto-committed PostgREST call: a failure
-- (e.g. a statement timeout) anywhere between the object inserts (step 1) and
-- the dead-last FK assignment (step 7) committed the new objects but never
-- linked their targets, stranding "ghost" objects (active, valid object_id,
-- ZERO member targets) that then collided with objects_object_id_key on every
-- re-run. See GitHub #184.
--
-- One plpgsql function == one transaction, so any failure rolls the entire
-- apply back: no partial state is ever possible. Inserts are additionally made
-- idempotent (ON CONFLICT (object_id) DO UPDATE) so a re-run after any pre-
-- existing ghost mess self-heals by re-adopting the stranded row instead of
-- dying on the unique constraint.
--
-- Splits and merges are NOT handled here. They only occur on the interactive
-- operator path (the deploy path aborts before apply when they are detected),
-- and they carry photometry + list-membership migration that stays in Python.
--
-- Payload (all JSON-safe; member_target_db_ids travels INSIDE each element so
-- FK assignment shares this transaction with the inserts):
--   p_inserts:  [{object_id, ra, dec, n_targets, n_spectra, programs[],
--                 gratings[], observations[], max_snr, max_exposure_time,
--                 member_target_db_ids[]}]
--   p_revivals: as p_inserts plus {object_db_id} (the inactive row to revive)
--   p_updates:  as p_revivals plus {staleness_reason (nullable),
--                 reactivate (bool)}
--   p_orphan_ids: integer[] of object db ids to soft-delete.
--
-- Returns jsonb: {insert_id_map: {object_id: db_id}, revived_ids: [db_id],
--   updated_ids: [db_id], inserted_count, revived_count, updated_count,
--   reactivated_count, orphaned_count, target_fks_set}.
-- =============================================================================
-- recompute_object_search_text
--   Refresh the denormalized objects.search_text blob (object_id + member
--   target_ids + program_slugs + observations) from currently-linked targets.
--   Call with a list of object ids (the rows an apply touched) or NULL for all
--   (one-time backfill). SECURITY DEFINER so deploy/service-role and migration
--   callers write this aggregate column past enforce_object_user_update_scope
--   (which lets auth.uid() IS NULL / admins through). The IS DISTINCT FROM guard
--   avoids no-op writes (search_text has no updated_at trigger, but stay clean).
-- =============================================================================
CREATE OR REPLACE FUNCTION public.recompute_object_search_text(p_object_ids integer[] DEFAULT NULL)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_count integer;
BEGIN
  WITH new_vals AS (
    SELECT o.id,
           concat_ws(' ',
             o.object_id,
             string_agg(DISTINCT t.target_id, ' '),
             string_agg(DISTINCT t.program_slug, ' '),
             string_agg(DISTINCT t.observation, ' ')
           ) AS st
    FROM objects o
    LEFT JOIN targets t ON t.object_id = o.id
    WHERE (p_object_ids IS NULL OR o.id = ANY(p_object_ids))
    GROUP BY o.id, o.object_id
  )
  UPDATE objects o
  SET search_text = nv.st
  FROM new_vals nv
  WHERE o.id = nv.id
    AND o.search_text IS DISTINCT FROM nv.st;
  GET DIAGNOSTICS v_count = ROW_COUNT;
  RETURN v_count;
END;
$$;

GRANT EXECUTE ON FUNCTION public.recompute_object_search_text(integer[]) TO service_role;


CREATE OR REPLACE FUNCTION public.apply_object_reconciliation(
  p_field       TEXT,
  p_inserts     JSONB DEFAULT '[]'::jsonb,
  p_revivals    JSONB DEFAULT '[]'::jsonb,
  p_updates     JSONB DEFAULT '[]'::jsonb,
  p_orphan_ids  INTEGER[] DEFAULT '{}',
  p_updated_at  TIMESTAMPTZ DEFAULT now()
)
RETURNS JSONB
LANGUAGE plpgsql
SET statement_timeout = '300s'
AS $$
DECLARE
  v_insert_id_map JSONB := '{}'::jsonb;
  v_revived_ids   INTEGER[] := '{}';
  v_updated_ids   INTEGER[] := '{}';
  v_orphaned      INTEGER := 0;
  v_reactivated   INTEGER := 0;
  v_fk_count      INTEGER := 0;
BEGIN
  -- 1. Orphans FIRST: soft-delete active objects that lost all members, but
  --    EXCLUDE any whose object_id an insert is about to re-adopt. Ghost
  --    recovery: a stranded active ghost is classified as an orphan while the
  --    real new cluster is an insert holding the same IAU name; the upsert in
  --    step 2 adopts that row, so it must not be soft-deleted out from under it.
  UPDATE objects o SET
    is_active = false,
    last_data_change_at = p_updated_at,
    staleness_reason = 'membership_changed',
    updated_at = p_updated_at
  WHERE o.id = ANY(p_orphan_ids)
    AND o.is_active
    AND o.object_id NOT IN (
      SELECT x.object_id FROM jsonb_to_recordset(p_inserts) AS x(object_id TEXT)
    );
  GET DIAGNOSTICS v_orphaned = ROW_COUNT;

  -- 2. Inserts (idempotent). ON CONFLICT re-adopts a ghost / soft-deleted row
  --    holding this object_id. We deliberately do NOT touch version /
  --    redshift_* / last_inspected_* so inspection state — and the triggers
  --    scoped to those columns — stay untouched; a collision is only ever a
  --    freshly stranded ghost with no inspection history.
  WITH ins AS (
    INSERT INTO objects (
      object_id, field, ra, dec, n_targets, n_spectra,
      programs, gratings, observations, max_snr, max_exposure_time, updated_at
    )
    SELECT x.object_id, p_field, x.ra, x.dec, x.n_targets, x.n_spectra,
           x.programs, x.gratings, x.observations, x.max_snr,
           x.max_exposure_time, p_updated_at
    FROM jsonb_to_recordset(p_inserts) AS x(
      object_id TEXT, ra DOUBLE PRECISION, dec DOUBLE PRECISION,
      n_targets INTEGER, n_spectra INTEGER,
      programs TEXT[], gratings TEXT[], observations TEXT[],
      max_snr DOUBLE PRECISION, max_exposure_time DOUBLE PRECISION
    )
    ON CONFLICT (object_id) DO UPDATE SET
      field = EXCLUDED.field,
      ra = EXCLUDED.ra,
      dec = EXCLUDED.dec,
      n_targets = EXCLUDED.n_targets,
      n_spectra = EXCLUDED.n_spectra,
      programs = EXCLUDED.programs,
      gratings = EXCLUDED.gratings,
      observations = EXCLUDED.observations,
      max_snr = EXCLUDED.max_snr,
      max_exposure_time = EXCLUDED.max_exposure_time,
      is_active = true,
      last_data_change_at = p_updated_at,
      staleness_reason = 'membership_changed',
      updated_at = p_updated_at
    RETURNING id, object_id
  )
  SELECT coalesce(jsonb_object_agg(object_id, id), '{}'::jsonb)
  INTO v_insert_id_map
  FROM ins;

  -- 3. Revivals: reactivate inactive objects matched by position; refresh
  --    centroid + aggregates.
  WITH rev AS (
    UPDATE objects o SET
      is_active = true,
      object_id = r.object_id,
      ra = r.ra,
      dec = r.dec,
      n_targets = r.n_targets,
      n_spectra = r.n_spectra,
      programs = r.programs,
      gratings = r.gratings,
      observations = r.observations,
      max_snr = r.max_snr,
      max_exposure_time = r.max_exposure_time,
      last_data_change_at = p_updated_at,
      staleness_reason = 'membership_changed',
      updated_at = p_updated_at
    FROM jsonb_to_recordset(p_revivals) AS r(
      object_db_id INTEGER, object_id TEXT, ra DOUBLE PRECISION,
      dec DOUBLE PRECISION, n_targets INTEGER, n_spectra INTEGER,
      programs TEXT[], gratings TEXT[], observations TEXT[],
      max_snr DOUBLE PRECISION, max_exposure_time DOUBLE PRECISION
    )
    WHERE o.id = r.object_db_id
    RETURNING o.id
  )
  SELECT coalesce(array_agg(id), '{}') INTO v_revived_ids FROM rev;

  -- 4. Updates: refresh aggregates; set staleness only when provided;
  --    reactivate a membership-matched inactive object when reactivate=true.
  WITH upd AS (
    UPDATE objects o SET
      object_id = u.object_id,
      ra = u.ra,
      dec = u.dec,
      n_targets = u.n_targets,
      n_spectra = u.n_spectra,
      programs = u.programs,
      gratings = u.gratings,
      observations = u.observations,
      max_snr = u.max_snr,
      max_exposure_time = u.max_exposure_time,
      is_active = CASE WHEN u.reactivate THEN true ELSE o.is_active END,
      staleness_reason = CASE
        WHEN u.reactivate THEN 'membership_changed'
        WHEN u.staleness_reason IS NOT NULL THEN u.staleness_reason
        ELSE o.staleness_reason
      END,
      last_data_change_at = CASE
        WHEN u.reactivate OR u.staleness_reason IS NOT NULL THEN p_updated_at
        ELSE o.last_data_change_at
      END,
      updated_at = p_updated_at
    FROM jsonb_to_recordset(p_updates) AS u(
      object_db_id INTEGER, object_id TEXT, ra DOUBLE PRECISION,
      dec DOUBLE PRECISION, n_targets INTEGER, n_spectra INTEGER,
      programs TEXT[], gratings TEXT[], observations TEXT[],
      max_snr DOUBLE PRECISION, max_exposure_time DOUBLE PRECISION,
      staleness_reason TEXT, reactivate BOOLEAN
    )
    WHERE o.id = u.object_db_id
    RETURNING o.id AS id, u.reactivate AS reactivated
  )
  SELECT coalesce(array_agg(id), '{}'),
         coalesce(count(*) FILTER (WHERE reactivated), 0)
  INTO v_updated_ids, v_reactivated
  FROM upd;

  -- 5. Target FK assignment — the actual fix. Derive (target, object) pairs
  --    from each operation's member_target_db_ids (resolving insert object ids
  --    via the RETURNING map) and apply them in ONE typed, index-friendly
  --    UPDATE, inside this same transaction as the inserts above.
  WITH pairs AS (
    SELECT unnest(x.member_target_db_ids) AS target_id,
           (v_insert_id_map ->> x.object_id)::INTEGER AS object_id
    FROM jsonb_to_recordset(p_inserts)
      AS x(object_id TEXT, member_target_db_ids INTEGER[])
    UNION ALL
    SELECT unnest(r.member_target_db_ids), r.object_db_id
    FROM jsonb_to_recordset(p_revivals)
      AS r(object_db_id INTEGER, member_target_db_ids INTEGER[])
    UNION ALL
    SELECT unnest(u.member_target_db_ids), u.object_db_id
    FROM jsonb_to_recordset(p_updates)
      AS u(object_db_id INTEGER, member_target_db_ids INTEGER[])
  )
  UPDATE targets t SET
    object_id = p.object_id,
    updated_at = p_updated_at
  FROM pairs p
  WHERE t.id = p.target_id;
  GET DIAGNOSTICS v_fk_count = ROW_COUNT;

  -- 6. Refresh denormalized search_text for every object touched above. Targets
  --    are relinked (step 5), so member target_ids / programs / observations
  --    resolve. Scoped to the affected ids so it stays cheap per apply.
  PERFORM public.recompute_object_search_text(
    (SELECT coalesce(array_agg(value::int), '{}'::integer[])
       FROM jsonb_each_text(v_insert_id_map))
    || v_revived_ids
    || v_updated_ids
  );

  RETURN jsonb_build_object(
    'insert_id_map', v_insert_id_map,
    'revived_ids', to_jsonb(v_revived_ids),
    'updated_ids', to_jsonb(v_updated_ids),
    'inserted_count', (SELECT count(*) FROM jsonb_object_keys(v_insert_id_map)),
    'revived_count', coalesce(array_length(v_revived_ids, 1), 0),
    'updated_count', coalesce(array_length(v_updated_ids, 1), 0),
    'reactivated_count', v_reactivated,
    'orphaned_count', v_orphaned,
    'target_fks_set', v_fk_count
  );
END;
$$;

GRANT EXECUTE ON FUNCTION public.apply_object_reconciliation(TEXT, JSONB, JSONB, JSONB, INTEGER[], TIMESTAMPTZ) TO service_role;


-- =============================================================================
-- Recompute target aggregate columns from spectra
-- =============================================================================
-- Bulk-recomputes max_snr and max_exposure_time on targets from the spectra
-- table. Called by the deploy CLI after batch spectra upserts, replacing the
-- old per-row triggers which caused statement timeouts on large batches.

CREATE OR REPLACE FUNCTION public.recompute_target_aggregates(
  p_target_ids TEXT[]
)
RETURNS INTEGER
LANGUAGE plpgsql
AS $$
DECLARE
  n INTEGER;
BEGIN
  UPDATE targets t SET
    max_snr = sub.max_snr,
    max_exposure_time = sub.max_exposure_time
  FROM (
    SELECT
      s.target_id,
      MAX(s.signal_to_noise) AS max_snr,
      MAX(s.exposure_time) AS max_exposure_time
    FROM spectra s
    WHERE s.target_id = ANY(p_target_ids)
    GROUP BY s.target_id
  ) sub
  WHERE t.target_id = sub.target_id;

  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$$;

GRANT EXECUTE ON FUNCTION public.recompute_target_aggregates(TEXT[]) TO authenticated;
GRANT EXECUTE ON FUNCTION public.recompute_target_aggregates(TEXT[]) TO service_role;

-- =============================================================================
-- Compute objects.redshift_auto from best member spectrum (grating-priority)
-- =============================================================================
-- For each object in the field, set redshift_auto to the redshift_auto of
-- its best member spectrum under a grating-priority hierarchy:
--   1. PRISM (3x wavelength coverage, highest z-confirmation efficiency)
--   2. Medium-resolution gratings (G140M, G235M, G395M)
--   3. High-resolution gratings (G140H, G235H, G395H)
-- Ties within a tier are broken by longest exposure_time, then lowest id.
-- SNR is intentionally not used: contamination can inflate SNR and PRISM's
-- wavelength coverage makes it the most reliable discriminator even at
-- modest SNR. Objects whose members all have NULL redshift_auto are nulled
-- out. Called by reconcile_field_objects() after membership/aggregate
-- updates.
--
-- Replaces the old two-hop path (pipeline → target.redshift_auto → object
-- via update_object_best_redshift trigger) with a direct one-hop path
-- (spectra.redshift_auto → objects.redshift_auto at reconciliation time).

-- The CTE + IS DISTINCT FROM guard ensures we only rewrite rows whose
-- redshift_auto actually changed, and updated_at is bumped in the same
-- statement so get_objects_for_sync (which uses updated_at as its delta
-- cursor) picks the change up on the next client sync. Without the bump,
-- clients would silently miss redshift_auto changes from pipeline reruns.
-- ROW_COUNT is then the true number of objects whose value changed.
--
-- Sign-off pinning is now handled at write time by the
-- pin_redshift_on_signoff trigger: any object reaching quality >= 2 with
-- redshift_inspected = NULL has its current redshift_auto promoted into
-- redshift_inspected and inspected_used_auto = true. The displayed redshift
-- (the generated `redshift` column) is therefore stable across reprocessing
-- for every signed-off object — this function never has to worry about
-- moving a value out from under an inspector.
--
-- Staleness signal: when redshift_auto changes for an already-signed-off
-- object (quality >= 2), we still flag staleness_reason='reprocessed' and
-- bump last_data_change_at so the UI surfaces a "Needs Review" badge.
-- The pinned displayed redshift is unchanged, but the inspector should
-- know the underlying fit shifted in case they want to update their
-- override or reaffirm the existing one.
CREATE OR REPLACE FUNCTION public.compute_object_redshift_auto(p_field TEXT)
RETURNS INTEGER
LANGUAGE plpgsql
-- Per-object correlated subquery over targets⋈spectra across the whole field
-- (~7k objects on egs). Runs right after the reconcile apply; without an
-- explicit guard it ran at the short role default and was a latent second
-- timeout. Matches the other heavy reconcile RPCs. See #184.
SET statement_timeout = '300s'
AS $$
DECLARE
  n INTEGER;
BEGIN
  WITH computed AS (
    SELECT o.id,
           o.redshift_auto AS old_auto,
           o.redshift_quality AS quality,
           (
             SELECT s.redshift_auto
             FROM targets t
             JOIN spectra s ON s.target_id = t.target_id
             WHERE t.object_id = o.id
               AND s.redshift_auto IS NOT NULL
             ORDER BY
               CASE
                 WHEN s.grating = 'PRISM' THEN 0
                 WHEN s.grating IN ('G140M', 'G235M', 'G395M') THEN 1
                 WHEN s.grating IN ('G140H', 'G235H', 'G395H') THEN 2
                 ELSE 3
               END ASC,
               s.exposure_time DESC NULLS LAST,
               s.id ASC
             LIMIT 1
           ) AS new_val
    FROM objects o
    WHERE o.field = p_field
  )
  UPDATE objects o
  SET redshift_auto = c.new_val,
      staleness_reason = CASE
        WHEN c.quality >= 2
             AND c.old_auto IS DISTINCT FROM c.new_val
        THEN 'reprocessed'
        ELSE o.staleness_reason
      END,
      last_data_change_at = CASE
        WHEN c.quality >= 2
             AND c.old_auto IS DISTINCT FROM c.new_val
        THEN NOW()
        ELSE o.last_data_change_at
      END,
      updated_at = NOW()
  FROM computed c
  WHERE o.id = c.id
    AND o.redshift_auto IS DISTINCT FROM c.new_val;

  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$$;

GRANT EXECUTE ON FUNCTION public.compute_object_redshift_auto(TEXT) TO service_role;


-- Re-link object_list_members.object_id after object rebuild for a field.
-- Uses spatial tolerance (0.3 arcsec) to match members to the nearest
-- rebuilt object. Returns JSONB with counts:
--   { "relinked": N, "orphaned": N, "orphaned_details": [...] }
--
-- Operates on members whose previous object was in this field (now NULL
-- after ON DELETE SET NULL) or whose coordinates fall within the field's
-- bounding box.
DROP FUNCTION IF EXISTS public.relink_list_members_for_field(TEXT);

CREATE OR REPLACE FUNCTION public.relink_list_members_for_field(p_field TEXT)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  n_relinked INTEGER := 0;
  n_orphaned INTEGER := 0;
  v_orphaned_details JSONB := '[]'::JSONB;
  v_field_ra_min DOUBLE PRECISION;
  v_field_ra_max DOUBLE PRECISION;
  v_field_dec_min DOUBLE PRECISION;
  v_field_dec_max DOUBLE PRECISION;
  v_tolerance_deg DOUBLE PRECISION := 0.3 / 3600.0;  -- 0.3 arcsec in degrees
BEGIN
  -- Get bounding box of objects in this field (with padding)
  SELECT MIN(o.ra) - v_tolerance_deg, MAX(o.ra) + v_tolerance_deg,
         MIN(o.dec) - v_tolerance_deg, MAX(o.dec) + v_tolerance_deg
  INTO v_field_ra_min, v_field_ra_max, v_field_dec_min, v_field_dec_max
  FROM objects o WHERE o.field = p_field;

  IF v_field_ra_min IS NULL THEN
    -- No objects in field, nothing to re-link
    RETURN jsonb_build_object('relinked', 0, 'orphaned', 0, 'orphaned_details', '[]'::jsonb);
  END IF;

  -- Re-link: for each unlinked member whose coords fall in this field,
  -- find the nearest object within 0.3 arcsec tolerance.
  WITH candidates AS (
    SELECT olm.id AS member_id,
           olm.ra AS member_ra,
           olm.dec AS member_dec,
           olm.list_id,
           o.id AS obj_id,
           -- Angular distance approximation (sufficient for sub-arcsec)
           SQRT(
             POWER((olm.ra - o.ra) * COS(RADIANS(olm.dec)), 2) +
             POWER(olm.dec - o.dec, 2)
           ) AS dist_deg,
           ROW_NUMBER() OVER (
             PARTITION BY olm.id
             ORDER BY SQRT(
               POWER((olm.ra - o.ra) * COS(RADIANS(olm.dec)), 2) +
               POWER(olm.dec - o.dec, 2)
             ) ASC
           ) AS rn
    FROM object_list_members olm
    CROSS JOIN LATERAL (
      SELECT o.id, o.ra, o.dec
      FROM objects o
      WHERE o.field = p_field
        AND o.ra BETWEEN olm.ra - v_tolerance_deg AND olm.ra + v_tolerance_deg
        AND o.dec BETWEEN olm.dec - v_tolerance_deg AND olm.dec + v_tolerance_deg
    ) o
    WHERE olm.object_id IS NULL
      AND olm.ra BETWEEN v_field_ra_min AND v_field_ra_max
      AND olm.dec BETWEEN v_field_dec_min AND v_field_dec_max
  ),
  best_match AS (
    SELECT member_id, obj_id, dist_deg
    FROM candidates
    WHERE rn = 1 AND dist_deg <= v_tolerance_deg
  ),
  updated AS (
    UPDATE object_list_members olm
    SET object_id = bm.obj_id
    FROM best_match bm
    WHERE olm.id = bm.member_id
    RETURNING olm.id
  )
  SELECT COUNT(*) INTO n_relinked FROM updated;

  -- Count orphaned members (still NULL after re-link, coords in field bbox)
  SELECT COUNT(*),
         COALESCE(jsonb_agg(jsonb_build_object(
           'list_slug', ol.slug,
           'list_name', ol.name,
           'ra', olm.ra,
           'dec', olm.dec
         )), '[]'::jsonb)
  INTO n_orphaned, v_orphaned_details
  FROM object_list_members olm
  JOIN object_lists ol ON ol.id = olm.list_id
  WHERE olm.object_id IS NULL
    AND olm.ra BETWEEN v_field_ra_min AND v_field_ra_max
    AND olm.dec BETWEEN v_field_dec_min AND v_field_dec_max;

  RETURN jsonb_build_object(
    'relinked', n_relinked,
    'orphaned', n_orphaned,
    'orphaned_details', v_orphaned_details
  );
END;
$$;

GRANT EXECUTE ON FUNCTION public.relink_list_members_for_field(TEXT) TO service_role;


-- =============================================================================
-- relink_photometry_for_field
-- (re-link object_photometry.object_id after objects rebuild)
-- =============================================================================

DROP FUNCTION IF EXISTS public.relink_photometry_for_field(TEXT);

CREATE OR REPLACE FUNCTION public.relink_photometry_for_field(p_field TEXT)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  n_relinked INTEGER := 0;
  n_orphaned INTEGER := 0;
  v_field_ra_min DOUBLE PRECISION;
  v_field_ra_max DOUBLE PRECISION;
  v_field_dec_min DOUBLE PRECISION;
  v_field_dec_max DOUBLE PRECISION;
  v_tolerance_deg DOUBLE PRECISION := 0.3 / 3600.0;  -- 0.3 arcsec in degrees
BEGIN
  -- Get bounding box of objects in this field (with padding)
  SELECT MIN(o.ra) - v_tolerance_deg, MAX(o.ra) + v_tolerance_deg,
         MIN(o.dec) - v_tolerance_deg, MAX(o.dec) + v_tolerance_deg
  INTO v_field_ra_min, v_field_ra_max, v_field_dec_min, v_field_dec_max
  FROM objects o WHERE o.field = p_field;

  IF v_field_ra_min IS NULL THEN
    RETURN jsonb_build_object('relinked', 0, 'orphaned', 0);
  END IF;

  -- Re-link: for each photometry row in this field,
  -- find the nearest object within 0.3 arcsec tolerance.
  WITH candidates AS (
    SELECT op.id AS phot_id,
           o.id AS obj_id,
           SQRT(
             POWER((op.ra - o.ra) * COS(RADIANS(op.dec)), 2) +
             POWER(op.dec - o.dec, 2)
           ) AS dist_deg,
           ROW_NUMBER() OVER (
             PARTITION BY op.id
             ORDER BY SQRT(
               POWER((op.ra - o.ra) * COS(RADIANS(op.dec)), 2) +
               POWER(op.dec - o.dec, 2)
             ) ASC
           ) AS rn
    FROM object_photometry op
    CROSS JOIN LATERAL (
      SELECT o.id, o.ra, o.dec
      FROM objects o
      WHERE o.field = p_field
        AND o.ra BETWEEN op.ra - v_tolerance_deg AND op.ra + v_tolerance_deg
        AND o.dec BETWEEN op.dec - v_tolerance_deg AND op.dec + v_tolerance_deg
    ) o
    WHERE op.field = p_field
  ),
  best_match AS (
    SELECT phot_id, obj_id
    FROM candidates
    WHERE rn = 1 AND dist_deg <= v_tolerance_deg
  ),
  updated AS (
    UPDATE object_photometry op
    SET object_id = bm.obj_id
    FROM best_match bm
    WHERE op.id = bm.phot_id
    RETURNING op.id
  )
  SELECT COUNT(*) INTO n_relinked FROM updated;

  -- Set NULL for unmatched rows in this field
  UPDATE object_photometry
  SET object_id = NULL
  WHERE field = p_field
    AND id NOT IN (SELECT id FROM object_photometry WHERE field = p_field AND object_id IS NOT NULL);

  -- Count orphaned
  SELECT COUNT(*) INTO n_orphaned
  FROM object_photometry
  WHERE field = p_field AND object_id IS NULL;

  RETURN jsonb_build_object(
    'relinked', n_relinked,
    'orphaned', n_orphaned
  );
END;
$$;

GRANT EXECUTE ON FUNCTION public.relink_photometry_for_field(TEXT) TO service_role;


-- =============================================================================
-- sync_photometry_to_objects
-- (copy photo_z + has_photometry from object_photometry to objects)
-- =============================================================================

DROP FUNCTION IF EXISTS public.sync_photometry_to_objects(TEXT);

CREATE OR REPLACE FUNCTION public.sync_photometry_to_objects(p_field TEXT)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  n_updated INTEGER;
BEGIN
  -- Update objects that have linked photometry
  WITH phot AS (
    SELECT DISTINCT ON (op.object_id)
      op.object_id,
      op.photo_z,
      op.photo_z_err_lo,
      op.photo_z_err_hi
    FROM object_photometry op
    WHERE op.field = p_field AND op.object_id IS NOT NULL
    ORDER BY op.object_id, op.updated_at DESC
  )
  UPDATE objects o
  SET photo_z = phot.photo_z,
      photo_z_err_lo = phot.photo_z_err_lo,
      photo_z_err_hi = phot.photo_z_err_hi,
      has_photometry = TRUE
  FROM phot
  WHERE o.id = phot.object_id;

  GET DIAGNOSTICS n_updated = ROW_COUNT;

  -- Clear photometry flags for objects in this field that have no linked photometry
  UPDATE objects o
  SET photo_z = NULL,
      photo_z_err_lo = NULL,
      photo_z_err_hi = NULL,
      has_photometry = FALSE
  WHERE o.field = p_field
    AND o.has_photometry = TRUE
    AND NOT EXISTS (
      SELECT 1 FROM object_photometry op
      WHERE op.object_id = o.id
    );

  RETURN n_updated;
END;
$$;

GRANT EXECUTE ON FUNCTION public.sync_photometry_to_objects(TEXT) TO service_role;
