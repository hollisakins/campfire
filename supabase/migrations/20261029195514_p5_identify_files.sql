SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.identify_files (
  p_files               jsonb,
  p_program_slugs       text[],
  p_include_unpublished boolean DEFAULT false
)
  RETURNS jsonb
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
DECLARE
  f jsonb;
  v_sha text;
  v_sci text;
  v_hex text;
  v_out jsonb := '[]'::jsonb;
  v_live jsonb;
  v_rel jsonb;
  v_col jsonb;
BEGIN
  IF p_files IS NULL OR jsonb_typeof(p_files) <> 'array' THEN
    RAISE EXCEPTION 'p_files must be a JSON array' USING ERRCODE = '22023';
  END IF;
  IF jsonb_array_length(p_files) > 200 THEN
    RAISE EXCEPTION 'at most 200 files per call' USING ERRCODE = '22023';
  END IF;

  FOR f IN SELECT * FROM jsonb_array_elements(p_files) LOOP
    v_sha := NULLIF(f->>'sha256', '');
    v_sci := NULLIF(f->>'science_hash', '');
    IF v_sha IS NOT NULL AND v_sha !~ '^sha256:[0-9a-f]{64}$' THEN v_sha := NULL; END IF;
    IF v_sci IS NOT NULL AND v_sci !~ '^sha256:[0-9a-f]{64}$' THEN v_sci := NULL; END IF;
    v_hex := substr(v_sha, 8);

    -- live spectra in scope
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
             'spectrum_id', s.spectrum_id, 'id', s.id, 'observation', s.observation,
             'program_slug', s.program_slug,
             -- the registry's (canonical) key of the final; fits_path is the legacy scheme
             'storage_key', COALESCE(so.storage_key, s.fits_path),
             'deploy_status', s.deploy_status,
             'file_hash', s.file_hash, 'science_hash', s.science_hash,
             'match', CASE WHEN v_sha IS NOT NULL AND s.file_hash = v_sha THEN 'file' ELSE 'science' END,
             'deployed_at', so.updated_at)
           ORDER BY s.spectrum_id), '[]'::jsonb)
      INTO v_live
      FROM spectra s
      LEFT JOIN LATERAL (SELECT so.storage_key, so.updated_at FROM storage_objects so
                          WHERE so.spectrum_id = s.spectrum_id AND so.product_type = 'nirspec_spec'
                            AND so.root = 'live' ORDER BY so.updated_at DESC LIMIT 1) so ON true
     WHERE ((v_sha IS NOT NULL AND s.file_hash = v_sha) OR (v_sci IS NOT NULL AND s.science_hash = v_sci))
       AND s.program_slug = ANY (p_program_slugs)
       AND (s.deploy_status = 'published' OR p_include_unpublished);

    -- live products that are not a spectrum's final (mosaics, sidecars, ...)
    -- by their registry hash, under the storage policy's API restatement
    -- (filter_accessible_storage_keys: the same scope the presign route
    -- applies); a final already listed through its spectrum is not repeated
    IF v_sha IS NOT NULL THEN
      SELECT v_live || COALESCE(jsonb_agg(jsonb_build_object(
               'storage_key', so.storage_key, 'product_type', so.product_type,
               'spectrum_id', so.spectrum_id, 'observation', so.observation, 'field', so.field,
               'file_hash', so.content_hash, 'science_hash', so.science_hash,
               'match', 'file', 'deployed_at', so.updated_at)
             ORDER BY so.storage_key), '[]'::jsonb)
        INTO v_live
        FROM storage_objects so
       WHERE so.root = 'live' AND so.status = 'active' AND so.content_hash = v_sha
         AND so.storage_key IN (SELECT fk.storage_key FROM filter_accessible_storage_keys(
               ARRAY(SELECT x.storage_key FROM storage_objects x
                      WHERE x.root = 'live' AND x.status = 'active' AND x.content_hash = v_sha),
               p_program_slugs, p_include_unpublished) fk)
         AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(v_live) l WHERE l->>'storage_key' = so.storage_key);
    END IF;

    -- release members in scope: through the frozen spectrum (program rows) or
    -- the manifest alone (field products, open to every account)
    WITH visible AS (
      SELECT r.tag, r.state, r.published_at
        FROM dr_releases r
       WHERE r.state = 'published'
          OR (p_include_unpublished AND r.state NOT IN ('draft', 'freezing', 'failed'))
    ),
    hits AS (
      SELECT v.tag, v.state, v.published_at,
             m.storage_key, m.program_slug, m.product_type, m.spectrum_id AS spectrum_name,
             m.archive_key, m.byte_state, m.stamped_hash, m.content_hash, m.science_hash,
             ds.id AS spectrum_db_id, ds.science_hash AS frozen_science_hash, ds.file_hash AS frozen_file_hash,
             CASE WHEN v_sha IS NOT NULL AND m.stamped_hash = v_sha THEN 'stamped'
                  WHEN v_sha IS NOT NULL AND (m.content_hash = v_sha OR ds.file_hash = v_sha) THEN 'file'
                  ELSE 'science' END AS match
        FROM visible v
        JOIN dr_manifest m ON m.tag = v.tag
        -- the frozen spectrum row describes the spectrum FILE only: its
        -- sidecars (JSON, zfit, line fit) share the spectrum_id but are other
        -- bytes, matched by their own manifest hashes (local Codex)
        LEFT JOIN dr_spectra ds ON ds.tag = m.tag AND ds.spectrum_id = m.spectrum_id
                                AND m.product_type = 'nirspec_spec'
       WHERE ((v_sha IS NOT NULL AND (m.stamped_hash = v_sha OR m.content_hash = v_sha OR ds.file_hash = v_sha))
              OR (v_sci IS NOT NULL AND (m.science_hash = v_sci OR ds.science_hash = v_sci)))
         AND (m.program_slug IS NULL OR m.program_slug = ANY (p_program_slugs))
    )
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
             'tag', h.tag, 'state', h.state, 'published_at', h.published_at,
             'storage_key', h.storage_key, 'program_slug', h.program_slug,
             'product_type', h.product_type, 'spectrum_id', h.spectrum_name,
             'archive_key', h.archive_key, 'byte_state', h.byte_state,
             'stamped_hash', h.stamped_hash, 'content_hash', h.content_hash,
             'science_hash', COALESCE(h.frozen_science_hash, h.science_hash),
             'match', h.match,
             -- current: the live spectrum still carries the frozen science
             -- hash; when either side has none (rows that predate it), the
             -- whole-file hash decides, as dr_diff does (local Codex). Keyed
             -- on the frozen spectrum NAME, as dr_diff is: an observation
             -- removed and redeployed gets new numeric ids (remote Codex)
             'current', CASE WHEN h.spectrum_db_id IS NULL THEN NULL
                             ELSE EXISTS (SELECT 1 FROM spectra ls
                                           WHERE ls.spectrum_id = h.spectrum_name
                                             AND ls.deploy_status = 'published'
                                             AND COALESCE(ls.science_hash = h.frozen_science_hash,
                                                          ls.file_hash = h.frozen_file_hash)) END,
             'live_deployed_at', (SELECT so.updated_at FROM storage_objects so
                                   WHERE so.storage_key = h.storage_key AND so.root = 'live' LIMIT 1))
           ORDER BY h.tag, h.storage_key), '[]'::jsonb)
      INTO v_rel
      FROM hits h;

    -- tombstones of archived copies of these bytes
    SELECT COALESCE(jsonb_agg(jsonb_build_object('storage_key', c.storage_key, 'collected_at', c.collected_at)
                              ORDER BY c.collected_at DESC), '[]'::jsonb)
      INTO v_col
      FROM archive_collections c
     WHERE v_hex IS NOT NULL AND c.storage_key LIKE 'archive/' || v_hex || '/%';

    v_out := v_out || jsonb_build_array(jsonb_build_object(
      'sha256', v_sha, 'science_hash', v_sci,
      'live', v_live, 'releases', v_rel, 'collected', v_col));
  END LOOP;
  RETURN v_out;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."identify_files"(jsonb, text[], boolean) FROM PUBLIC, "anon", "authenticated";

COMMENT ON FUNCTION "public"."identify_files"(jsonb, text[], boolean) IS 'What the archive knows about files by their bytes (P5-2, campfire identify): per input {sha256, science_hash}, the live spectra, the release members (with current = the live spectrum still carries the frozen science hash) and the archive tombstones matching, under the caller''s explicit program scope (p_include_unpublished = admin: drafts and every release past its freeze). The client phrases the answer.';

REVOKE ALL ON FUNCTION "public"."identify_files"(jsonb, text[], boolean) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."identify_files"(jsonb, text[], boolean) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."identify_files"(jsonb, text[], boolean) TO "service_role";
