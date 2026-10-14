// GET /api/v1/objects runs the objects RPC on the service-role client, so RLS
// does not scope each object's `lists`: the RPC does, from p_viewer_id. Guards
// that the key's user is passed, and that the deploy-window retry against an
// RPC without p_viewer_id (which does not scope lists) withholds them.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { NextRequest } from 'next/server';

vi.mock('server-only', () => ({}));

const USER = 'user-uuid';
vi.mock('@/lib/api-auth', () => ({ validateAuth: async () => USER }));
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
    rpc: async (_name: string, params: Record<string, unknown>) => {
      rpcCalls.push(params);
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
