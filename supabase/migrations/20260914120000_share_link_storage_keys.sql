-- Share-link bulk download: filter_link_storage_keys.
--
-- Hand-authored: no local Docker for `supabase db diff`. The function body
-- below is copied verbatim from supabase/schemas/functions.sql (the source of
-- truth); this file carries nothing else. New function, no existing signature
-- touched, so an old Vercel deployment keeps working while this lands.
-- =============================================================================
-- filter_link_storage_keys (share-link bulk download)
-- =============================================================================
-- Per-key authorization for a SHARE LINK on the /api/v1/storage/* routes, the
-- link-account counterpart of filter_accessible_storage_keys above.
--
-- Why a second function rather than two more parameters on the first: a link
-- account is not narrowed by program at all (a field link has NO accessible
-- program), it is narrowed on the observation/field axis, and it may see draft
-- rows inside its scope. Folding that into the ordinary-user function would put
-- four link-only branches on the hot path of every API download.
--
-- This is the API-path restatement of the LINK BRANCHES of the storage_objects
-- SELECT policy (supabase/schemas/policies.sql select_storage_objects_by_access)
-- exactly as filter_accessible_storage_keys restates its ordinary-user branches.
-- Keep the two in lock-step; supabase/tests/check_share_link_scoping.sql pins
-- this one against the policy it mirrors.
--
-- The caller passes the link's OWN scope, read from share_links under the
-- service role (web/lib/auth/access-context.ts). allow_download is NOT a
-- parameter: it is a whole-credential gate, refused by the route before any key
-- is considered (403), not a per-key filter. A revoked or expired link never
-- reaches here — its scope resolves to "sees nothing" upstream.
CREATE OR REPLACE FUNCTION public.filter_link_storage_keys(
  p_keys TEXT[],
  p_program_slugs TEXT[],
  p_observation TEXT DEFAULT NULL,
  p_field TEXT DEFAULT NULL,
  p_include_drafts BOOLEAN DEFAULT FALSE
)
RETURNS TABLE(storage_key TEXT)
LANGUAGE sql STABLE
AS $$
  SELECT so.storage_key
  FROM storage_objects so
  WHERE so.storage_key = ANY(p_keys)
    AND so.status = 'active'
    -- A link is scoped to exactly one axis (share_links_scope_check). Neither
    -- set means no scope at all: authorize nothing rather than everything.
    AND (p_observation IS NOT NULL OR p_field IS NOT NULL)
    AND (
      (so.spectrum_id IS NOT NULL AND EXISTS (
         SELECT 1 FROM spectra s
         WHERE s.spectrum_id = so.spectrum_id
           AND (s.deploy_status = 'published'
                OR (s.deploy_status = 'draft' AND p_include_drafts))
           AND s.program_slug = ANY(p_program_slugs)
           AND s.observation = p_observation))
      OR (so.spectrum_id IS NULL AND so.deployment_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM deployments d
            LEFT JOIN observations o ON o.name = d.observation
            WHERE d.id = so.deployment_id
              AND (d.status = 'published'
                   OR (d.status = 'draft' AND p_include_drafts))
              -- NIRCam field deploy (epic #261, N1): multi-program, public to
              -- all when published -- so the scope match on the next line is
              -- the ONLY thing keeping a field link off every other field.
              AND (d.field IS NOT NULL OR o.program_slug = ANY(p_program_slugs))
              AND (d.observation = p_observation OR d.field = p_field)))
    );
$$;

GRANT EXECUTE ON FUNCTION public.filter_link_storage_keys(TEXT[], TEXT[], TEXT, TEXT, BOOLEAN) TO authenticated;
GRANT EXECUTE ON FUNCTION public.filter_link_storage_keys(TEXT[], TEXT[], TEXT, TEXT, BOOLEAN) TO service_role;
