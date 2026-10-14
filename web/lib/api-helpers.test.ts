// resolveListIds runs on the service-role client in the bearer routes, so it is
// the only thing between an API key and filtering by (and so enumerating) the
// members of someone else's private list. It must keep only the lists
// viewable_list_ids() would give the viewer: owned, public, or shared.
import { describe, it, expect, vi } from 'vitest';
import type { SupabaseClient } from '@supabase/supabase-js';

vi.mock('@/lib/supabase/service', () => ({ createServiceClient: vi.fn() }));

import { resolveListIds } from './api-helpers';

const VIEWER = 'viewer-uuid';
const OTHER = 'other-uuid';

const LISTS = [
  { id: 1, slug: 'mine-private', created_by: VIEWER, visibility: 'private' },
  { id: 2, slug: 'theirs-private', created_by: OTHER, visibility: 'private' },
  { id: 3, slug: 'theirs-read', created_by: OTHER, visibility: 'public_read' },
  { id: 4, slug: 'theirs-edit', created_by: OTHER, visibility: 'public_edit' },
  { id: 5, slug: 'theirs-shared', created_by: OTHER, visibility: 'private' },
  { id: 6, slug: 'system', created_by: null, visibility: 'private' },
];

/** The two PostgREST queries resolveListIds issues, answered from fixtures. */
function fakeClient(shares: { data: unknown; error: unknown }) {
  const from = vi.fn((table: string) => ({
    select: () => {
      if (table === 'object_lists') {
        return {
          in: (_col: string, slugs: string[]) =>
            Promise.resolve({ data: LISTS.filter(l => slugs.includes(l.slug)), error: null }),
        };
      }
      return {
        eq: (_col: string, userId: string) =>
          Promise.resolve(userId === VIEWER ? shares : { data: [], error: null }),
      };
    },
  }));
  return { from } as unknown as SupabaseClient;
}

const ALL_SLUGS = LISTS.map(l => l.slug);

describe('resolveListIds', () => {
  it('returns null when no slugs were given (no list filter)', async () => {
    const client = fakeClient({ data: [], error: null });
    expect(await resolveListIds(client, null, VIEWER)).toBeNull();
    expect(await resolveListIds(client, [], VIEWER)).toBeNull();
  });

  it("keeps owned, public and shared lists and drops other users' private ones", async () => {
    const client = fakeClient({ data: [{ list_id: 5 }], error: null });
    expect(await resolveListIds(client, ALL_SLUGS, VIEWER)).toEqual([1, 3, 4, 5]);
  });

  it('resolves an invisible list exactly like an unknown slug', async () => {
    const client = fakeClient({ data: [], error: null });
    expect(await resolveListIds(client, ['theirs-private'], VIEWER)).toEqual([]);
    expect(await resolveListIds(client, ['no-such-list'], VIEWER)).toEqual([]);
  });

  it('fails closed to owned and public lists when the shares lookup fails', async () => {
    const client = fakeClient({ data: null, error: { message: 'boom' } });
    expect(await resolveListIds(client, ALL_SLUGS, VIEWER)).toEqual([1, 3, 4]);
  });
});
