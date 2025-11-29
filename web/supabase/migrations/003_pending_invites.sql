-- Create pending_invites table for admin user invitation system
CREATE TABLE IF NOT EXISTS pending_invites (
  id SERIAL PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  program_ids INTEGER[] DEFAULT '{}',
  is_admin BOOLEAN DEFAULT FALSE,
  can_comment BOOLEAN DEFAULT TRUE,
  invited_by UUID REFERENCES auth.users(id),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  accepted_at TIMESTAMPTZ
);

-- Create index on email for faster lookups
CREATE INDEX IF NOT EXISTS idx_pending_invites_email ON pending_invites(email);

-- Enable RLS
ALTER TABLE pending_invites ENABLE ROW LEVEL SECURITY;

-- Policy: Admins can view all invites
CREATE POLICY "Admins can view invites" ON pending_invites
  FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM user_profiles
      WHERE user_id = auth.uid() AND is_admin = true
    )
  );

-- Policy: Admins can insert invites
CREATE POLICY "Admins can create invites" ON pending_invites
  FOR INSERT TO authenticated
  WITH CHECK (
    EXISTS (
      SELECT 1 FROM user_profiles
      WHERE user_id = auth.uid() AND is_admin = true
    )
  );

-- Policy: Admins can update invites (for marking as accepted)
CREATE POLICY "Admins can update invites" ON pending_invites
  FOR UPDATE TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM user_profiles
      WHERE user_id = auth.uid() AND is_admin = true
    )
  );

-- Policy: Admins can delete invites
CREATE POLICY "Admins can delete invites" ON pending_invites
  FOR DELETE TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM user_profiles
      WHERE user_id = auth.uid() AND is_admin = true
    )
  );

-- Policy: Users can read their own invite (by email) - needed for signup flow
CREATE POLICY "Users can read own invite by email" ON pending_invites
  FOR SELECT TO authenticated
  USING (
    email = (SELECT email FROM auth.users WHERE id = auth.uid())
  );

-- Policy: Service role can do everything (for the invite API)
-- This is implicit with service role key
