-- Add full_name column to pending_invites table
-- This allows the name from account requests to be passed through to the welcome page

ALTER TABLE pending_invites ADD COLUMN IF NOT EXISTS full_name TEXT;
