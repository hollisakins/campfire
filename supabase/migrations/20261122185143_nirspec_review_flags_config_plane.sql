SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- NIRSpec stuck-shutter / background-override lists on the config plane.
--   nirspec_source_review.stuck_source: the provenance tag of a stuck entry
--     (hand / web / auto), so the cloud can tell a vetted entry from a detected
--     one and `campfire config pull` reproduces the tagged TOML. NULL beside a
--     non-null list is "unknown" and reads as hand; no backfill here.
--   nirspec_review_apply: the guarded write of `campfire config push`.
--   deployments.stuck_shutter_sources / bkg_overrides: the per-deploy snapshot
--     gains the tags and the overrides (provenance only).
-- Three nullable ADD COLUMNs and one CHECK on a table of a few hundred rows.


SET local check_function_bodies = off;

ALTER TABLE "public"."deployments"
  ADD COLUMN "stuck_shutter_sources" jsonb;

ALTER TABLE "public"."deployments"
  ADD COLUMN "bkg_overrides" jsonb;

ALTER TABLE "public"."nirspec_source_review"
  ADD COLUMN "stuck_source" text;

CREATE OR REPLACE FUNCTION public.nirspec_review_apply (
  p_observation text,
  p_changes     jsonb
)
  RETURNS jsonb
  LANGUAGE plpgsql
  SET search_path TO 'public'
  AS $function$
DECLARE
  c            jsonb;
  v_channel    text;
  v_root       text;
  v_sid        integer;
  v_row        public.nirspec_source_review%ROWTYPE;
  v_found      boolean;
  cur_value    jsonb;
  cur_source   text;
  exp_value    jsonb;
  exp_source   text;
  new_value    jsonb;
  new_source   text;
  v_applied    integer := 0;
  v_unchanged  integer := 0;
  v_refused    jsonb := '[]'::jsonb;
BEGIN
  IF NOT (COALESCE((SELECT public.is_admin()), false)
          OR COALESCE(auth.role(), '') = 'service_role'
          OR COALESCE(current_setting('request.jwt.claims', true), '') = '') THEN
    RAISE EXCEPTION 'nirspec_review_apply: admin only' USING ERRCODE = '42501';
  END IF;
  IF p_observation IS NULL OR p_observation = '' THEN
    RAISE EXCEPTION 'nirspec_review_apply: p_observation is required' USING ERRCODE = '22023';
  END IF;
  IF p_changes IS NULL OR jsonb_typeof(p_changes) <> 'array' THEN
    RAISE EXCEPTION 'nirspec_review_apply: p_changes must be a JSON array' USING ERRCODE = '22023';
  END IF;

  FOR c IN SELECT * FROM jsonb_array_elements(p_changes) LOOP
    v_channel := c ->> 'channel';
    v_root    := c ->> 'exposure_root';
    IF v_channel IS NULL OR v_channel NOT IN ('stuck', 'bkg') THEN
      RAISE EXCEPTION 'nirspec_review_apply: channel must be stuck or bkg, got %', COALESCE(v_channel, 'null')
        USING ERRCODE = '22023';
    END IF;
    IF v_root IS NULL OR v_root = '' OR jsonb_typeof(c -> 'source_id') IS DISTINCT FROM 'number' THEN
      RAISE EXCEPTION 'nirspec_review_apply: a change needs exposure_root and an integer source_id'
        USING ERRCODE = '22023';
    END IF;
    v_sid := (c ->> 'source_id')::integer;

    -- JSON null and a missing key both mean "no entry".
    exp_value  := NULLIF(c -> 'expect' -> 'value', 'null'::jsonb);
    exp_source := c -> 'expect' ->> 'source';
    new_value  := NULLIF(c -> 'set' -> 'value', 'null'::jsonb);
    new_source := c -> 'set' ->> 'source';

    IF v_channel = 'stuck' THEN
      IF new_value IS NOT NULL AND jsonb_typeof(new_value) <> 'array' THEN
        RAISE EXCEPTION 'nirspec_review_apply: a stuck entry is a list of shutter ordinals (% %)', v_root, v_sid
          USING ERRCODE = '22023';
      END IF;
      IF new_source IS NOT NULL AND new_source NOT IN ('hand', 'web', 'auto') THEN
        RAISE EXCEPTION 'nirspec_review_apply: stuck source must be hand, web or auto, got %', new_source
          USING ERRCODE = '22023';
      END IF;
      IF new_value IS NULL THEN
        new_source := NULL;   -- no entry, no provenance
      END IF;
    ELSE
      IF new_value IS NOT NULL AND jsonb_typeof(new_value) <> 'object' THEN
        RAISE EXCEPTION 'nirspec_review_apply: a bkg entry is a {nod: [nods]} map (% %)', v_root, v_sid
          USING ERRCODE = '22023';
      END IF;
      new_source := NULL;
      exp_source := NULL;
    END IF;

    SELECT * INTO v_row FROM nirspec_source_review
     WHERE observation = p_observation AND exposure_root = v_root AND source_id = v_sid
       FOR UPDATE;
    v_found := FOUND;
    IF NOT v_found AND new_value IS NOT NULL THEN
      -- Take the row (an empty one: no entry in either channel) so the comparison
      -- below runs under its lock even when another session inserts it first.
      INSERT INTO nirspec_source_review (observation, exposure_root, source_id)
      VALUES (p_observation, v_root, v_sid)
      ON CONFLICT (observation, exposure_root, source_id) DO NOTHING;
      SELECT * INTO v_row FROM nirspec_source_review
       WHERE observation = p_observation AND exposure_root = v_root AND source_id = v_sid
         FOR UPDATE;
      v_found := FOUND;
    END IF;

    IF v_found AND v_channel = 'stuck' THEN
      cur_value  := NULLIF(v_row.stuck_shutters, 'null'::jsonb);
      cur_source := CASE WHEN cur_value IS NULL THEN NULL ELSE v_row.stuck_source END;
    ELSIF v_found THEN
      cur_value  := NULLIF(v_row.bkg_overrides, 'null'::jsonb);
      cur_source := NULL;
    ELSE
      cur_value  := NULL;
      cur_source := NULL;
    END IF;

    IF cur_value IS NOT DISTINCT FROM new_value AND cur_source IS NOT DISTINCT FROM new_source THEN
      v_unchanged := v_unchanged + 1;
      CONTINUE;
    END IF;

    IF cur_value IS DISTINCT FROM exp_value OR cur_source IS DISTINCT FROM exp_source THEN
      v_refused := v_refused || jsonb_build_array(jsonb_build_object(
        'channel', v_channel, 'exposure_root', v_root, 'source_id', v_sid,
        'current', CASE WHEN cur_value IS NULL THEN NULL
                        WHEN v_channel = 'stuck' THEN jsonb_build_object('value', cur_value, 'source', cur_source)
                        ELSE jsonb_build_object('value', cur_value) END));
      CONTINUE;
    END IF;

    IF v_channel = 'stuck' THEN
      UPDATE nirspec_source_review
         SET stuck_shutters = new_value, stuck_source = new_source, updated_at = now()
       WHERE id = v_row.id;
    ELSE
      UPDATE nirspec_source_review
         SET bkg_overrides = new_value, updated_at = now()
       WHERE id = v_row.id;
    END IF;
    v_applied := v_applied + 1;
  END LOOP;

  RETURN jsonb_build_object('applied', v_applied, 'unchanged', v_unchanged, 'refused', v_refused);
END;
$function$;

REVOKE ALL ON FUNCTION "public"."nirspec_review_apply"(text, jsonb) FROM PUBLIC, "anon";

ALTER TABLE "public"."nirspec_source_review"
  ADD CONSTRAINT "nirspec_source_review_stuck_source_check" CHECK ((stuck_source = ANY (ARRAY['hand'::text, 'web'::text, 'auto'::text])));

COMMENT ON FUNCTION "public"."nirspec_review_apply"(text, jsonb) IS 'Guarded write of the NIRSpec stuck-shutter / background-override entries (campfire config push, and the deploy of an observation): each change carries the value the client read (expect) and the value it wants (set) for one channel of one (exposure_root, source_id), and lands only while the entry still holds what was read, so a portal edit made in between is refused and reported instead of overwritten. Returns {applied, unchanged, refused: [{channel, exposure_root, source_id, current}]}. Admin or service role.';

COMMENT ON TABLE "public"."nirspec_source_review" IS 'Cloud home of the NIRSpec stuck-shutter and nodded-background-override lists (reference/nirspec/<obs>/*.toml), synced by `campfire config push / pull / diff` and edited in the portal''s nods view. One row per (observation, exposure_root, source_id) holding two independent entries: stuck_shutters ([ordinals]; [] = vetted, none stuck; NULL = no entry) with its provenance stuck_source (hand / web / auto; NULL = unknown, reads as hand), and bkg_overrides ({nod: [nods]}; NULL = no entry). Admin-only; rows are never deleted; CLI writes go through nirspec_review_apply.';

GRANT EXECUTE ON FUNCTION "public"."nirspec_review_apply"(text, jsonb) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."nirspec_review_apply"(text, jsonb) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."nirspec_review_apply"(text, jsonb) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."nirspec_review_apply"(text, jsonb) TO "service_role";
