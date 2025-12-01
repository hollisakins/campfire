'use server';

import { createClient } from '@/lib/supabase/server';
import { generateApiKey } from '@/lib/api-auth';

export interface ApiKey {
  id: string;
  key_prefix: string;
  name: string | null;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  is_active: boolean;
}

export interface CreateApiKeyResult {
  success: boolean;
  key?: string;  // Only returned once, on creation
  error?: string;
}

export interface ApiKeysResult {
  keys: ApiKey[];
  error?: string;
}

/**
 * Create a new API key for the current user
 */
export async function createUserApiKey(name?: string): Promise<CreateApiKeyResult> {
  const supabase = await createClient();

  // Check authentication
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return {
      success: false,
      error: 'Authentication required',
    };
  }

  try {
    // Generate new API key
    const { key, prefix, hash } = generateApiKey();

    // Insert into database
    const { error } = await supabase.from('api_keys').insert({
      user_id: user.id,
      key_hash: hash,
      key_prefix: prefix,
      name: name || null,
    });

    if (error) {
      console.error('Error creating API key:', error);
      return {
        success: false,
        error: 'Failed to create API key',
      };
    }

    // Return the unhashed key (only time it's visible!)
    return {
      success: true,
      key,
    };
  } catch (error) {
    console.error('Error creating API key:', error);
    return {
      success: false,
      error: 'Failed to create API key',
    };
  }
}

/**
 * Get all API keys for the current user
 */
export async function getUserApiKeys(): Promise<ApiKeysResult> {
  const supabase = await createClient();

  // Check authentication
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return {
      keys: [],
      error: 'Authentication required',
    };
  }

  try {
    const { data: keys, error } = await supabase
      .from('api_keys')
      .select('id, key_prefix, name, created_at, last_used_at, expires_at, is_active')
      .eq('user_id', user.id)
      .order('created_at', { ascending: false });

    if (error) {
      console.error('Error fetching API keys:', error);
      return {
        keys: [],
        error: 'Failed to fetch API keys',
      };
    }

    return {
      keys: keys || [],
    };
  } catch (error) {
    console.error('Error fetching API keys:', error);
    return {
      keys: [],
      error: 'Failed to fetch API keys',
    };
  }
}

/**
 * Revoke (deactivate) an API key
 */
export async function revokeApiKey(keyId: string): Promise<{ success: boolean; error?: string }> {
  const supabase = await createClient();

  // Check authentication
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return {
      success: false,
      error: 'Authentication required',
    };
  }

  try {
    // Update the key to mark it as inactive
    const { error } = await supabase
      .from('api_keys')
      .update({ is_active: false })
      .eq('id', keyId)
      .eq('user_id', user.id); // Ensure user owns this key

    if (error) {
      console.error('Error revoking API key:', error);
      return {
        success: false,
        error: 'Failed to revoke API key',
      };
    }

    return {
      success: true,
    };
  } catch (error) {
    console.error('Error revoking API key:', error);
    return {
      success: false,
      error: 'Failed to revoke API key',
    };
  }
}

/**
 * Delete an API key permanently
 */
export async function deleteApiKey(keyId: string): Promise<{ success: boolean; error?: string }> {
  const supabase = await createClient();

  // Check authentication
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return {
      success: false,
      error: 'Authentication required',
    };
  }

  try {
    const { error } = await supabase
      .from('api_keys')
      .delete()
      .eq('id', keyId)
      .eq('user_id', user.id); // Ensure user owns this key

    if (error) {
      console.error('Error deleting API key:', error);
      return {
        success: false,
        error: 'Failed to delete API key',
      };
    }

    return {
      success: true,
    };
  } catch (error) {
    console.error('Error deleting API key:', error);
    return {
      success: false,
      error: 'Failed to delete API key',
    };
  }
}
