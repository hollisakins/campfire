import { NextRequest } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import crypto from 'crypto';

/**
 * Hash an API key using SHA-256
 * This is a one-way hash for secure storage
 */
export function hashApiKey(apiKey: string): string {
  return crypto.createHash('sha256').update(apiKey).digest('hex');
}

/**
 * Validate API key from request headers
 * Returns user_id if valid, null otherwise
 */
export async function validateApiKey(request: NextRequest): Promise<string | null> {
  // Get API key from Authorization header
  const authHeader = request.headers.get('authorization');

  if (!authHeader || !authHeader.startsWith('Bearer ')) {
    return null;
  }

  const apiKey = authHeader.replace('Bearer ', '');

  if (!apiKey || !apiKey.startsWith('sk_')) {
    return null;
  }

  // Create service role client to bypass RLS
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;

  const supabase = createClient(supabaseUrl, supabaseServiceKey);

  // Hash the API key
  const keyHash = hashApiKey(apiKey);

  // Validate using the database function
  const { data, error } = await supabase.rpc('validate_api_key', {
    key_hash_input: keyHash
  });

  if (error || !data || data.length === 0) {
    return null;
  }

  const result = data[0];

  if (!result.is_valid) {
    return null;
  }

  // Update last_used_at timestamp asynchronously (don't wait for it)
  supabase.rpc('update_api_key_last_used', {
    key_hash_input: keyHash
  }).then(() => {}).catch((err) => {
    console.error('Failed to update API key last_used_at:', err);
  });

  return result.user_id;
}

/**
 * Generate a new API key
 * Format: sk_live_<32 random hex chars>
 */
export function generateApiKey(): { key: string; prefix: string; hash: string } {
  const randomBytes = crypto.randomBytes(32);
  const randomHex = randomBytes.toString('hex');
  const key = `sk_live_${randomHex}`;
  const prefix = `sk_live_${randomHex.substring(0, 8)}...`;
  const hash = hashApiKey(key);

  return { key, prefix, hash };
}

/**
 * Check if user has access to a specific program based on RLS policies
 * This leverages the existing RLS infrastructure
 */
export async function checkProgramAccess(
  userId: string,
  programId: number
): Promise<boolean> {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;

  const supabase = createClient(supabaseUrl, supabaseServiceKey, {
    global: {
      headers: {
        // Set the user context for RLS
        'sb-user-id': userId
      }
    }
  });

  // Try to fetch a program - RLS will filter based on user access
  const { data, error } = await supabase
    .from('programs')
    .select('program_id')
    .eq('program_id', programId)
    .single();

  return !error && !!data;
}
