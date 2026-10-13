SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.dr_diff (
  p_tag   text,
  p_limit integer DEFAULT 20
)
  RETURNS jsonb
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  SET statement_timeout TO '120s'
  AS $function$
DECLARE
  r dr_releases%ROWTYPE;
  v_limit int := LEAST(GREATEST(COALESCE(p_limit, 20), 0), 500);
  v_out jsonb;
BEGIN
  PERFORM dr_require_admin();
  SELECT * INTO r FROM dr_releases WHERE tag = p_tag;
  IF NOT FOUND THEN RAISE EXCEPTION 'no release %', p_tag; END IF;
  IF r.state IN ('draft', 'freezing', 'failed') THEN
    RAISE EXCEPTION 'release % is % (nothing frozen to compare)', p_tag, r.state;
  END IF;

  WITH
  sp AS (
    SELECT d.spectrum_id, d.program_slug, d.observation,
           CASE
             WHEN s.id IS NULL THEN 'spectrum_gone'
             WHEN s.deploy_status IS DISTINCT FROM 'published' THEN 'spectrum_unpublished'
             WHEN d.science_hash IS NOT NULL AND s.science_hash IS NOT NULL
                  AND d.science_hash <> s.science_hash THEN 'spectrum_science_changed'
             WHEN (d.science_hash IS NULL OR s.science_hash IS NULL)
                  AND d.file_hash IS DISTINCT FROM s.file_hash THEN 'spectrum_science_changed'
           END AS kind,
           jsonb_build_object('frozen', d.science_hash, 'live', s.science_hash,
                              'live_status', s.deploy_status) AS detail
    FROM dr_spectra d
    LEFT JOIN spectra s ON s.spectrum_id = d.spectrum_id
    WHERE d.tag = p_tag
  ),
  sp_new AS (
    SELECT s.spectrum_id, s.program_slug, s.observation, 'spectrum_new'::text AS kind,
           jsonb_build_object('live', s.science_hash) AS detail
    FROM spectra s
    WHERE s.deploy_status = 'published'
      AND (r.components ? 'nirspec')
      AND NOT EXISTS (SELECT 1 FROM dr_spectra d WHERE d.tag = p_tag AND d.spectrum_id = s.spectrum_id)
  ),
  ob AS (
    SELECT d.object_id, d.name, k.kind, k.detail
    FROM dr_objects d
    LEFT JOIN objects o ON o.id = d.object_id
    CROSS JOIN LATERAL (VALUES
      (CASE WHEN o.id IS NULL OR NOT o.is_active THEN 'object_gone' END,
       jsonb_build_object('live_active', o.is_active)),
      (CASE WHEN o.is_active AND o.object_id IS DISTINCT FROM d.name THEN 'object_renamed' END,
       jsonb_build_object('frozen', d.name, 'live', o.object_id)),
      (CASE WHEN o.is_active AND (o.redshift IS NULL) <> (d.redshift IS NULL)
             OR o.is_active AND abs(o.redshift - d.redshift) > 1e-6 THEN 'object_redshift_changed' END,
       jsonb_build_object('frozen', d.redshift, 'live', o.redshift)),
      (CASE WHEN o.is_active AND o.redshift_quality IS DISTINCT FROM d.redshift_quality THEN 'object_quality_changed' END,
       jsonb_build_object('frozen', d.redshift_quality, 'live', o.redshift_quality)),
      (CASE WHEN o.is_active AND COALESCE(o.needs_review, false)
                 AND NOT COALESCE((d.review_state->>'needs_review')::boolean, false) THEN 'review_raised' END,
       jsonb_build_object('live_reviewed', o.reviewed_spectrum_ids)),
      (CASE WHEN o.is_active AND NOT COALESCE(o.needs_review, false)
                 AND COALESCE((d.review_state->>'needs_review')::boolean, false) THEN 'review_cleared' END,
       '{}'::jsonb)
    ) AS k(kind, detail)
    WHERE d.tag = p_tag AND k.kind IS NOT NULL
  ),
  allk AS (
    SELECT kind, jsonb_build_object('spectrum_id', spectrum_id, 'program_slug', program_slug,
                                    'observation', observation) || detail AS item
    FROM sp WHERE kind IS NOT NULL
    UNION ALL
    SELECT kind, jsonb_build_object('spectrum_id', spectrum_id, 'program_slug', program_slug,
                                    'observation', observation) || detail
    FROM sp_new
    UNION ALL
    SELECT kind, jsonb_build_object('object_id', object_id, 'name', name) || detail FROM ob
  ),
  ranked AS (
    SELECT kind, item, count(*) OVER (PARTITION BY kind) AS n,
           row_number() OVER (PARTITION BY kind ORDER BY item::text) AS rn
    FROM allk
  )
  SELECT jsonb_build_object(
           'tag', p_tag, 'state', r.state, 'frozen_at', r.frozen_at,
           'counts', COALESCE((SELECT jsonb_object_agg(kind, n) FROM (SELECT DISTINCT kind, n FROM ranked) c), '{}'::jsonb),
           'samples', COALESCE((SELECT jsonb_object_agg(kind, items) FROM (
                                  SELECT kind, jsonb_agg(item ORDER BY rn) AS items
                                  FROM ranked WHERE rn <= v_limit GROUP BY kind) s), '{}'::jsonb))
    INTO v_out;
  RETURN v_out;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."dr_diff"(text, integer) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.dr_observation_references (
  p_observation text
)
  RETURNS TABLE (
    tag          text,
    state        text,
    n_spectra    bigint,
    n_members    bigint,
    n_unarchived bigint,
    blocks       boolean
  )
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
BEGIN
  PERFORM dr_require_admin();
  RETURN QUERY
  WITH sp AS (
    SELECT d.tag, d.spectrum_id FROM dr_spectra d WHERE d.observation = p_observation
  ),
  mem AS (
    SELECT m.tag, count(*) AS n, count(*) FILTER (WHERE m.byte_state NOT IN ('copied', 'stamped')) AS open
    FROM dr_members m
    JOIN sp ON sp.tag = m.tag AND sp.spectrum_id = m.spectrum_id
    GROUP BY m.tag
  )
  SELECT r.tag, r.state, (SELECT count(*) FROM sp WHERE sp.tag = r.tag),
         COALESCE(mem.n, 0), COALESCE(mem.open, 0),
         -- a published release keeps its observation live (plan §6 P4d); an
         -- unpublished one blocks only while bytes it froze are not archived yet
         (r.state = 'published' OR (r.state NOT IN ('withdrawn', 'failed') AND COALESCE(mem.open, 0) > 0))
  FROM dr_releases r
  LEFT JOIN mem ON mem.tag = r.tag
  WHERE EXISTS (SELECT 1 FROM sp WHERE sp.tag = r.tag)
  ORDER BY r.tag;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."dr_observation_references"(text) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.dr_publish (
  p_tag          text,
  p_doi          text DEFAULT NULL::text,
  p_known_issues text DEFAULT NULL::text
)
  RETURNS jsonb
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
DECLARE
  r dr_releases%ROWTYPE;
  v_state text;
BEGIN
  PERFORM dr_require_admin();
  SELECT * INTO r FROM dr_releases WHERE tag = p_tag FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'no release %', p_tag; END IF;
  IF p_doi IS NOT NULL AND p_doi !~ '^10\.[0-9]{4,9}/\S+$' THEN
    RAISE EXCEPTION 'not a DOI: % (expected 10.<registrant>/<suffix>)', p_doi;
  END IF;
  IF r.state = 'published' THEN
    RAISE EXCEPTION 'release % is already published', p_tag;
  END IF;
  -- Serialized with begin_deploy_scope (same lock): a deploy or a removal
  -- holding a live lease may be deleting or replacing what this release
  -- names, and a removal checks the releases only after taking its lease.
  -- Either the removal sees `published` and refuses, or this sees the lease
  -- (Codex on #602).
  PERFORM pg_advisory_xact_lock(hashtext('campfire.dr_freeze'));
  IF EXISTS (SELECT 1 FROM deploy_scope_state d
              WHERE d.in_progress_since IS NOT NULL
                AND COALESCE(d.heartbeat_at, d.in_progress_since) >= now() - deploy_lease_ttl()) THEN
    RAISE EXCEPTION 'a deploy or removal is in progress (live deploy lease); publish % when it has finished', p_tag;
  END IF;
  IF r.state <> 'withdrawn' THEN
    v_state := dr_derive_state(p_tag);
    IF v_state IS DISTINCT FROM 'ready' THEN
      RAISE EXCEPTION 'release % is % (publish needs ready: every export file built and every member archived)', p_tag, v_state;
    END IF;
  END IF;
  UPDATE dr_releases
     SET state = 'published', published_at = now(), published_by = auth.uid(),
         doi = COALESCE(p_doi, doi), known_issues = COALESCE(p_known_issues, known_issues),
         withdrawn_at = NULL, withdrawn_by = NULL, updated_at = now()
   WHERE tag = p_tag;
  INSERT INTO deploy_events (actor, action, affected_count, metadata)
  VALUES (auth.uid(), 'publish_dr', COALESCE((r.counts->>'spectra')::int, 0),
          jsonb_build_object('dr_tag', p_tag, 'doi', COALESCE(p_doi, r.doi), 'state_before', r.state));
  RETURN (SELECT to_jsonb(x) - 'snapshot_xmin' FROM dr_releases x WHERE x.tag = p_tag);
END;
$function$;

REVOKE ALL ON FUNCTION "public"."dr_publish"(text, text, text) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.dr_withdraw (
  p_tag    text,
  p_reason text
)
  RETURNS jsonb
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
DECLARE
  r dr_releases%ROWTYPE;
BEGIN
  PERFORM dr_require_admin();
  IF p_reason IS NULL OR length(btrim(p_reason)) = 0 THEN
    RAISE EXCEPTION 'a withdrawal needs a reason (it is recorded in known_issues)';
  END IF;
  SELECT * INTO r FROM dr_releases WHERE tag = p_tag FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'no release %', p_tag; END IF;
  IF r.state <> 'published' THEN
    RAISE EXCEPTION 'release % is % (only a published release is withdrawn; dr purge removes an unpublished one)', p_tag, r.state;
  END IF;
  UPDATE dr_releases
     SET state = 'withdrawn', withdrawn_at = now(), withdrawn_by = auth.uid(),
         known_issues = concat_ws(E'\n', known_issues,
                                  'Withdrawn ' || to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD') || ': ' || btrim(p_reason)),
         updated_at = now()
   WHERE tag = p_tag;
  INSERT INTO deploy_events (actor, action, affected_count, metadata)
  VALUES (auth.uid(), 'withdraw_dr', 0, jsonb_build_object('dr_tag', p_tag, 'reason', btrim(p_reason)));
  RETURN (SELECT to_jsonb(x) - 'snapshot_xmin' FROM dr_releases x WHERE x.tag = p_tag);
END;
$function$;

REVOKE ALL ON FUNCTION "public"."dr_withdraw"(text, text) FROM PUBLIC, "anon";

COMMENT ON FUNCTION "public"."dr_diff"(text, integer) IS 'What changed in live since a data release was frozen (P4d): counts and up to p_limit samples per kind -- spectrum_science_changed / spectrum_unpublished / spectrum_gone / spectrum_new, object_gone / object_renamed / object_redshift_changed / object_quality_changed, review_raised / review_cleared. Admin or service_role.';

COMMENT ON FUNCTION "public"."dr_observation_references"(text) IS 'Releases that froze spectra of an observation (P4d), with their member counts and how many still lack archived bytes; blocks = true when a destructive live change (deploy remove, a grating delete) must be refused: the release is published, or it is unpublished and some of those members are not archived yet. Admin or service_role.';

COMMENT ON FUNCTION "public"."dr_publish"(text, text, text) IS 'Publish a data release (P4d): ready (re-derived) or withdrawn -> published, with an optional DOI and known-issues text; a publish_dr deploy event. The dr_* policies show published tags to every non-link reader within their programs. Admin or service_role.';

COMMENT ON FUNCTION "public"."dr_withdraw"(text, text) IS 'Withdraw a published data release (P4d): published -> withdrawn, hidden from non-admins; rows and archive objects kept; the reason is appended to known_issues; a withdraw_dr deploy event. Admin or service_role.';

GRANT EXECUTE ON FUNCTION "public"."dr_diff"(text, integer) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."dr_diff"(text, integer) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_diff"(text, integer) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_diff"(text, integer) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."dr_observation_references"(text) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."dr_observation_references"(text) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_observation_references"(text) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_observation_references"(text) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."dr_publish"(text, text, text) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."dr_publish"(text, text, text) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_publish"(text, text, text) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_publish"(text, text, text) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."dr_withdraw"(text, text) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."dr_withdraw"(text, text) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_withdraw"(text, text) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_withdraw"(text, text) TO "service_role";
