import crypto from 'crypto';
import { createClient } from '@supabase/supabase-js';

// Base-20 alphabet excluding confusable characters (0/O, 1/l/I)
const USER_CODE_ALPHABET = 'BCDFGHJKMNPQRSTVWXYZ';
const USER_CODE_LENGTH = 8;
const DEVICE_CODE_EXPIRY_MINUTES = 15;
const POLLING_INTERVAL_SECONDS = 5;

/**
 * Generate a cryptographically secure device code
 * Format: 32 random bytes as hex (64 characters)
 */
export function generateDeviceCode(): string {
  return crypto.randomBytes(32).toString('hex');
}

/**
 * Generate a human-readable user code
 * Format: XXXX-XXXX (8 characters from base-20 alphabet)
 */
export function generateUserCode(): string {
  const bytes = crypto.randomBytes(USER_CODE_LENGTH);
  let code = '';

  for (let i = 0; i < USER_CODE_LENGTH; i++) {
    // Use modulo to map byte to alphabet character
    const index = bytes[i] % USER_CODE_ALPHABET.length;
    code += USER_CODE_ALPHABET[index];
  }

  // Format as XXXX-XXXX for readability
  return `${code.substring(0, 4)}-${code.substring(4, 8)}`;
}

/**
 * Normalize user code input (remove dashes, uppercase)
 */
export function normalizeUserCode(code: string): string {
  return code.replace(/-/g, '').toUpperCase();
}

/**
 * Create a new device authorization request
 */
export async function createDeviceAuthorization(
  verificationUri: string,
  clientIp?: string,
  userAgent?: string
): Promise<{
  deviceCode: string;
  userCode: string;
  verificationUri: string;
  verificationUriComplete: string;
  expiresIn: number;
  interval: number;
}> {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
  const supabase = createClient(supabaseUrl, supabaseServiceKey);

  const deviceCode = generateDeviceCode();
  const userCode = generateUserCode();
  const expiresAt = new Date(Date.now() + DEVICE_CODE_EXPIRY_MINUTES * 60 * 1000);

  const { error } = await supabase.from('device_codes').insert({
    device_code: deviceCode,
    user_code: normalizeUserCode(userCode), // Store normalized
    verification_uri: verificationUri,
    expires_at: expiresAt.toISOString(),
    interval_seconds: POLLING_INTERVAL_SECONDS,
    status: 'pending',
    client_ip: clientIp,
    user_agent: userAgent,
  });

  if (error) {
    console.error('Failed to create device authorization:', error);
    throw new Error('Failed to create device authorization');
  }

  return {
    deviceCode,
    userCode, // Return formatted for display
    verificationUri,
    verificationUriComplete: `${verificationUri}?code=${encodeURIComponent(userCode)}`,
    expiresIn: DEVICE_CODE_EXPIRY_MINUTES * 60,
    interval: POLLING_INTERVAL_SECONDS,
  };
}

/**
 * Check the status of a device code (for polling endpoint)
 */
export async function checkDeviceCodeStatus(
  deviceCode: string
): Promise<{
  status: 'pending' | 'authorized' | 'denied' | 'expired' | 'not_found';
  userId?: string;
}> {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
  const supabase = createClient(supabaseUrl, supabaseServiceKey);

  const { data, error } = await supabase.rpc('check_device_code_status', {
    p_device_code: deviceCode,
  });

  if (error || !data || data.length === 0) {
    return { status: 'not_found' };
  }

  const result = data[0];

  if (result.is_expired) {
    return { status: 'expired' };
  }

  return {
    status: result.status,
    userId: result.user_id,
  };
}

/**
 * Authorize a device code (called when user approves in browser)
 */
export async function authorizeDeviceCode(
  userCode: string,
  userId: string
): Promise<boolean> {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
  const supabase = createClient(supabaseUrl, supabaseServiceKey);

  const normalizedCode = normalizeUserCode(userCode);

  const { data, error } = await supabase.rpc('authorize_device_code', {
    p_user_code: normalizedCode,
    p_user_id: userId,
  });

  if (error) {
    console.error('Failed to authorize device code:', error);
    return false;
  }

  return data === true;
}

/**
 * Deny a device code (called when user denies in browser)
 */
export async function denyDeviceCode(userCode: string): Promise<boolean> {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
  const supabase = createClient(supabaseUrl, supabaseServiceKey);

  const normalizedCode = normalizeUserCode(userCode);

  const { data, error } = await supabase.rpc('deny_device_code', {
    p_user_code: normalizedCode,
  });

  if (error) {
    console.error('Failed to deny device code:', error);
    return false;
  }

  return data === true;
}

/**
 * Consume a device code after tokens are issued
 * Returns user_id if successful, null otherwise
 */
export async function consumeDeviceCode(deviceCode: string): Promise<string | null> {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
  const supabase = createClient(supabaseUrl, supabaseServiceKey);

  const { data, error } = await supabase.rpc('consume_device_code', {
    p_device_code: deviceCode,
  });

  if (error) {
    console.error('Failed to consume device code:', error);
    return null;
  }

  return data;
}

/**
 * Get device code info by user code (for verification page)
 */
export async function getDeviceCodeByUserCode(
  userCode: string
): Promise<{
  exists: boolean;
  isExpired: boolean;
  isPending: boolean;
} | null> {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
  const supabase = createClient(supabaseUrl, supabaseServiceKey);

  const normalizedCode = normalizeUserCode(userCode);

  const { data, error } = await supabase
    .from('device_codes')
    .select('status, expires_at')
    .eq('user_code', normalizedCode)
    .single();

  if (error || !data) {
    return null;
  }

  const isExpired = new Date(data.expires_at) < new Date();

  return {
    exists: true,
    isExpired,
    isPending: data.status === 'pending' && !isExpired,
  };
}
