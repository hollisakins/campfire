-- Create API keys table for Python API authentication
CREATE TABLE IF NOT EXISTS api_keys (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  key_hash TEXT NOT NULL UNIQUE,
  key_prefix TEXT NOT NULL,  -- e.g., "sk_live_abc..." for display in UI
  name TEXT,  -- Optional: "My notebook", "Server analysis", etc.
  created_at TIMESTAMPTZ DEFAULT NOW(),
  last_used_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  is_active BOOLEAN DEFAULT TRUE,
  rate_limit_per_minute INTEGER DEFAULT 60
);

-- Create indexes for performance
CREATE INDEX IF NOT EXISTS idx_api_keys_user_id ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_key_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_is_active ON api_keys(is_active) WHERE is_active = TRUE;

-- Enable RLS
ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;

-- Policy: Users can view their own API keys
CREATE POLICY "Users can view own API keys" ON api_keys
  FOR SELECT TO authenticated
  USING (auth.uid() = user_id);

-- Policy: Users can create their own API keys
CREATE POLICY "Users can create own API keys" ON api_keys
  FOR INSERT TO authenticated
  WITH CHECK (auth.uid() = user_id);

-- Policy: Users can update their own API keys (revoke, rename, etc.)
CREATE POLICY "Users can update own API keys" ON api_keys
  FOR UPDATE TO authenticated
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

-- Policy: Users can delete their own API keys
CREATE POLICY "Users can delete own API keys" ON api_keys
  FOR DELETE TO authenticated
  USING (auth.uid() = user_id);

-- Policy: Service role can do everything (for API key validation)
-- This is implicit with service role key

-- Create function to validate API key and return user_id
CREATE OR REPLACE FUNCTION validate_api_key(key_hash_input TEXT)
RETURNS TABLE (user_id UUID, is_valid BOOLEAN) AS $$
BEGIN
  RETURN QUERY
  SELECT
    ak.user_id,
    (ak.is_active AND (ak.expires_at IS NULL OR ak.expires_at > NOW()))::BOOLEAN AS is_valid
  FROM api_keys ak
  WHERE ak.key_hash = key_hash_input;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Create function to update last_used_at timestamp
CREATE OR REPLACE FUNCTION update_api_key_last_used(key_hash_input TEXT)
RETURNS VOID AS $$
BEGIN
  UPDATE api_keys
  SET last_used_at = NOW()
  WHERE key_hash = key_hash_input;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
