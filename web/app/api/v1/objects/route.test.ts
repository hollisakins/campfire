// GET /api/v1/objects runs the objects RPC on the service-role client, so RLS
// does not scope each object's `lists`: the RPC does, from p_viewer_id. Guards
// that the key's user is passed, and that the deploy-window retry against an
// RPC without p_viewer_id (which does not scope lists) withholds them.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { NextRequest } from 'next/server';

vi.mock('server-only', () => ({}));

const USER = 'user-uuid';
vi.mock('@/lib/api-auth', () => ({
  authenticateApiRequest: async () => ({ userId: USER, method: 'api_key', access: { isAdmin: false, isLinkAccount: false, accessibleSlugs: ['pub'] } }),
}));
vi.mock('@/lib/server/bearer-release', () => ({ assertBearerReleaseAccess: async () => ({ tag: null }) }));
vi.mock('@/lib/api-helpers', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api-helpers')>('@/lib/api-helpers');
  return {
    ...actual,
    getAccessiblePrograms: async () => ['pub'],
    isAdminUser: async () => false,
  };
});

type RpcResult = { data: unknown; error: { code: string; message: string } | null };
let rpcCalls: Record<string, unknown>[] = [];
let rpcResults: RpcResult[] = [];
vi.mock('@/lib/supabase/server', () => ({
  createServiceClient: () => ({
    rpc: async (name: string, params: Record<string, unknown>) => {
      rpcCalls.push({ __rpc: name, ...params });
      return rpcResults.shift();
    },
  }),
}));

import { GET } from './route';

const privateList = { id: 7, name: 'Theirs', slug: 'theirs', icon: null, color: null };
const page = (lists: unknown[]) => ({
  data: [{ targets: [{ object_id: 'O-1', lists }], total_count: 1, has_more: false }],
  error: null,
});

function get() {
  return GET(new NextRequest('http://x/api/v1/objects', { headers: { authorization: 'Bearer sk_t' } }));
}

beforeEach(() => {
  rpcCalls = [];
  rpcResults = [];
});

describe('GET /api/v1/objects lists scoping', () => {
  it("passes the key's user as p_viewer_id and returns the RPC's lists", async () => {
    rpcResults = [page([privateList])];
    const res = await get();
    expect(res.status).toBe(200);
    expect(rpcCalls).toHaveLength(1);
    expect(rpcCalls[0].p_viewer_id).toBe(USER);
    const body = await res.json();
    expect(body.data[0].lists).toEqual([privateList]);
  });

  it('withholds lists when it falls back to an RPC without p_viewer_id', async () => {
    rpcResults = [
      { data: null, error: { code: 'PGRST202', message: 'no matching function' } },
      page([privateList]),
    ];
    const res = await get();
    expect(res.status).toBe(200);
    expect(rpcCalls).toHaveLength(2);
    expect(rpcCalls[1]).not.toHaveProperty('p_viewer_id');
    const body = await res.json();
    expect(body.data).toEqual([{ object_id: 'O-1', lists: [] }]);
  });

  it('does not retry on other errors', async () => {
    rpcResults = [{ data: null, error: { code: '57014', message: 'statement timeout' } }];
    const res = await get();
    expect(res.status).toBe(500);
    expect(rpcCalls).toHaveLength(1);
  });
});

// `q=` (Catalog Read Model phase 3): a predicate tree goes to
// catalog_query_objects in place of the filter parameters; the database's
// whitelist refusal (22023) is a 400 that says what was refused.
describe('GET /api/v1/objects with a predicate tree', () => {
  const tree = { and: [{ col: 'redshift', op: 'gt', v: 4.9 }, { exists: 'lines', where: [{ col: 'line', op: 'eq', v: 'CIII1908' }] }] };
  const getQ = (q: string) =>
    GET(new NextRequest(`http://x/api/v1/objects?q=${encodeURIComponent(q)}&sort=redshift&sort_dir=desc`, { headers: { authorization: 'Bearer sk_t' } }));

  it('selects through catalog_query_objects with the tree, the sort and the viewer', async () => {
    rpcResults = [page([])];
    const res = await getQ(JSON.stringify(tree));
    expect(res.status).toBe(200);
    expect(rpcCalls).toHaveLength(1);
    expect(rpcCalls[0].__rpc).toBe('catalog_query_objects');
    expect(rpcCalls[0].p_tree).toEqual(tree);
    expect(rpcCalls[0].p_program_slugs).toEqual(['pub']);
    expect(rpcCalls[0].p_sort_column).toBe('redshift');
    expect(rpcCalls[0].p_viewer_id).toBe(USER);
    expect(rpcCalls[0]).not.toHaveProperty('p_fields');
  });

  it('passes ra / dec as the distance reference for a tree query', async () => {
    rpcResults = [page([])];
    const res = await GET(new NextRequest(`http://x/api/v1/objects?q=${encodeURIComponent(JSON.stringify(tree))}&ra=150.1&dec=2.2&sort=distance`, { headers: { authorization: 'Bearer sk_t' } }));
    expect(res.status).toBe(200);
    expect(rpcCalls[0].__rpc).toBe('catalog_query_objects');
    expect(rpcCalls[0].p_coord_ra).toBe(150.1);
    expect(rpcCalls[0].p_coord_dec).toBe(2.2);
    expect(rpcCalls[0].p_sort_column).toBe('distance');
  });

  it('rejects a q that is not a JSON object before any RPC', async () => {
    for (const bad of ['{not json', '[1,2]', '"x"']) {
      const res = await getQ(bad);
      expect(res.status).toBe(400);
    }
    expect(rpcCalls).toHaveLength(0);
  });

  it("turns the database's whitelist refusal into a 400 with its message", async () => {
    rpcResults = [{ data: null, error: { code: '22023', message: 'catalog_query: objects has no column passwd' } }];
    const res = await getQ(JSON.stringify({ col: 'passwd', op: 'eq', v: 1 }));
    expect(res.status).toBe(400);
    expect((await res.json()).error).toBe('catalog_query: objects has no column passwd');
    expect(rpcCalls).toHaveLength(1);
  });

  it('does not fall back to the legacy RPC for a tree', async () => {
    rpcResults = [{ data: null, error: { code: 'PGRST202', message: 'no matching function' } }];
    const res = await getQ(JSON.stringify(tree));
    expect(res.status).toBe(500);
    expect(rpcCalls).toHaveLength(1);
  });
});
