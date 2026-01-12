-- Create refresh_tokens table for OAuth token management
-- Stores refresh tokens with single-use rotation for security

CREATE TABLE IF NOT EXISTS refresh_tokens (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  token_hash TEXT NOT NULL UNIQUE,         -- SHA-256 hash of the refresh token
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  device_name TEXT,                        -- Optional device identifier (e.g., "MacBook Pro")
  expires_at TIMESTAMPTZ NOT NULL,         -- 90 days from creation
  created_at TIMESTAMPTZ DEFAULT NOW(),
  last_used_at TIMESTAMPTZ,
  is_revoked BOOLEAN DEFAULT FALSE,
  revoked_at TIMESTAMPTZ,
  replaced_by UUID REFERENCES refresh_tokens(id),  -- Points to new token after rotation
  client_ip TEXT,                          -- For audit logging
  user_agent TEXT                          -- For audit logging
);

-- Create indexes for efficient lookups
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_token_hash ON refresh_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user_id ON refresh_tokens(user_id);
-- Partial index for non-revoked tokens (expiration checked at query time)
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_active ON refresh_tokens(user_id, expires_at)
  WHERE is_revoked = FALSE;

-- Enable RLS
ALTER TABLE refresh_tokens ENABLE ROW LEVEL SECURITY;

-- Policy: Users can view their own refresh tokens (for session management UI)
CREATE POLICY "Users can view own tokens" ON refresh_tokens
  FOR SELECT TO authenticated
  USING (auth.uid() = user_id);

-- Policy: Users can revoke their own refresh tokens
CREATE POLICY "Users can update own tokens" ON refresh_tokens
  FOR UPDATE TO authenticated
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

-- Policy: Service role full access (for token validation and creation)
CREATE POLICY "Service role full access" ON refresh_tokens
  FOR ALL TO service_role
  USING (true)
  WITH CHECK (true);

-- Function to validate a refresh token
CREATE OR REPLACE FUNCTION validate_refresh_token(p_token_hash TEXT)
RETURNS TABLE (
  is_valid BOOLEAN,
  user_id UUID,
  token_id UUID
) AS $$
BEGIN
  -- Also update last_used_at when validating
  UPDATE refresh_tokens
  SET last_used_at = NOW()
  WHERE token_hash = p_token_hash
    AND is_revoked = FALSE
    AND expires_at > NOW();

  RETURN QUERY
  SELECT
    (rt.is_revoked = FALSE AND rt.expires_at > NOW())::BOOLEAN AS is_valid,
    rt.user_id,
    rt.id AS token_id
  FROM refresh_tokens rt
  WHERE rt.token_hash = p_token_hash;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to rotate a refresh token (invalidate old, return info for new)
CREATE OR REPLACE FUNCTION rotate_refresh_token(
  p_old_token_hash TEXT,
  p_new_token_hash TEXT,
  p_expires_at TIMESTAMPTZ,
  p_client_ip TEXT DEFAULT NULL,
  p_user_agent TEXT DEFAULT NULL
)
RETURNS TABLE (
  success BOOLEAN,
  user_id UUID,
  new_token_id UUID
) AS $$
DECLARE
  v_user_id UUID;
  v_old_token_id UUID;
  v_new_token_id UUID;
  v_device_name TEXT;
BEGIN
  -- First, validate and get the old token info
  SELECT rt.user_id, rt.id, rt.device_name
  INTO v_user_id, v_old_token_id, v_device_name
  FROM refresh_tokens rt
  WHERE rt.token_hash = p_old_token_hash
    AND rt.is_revoked = FALSE
    AND rt.expires_at > NOW();

  IF v_user_id IS NULL THEN
    -- Token not found or invalid
    RETURN QUERY SELECT FALSE, NULL::UUID, NULL::UUID;
    RETURN;
  END IF;

  -- Create new token
  INSERT INTO refresh_tokens (
    token_hash,
    user_id,
    device_name,
    expires_at,
    client_ip,
    user_agent
  ) VALUES (
    p_new_token_hash,
    v_user_id,
    v_device_name,
    p_expires_at,
    p_client_ip,
    p_user_agent
  )
  RETURNING id INTO v_new_token_id;

  -- Revoke old token and link to new one
  UPDATE refresh_tokens
  SET
    is_revoked = TRUE,
    revoked_at = NOW(),
    replaced_by = v_new_token_id
  WHERE id = v_old_token_id;

  RETURN QUERY SELECT TRUE, v_user_id, v_new_token_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to revoke a single refresh token
CREATE OR REPLACE FUNCTION revoke_refresh_token(p_token_id UUID, p_user_id UUID)
RETURNS BOOLEAN AS $$
DECLARE
  updated_rows INTEGER;
BEGIN
  UPDATE refresh_tokens
  SET
    is_revoked = TRUE,
    revoked_at = NOW()
  WHERE
    id = p_token_id
    AND user_id = p_user_id
    AND is_revoked = FALSE;

  GET DIAGNOSTICS updated_rows = ROW_COUNT;
  RETURN updated_rows > 0;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to revoke all refresh tokens for a user (logout from all devices)
CREATE OR REPLACE FUNCTION revoke_all_user_refresh_tokens(p_user_id UUID)
RETURNS INTEGER AS $$
DECLARE
  updated_rows INTEGER;
BEGIN
  UPDATE refresh_tokens
  SET
    is_revoked = TRUE,
    revoked_at = NOW()
  WHERE
    user_id = p_user_id
    AND is_revoked = FALSE;

  GET DIAGNOSTICS updated_rows = ROW_COUNT;
  RETURN updated_rows;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to clean up expired refresh tokens (can be called periodically)
CREATE OR REPLACE FUNCTION cleanup_expired_refresh_tokens()
RETURNS INTEGER AS $$
DECLARE
  deleted_count INTEGER;
BEGIN
  -- Delete tokens that expired more than 30 days ago (keep recent for audit)
  DELETE FROM refresh_tokens
  WHERE expires_at < NOW() - INTERVAL '30 days';

  GET DIAGNOSTICS deleted_count = ROW_COUNT;
  RETURN deleted_count;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
