-- Raise statement_timeout on the three /sync/* RPCs.
--
-- OFFSET-based pagination is linear in offset: each page has to scan and
-- order up to `offset + limit` rows before returning. On a 30k-object
-- catalog the final `--full` sync page (offset=29000) was tipping past
-- the default service_role statement timeout, failing with SQLSTATE
-- 57014 ("canceling statement due to statement timeout"). The per-page
-- aggregate CTEs in get_objects_for_sync add to the floor.
--
-- Raising the per-function timeout to 120s unblocks the current Python
-- client without a client-side change. The right fix is to switch these
-- RPCs to keyset pagination (WHERE object_id > cursor ORDER BY ...),
-- which is O(log N + limit) per page; that change can follow in a
-- separate PR and this SET can be dropped at that point.

ALTER FUNCTION public.get_objects_for_sync(
  text[], uuid, timestamptz, integer, integer, boolean
) SET statement_timeout TO '120s';

ALTER FUNCTION public.get_spectra_for_sync(
  text[], uuid, timestamptz, integer, integer, boolean
) SET statement_timeout TO '120s';

ALTER FUNCTION public.get_photometry_for_sync(
  text[], timestamptz, integer, integer
) SET statement_timeout TO '120s';
