SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

DROP INDEX "public"."idx_dr_members_tag_pending";

CREATE OR REPLACE FUNCTION public.dr_bytes_mark (
  p_tag     text,
  p_results jsonb
)
  RETURNS jsonb
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
DECLARE
  v_state text;
  e jsonb;
  m dr_members%ROWTYPE;
  v_new text; v_key text; v_hash text;
  n_marked int := 0; n_registered int := 0; n_failed int := 0;
  n_copied int := 0; n_stamped int := 0; n_pending int := 0;
  v_partitions text[] := '{}';
  v_rows int;
BEGIN
  PERFORM dr_require_admin();
  IF jsonb_typeof(p_results) IS DISTINCT FROM 'array' THEN
    RAISE EXCEPTION 'results must be a json array';
  END IF;
  IF jsonb_array_length(p_results) > 2000 THEN
    RAISE EXCEPTION 'at most 2000 results per call (got %)', jsonb_array_length(p_results);
  END IF;
  SELECT state INTO v_state FROM dr_releases WHERE tag = p_tag FOR UPDATE;
  IF v_state IS NULL THEN RAISE EXCEPTION 'no release %', p_tag; END IF;
  IF v_state NOT IN ('frozen', 'exporting', 'exported', 'copying', 'ready') THEN
    RAISE EXCEPTION 'release % is % (the byte pass needs a frozen, unpublished release)', p_tag, v_state;
  END IF;

  FOR e IN SELECT * FROM jsonb_array_elements(p_results) LOOP
    v_new := e->>'state';
    v_key := e->>'archive_key';
    v_hash := e->>'stamped_hash';
    IF v_new IS NULL OR v_new NOT IN ('pending', 'copied', 'stamped', 'failed') THEN
      RAISE EXCEPTION 'invalid byte state % for %', v_new, e->>'storage_key';
    END IF;
    IF v_new IN ('copied', 'stamped') AND (v_key IS NULL OR v_key NOT LIKE 'archive/%') THEN
      RAISE EXCEPTION 'a % member needs an archive key under archive/ (%)', v_new, e->>'storage_key';
    END IF;
    IF v_new = 'stamped' AND (v_hash IS NULL OR v_hash !~ '^sha256:[0-9a-f]{64}$') THEN
      RAISE EXCEPTION 'a stamped member needs the sha256 of the stamped bytes (%)', e->>'storage_key';
    END IF;
    SELECT * INTO m FROM dr_members WHERE tag = p_tag AND storage_key = e->>'storage_key' FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'no member % in %', e->>'storage_key', p_tag; END IF;
    IF v_new = 'stamped' AND m.product_type NOT IN ('nirspec_spec', 'nirspec_lines') THEN
      RAISE EXCEPTION 'only NIRSpec FITS finals are stamped (% is %)', m.storage_key, m.product_type;
    END IF;
    -- A science hash the pass computed for a member frozen without one is kept
    -- (member + archive row), so later releases can reuse the stamped object
    -- (Codex on #601). A frozen hash is never overwritten.
    IF e->>'science_hash' IS NOT NULL THEN
      IF e->>'science_hash' !~ '^sha256:[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'malformed science hash for %', m.storage_key;
      END IF;
      IF m.science_hash IS NOT NULL AND m.science_hash <> e->>'science_hash' THEN
        RAISE EXCEPTION 'science hash of % disagrees with the frozen one', m.storage_key;
      END IF;
      m.science_hash := COALESCE(m.science_hash, e->>'science_hash');
    END IF;

    -- A newly created archive object gets its registry row (an existing row:
    -- the same object reused, or a crash after the insert, is kept as it is;
    -- archive rows are immutable).
    IF v_new IN ('copied', 'stamped') AND (e->'registry') IS NOT NULL AND jsonb_typeof(e->'registry') = 'object' THEN
      INSERT INTO storage_objects (backend, bucket, storage_key, root, status, content_hash, science_hash,
                                   size_bytes, stored_size_bytes, content_type, product_type, instrument,
                                   observation, field, filter, spectrum_id, program_slug, deployment_id,
                                   cfpipe_version, archived_from_key, archived_at, uploaded_by, updated_at)
      VALUES (m.backend, m.bucket, v_key, 'archive', 'archived',
              CASE WHEN v_new = 'stamped' THEN v_hash ELSE m.content_hash END,
              m.science_hash,
              COALESCE((e->'registry'->>'size_bytes')::bigint, m.size_bytes),
              (e->'registry'->>'stored_size_bytes')::bigint,
              COALESCE(e->'registry'->>'content_type', 'application/octet-stream'),
              m.product_type, e->'registry'->>'instrument',
              -- provenance only: an observation or deployment removed since
              -- the freeze must not abort the batch on its foreign key
              (SELECT o.name FROM observations o WHERE o.name = e->'registry'->>'observation'),
              m.field, e->'registry'->>'filter', m.spectrum_id, m.program_slug,
              (SELECT d.id FROM deployments d WHERE d.id = (e->'registry'->>'deployment_id')::integer),
              e->'registry'->>'cfpipe_version',
              m.storage_key, now(), auth.uid(), now())
      ON CONFLICT (backend, bucket, storage_key) DO NOTHING;
      GET DIAGNOSTICS v_rows = ROW_COUNT;
      n_registered := n_registered + v_rows;
    END IF;
    IF v_new IN ('copied', 'stamped') AND NOT EXISTS (
         SELECT 1 FROM storage_objects so
         WHERE so.backend = m.backend AND so.bucket = m.bucket AND so.storage_key = v_key
           AND so.root = 'archive' AND so.status = 'archived') THEN
      RAISE EXCEPTION 'archive object % is not registered (pass its registry row)', v_key;
    END IF;

    UPDATE dr_members
       SET byte_state = v_new,
           science_hash = m.science_hash,
           archive_key = CASE WHEN v_new IN ('copied', 'stamped') THEN v_key END,
           stamped_hash = CASE WHEN v_new = 'stamped' THEN v_hash END,
           byte_error = CASE WHEN v_new = 'failed' THEN left(COALESCE(e->>'error', 'failed'), 2000) END,
           updated_at = now()
     WHERE tag = p_tag AND storage_key = m.storage_key;
    n_marked := n_marked + 1;
    CASE v_new WHEN 'copied' THEN n_copied := n_copied + 1;
               WHEN 'stamped' THEN n_stamped := n_stamped + 1;
               WHEN 'failed' THEN n_failed := n_failed + 1;
               ELSE n_pending := n_pending + 1; END CASE;
    v_partitions := array_append(v_partitions, COALESCE(m.program_slug, m.component));
  END LOOP;

  -- Built member files no longer match the rows: back to pending.
  UPDATE dr_exports
     SET state = 'pending', sha256 = NULL, size_bytes = NULL, rows = NULL, built_at = NULL, error = NULL
   -- `building` too: a builder mid-way through this file read the old rows,
   -- and dr_export_mark refuses its ready mark once the row is not building
   WHERE tag = p_tag AND stream = 'members' AND state IN ('ready', 'building') AND partition = ANY (v_partitions);

  IF n_copied + n_stamped > 0 THEN
    INSERT INTO deploy_events (actor, action, affected_count, metadata)
    VALUES (auth.uid(), 'archive', n_copied + n_stamped,
            jsonb_build_object('dr_tag', p_tag, 'copied', n_copied, 'stamped', n_stamped,
                               'registered', n_registered, 'failed', n_failed));
  END IF;

  RETURN jsonb_build_object('tag', p_tag, 'marked', n_marked, 'copied', n_copied, 'stamped', n_stamped,
                            'failed', n_failed, 'pending', n_pending, 'registered', n_registered,
                            'state', dr_derive_state(p_tag));
END;
$function$;

REVOKE ALL ON FUNCTION "public"."dr_bytes_mark"(text, jsonb) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.dr_bytes_pending (
  p_tag            text,
  p_after          text    DEFAULT NULL::text,
  p_limit          integer DEFAULT 500,
  p_include_failed boolean DEFAULT false,
  p_prefix         text    DEFAULT NULL::text
)
  RETURNS TABLE (
    storage_key        text,
    product_type       text,
    component          text,
    program_slug       text,
    field              text,
    spectrum_id        text,
    backend            text,
    bucket             text,
    content_hash       text,
    science_hash       text,
    size_bytes         bigint,
    byte_state         text,
    source_key         text,
    source_root        text,
    source_stored_size bigint,
    reuse_archive_key  text,
    reuse_stamped_hash text,
    reuse_size_bytes   bigint,
    registry           jsonb,
    frozen_at          timestamp with time zone,
    db_now             timestamp with time zone
  )
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
BEGIN
  PERFORM dr_require_admin();
  IF NOT EXISTS (SELECT 1 FROM dr_releases r WHERE r.tag = p_tag) THEN
    RAISE EXCEPTION 'no release %', p_tag;
  END IF;
  RETURN QUERY
  WITH page AS (
    SELECT m.* FROM dr_members m
    WHERE m.tag = p_tag
      AND m.byte_state IN ('pending', 'failed')
      AND (m.byte_state = 'pending' OR p_include_failed)
      AND (p_after IS NULL OR m.storage_key > p_after)
      -- one observation / field first (the S gate): a key prefix
      AND (p_prefix IS NULL OR (m.storage_key >= p_prefix AND left(m.storage_key, length(p_prefix)) = p_prefix))
    ORDER BY m.storage_key
    LIMIT LEAST(GREATEST(COALESCE(p_limit, 500), 1), 2000)
  )
  SELECT pg.storage_key, pg.product_type, pg.component, pg.program_slug, pg.field,
         pg.spectrum_id, pg.backend, pg.bucket, pg.content_hash, pg.science_hash,
         pg.size_bytes, pg.byte_state,
         CASE WHEN live.id IS NOT NULL THEN live.storage_key ELSE ret.storage_key END,
         CASE WHEN live.id IS NOT NULL THEN 'live' WHEN ret.id IS NOT NULL THEN 'archive' END,
         CASE WHEN live.id IS NOT NULL THEN COALESCE(live.stored_size_bytes, live.size_bytes)
              ELSE COALESCE(ret.stored_size_bytes, ret.size_bytes) END,
         reuse.archive_key, reuse.stamped_hash, reuse.size_bytes,
         jsonb_build_object(
           'content_type', COALESCE(live.content_type, ret.content_type, src.content_type),
           'stored_size_bytes', COALESCE(live.stored_size_bytes, ret.stored_size_bytes),
           'instrument', COALESCE(live.instrument, ret.instrument, src.instrument),
           'observation', COALESCE(live.observation, ret.observation, src.observation),
           'filter', COALESCE(live.filter, ret.filter, src.filter),
           'deployment_id', COALESCE(live.deployment_id, ret.deployment_id, src.deployment_id),
           'cfpipe_version', COALESCE(live.cfpipe_version, ret.cfpipe_version, src.cfpipe_version),
           'retained_key', ret.storage_key),
         -- the freeze's snapshot time: a live object last modified before it
         -- still holds the frozen bytes; one modified since must be verified
         -- by its hash before it is archived (Codex on #601)
         (SELECT r.frozen_at FROM dr_releases r WHERE r.tag = p_tag),
         -- the database clock, so the pass can measure its skew against the
         -- object store's and size the window around frozen_at to it
         clock_timestamp()
  FROM page pg
  -- the registry row the freeze captured (its metadata survives a redeploy)
  LEFT JOIN storage_objects src ON src.id = pg.registry_id
  -- the live object, while it still holds the frozen bytes
  LEFT JOIN LATERAL (
    SELECT so.* FROM storage_objects so
    WHERE so.backend = pg.backend AND so.bucket = pg.bucket AND so.storage_key = pg.storage_key
      AND so.root = 'live' AND so.status = 'active' AND so.content_hash = pg.content_hash
    LIMIT 1
  ) live ON true
  -- an unstamped archive copy of exactly the frozen bytes (retain-on-supersede,
  -- or an earlier release's server-side copy)
  LEFT JOIN LATERAL (
    SELECT so.* FROM storage_objects so
    WHERE so.root = 'archive' AND so.status = 'archived'
      AND so.content_hash = pg.content_hash AND so.archived_from_key = pg.storage_key
    ORDER BY so.id
    LIMIT 1
  ) ret ON true
  -- a stamped object another release made from the same science (D-P4-2)
  LEFT JOIN LATERAL (
    SELECT d.archive_key, d.stamped_hash, COALESCE(so.stored_size_bytes, so.size_bytes) AS size_bytes FROM dr_members d
    JOIN storage_objects so ON so.root = 'archive' AND so.status = 'archived'
                           AND so.storage_key = d.archive_key
    WHERE pg.product_type IN ('nirspec_spec', 'nirspec_lines') AND pg.science_hash IS NOT NULL
      AND d.science_hash = pg.science_hash AND d.storage_key = pg.storage_key
      AND d.tag <> pg.tag AND d.byte_state = 'stamped'
    ORDER BY d.updated_at
    LIMIT 1
  ) reuse ON true
  ORDER BY pg.storage_key;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."dr_bytes_pending"(text, text, integer, boolean, text) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.dr_bytes_summary (
  p_tag text
)
  RETURNS jsonb
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
DECLARE
  out jsonb;
BEGIN
  PERFORM dr_require_admin();
  SELECT jsonb_build_object(
    'tag', p_tag,
    'state', (SELECT state FROM dr_releases WHERE tag = p_tag),
    'members', count(*),
    'by_state', (SELECT COALESCE(jsonb_object_agg(s.byte_state, jsonb_build_object('count', s.n, 'bytes', s.b)), '{}'::jsonb)
                 FROM (SELECT byte_state, count(*) AS n, COALESCE(sum(size_bytes), 0) AS b
                       FROM dr_members WHERE tag = p_tag GROUP BY byte_state) s),
    'pending_bytes', COALESCE(sum(size_bytes) FILTER (WHERE byte_state IN ('pending', 'failed')), 0),
    'failed_sample', (SELECT COALESCE(jsonb_agg(jsonb_build_object('storage_key', f.storage_key, 'error', f.byte_error)), '[]'::jsonb)
                      FROM (SELECT storage_key, byte_error FROM dr_members
                            WHERE tag = p_tag AND byte_state = 'failed' ORDER BY storage_key LIMIT 10) f))
  INTO out
  FROM dr_members WHERE tag = p_tag;
  RETURN out;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."dr_bytes_summary"(text) FROM PUBLIC, "anon";

CREATE INDEX idx_dr_members_tag_pending ON public.dr_members USING btree (TAG, storage_key)
  WHERE (byte_state = ANY (ARRAY['pending'::text, 'failed'::text]));

COMMENT ON FUNCTION "public"."dr_bytes_mark"(text, jsonb) IS 'Record a batch (at most 2000) of data-release byte-pass outcomes (P4c): [{storage_key, state copied|stamped|failed|pending, archive_key, stamped_hash, error, registry}]; inserts the archive registry row for each newly created object (registry given), refuses a copied/stamped member whose archive object is not registered, sends the touched member partitions'' export files back to pending, logs one archive deploy event and derives the release state once. Admin or service_role.';

COMMENT ON FUNCTION "public"."dr_bytes_pending"(text, text, integer, boolean, text) IS 'One keyset page (by storage_key, at most 2000) of a data release''s members still to archive (P4c): pending, plus failed when asked, optionally under one key prefix; per member the source of its frozen bytes (the live key while it still holds them, else the retained archive copy, else NULL), a stamped archive object of the same science from another release to reuse, and the registry metadata for the archive row. Admin or service_role.';

COMMENT ON FUNCTION "public"."dr_bytes_summary"(text) IS 'Byte-pass progress of a data release (P4c): members per byte_state with their bytes, the bytes still pending, a sample of failures. Admin or service_role.';

GRANT EXECUTE ON FUNCTION "public"."dr_bytes_mark"(text, jsonb) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."dr_bytes_mark"(text, jsonb) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_bytes_mark"(text, jsonb) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_bytes_mark"(text, jsonb) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."dr_bytes_pending"(text, text, integer, boolean, text) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."dr_bytes_pending"(text, text, integer, boolean, text) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_bytes_pending"(text, text, integer, boolean, text) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_bytes_pending"(text, text, integer, boolean, text) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."dr_bytes_summary"(text) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."dr_bytes_summary"(text) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_bytes_summary"(text) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_bytes_summary"(text) TO "service_role";
