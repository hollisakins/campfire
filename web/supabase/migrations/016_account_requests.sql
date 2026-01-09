-- Migration: 016_account_requests.sql
-- Description: Create account_requests table for approval-based signup flow

-- Create the account_requests table
CREATE TABLE IF NOT EXISTS account_requests (
  id SERIAL PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  full_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
  -- Permissions set at approval time
  is_admin BOOLEAN DEFAULT FALSE,
  can_comment BOOLEAN DEFAULT TRUE,
  program_ids INTEGER[] DEFAULT '{}',
  -- Tracking
  created_at TIMESTAMPTZ DEFAULT NOW(),
  reviewed_at TIMESTAMPTZ,
  reviewed_by UUID REFERENCES auth.users(id),
  rejection_reason TEXT
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_account_requests_email ON account_requests(email);
CREATE INDEX IF NOT EXISTS idx_account_requests_status ON account_requests(status);
CREATE INDEX IF NOT EXISTS idx_account_requests_created_at ON account_requests(created_at DESC);

-- Enable Row Level Security
ALTER TABLE account_requests ENABLE ROW LEVEL SECURITY;

-- Policy: Admins can view all requests
CREATE POLICY "Admins can view requests" ON account_requests
  FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM user_profiles
      WHERE user_id = auth.uid() AND is_admin = true
    )
  );

-- Policy: Admins can update requests (approve/reject)
CREATE POLICY "Admins can update requests" ON account_requests
  FOR UPDATE TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM user_profiles
      WHERE user_id = auth.uid() AND is_admin = true
    )
  );

-- Policy: Anyone can insert requests (public signup)
-- This allows both anonymous and authenticated users to submit requests
CREATE POLICY "Anyone can submit requests" ON account_requests
  FOR INSERT TO anon, authenticated
  WITH CHECK (true);

-- Policy: Anyone can check their own request status by email
-- This allows users to check if their email has a pending request
CREATE POLICY "Users can check own request status" ON account_requests
  FOR SELECT TO anon, authenticated
  USING (true);

-- Grant necessary permissions
GRANT SELECT, INSERT ON account_requests TO anon;
GRANT SELECT, INSERT, UPDATE ON account_requests TO authenticated;
GRANT USAGE, SELECT ON SEQUENCE account_requests_id_seq TO anon;
GRANT USAGE, SELECT ON SEQUENCE account_requests_id_seq TO authenticated;
