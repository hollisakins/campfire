// The sync routes and the nightly snapshot builder share fetchSyncPage, so a
// snapshot row is exactly what a live walk returns. These pin the per-stream
// RPC argument mapping, the deploy-window fallbacks and the snapshot extras
// resolution.
import { describe, it, expect, vi } from 'vitest';

vi.mock('server-only', () => ({}));

import { fetchSyncPage, type SyncPageRequest } from './sync-streams';
import { resolveSnapshotExtras } from './sync-snapshot';

type RpcCall = { fn: string; args: Record<string, unknown> };

function fakeRpc(responses: Array<{ data?: unknown; error?: unknown }>) {
  const calls: RpcCall[] = [];
  const client = {
    rpc: async (fn: string, args: Record<string, unknown>) => {
      calls.push({ fn, args });
      const r = responses.shift() ?? { data: [] };
      return { data: r.data ?? null, error: r.error ?? null };
    },
  };
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return { client: client as any, calls };
}

const base: SyncPageRequest = {
  programSlugs: ['p1', 'p2'],
  userId: 'u1',
  updatedSince: null,
  limit: 5000,
  includeCounts: true,
  includeUnpublished: false,
  after: null,
};

describe('fetchSyncPage', () => {
  it('maps the objects stream and returns its rows, counts and tombstones', async () => {
    const { client, calls } = fakeRpc([
      { data: [{ objects: [{ object_id: 'a' }], total_count: 7, total_accessible_count: 9, deleted_ids: [3] }] },
    ]);
    const { page } = await fetchSyncPage(client, 'objects', { ...base, after: 'J1' });
    expect(calls[0].fn).toBe('get_objects_for_sync');
    expect(calls[0].args).toEqual({
      p_program_slugs: ['p1', 'p2'],
      p_user_id: 'u1',
      p_updated_since: null,
      p_limit: 5000,
      p_include_counts: true,
      p_include_unpublished: false,
      p_after_object_id: 'J1',
    });
    expect(page).toEqual({ rows: [{ object_id: 'a' }], total: 7, totalAccessible: 9, deletedIds: [3] });
  });

  it('sends the objects extras filter only in extras mode', async () => {
    const { client, calls } = fakeRpc([{ data: [{ objects: [] }] }]);
    await fetchSyncPage(client, 'objects', { ...base, filterProgramSlugs: ['p9'] });
    expect(calls[0].args.p_filter_program_slugs).toEqual(['p9']);
  });

  it('reads photometry / line-fit rows from their own fields, with no accessible count', async () => {
    const { client, calls } = fakeRpc([
      { data: [{ photometry_records: [{ id: 1 }], total_count: 1 }] },
      { data: [{ line_fit_records: [{ spectrum_id: 2 }], total_count: 1 }] },
    ]);
    const phot = await fetchSyncPage(client, 'photometry', { ...base, after: 10 });
    const lines = await fetchSyncPage(client, 'lines', base);
    expect(calls.map((c) => c.fn)).toEqual(['get_photometry_for_sync', 'get_line_fits_for_sync']);
    expect(calls[0].args.p_after_id).toBe(10);
    expect('p_user_id' in calls[0].args).toBe(false);
    expect(phot.page?.rows).toEqual([{ id: 1 }]);
    expect(phot.page?.totalAccessible).toBeNull();
    expect(lines.page?.rows).toEqual([{ spectrum_id: 2 }]);
  });

  it('retries a scoped storage page unscoped when the RPC predates the scope args', async () => {
    const { client, calls } = fakeRpc([
      { error: { code: 'PGRST202', message: 'no function' } },
      { data: [{ objects: [{ id: 5 }], total_count: 1, total_accessible_count: 1 }] },
    ]);
    const { page, error } = await fetchSyncPage(client, 'storage', {
      ...base,
      productTypes: ['nirspec_spec'],
    });
    expect(error).toBeNull();
    expect(calls).toHaveLength(2);
    expect(calls[0].args.p_product_types).toEqual(['nirspec_spec']);
    expect('p_product_types' in calls[1].args).toBe(false);
    expect(page?.rows).toEqual([{ id: 5 }]);
  });

  it('surfaces any other RPC error', async () => {
    const { client } = fakeRpc([{ error: { code: '57014', message: 'timeout' } }]);
    const { page, error } = await fetchSyncPage(client, 'spectra', base);
    expect(page).toBeNull();
    expect(error).toMatchObject({ code: '57014' });
  });
});

function fakeSnapshots(row: { public_programs: string[] } | null) {
  const filters: Array<[string, unknown]> = [];
  const query = {
    select: () => query,
    eq: (col: string, v: unknown) => {
      filters.push([col, v]);
      return query;
    },
    maybeSingle: async () => ({ data: row, error: null }),
  };
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return { client: { from: () => query } as any, filters };
}

describe('resolveSnapshotExtras', () => {
  const params = (q = '') => new URLSearchParams(q);

  it('is the accessible programs minus the snapshot programs, read from the ready row', async () => {
    const { client, filters } = fakeSnapshots({ public_programs: ['p1', 'p2'] });
    const r = await resolveSnapshotExtras(client, '4', ['p1', 'p2', 'p3', 'p7'], params());
    expect(r.extras).toEqual(['p3', 'p7']);
    expect(filters).toEqual([['id', 4], ['status', 'ready']]);
  });

  it('includes a program made public after the build', async () => {
    const { client } = fakeSnapshots({ public_programs: ['p1'] });
    const r = await resolveSnapshotExtras(client, '4', ['p1', 'p2'], params());
    expect(r.extras).toEqual(['p2']);
  });

  it('refuses updated_since, malformed ids and unknown snapshots', async () => {
    const { client } = fakeSnapshots(null);
    for (const [id, q] of [['4', 'updated_since=2026-01-01'], ['x', ''], ['-1', ''], ['4', '']]) {
      const r = await resolveSnapshotExtras(client, id, ['p1'], params(q));
      expect(r.extras).toBeNull();
      expect(r.error?.status).toBe(400);
    }
  });
});
