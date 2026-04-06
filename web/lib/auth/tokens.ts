import crypto from 'crypto';
import { createClient } from '@supabase/supabase-js';
import { SignJWT, jwtVerify, JWTPayload } from 'jose';

// Token configuration
const ACCESS_TOKEN_EXPIRY_HOURS = 1;
const REFRESH_TOKEN_EXPIRY_DAYS = 90;

// JWT secret - should be set in environment variables
function getJwtSecret(): Uint8Array {
  const secret = process.env.JWT_SECRET;
  if (!secret) {
    throw new Error('JWT_SECRET environment variable is not set');
  }
  return new TextEncoder().encode(secret);
}

// Supabase JWT secret - for minting Supabase-compatible tokens
function getSupabaseJwtSecret(): Uint8Array {
  const secret = process.env.SUPABASE_JWT_SECRET;
  if (!secret) {
    throw new Error('SUPABASE_JWT_SECRET environment variable is not set');
  }
  return new TextEncoder().encode(secret);
}

/**
 * Hash a refresh token using SHA-256
 */
export function hashRefreshToken(token: string): string {
  return crypto.createHash('sha256').update(token).digest('hex');
}

/**
 * Generate a cryptographically secure refresh token
 * Format: rt_<64 random hex chars>
 */
export function generateRefreshToken(): string {
  const randomBytes = crypto.randomBytes(32);
  return `rt_${randomBytes.toString('hex')}`;
}

interface AccessTokenPayload extends JWTPayload {
  sub: string; // user_id
  email?: string;
  type: 'access';
}

/**
 * Generate a signed JWT access token
 */
export async function generateAccessToken(
  userId: string,
  email?: string
): Promise<{ token: string; expiresIn: number; expiresAt: Date }> {
  const secret = getJwtSecret();
  const expiresAt = new Date(Date.now() + ACCESS_TOKEN_EXPIRY_HOURS * 60 * 60 * 1000);
  const expiresIn = ACCESS_TOKEN_EXPIRY_HOURS * 60 * 60; // in seconds

  const token = await new SignJWT({
    sub: userId,
    email,
    type: 'access',
  } as AccessTokenPayload)
    .setProtectedHeader({ alg: 'HS256' })
    .setIssuedAt()
    .setExpirationTime(expiresAt)
    .setIssuer('campfire')
    .setAudience('campfire-api')
    .sign(secret);

  return { token, expiresIn, expiresAt };
}

/**
 * Generate a Supabase-compatible JWT for CLI deploy operations.
 *
 * This token is signed with SUPABASE_JWT_SECRET so Supabase RLS policies
 * can authenticate the user directly, eliminating the need to distribute
 * the service_role_key.
 */
export async function generateSupabaseToken(
  userId: string,
  email?: string
): Promise<{ token: string; expiresIn: number }> {
  const secret = getSupabaseJwtSecret();
  const expiresIn = ACCESS_TOKEN_EXPIRY_HOURS * 60 * 60;
  const expiresAt = new Date(Date.now() + expiresIn * 1000);

  const token = await new SignJWT({
    sub: userId,
    email,
    role: 'authenticated',
  })
    .setProtectedHeader({ alg: 'HS256' })
    .setIssuedAt()
    .setExpirationTime(expiresAt)
    .setIssuer('supabase')
    .setAudience('authenticated')
    .sign(secret);

  return { token, expiresIn };
}

/**
 * Validate a JWT access token
 * Returns user_id if valid, null otherwise
 */
export async function validateAccessToken(token: string): Promise<string | null> {
  try {
    const secret = getJwtSecret();

    const { payload } = await jwtVerify(token, secret, {
      issuer: 'campfire',
      audience: 'campfire-api',
    });

    const accessPayload = payload as AccessTokenPayload;

    if (accessPayload.type !== 'access') {
      return null;
    }

    return accessPayload.sub || null;
  } catch (error) {
    // Token is invalid or expired
    return null;
  }
}

/**
 * Create and store a new refresh token
 */
export async function createRefreshToken(
  userId: string,
  deviceName?: string,
  clientIp?: string,
  userAgent?: string
): Promise<{ token: string; expiresAt: Date }> {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
  const supabase = createClient(supabaseUrl, supabaseServiceKey);

  const token = generateRefreshToken();
  const tokenHash = hashRefreshToken(token);
  const expiresAt = new Date(Date.now() + REFRESH_TOKEN_EXPIRY_DAYS * 24 * 60 * 60 * 1000);

  const { error } = await supabase.from('refresh_tokens').insert({
    token_hash: tokenHash,
    user_id: userId,
    device_name: deviceName,
    expires_at: expiresAt.toISOString(),
    client_ip: clientIp,
    user_agent: userAgent,
  });

  if (error) {
    console.error('Failed to create refresh token:', error);
    throw new Error('Failed to create refresh token');
  }

  return { token, expiresAt };
}

/**
 * Validate a refresh token
 * Returns user_id and token_id if valid, null otherwise
 */
export async function validateRefreshToken(
  token: string
): Promise<{ userId: string; tokenId: string } | null> {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
  const supabase = createClient(supabaseUrl, supabaseServiceKey);

  const tokenHash = hashRefreshToken(token);

  const { data, error } = await supabase.rpc('validate_refresh_token', {
    p_token_hash: tokenHash,
  });

  if (error || !data || data.length === 0) {
    return null;
  }

  const result = data[0];

  if (!result.is_valid) {
    return null;
  }

  return {
    userId: result.user_id,
    tokenId: result.token_id,
  };
}

/**
 * Rotate a refresh token (invalidate old, create new)
 * Returns new tokens if successful
 */
export async function rotateRefreshToken(
  oldToken: string,
  clientIp?: string,
  userAgent?: string
): Promise<{
  accessToken: string;
  refreshToken: string;
  supabaseToken: string;
  expiresIn: number;
  userId: string;
} | null> {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
  const supabase = createClient(supabaseUrl, supabaseServiceKey);

  const oldTokenHash = hashRefreshToken(oldToken);
  const newToken = generateRefreshToken();
  const newTokenHash = hashRefreshToken(newToken);
  const expiresAt = new Date(Date.now() + REFRESH_TOKEN_EXPIRY_DAYS * 24 * 60 * 60 * 1000);

  const { data, error } = await supabase.rpc('rotate_refresh_token', {
    p_old_token_hash: oldTokenHash,
    p_new_token_hash: newTokenHash,
    p_expires_at: expiresAt.toISOString(),
    p_client_ip: clientIp,
    p_user_agent: userAgent,
  });

  if (error || !data || data.length === 0) {
    console.error('Failed to rotate refresh token:', error);
    return null;
  }

  const result = data[0];

  if (!result.success) {
    return null;
  }

  // Generate new access token and supabase token
  const email = await getUserEmail(result.user_id);
  const [accessResult, supabaseResult] = await Promise.all([
    generateAccessToken(result.user_id, email || undefined),
    generateSupabaseToken(result.user_id, email || undefined),
  ]);

  return {
    accessToken: accessResult.token,
    refreshToken: newToken,
    supabaseToken: supabaseResult.token,
    expiresIn: accessResult.expiresIn,
    userId: result.user_id,
  };
}

/**
 * Revoke a specific refresh token
 */
export async function revokeRefreshToken(
  tokenId: string,
  userId: string
): Promise<boolean> {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
  const supabase = createClient(supabaseUrl, supabaseServiceKey);

  const { data, error } = await supabase.rpc('revoke_refresh_token', {
    p_token_id: tokenId,
    p_user_id: userId,
  });

  if (error) {
    console.error('Failed to revoke refresh token:', error);
    return false;
  }

  return data === true;
}

/**
 * Revoke all refresh tokens for a user
 */
export async function revokeAllUserRefreshTokens(userId: string): Promise<number> {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
  const supabase = createClient(supabaseUrl, supabaseServiceKey);

  const { data, error } = await supabase.rpc('revoke_all_user_refresh_tokens', {
    p_user_id: userId,
  });

  if (error) {
    console.error('Failed to revoke all refresh tokens:', error);
    return 0;
  }

  return data || 0;
}

/**
 * Get user email by user_id (for token generation)
 */
export async function getUserEmail(userId: string): Promise<string | null> {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
  const supabase = createClient(supabaseUrl, supabaseServiceKey);

  const { data, error } = await supabase.auth.admin.getUserById(userId);

  if (error || !data.user) {
    return null;
  }

  return data.user.email || null;
}

/**
 * Issue a complete set of tokens for a user
 * Used after successful device authorization
 */
export async function issueTokens(
  userId: string,
  deviceName?: string,
  clientIp?: string,
  userAgent?: string
): Promise<{
  accessToken: string;
  refreshToken: string;
  supabaseToken: string;
  expiresIn: number;
  tokenType: string;
}> {
  const email = await getUserEmail(userId);

  const [accessTokenResult, refreshTokenResult, supabaseTokenResult] = await Promise.all([
    generateAccessToken(userId, email || undefined),
    createRefreshToken(userId, deviceName, clientIp, userAgent),
    generateSupabaseToken(userId, email || undefined),
  ]);

  return {
    accessToken: accessTokenResult.token,
    refreshToken: refreshTokenResult.token,
    supabaseToken: supabaseTokenResult.token,
    expiresIn: accessTokenResult.expiresIn,
    tokenType: 'Bearer',
  };
}
