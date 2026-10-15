import { describe, expect, it, vi } from 'vitest';

// The module imports next/headers and next/navigation; neither is needed for
// the confirmation itself, so both are stubbed.
vi.mock('next/headers', () => ({ cookies: async () => ({ getAll: () => [], set: () => {} }) }));
const notFound = vi.fn(() => { throw new Error('NEXT_NOT_FOUND'); });
vi.mock('next/navigation', () => ({ notFound: () => notFound() }));
vi.mock('@/lib/dr/server', () => ({ requestDrTag: async () => null }));

import { confirmBrowseTag } from './server';
import type { SupabaseClient } from '@supabase/supabase-js';

function clientAnswering(result: { data?: unknown; error?: { code?: string; message: string } | null }) {
  return { rpc: vi.fn(async () => ({ data: result.data ?? null, error: result.error ?? null })) } as unknown as SupabaseClient;
}

describe('confirmBrowseTag (a release request never falls back to live)', () => {
  it('passes when the database echoes the tag', async () => {
    const c = clientAnswering({ data: 'DR-seed' });
    await expect(confirmBrowseTag(c, 'DR-seed')).resolves.toBeUndefined();
    expect(c.rpc).toHaveBeenCalledWith('dr_browse_tag');
  });
  it('is the 404 when the hook refuses the tag', async () => {
    const c = clientAnswering({ error: { code: 'PT404', message: 'unknown data release' } });
    await expect(confirmBrowseTag(c, 'DR-nope')).rejects.toThrow('NEXT_NOT_FOUND');
    expect(notFound).toHaveBeenCalled();
  });
  it('is the 404 for an anonymous caller (no EXECUTE on the function)', async () => {
    const c = clientAnswering({ error: { code: '42501', message: 'permission denied for function dr_browse_tag' } });
    await expect(confirmBrowseTag(c, 'DR-seed')).rejects.toThrow('NEXT_NOT_FOUND');
  });
  it('throws when the database answers live (hook not registered)', async () => {
    const c = clientAnswering({ data: null });
    await expect(confirmBrowseTag(c, 'DR-seed')).rejects.toThrow(/pre-request hook not active/);
  });
  it('throws on any other error', async () => {
    const c = clientAnswering({ error: { message: 'boom' } });
    await expect(confirmBrowseTag(c, 'DR-seed')).rejects.toThrow('boom');
  });
});
