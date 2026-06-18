-- Admins inherit access to every program.
--
-- Every program-gated RLS SELECT policy routes through
-- accessible_program_slugs() (observations, targets, spectra, objects,
-- object_photometry, ...). Previously admins were NOT granted all programs
-- here, so a deploy that creates rows for a PRIVATE program the deployer has
-- no explicit grant to could not read those rows back. PostgREST upserts use
-- `Prefer: return=representation` (INSERT ... RETURNING), and PostgreSQL
-- enforces the SELECT policy on the returned row, so the write failed with
-- `42501 new row violates row-level security policy` even though the INSERT
-- WITH CHECK (is_admin()) passed.
--
-- Granting admins all programs at this single function fixes read-back for
-- every program-gated table at once and makes the access model consistent:
-- a user can access a program if it is granted to them, OR it is public, OR
-- they are an admin (admins already have this for programs/nircam_exposures).

CREATE OR REPLACE FUNCTION public.accessible_program_slugs()
RETURNS text[]
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE(array_agg(DISTINCT slug), '{}')
  FROM (
    SELECT program_slug AS slug
    FROM user_program_access
    WHERE user_id = auth.uid()
    UNION
    SELECT slug
    FROM programs
    WHERE is_public = true
    UNION
    SELECT slug
    FROM programs
    WHERE public.is_admin()
  ) sub;
$$;
