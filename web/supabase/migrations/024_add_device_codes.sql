-- Create device_codes table for OAuth 2.0 Device Authorization Grant
-- Used to track pending device authorization requests during the authentication flow

CREATE TABLE IF NOT EXISTS device_codes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  device_code TEXT NOT NULL UNIQUE,        -- 32-byte random hex (secret, used for polling)
  user_code TEXT NOT NULL UNIQUE,          -- 8-char human-readable code (XXXX-XXXX)
  user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,  -- NULL until authorized
  verification_uri TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,         -- 15 minutes from creation
  interval_seconds INTEGER DEFAULT 5,      -- Minimum polling interval
  status TEXT NOT NULL DEFAULT 'pending',  -- pending | authorized | denied | expired
  created_at TIMESTAMPTZ DEFAULT NOW(),
  authorized_at TIMESTAMPTZ,
  client_ip TEXT,                          -- For audit logging
  user_agent TEXT                          -- For audit logging
);

-- Create indexes for efficient lookups
CREATE INDEX IF NOT EXISTS idx_device_codes_device_code ON device_codes(device_code);
CREATE INDEX IF NOT EXISTS idx_device_codes_user_code ON device_codes(user_code);
CREATE INDEX IF NOT EXISTS idx_device_codes_expires_at ON device_codes(expires_at);
CREATE INDEX IF NOT EXISTS idx_device_codes_status ON device_codes(status) WHERE status = 'pending';

-- Enable RLS
ALTER TABLE device_codes ENABLE ROW LEVEL SECURITY;

-- Policy: Only service role can access device_codes (all operations via API)
-- Authenticated users should not directly access this table
CREATE POLICY "Service role full access" ON device_codes
  FOR ALL TO service_role
  USING (true)
  WITH CHECK (true);

-- Function to clean up expired device codes (can be called periodically)
CREATE OR REPLACE FUNCTION cleanup_expired_device_codes()
RETURNS INTEGER AS $$
DECLARE
  deleted_count INTEGER;
BEGIN
  DELETE FROM device_codes
  WHERE expires_at < NOW()
  RETURNING 1 INTO deleted_count;

  GET DIAGNOSTICS deleted_count = ROW_COUNT;
  RETURN deleted_count;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to check device code status (used by polling endpoint)
CREATE OR REPLACE FUNCTION check_device_code_status(p_device_code TEXT)
RETURNS TABLE (
  status TEXT,
  user_id UUID,
  is_expired BOOLEAN
) AS $$
BEGIN
  RETURN QUERY
  SELECT
    dc.status,
    dc.user_id,
    (dc.expires_at < NOW())::BOOLEAN AS is_expired
  FROM device_codes dc
  WHERE dc.device_code = p_device_code;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to authorize a device code (called when user approves in browser)
CREATE OR REPLACE FUNCTION authorize_device_code(p_user_code TEXT, p_user_id UUID)
RETURNS BOOLEAN AS $$
DECLARE
  updated_rows INTEGER;
BEGIN
  UPDATE device_codes
  SET
    status = 'authorized',
    user_id = p_user_id,
    authorized_at = NOW()
  WHERE
    user_code = p_user_code
    AND status = 'pending'
    AND expires_at > NOW();

  GET DIAGNOSTICS updated_rows = ROW_COUNT;
  RETURN updated_rows > 0;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to deny a device code (called when user denies in browser)
CREATE OR REPLACE FUNCTION deny_device_code(p_user_code TEXT)
RETURNS BOOLEAN AS $$
DECLARE
  updated_rows INTEGER;
BEGIN
  UPDATE device_codes
  SET status = 'denied'
  WHERE
    user_code = p_user_code
    AND status = 'pending';

  GET DIAGNOSTICS updated_rows = ROW_COUNT;
  RETURN updated_rows > 0;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to mark device code as used (after tokens are issued)
CREATE OR REPLACE FUNCTION consume_device_code(p_device_code TEXT)
RETURNS UUID AS $$
DECLARE
  v_user_id UUID;
BEGIN
  UPDATE device_codes
  SET status = 'consumed'
  WHERE
    device_code = p_device_code
    AND status = 'authorized'
    AND expires_at > NOW()
  RETURNING user_id INTO v_user_id;

  RETURN v_user_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
