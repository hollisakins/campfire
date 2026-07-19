-- Secure access-code redemption.
--
-- 1. Drop the world-readable SELECT policy on access_codes: it allowed any
--    caller (including anon) to enumerate every active code and its granted
--    programs, bypassing the invitation model entirely.
-- 2. Add redeem_access_code(p_code): a SECURITY DEFINER RPC that performs the
--    whole redemption (validate, grant, record, increment) in one transaction
--    with a row lock, so max_uses is enforced correctly under concurrent
--    redemptions and codes never need to be readable by non-admins.
--
-- NOTE: hand-authored (no local Supabase available to run `supabase db diff`);
-- mirrors supabase/schemas/{policies,functions}.sql exactly.

drop policy if exists "Anyone can read active codes" on "public"."access_codes";

-- Users can still read codes they have already redeemed (the profile page's
-- redemption history embeds access_codes via code_redemptions). Redeeming
-- required knowing the code, so this reveals nothing new; unredeemed codes
-- stay invisible to non-admins.
CREATE POLICY "Users can read own redeemed codes"
  ON "public"."access_codes" FOR SELECT
  USING (EXISTS (
    SELECT 1 FROM code_redemptions
    WHERE code_redemptions.code_id = access_codes.id
      AND code_redemptions.user_id = (SELECT auth.uid())
  ));

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
