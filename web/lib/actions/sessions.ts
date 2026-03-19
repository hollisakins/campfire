'use server';

import { createClient } from '@/lib/supabase/server';

export interface Session {
  id: string;
  device_name: string | null;
  created_at: string;
  last_used_at: string | null;
  client_ip: string | null;
  user_agent: string | null;
}

export interface SessionsResult {
  sessions: Session[];
  error?: string;
}

/**
 * Get active (non-revoked, non-expired) CLI sessions for the current user.
 * Queries the refresh_tokens table — RLS scopes to the authenticated user.
 */
export async function getUserSessions(): Promise<SessionsResult> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return { sessions: [], error: 'Authentication required' };
  }

  try {
    const { data, error } = await supabase
      .from('refresh_tokens')
      .select('id, device_name, created_at, last_used_at, client_ip, user_agent')
      .eq('is_revoked', false)
      .gt('expires_at', new Date().toISOString())
      .order('last_used_at', { ascending: false, nullsFirst: false });

    if (error) {
      console.error('Error fetching sessions:', error);
      return { sessions: [], error: 'Failed to fetch sessions' };
    }

    return { sessions: data || [] };
  } catch (error) {
    console.error('Error fetching sessions:', error);
    return { sessions: [], error: 'Failed to fetch sessions' };
  }
}

/**
 * Revoke a CLI session by its refresh token ID.
 * Uses the existing revoke_refresh_token RPC (SECURITY DEFINER, validates ownership).
 */
export async function revokeSession(sessionId: string): Promise<{ success: boolean; error?: string }> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return { success: false, error: 'Authentication required' };
  }

  try {
    const { data, error } = await supabase.rpc('revoke_refresh_token', {
      p_token_id: sessionId,
      p_user_id: user.id,
    });

    if (error) {
      console.error('Error revoking session:', error);
      return { success: false, error: 'Failed to revoke session' };
    }

    return { success: data === true };
  } catch (error) {
    console.error('Error revoking session:', error);
    return { success: false, error: 'Failed to revoke session' };
  }
}
