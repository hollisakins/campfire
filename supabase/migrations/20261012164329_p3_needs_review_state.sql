SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

ALTER TABLE "public"."objects"
  ADD COLUMN "reviewed_spectrum_ids" integer[];

ALTER TABLE "public"."objects"
  ADD COLUMN "needs_review" boolean NOT NULL DEFAULT false;

CREATE OR REPLACE FUNCTION public.recompute_needs_review (
  p_object_ids integer[]
)
  RETURNS integer
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
DECLARE
  v_n integer := 0;
BEGIN
  IF p_object_ids IS NULL OR cardinality(p_object_ids) = 0 THEN
    RETURN 0;
  END IF;

  WITH ids AS (
    SELECT DISTINCT x AS id FROM unnest(p_object_ids) AS x WHERE x IS NOT NULL
  ),
  current_members AS (
    SELECT t.object_id AS id, array_agg(s.id ORDER BY s.id) AS ids
    FROM targets t
    JOIN spectra s ON s.target_id = t.target_id
    WHERE t.object_id IN (SELECT id FROM ids)
      AND s.deploy_status = 'published'
    GROUP BY t.object_id
  ),
  flags AS (
    SELECT o.id,
           (o.is_active
            AND o.last_inspected_at IS NOT NULL
            AND o.reviewed_spectrum_ids IS NOT NULL
            AND o.redshift_quality BETWEEN 1 AND 3
            AND COALESCE(c.ids, '{}'::integer[])
                IS DISTINCT FROM
                (SELECT COALESCE(array_agg(DISTINCT r ORDER BY r), '{}'::integer[])
                 FROM unnest(o.reviewed_spectrum_ids) AS r)
           ) AS flag
    FROM objects o
    JOIN ids ON ids.id = o.id
    LEFT JOIN current_members c ON c.id = o.id
  ),
  upd AS (
    UPDATE objects o
    SET needs_review = f.flag,
        updated_at = now()
    FROM flags f
    WHERE o.id = f.id
      AND o.needs_review IS DISTINCT FROM f.flag
    RETURNING o.id
  )
  SELECT count(*) INTO v_n FROM upd;

  RETURN v_n;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."recompute_needs_review"(integer[]) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.recompute_needs_review_from_objects()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
BEGIN
    -- The rule's own inputs: quality, sign-off (last_inspected_at, which
    -- stamp_reviewed_spectra turns into a fresh reviewed set — in the list so
    -- "mark as reviewed" recomputes too), the reviewed set, activity.
    PERFORM public.recompute_needs_review(COALESCE(ARRAY(
        SELECT n.id
        FROM old_rows o
        JOIN new_rows n ON n.id = o.id
        WHERE o.redshift_quality IS DISTINCT FROM n.redshift_quality
           OR o.last_inspected_at IS DISTINCT FROM n.last_inspected_at
           OR o.reviewed_spectrum_ids IS DISTINCT FROM n.reviewed_spectrum_ids
           OR o.is_active IS DISTINCT FROM n.is_active), '{}'::integer[]));
    RETURN NULL;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."recompute_needs_review_from_objects"() FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.recompute_needs_review_from_spectra()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
DECLARE
    v_ids integer[] := '{}';
BEGIN
    IF TG_OP = 'INSERT' THEN
        v_ids := COALESCE(ARRAY(
            SELECT DISTINCT t.object_id FROM new_rows n
            JOIN targets t ON t.target_id = n.target_id
            WHERE t.object_id IS NOT NULL), '{}'::integer[]);
    ELSIF TG_OP = 'DELETE' THEN
        v_ids := COALESCE(ARRAY(
            SELECT DISTINCT t.object_id FROM old_rows o
            JOIN targets t ON t.target_id = o.target_id
            WHERE t.object_id IS NOT NULL), '{}'::integer[]);
    ELSE
        -- Only a publication flip or a re-targeting changes an object's
        -- published member set; everything else (hashes, flags, timestamps)
        -- is not a membership event.
        v_ids := COALESCE(ARRAY(
            SELECT DISTINCT t.object_id
            FROM old_rows o
            JOIN new_rows n ON n.id = o.id
            JOIN targets t ON t.target_id IN (o.target_id, n.target_id)
            WHERE t.object_id IS NOT NULL
              AND (o.deploy_status IS DISTINCT FROM n.deploy_status
                   OR o.target_id IS DISTINCT FROM n.target_id)), '{}'::integer[]);
    END IF;
    PERFORM public.recompute_needs_review(v_ids);
    RETURN NULL;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."recompute_needs_review_from_spectra"() FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.recompute_needs_review_from_targets()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
DECLARE
    v_ids integer[] := '{}';
BEGIN
    IF TG_OP = 'DELETE' THEN
        v_ids := COALESCE(ARRAY(
            SELECT DISTINCT o.object_id FROM old_rows o WHERE o.object_id IS NOT NULL), '{}'::integer[]);
    ELSE
        -- A target changing object: both the losing and the receiving object.
        v_ids := COALESCE(ARRAY(
            SELECT DISTINCT x.object_id
            FROM old_rows o
            JOIN new_rows n ON n.id = o.id
            CROSS JOIN LATERAL (VALUES (o.object_id), (n.object_id)) AS x(object_id)
            WHERE o.object_id IS DISTINCT FROM n.object_id
              AND x.object_id IS NOT NULL), '{}'::integer[]);
    END IF;
    PERFORM public.recompute_needs_review(v_ids);
    RETURN NULL;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."recompute_needs_review_from_targets"() FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.seed_reviewed_spectra (
  p_after_id   integer   DEFAULT 0,
  p_limit      integer   DEFAULT 1000,
  p_object_ids integer[] DEFAULT NULL::integer[],
  p_dry_run    boolean   DEFAULT false
)
  RETURNS json
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
DECLARE
  v_ids integer[];
  v_seeded integer := 0;
  v_flipped integer := 0;
BEGIN
  IF NOT (public.is_admin() OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  SELECT COALESCE(array_agg(id ORDER BY id), '{}'::integer[]) INTO v_ids
  FROM (
    SELECT o.id
    FROM objects o
    WHERE o.id > COALESCE(p_after_id, 0)
      AND (p_object_ids IS NULL OR o.id = ANY(p_object_ids))
      AND o.last_inspected_at IS NOT NULL
      AND o.reviewed_spectrum_ids IS NULL
    ORDER BY o.id
    LIMIT GREATEST(COALESCE(p_limit, 1000), 1)
  ) page;

  IF cardinality(v_ids) = 0 THEN
    RETURN json_build_object('scanned', 0, 'seeded', 0, 'flipped', 0, 'last_id', NULL);
  END IF;
  IF p_dry_run THEN
    RETURN json_build_object('scanned', cardinality(v_ids), 'seeded', 0, 'flipped', 0,
                             'last_id', v_ids[cardinality(v_ids)]);
  END IF;

  WITH upd AS (
    UPDATE objects o
    SET reviewed_spectrum_ids = COALESCE((
          SELECT array_agg(s.id ORDER BY s.id)
          FROM targets t
          JOIN spectra s ON s.target_id = t.target_id
          WHERE t.object_id = o.id
            AND s.deploy_status = 'published'
            -- spectra.created_at is timestamp WITHOUT time zone, written by now()
            -- on a UTC server; compare it as UTC rather than via the session zone.
            AND (s.created_at AT TIME ZONE 'UTC') <= o.last_inspected_at
        ), '{}'::integer[])
    WHERE o.id = ANY(v_ids)
    RETURNING o.id
  )
  SELECT count(*) INTO v_seeded FROM upd;

  v_flipped := public.recompute_needs_review(v_ids);
  RETURN json_build_object('scanned', cardinality(v_ids), 'seeded', v_seeded, 'flipped', v_flipped,
                           'last_id', v_ids[cardinality(v_ids)]);
END;
$function$;

REVOKE ALL ON FUNCTION "public"."seed_reviewed_spectra"(integer, integer, integer[], boolean) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.stamp_reviewed_spectra()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
BEGIN
    IF NEW.last_inspected_at IS NOT NULL
       AND NEW.last_inspected_at IS DISTINCT FROM OLD.last_inspected_at THEN
        NEW.reviewed_spectrum_ids := COALESCE((
            SELECT array_agg(s.id ORDER BY s.id)
            FROM targets t
            JOIN spectra s ON s.target_id = t.target_id
            WHERE t.object_id = NEW.id
              AND s.deploy_status = 'published'
        ), '{}'::integer[]);
        NEW.needs_review := false;
    END IF;
    RETURN NEW;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."stamp_reviewed_spectra"() FROM PUBLIC, "anon";

CREATE INDEX idx_objects_needs_review ON public.objects USING btree (id)
  WHERE needs_review;

CREATE TRIGGER recompute_needs_review_objects_update
  AFTER UPDATE ON public.objects REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
  FOR EACH STATEMENT
  EXECUTE FUNCTION public.recompute_needs_review_from_objects();

CREATE TRIGGER stamp_reviewed_spectra_trigger
  BEFORE UPDATE OF last_inspected_at ON public.objects
  FOR EACH ROW
  EXECUTE FUNCTION public.stamp_reviewed_spectra();

CREATE TRIGGER recompute_needs_review_spectra_delete
  AFTER DELETE ON public.spectra REFERENCING OLD TABLE AS old_rows
  FOR EACH STATEMENT
  EXECUTE FUNCTION public.recompute_needs_review_from_spectra();

CREATE TRIGGER recompute_needs_review_spectra_insert
  AFTER INSERT ON public.spectra REFERENCING NEW TABLE AS new_rows
  FOR EACH STATEMENT
  EXECUTE FUNCTION public.recompute_needs_review_from_spectra();

CREATE TRIGGER recompute_needs_review_spectra_update
  AFTER UPDATE ON public.spectra REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
  FOR EACH STATEMENT
  EXECUTE FUNCTION public.recompute_needs_review_from_spectra();

CREATE TRIGGER recompute_needs_review_targets_delete
  AFTER DELETE ON public.targets REFERENCING OLD TABLE AS old_rows
  FOR EACH STATEMENT
  EXECUTE FUNCTION public.recompute_needs_review_from_targets();

CREATE TRIGGER recompute_needs_review_targets_update
  AFTER UPDATE ON public.targets REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
  FOR EACH STATEMENT
  EXECUTE FUNCTION public.recompute_needs_review_from_targets();

COMMENT ON COLUMN "public"."objects"."needs_review" IS 'Needs-review (P3): true iff the object was signed off at quality 1-3 and its published member spectrum set differs from reviewed_spectrum_ids (a spectrum the inspector never saw joined, or one they saw left). Quality 4 and reprocessing never badge. Derived: maintained by recompute_needs_review() via statement triggers; supersedes the staleness_reason / last_data_change_at predicate.';

COMMENT ON COLUMN "public"."objects"."reviewed_spectrum_ids" IS 'Needs-review (P3): ids of the published member spectra at the last sign-off, sorted. Stamped by the stamp_reviewed_spectra trigger whenever last_inspected_at changes; NULL until the object is signed off (or seeded by scripts/backfill_reviewed_spectra.py). Never written by clients.';

GRANT EXECUTE ON FUNCTION "public"."recompute_needs_review"(integer[]) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."recompute_needs_review"(integer[]) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."recompute_needs_review"(integer[]) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."recompute_needs_review"(integer[]) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."recompute_needs_review_from_objects"() TO "authenticated";

REVOKE ALL ON FUNCTION "public"."recompute_needs_review_from_objects"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."recompute_needs_review_from_objects"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."recompute_needs_review_from_objects"() TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."recompute_needs_review_from_spectra"() TO "authenticated";

REVOKE ALL ON FUNCTION "public"."recompute_needs_review_from_spectra"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."recompute_needs_review_from_spectra"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."recompute_needs_review_from_spectra"() TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."recompute_needs_review_from_targets"() TO "authenticated";

REVOKE ALL ON FUNCTION "public"."recompute_needs_review_from_targets"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."recompute_needs_review_from_targets"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."recompute_needs_review_from_targets"() TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."seed_reviewed_spectra"(integer, integer, integer[], boolean) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."seed_reviewed_spectra"(integer, integer, integer[], boolean) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."seed_reviewed_spectra"(integer, integer, integer[], boolean) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."seed_reviewed_spectra"(integer, integer, integer[], boolean) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."stamp_reviewed_spectra"() TO "authenticated";

REVOKE ALL ON FUNCTION "public"."stamp_reviewed_spectra"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."stamp_reviewed_spectra"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."stamp_reviewed_spectra"() TO "service_role";
