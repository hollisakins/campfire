// The server-side write guard (P6c-1): a `?dr=` request never writes.
import { describe, expect, it, vi } from 'vitest';

let drTag: string | null = null;
vi.mock('server-only', () => ({}));
vi.mock('next/headers', () => ({
  headers: async () => new Headers(drTag ? { 'x-campfire-dr': drTag } : {}),
}));

import { assertLiveRequest, readOnlyResponse, requestDrTag } from './server';

describe('requestDrTag / assertLiveRequest / readOnlyResponse', () => {
  it('reads the stamped header', async () => {
    drTag = 'DR-seed';
    expect(await requestDrTag()).toBe('DR-seed');
    drTag = null;
    expect(await requestDrTag()).toBeNull();
  });
  it('lets a live request through', async () => {
    drTag = null;
    await expect(assertLiveRequest()).resolves.toBeUndefined();
    expect(await readOnlyResponse()).toBeNull();
  });
  it('refuses under a release with a 409', async () => {
    drTag = 'DR-seed';
    await expect(assertLiveRequest()).rejects.toMatchObject({ status: 409 });
    const res = await readOnlyResponse();
    expect(res?.status).toBe(409);
    expect(await res?.json()).toEqual({ error: 'read-only under a data release' });
  });
});

describe('a mutating route and a mutating action under a release', () => {
  it('the inspect route answers 409 before touching identity', async () => {
    drTag = 'DR-seed';
    vi.doMock('@/lib/auth/identity', () => ({ getRequestIdentity: vi.fn(async () => { throw new Error('must not be called'); }) }));
    vi.doMock('@/lib/auth/access-context', () => ({ getAccessContext: vi.fn() }));
    const { PATCH } = await import('@/app/api/objects/[id]/inspect/route');
    const { NextRequest } = await import('next/server');
    const res = await PATCH(new NextRequest('http://x/api/objects/1/inspect', { method: 'PATCH' }), { params: Promise.resolve({ id: '1' }) });
    expect(res.status).toBe(409);
  });
  it('addObjectToList refuses with its error shape', async () => {
    drTag = 'DR-seed';
    vi.doMock('@/lib/auth/identity', () => ({ getRequestIdentity: vi.fn(async () => { throw new Error('must not be called'); }) }));
    const { addObjectToList } = await import('@/lib/actions/lists');
    expect(await addObjectToList(1, 2, 0, 0)).toEqual({ error: 'read-only under a data release' });
  });
  it('addObjectToList proceeds live', async () => {
    drTag = null;
    vi.doMock('@/lib/auth/identity', () => ({ getRequestIdentity: vi.fn(async () => ({ user: null, supabase: {} })) }));
    vi.resetModules();
    const { addObjectToList } = await import('@/lib/actions/lists');
    expect(await addObjectToList(1, 2, 0, 0)).toEqual({ error: 'Not authenticated' });
  });
});
