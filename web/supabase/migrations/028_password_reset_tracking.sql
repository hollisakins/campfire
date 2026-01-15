-- Migration 028: Password Reset Tracking
-- Creates table to log password reset attempts for security monitoring

-- Create password reset log table
CREATE TABLE password_reset_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  reset_at TIMESTAMPTZ DEFAULT NOW(),
  ip_address TEXT,
  user_agent TEXT
);

-- Add index for user lookups
CREATE INDEX idx_password_reset_log_user_id ON password_reset_log(user_id);

-- Add index for date range queries
CREATE INDEX idx_password_reset_log_reset_at ON password_reset_log(reset_at DESC);

-- Enable RLS
ALTER TABLE password_reset_log ENABLE ROW LEVEL SECURITY;

-- Policy: Users can view their own reset logs
CREATE POLICY "Users can view own reset logs"
  ON password_reset_log FOR SELECT
  USING (auth.uid() = user_id);

-- Policy: Admins can view all reset logs
CREATE POLICY "Admins can view all reset logs"
  ON password_reset_log FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM user_profiles
      WHERE user_id = auth.uid()
      AND is_admin = TRUE
    )
  );

-- Add comments for documentation
COMMENT ON TABLE password_reset_log IS 'Logs password reset attempts for security monitoring';
COMMENT ON COLUMN password_reset_log.user_id IS 'User who reset their password';
COMMENT ON COLUMN password_reset_log.reset_at IS 'When the password was reset';
COMMENT ON COLUMN password_reset_log.ip_address IS 'IP address from which the reset was performed';
COMMENT ON COLUMN password_reset_log.user_agent IS 'Browser user agent string';
