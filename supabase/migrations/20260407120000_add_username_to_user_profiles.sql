-- =============================================================================
-- Migration: Add username column to user_profiles
-- =============================================================================
-- Adds a unique, validated username to user_profiles. Existing users are
-- populated from the local part of their auth email address.
-- =============================================================================

-- Step 1: Add column as nullable first
ALTER TABLE public.user_profiles ADD COLUMN IF NOT EXISTS "username" text;

-- Step 2: Populate from auth.users email (local part, lowercased, sanitised)
UPDATE public.user_profiles up
SET username = lower(
  regexp_replace(
    regexp_replace(
      split_part(au.email, '@', 1),
      '[^a-z0-9._-]', '-', 'gi'
    ),
    '^[^a-z0-9]+|[^a-z0-9]+$', '', 'g'
  )
)
FROM auth.users au
WHERE au.id = up.user_id
  AND up.username IS NULL;

-- Step 3: Handle any remaining NULLs or too-short usernames
UPDATE public.user_profiles
SET username = 'user-' || left(user_id::text, 8)
WHERE username IS NULL OR length(username) < 2;

-- Step 4: Handle duplicate usernames by appending a suffix
DO $$
DECLARE
  rec RECORD;
  suffix INT;
  new_username TEXT;
BEGIN
  FOR rec IN
    SELECT user_id, username
    FROM public.user_profiles
    WHERE username IN (
      SELECT username FROM public.user_profiles
      GROUP BY username HAVING count(*) > 1
    )
    ORDER BY created_at ASC
    -- Skip the first (oldest) user with each duplicate username
  LOOP
    -- Check if this specific row still has a duplicate
    IF (SELECT count(*) FROM public.user_profiles WHERE username = rec.username) > 1 THEN
      -- This isn't the first user with this name, so add suffix
      -- (The first one in created_at order keeps the base name)
      IF (SELECT min(user_id) FROM public.user_profiles WHERE username = rec.username ORDER BY created_at ASC LIMIT 1) != rec.user_id THEN
        suffix := 2;
        LOOP
          new_username := rec.username || '-' || suffix;
          EXIT WHEN NOT EXISTS (SELECT 1 FROM public.user_profiles WHERE username = new_username);
          suffix := suffix + 1;
        END LOOP;
        UPDATE public.user_profiles SET username = new_username WHERE user_id = rec.user_id;
      END IF;
    END IF;
  END LOOP;
END $$;

-- Step 5: Now make it NOT NULL and add constraints
ALTER TABLE public.user_profiles ALTER COLUMN "username" SET NOT NULL;

ALTER TABLE public.user_profiles ADD CONSTRAINT "user_profiles_username_key" UNIQUE ("username");

ALTER TABLE public.user_profiles ADD CONSTRAINT "user_profiles_username_check"
  CHECK (username ~ '^[a-z0-9][a-z0-9._-]{0,38}[a-z0-9]$');
