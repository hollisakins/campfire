-- Migration 029: Email Change Tracking
-- Tracks email address changes for security auditing

-- Create email change log table
CREATE TABLE email_change_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  old_email TEXT NOT NULL,
  new_email TEXT NOT NULL,
  changed_at TIMESTAMPTZ DEFAULT NOW(),
  ip_address TEXT,
  user_agent TEXT
);

-- Add index for user lookups
CREATE INDEX idx_email_change_log_user_id ON email_change_log(user_id);

-- Add index for date range queries
CREATE INDEX idx_email_change_log_changed_at ON email_change_log(changed_at DESC);

-- Enable RLS
ALTER TABLE email_change_log ENABLE ROW LEVEL SECURITY;

-- Policy: Users can view their own email change logs
CREATE POLICY "Users can view own email change logs"
  ON email_change_log FOR SELECT
  USING (auth.uid() = user_id);

-- Policy: Admins can view all email change logs
CREATE POLICY "Admins can view all email change logs"
  ON email_change_log FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM user_profiles
      WHERE user_id = auth.uid()
      AND is_admin = TRUE
    )
  );

-- Add comments
COMMENT ON TABLE email_change_log IS 'Audit log of email address changes for security tracking';
COMMENT ON COLUMN email_change_log.old_email IS 'Email address before change';
COMMENT ON COLUMN email_change_log.new_email IS 'Email address after change';
COMMENT ON COLUMN email_change_log.changed_at IS 'Timestamp when email was changed';
COMMENT ON COLUMN email_change_log.ip_address IS 'IP address from which change was initiated';
COMMENT ON COLUMN email_change_log.user_agent IS 'User agent string from which change was initiated';
