// @vitest-environment node
import { NextRequest } from 'next/server';
import { afterEach, describe, expect, it, vi } from 'vitest';

let mode: 'off' | 'admin' | 'all' = 'admin';
vi.mock('@/lib/dr-pages', () => ({ drPagesMode: () => mode }));

import { applyDrDecision } from './middleware';

const req = (url: string, headers: Record<string, string> = {}, method = 'GET') =>
  new NextRequest(new URL(url, 'http://localhost:3000'), { headers, method });

describe('middleware: the release header (P6c-1)', () => {
  afterEach(() => { mode = 'admin'; });

  it('stamps x-campfire-dr from a valid ?dr= and continues', () => {
    const r = req('/nirspec?dr=DR-seed&page=2');
    expect(applyDrDecision(r)).toBeNull();
    expect(r.headers.get('x-campfire-dr')).toBe('DR-seed');
  });

  it('drops a client-supplied header and derives from the URL only', () => {
    const r = req('/nirspec', { 'x-campfire-dr': 'DR-forged' });
    expect(applyDrDecision(r)).toBeNull();
    expect(r.headers.get('x-campfire-dr')).toBeNull();
    const r2 = req('/nirspec?dr=DR-seed', { 'x-campfire-dr': 'DR-forged' });
    applyDrDecision(r2);
    expect(r2.headers.get('x-campfire-dr')).toBe('DR-seed');
  });

  it('stamps nothing while the flag is off', () => {
    mode = 'off';
    const r = req('/nirspec?dr=DR-seed');
    expect(applyDrDecision(r)).toBeNull();
    expect(r.headers.get('x-campfire-dr')).toBeNull();
  });

  it('answers a repeated or malformed parameter with a 404', () => {
    for (const u of ['/nirspec?dr=DR-seed&dr=DR-seed', '/nirspec?dr=A1&dr=B1', '/nirspec?dr=..%2Fx', '/nirspec?dr=']) {
      const r = req(u);
      const res = applyDrDecision(r);
      expect(res?.status, u).toBe(404);
      expect(r.headers.get('x-campfire-dr'), u).toBeNull();
    }
  });

  it('redirects live-only surfaces to the same URL without the parameter', () => {
    const res = applyDrDecision(req('/map?field=cosmos&dr=DR-seed&z=8'));
    expect(res?.status).toBe(307);
    expect(res?.headers.get('location')).toBe('http://localhost:3000/map?field=cosmos&z=8');
    for (const p of ['/nirspec/tags', '/inspect?start=J1', '/profile', '/admin', '/', '/nircam/cosmos/cutouts']) {
      const r = applyDrDecision(req(p + (p.includes('?') ? '&' : '?') + 'dr=DR-seed'));
      expect(r?.status, p).toBe(307);
      expect(r?.headers.get('location'), p).not.toContain('dr=');
    }
  });

  it('refuses API writes under a release with a 409, passes reads through', async () => {
    const patch = applyDrDecision(req('/api/users/u1?dr=DR-seed', {}, 'PATCH'));
    expect(patch?.status).toBe(409);
    expect(await patch?.json()).toEqual({ error: 'read-only under a data release' });
    const post = applyDrDecision(req('/api/admin/nircam/review?dr=DR-seed', {}, 'POST'));
    expect(post?.status).toBe(409);
    const r = req('/api/objects/near?ra=1&dec=2&radius=5&dr=DR-seed');
    expect(applyDrDecision(r)).toBeNull();
    expect(r.headers.get('x-campfire-dr')).toBe('DR-seed');
  });

  it('never stamps /api/v1 (the bearer gate owns it)', () => {
    const r = req('/api/v1/objects?dr=DR-seed');
    const res = applyDrDecision(r);
    expect(res?.status).toBe(307);
    expect(r.headers.get('x-campfire-dr')).toBeNull();
  });
});
