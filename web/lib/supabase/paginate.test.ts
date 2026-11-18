import { describe, expect, it, vi } from 'vitest';
import type { SupabaseClient } from '@supabase/supabase-js';
import { listAllAuthUsers } from './paginate';

function fakeClient(total: number, failOnPage?: number) {
  const listUsers = vi.fn(async ({ page, perPage }: { page: number; perPage: number }) => {
    if (page === failOnPage) return { data: { users: [] }, error: { message: 'boom' } };
    const start = (page - 1) * perPage;
    const n = Math.max(0, Math.min(perPage, total - start));
    return { data: { users: Array.from({ length: n }, (_, i) => ({ id: `u${start + i}` })) }, error: null };
  });
  return { client: { auth: { admin: { listUsers } } } as unknown as SupabaseClient, listUsers };
}

describe('listAllAuthUsers', () => {
  it('walks every page past the first', async () => {
    const { client, listUsers } = fakeClient(2500);
    const { users, error } = await listAllAuthUsers(client);
    expect(error).toBeNull();
    expect(users).toHaveLength(2500);
    expect(listUsers).toHaveBeenCalledTimes(3);
  });

  it('stops after an exactly full page with one empty read', async () => {
    const { client, listUsers } = fakeClient(1000);
    const { users } = await listAllAuthUsers(client);
    expect(users).toHaveLength(1000);
    expect(listUsers).toHaveBeenCalledTimes(2);
  });

  it('reports an error', async () => {
    const { client } = fakeClient(2500, 2);
    const { error } = await listAllAuthUsers(client);
    expect(error?.message).toBe('boom');
  });
});
