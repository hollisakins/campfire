// @vitest-environment node
import { NextRequest } from 'next/server';
import { afterEach, describe, expect, it, vi } from 'vitest';

let mode: 'off' | 'admin' | 'all' = 'admin';
vi.mock('@/lib/dr-pages', () => ({ drPagesMode: () => mode }));

import { applyDrDecision } from './middleware';

const req = (url: string, headers: Record<string, string> = {}) =>
  new NextRequest(new URL(url, 'http://localhost:3000'), { headers });

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

  it('redirects the map to the same URL without the parameter', () => {
    const res = applyDrDecision(req('/map?field=cosmos&dr=DR-seed&z=8'));
    expect(res?.status).toBe(307);
    expect(res?.headers.get('location')).toBe('http://localhost:3000/map?field=cosmos&z=8');
  });
});
