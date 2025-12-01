-- Create access_codes table for proprietary program access control
CREATE TABLE IF NOT EXISTS access_codes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  code TEXT UNIQUE NOT NULL,
  description TEXT,
  grants_all_programs BOOLEAN DEFAULT false,
  program_ids INTEGER[],
  created_by UUID REFERENCES auth.users(id),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  expires_at TIMESTAMPTZ,
  max_uses INTEGER,
  use_count INTEGER DEFAULT 0,
  is_active BOOLEAN DEFAULT true
);

-- Create code_redemptions table to track who redeemed which codes
CREATE TABLE IF NOT EXISTS code_redemptions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  code_id UUID REFERENCES access_codes(id) ON DELETE CASCADE,
  user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
  redeemed_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(code_id, user_id)
);

-- Create indexes for faster lookups
CREATE INDEX IF NOT EXISTS idx_access_codes_code ON access_codes(code);
CREATE INDEX IF NOT EXISTS idx_code_redemptions_user ON code_redemptions(user_id);

-- Enable RLS
ALTER TABLE access_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE code_redemptions ENABLE ROW LEVEL SECURITY;

-- Policy: Anyone can read active codes
CREATE POLICY "Anyone can read active codes" ON access_codes
  FOR SELECT
  USING (is_active = true);

-- Policy: Admins can manage codes (insert, update, delete)
CREATE POLICY "Admins can manage codes" ON access_codes
  USING (
    EXISTS (
      SELECT 1 FROM user_profiles
      WHERE user_id = auth.uid() AND is_admin = true
    )
  );

-- Policy: Users can redeem codes (insert own redemption)
CREATE POLICY "Users can redeem codes" ON code_redemptions
  FOR INSERT TO authenticated
  WITH CHECK (user_id = auth.uid());

-- Policy: Users can see their own redemptions
CREATE POLICY "Users can see own redemptions" ON code_redemptions
  FOR SELECT TO authenticated
  USING (user_id = auth.uid());

-- Policy: Admins can see all redemptions
CREATE POLICY "Admins can see all redemptions" ON code_redemptions
  FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM user_profiles
      WHERE user_id = auth.uid() AND is_admin = true
    )
  );
