SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- NIRCam reviewer masks and triage status on the config plane (#709): the guarded
-- write of `campfire config push --masks` and the field deploy.
SET local check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.nircam_review_apply (
  p_field   text,
  p_changes jsonb
)
  RETURNS jsonb
  LANGUAGE plpgsql
  SET search_path TO 'public'
  AS $function$
DECLARE
  c            jsonb;
  v_channel    text;
  v_filter     text;
  v_filename   text;
  v_row        public.nircam_exposures%ROWTYPE;
  cur_value    jsonb;
  exp_value    jsonb;
  new_value    jsonb;
  v_applied    integer := 0;
  v_unchanged  integer := 0;
  v_refused    jsonb := '[]'::jsonb;
  v_missing    jsonb := '[]'::jsonb;
BEGIN
  IF NOT (COALESCE((SELECT public.is_admin()), false)
          OR COALESCE(auth.role(), '') = 'service_role'
          OR COALESCE(current_setting('request.jwt.claims', true), '') = '') THEN
    RAISE EXCEPTION 'nircam_review_apply: admin only' USING ERRCODE = '42501';
  END IF;
  IF p_field IS NULL OR p_field = '' THEN
    RAISE EXCEPTION 'nircam_review_apply: p_field is required' USING ERRCODE = '22023';
  END IF;
  IF p_changes IS NULL OR jsonb_typeof(p_changes) <> 'array' THEN
    RAISE EXCEPTION 'nircam_review_apply: p_changes must be a JSON array' USING ERRCODE = '22023';
  END IF;

  FOR c IN SELECT * FROM jsonb_array_elements(p_changes) LOOP
    v_channel  := c ->> 'channel';
    v_filter   := c ->> 'filter';
    v_filename := c ->> 'filename';
    IF v_channel IS NULL OR v_channel NOT IN ('mask', 'status') THEN
      RAISE EXCEPTION 'nircam_review_apply: channel must be mask or status, got %', COALESCE(v_channel, 'null')
        USING ERRCODE = '22023';
    END IF;
    IF v_filter IS NULL OR v_filter = '' OR v_filename IS NULL OR v_filename = '' THEN
      RAISE EXCEPTION 'nircam_review_apply: a change needs filter and filename' USING ERRCODE = '22023';
    END IF;

    -- JSON null and a missing key both mean "NULL" (for a mask: no mask).
    exp_value := NULLIF(c -> 'expect' -> 'value', 'null'::jsonb);
    new_value := NULLIF(c -> 'set' -> 'value', 'null'::jsonb);

    IF v_channel = 'mask' THEN
      IF new_value IS NOT NULL AND (jsonb_typeof(new_value) <> 'object'
                                    OR jsonb_typeof(new_value -> 'polygons') IS DISTINCT FROM 'array') THEN
        RAISE EXCEPTION 'nircam_review_apply: a mask is {"polygons": [...]} (% %)', v_filter, v_filename
          USING ERRCODE = '22023';
      END IF;
    ELSE
      IF new_value IS NULL OR jsonb_typeof(new_value) <> 'string'
         OR (new_value #>> '{}') NOT IN ('pending', 'approved', 'excluded') THEN
        RAISE EXCEPTION 'nircam_review_apply: status must be pending, approved or excluded (% %)', v_filter, v_filename
          USING ERRCODE = '22023';
      END IF;
    END IF;

    SELECT * INTO v_row FROM nircam_exposures
     WHERE field = p_field AND filter = v_filter AND filename = v_filename
       FOR UPDATE;
    IF NOT FOUND THEN
      v_missing := v_missing || jsonb_build_array(jsonb_build_object(
        'channel', v_channel, 'filter', v_filter, 'filename', v_filename));
      CONTINUE;
    END IF;

    IF v_channel = 'mask' THEN
      cur_value := NULLIF(v_row.mask_regions, 'null'::jsonb);
    ELSE
      cur_value := to_jsonb(v_row.review_status);
    END IF;

    IF cur_value IS NOT DISTINCT FROM new_value THEN
      v_unchanged := v_unchanged + 1;
      CONTINUE;
    END IF;

    IF cur_value IS DISTINCT FROM exp_value THEN
      v_refused := v_refused || jsonb_build_array(jsonb_build_object(
        'channel', v_channel, 'filter', v_filter, 'filename', v_filename,
        'current', cur_value));
      CONTINUE;
    END IF;

    IF v_channel = 'mask' THEN
      UPDATE nircam_exposures
         SET mask_regions = new_value, updated_at = now()
       WHERE id = v_row.id;
    ELSE
      UPDATE nircam_exposures
         SET review_status = new_value #>> '{}', review_decided_at = now(), updated_at = now()
       WHERE id = v_row.id;
    END IF;
    v_applied := v_applied + 1;
  END LOOP;

  RETURN jsonb_build_object('applied', v_applied, 'unchanged', v_unchanged,
                            'refused', v_refused, 'missing', v_missing);
END;
$function$;

REVOKE ALL ON FUNCTION "public"."nircam_review_apply"(text, jsonb) FROM PUBLIC, "anon";

COMMENT ON FUNCTION "public"."nircam_review_apply"(text, jsonb) IS 'Guarded write of NIRCam review state (campfire config push --masks, the field deploy): each change carries the value the client read (expect) and the value it wants (set) for one channel (mask -> mask_regions, status -> review_status) of one (filter, filename) of p_field, and lands only while the column still holds what was read, so an edit made in between by the portal is refused and reported instead of overwritten. Returns {applied, unchanged, refused: [{channel, filter, filename, current}], missing: [{channel, filter, filename}]}. Admin or service role.';

GRANT EXECUTE ON FUNCTION "public"."nircam_review_apply"(text, jsonb) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."nircam_review_apply"(text, jsonb) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."nircam_review_apply"(text, jsonb) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."nircam_review_apply"(text, jsonb) TO "service_role";
