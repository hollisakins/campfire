-- Drop stale overloads of object-related RPCs.
--
-- These functions had their parameter lists evolve across many migrations.
-- Each `CREATE OR REPLACE FUNCTION` with a new argument list creates a *new
-- overload* rather than replacing the existing one. Most migrations paired
-- the new CREATE with a `drop function if exists` for the prior signature,
-- but 20260427235329_add_sort_to_get_filtered_object_ids.sql forgot to. Its
-- trailing
--   GRANT EXECUTE ON FUNCTION public.get_filtered_object_ids TO authenticated;
-- (no parameter list) is then ambiguous between the new 24-arg version and
-- the leftover 22-arg version, which aborts `supabase db reset` (issue #127).
--
-- The DROP statements below cover every historical signature for these
-- three RPCs. On a fresh `db reset`, only the 22-arg `get_filtered_object_ids`
-- is actually present — the rest are no-op skips. The full list is kept as
-- defensive cleanup so any drifted environment (e.g. production, which was
-- hand-patched as the manual workaround for #124) lands in the same state.
--
-- Timestamped one second before the offending migration so it runs first
-- on a fresh `db reset`. Each DROP uses `IF EXISTS` and is idempotent.

-- =============================================================================
-- get_filtered_object_ids: drop 4 stale overloads (canonical = 24 args)
-- =============================================================================

-- 17-arg original (20260330022835 / 20260407182855)
DROP FUNCTION IF EXISTS public.get_filtered_object_ids(
  text[], text[], text[], text[], text, integer[],
  double precision, double precision, double precision, double precision,
  double precision, double precision,
  text, boolean,
  double precision, double precision, double precision
);

-- 18-arg + p_list_ids (20260406144000)
DROP FUNCTION IF EXISTS public.get_filtered_object_ids(
  text[], text[], text[], text[], text, integer[],
  double precision, double precision, double precision, double precision,
  double precision, double precision,
  text, boolean, integer[],
  double precision, double precision, double precision
);

-- 21-arg + photometry (20260413140335 / 20260416194435)
DROP FUNCTION IF EXISTS public.get_filtered_object_ids(
  text[], text[], text[], text[], text, integer[],
  double precision, double precision, double precision, double precision,
  double precision, double precision,
  text, boolean, integer[],
  double precision, double precision, double precision,
  boolean, double precision, double precision
);

-- 22-arg + p_needs_review (20260422153719)
DROP FUNCTION IF EXISTS public.get_filtered_object_ids(
  text[], text[], text[], text[], text, integer[],
  double precision, double precision, double precision, double precision,
  double precision, double precision,
  text, boolean, boolean, integer[],
  double precision, double precision, double precision,
  boolean, double precision, double precision
);

-- =============================================================================
-- get_filtered_objects_paginated: drop 4 stale overloads (canonical = 27 args)
-- =============================================================================

-- 21-arg original (20260329022622 / 20260407182855)
DROP FUNCTION IF EXISTS public.get_filtered_objects_paginated(
  text[], text[], text[], text[], text, integer[],
  double precision, double precision, double precision, double precision,
  double precision, double precision,
  text, boolean,
  double precision, double precision, double precision,
  text, text, integer, integer
);

-- 22-arg + p_list_ids (20260406144000 / 20260407192933)
DROP FUNCTION IF EXISTS public.get_filtered_objects_paginated(
  text[], text[], text[], text[], text, integer[],
  double precision, double precision, double precision, double precision,
  double precision, double precision,
  text, boolean, integer[],
  double precision, double precision, double precision,
  text, text, integer, integer
);

-- 23-arg + p_observations (20260411231756)
DROP FUNCTION IF EXISTS public.get_filtered_objects_paginated(
  text[], text[], text[], text[], text, text[], integer[],
  double precision, double precision, double precision, double precision,
  double precision, double precision,
  text, boolean, integer[],
  double precision, double precision, double precision,
  text, text, integer, integer
);

-- 26-arg + photometry (20260413140335 / 20260416194435)
DROP FUNCTION IF EXISTS public.get_filtered_objects_paginated(
  text[], text[], text[], text[], text, text[], integer[],
  double precision, double precision, double precision, double precision,
  double precision, double precision,
  text, boolean, integer[],
  double precision, double precision, double precision,
  boolean, double precision, double precision,
  text, text, integer, integer
);

-- =============================================================================
-- get_objects_for_sync: drop 2 stale overloads (canonical = 6 args)
-- =============================================================================

-- 4-arg original (20260330233000 / 20260407192933)
DROP FUNCTION IF EXISTS public.get_objects_for_sync(
  text[], timestamptz, integer, integer
);

-- 5-arg + p_user_id (20260411211822 / 20260413194519 / 20260416194435)
DROP FUNCTION IF EXISTS public.get_objects_for_sync(
  text[], uuid, timestamptz, integer, integer
);
