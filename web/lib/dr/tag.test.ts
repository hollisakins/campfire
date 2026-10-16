import { describe, expect, it } from 'vitest';
import { drRequestDecision, isRefusedWriteUnderRelease, isReleasePath, parseDrTag, withDr, withoutDr } from './tag';

describe('parseDrTag', () => {
  it('accepts the database tag shape verbatim', () => {
    expect(parseDrTag('DR2026.1')).toBe('DR2026.1');
    expect(parseDrTag('DR-seed')).toBe('DR-seed');
    expect(parseDrTag('dr-seed')).toBe('dr-seed'); // never lowercased or uppercased
  });
  it('refuses anything else', () => {
    expect(parseDrTag('')).toBeNull();
    expect(parseDrTag('D')).toBeNull();
    expect(parseDrTag('-DR')).toBeNull();
    expect(parseDrTag('DR 1')).toBeNull();
    expect(parseDrTag('DR/1')).toBeNull();
    expect(parseDrTag('../x')).toBeNull();
    expect(parseDrTag('a'.repeat(65))).toBeNull();
    expect(parseDrTag(null)).toBeNull();
    expect(parseDrTag(undefined)).toBeNull();
  });
});

describe('drRequestDecision (the one place ?dr= is parsed)', () => {
  it('is live without the parameter, and whenever the flag is off', () => {
    expect(drRequestDecision('http://x/nirspec', 'admin')).toEqual({ kind: 'live' });
    expect(drRequestDecision('http://x/nirspec?dr=DR1', 'off')).toEqual({ kind: 'live' });
    expect(drRequestDecision('http://x/nirspec?dr=../x', 'off')).toEqual({ kind: 'live' });
  });
  it('selects exactly one well-formed tag', () => {
    expect(drRequestDecision('http://x/nirspec?dr=DR-seed&page=2', 'admin')).toEqual({ kind: 'release', tag: 'DR-seed' });
    expect(drRequestDecision('http://x/nirspec?dr=DR-seed', 'all')).toEqual({ kind: 'release', tag: 'DR-seed' });
  });
  it('fails closed on a repeated, empty or malformed parameter', () => {
    expect(drRequestDecision('http://x/nirspec?dr=A1&dr=A1', 'admin')).toEqual({ kind: 'not-found' });
    expect(drRequestDecision('http://x/nirspec?dr=A1&dr=B1', 'admin')).toEqual({ kind: 'not-found' });
    expect(drRequestDecision('http://x/nirspec?dr=', 'admin')).toEqual({ kind: 'not-found' });
    expect(drRequestDecision('http://x/nirspec?dr=..%2Fx', 'admin')).toEqual({ kind: 'not-found' });
    expect(drRequestDecision('http://x/nirspec?dr=dr%20seed', 'admin')).toEqual({ kind: 'not-found' });
  });
  it('strips the parameter from every live-only surface', () => {
    expect(drRequestDecision('http://x/map?field=cosmos&dr=DR1&z=8', 'admin'))
      .toEqual({ kind: 'strip', location: '/map?field=cosmos&z=8' });
    for (const p of ['/map', '/nirspec/tags', '/nirspec/tags/mine', '/inspect', '/profile', '/admin/releases', '/login', '/', '/docs', '/nircam/cosmos/cutouts', '/mapping', '/api/lists/membership', '/api/users/u1']) {
      expect(drRequestDecision(`http://x${p}?dr=DR1`, 'admin').kind, p).toBe('strip');
    }
    expect(drRequestDecision('http://x/map', 'admin')).toEqual({ kind: 'live' });
  });
  it('browses on the release-capable paths', () => {
    for (const p of ['/nirspec', '/nirspec/objects/J1', '/nirspec/metadata', '/nircam', '/nircam/cosmos', '/releases/DR1', '/api/objects/near', '/api/objects/adjacent', '/api/spectrum/sidecars', '/api/filter-options', '/api/metadata/observations', '/api/download', '/api/line-fit', '/api/redshift-fit', '/api/shutters', '/api/photometry-pz', '/api/dr/x']) {
      expect(drRequestDecision(`http://x${p}?dr=DR1`, 'admin'), p).toEqual({ kind: 'release', tag: 'DR1' });
    }
    expect(isReleasePath('/api/v1/objects')).toBe(false); // the bearer gate owns /api/v1; never stamped
  });
  it('refuses an API write under a release, before its handler', () => {
    expect(drRequestDecision('http://x/api/objects/1/inspect?dr=DR1', 'admin', 'PATCH')).toEqual({ kind: 'read-only' });
    expect(drRequestDecision('http://x/api/download?dr=DR1', 'admin', 'POST')).toEqual({ kind: 'read-only' });
    expect(drRequestDecision('http://x/api/objects/near?dr=DR1', 'admin', 'GET')).toEqual({ kind: 'release', tag: 'DR1' });
    expect(isRefusedWriteUnderRelease('/api/users/u1', 'PATCH')).toBe(true);
    expect(isRefusedWriteUnderRelease('/api/admin/nircam/review', 'POST')).toBe(true);
    expect(isRefusedWriteUnderRelease('/api/objects/near', 'HEAD')).toBe(false);
    expect(isRefusedWriteUnderRelease('/nirspec', 'POST')).toBe(false); // server actions post to page URLs
  });
});

describe('withDr', () => {
  it('appends to a bare path', () => {
    expect(withDr('/nirspec', 'DR1')).toBe('/nirspec?dr=DR1');
  });
  it('appends to an existing query and keeps the hash', () => {
    expect(withDr('/nirspec?view=objects&page=2#top', 'DR1')).toBe('/nirspec?view=objects&page=2&dr=DR1#top');
  });
  it('keeps a release already named on the href', () => {
    expect(withDr('/nirspec?dr=DR0', 'DR1')).toBe('/nirspec?dr=DR0');
  });
  it('is the identity without a release', () => {
    expect(withDr('/nirspec?x=1', null)).toBe('/nirspec?x=1');
    expect(withDr('/nirspec', undefined)).toBe('/nirspec');
  });
  it('covers the catalog link kinds', () => {
    const tag = 'DR2026.1';
    expect(withDr('/nirspec/objects/J1234%2B5678?view=objects&sort=redshift', tag))
      .toBe('/nirspec/objects/J1234%2B5678?view=objects&sort=redshift&dr=DR2026.1');
    expect(withDr('/nircam/cosmos', tag)).toBe('/nircam/cosmos?dr=DR2026.1');
    expect(withDr('/nirspec/metadata', tag)).toBe('/nirspec/metadata?dr=DR2026.1');
    expect(withDr('/nirspec?programs=a,b', tag)).toBe('/nirspec?programs=a%2Cb&dr=DR2026.1');
  });
});

describe('withoutDr', () => {
  it('drops the parameter and nothing else', () => {
    expect(withoutDr('/nirspec?view=objects&dr=DR1&page=2#x')).toBe('/nirspec?view=objects&page=2#x');
    expect(withoutDr('/nirspec?dr=DR1')).toBe('/nirspec');
    expect(withoutDr('/nirspec')).toBe('/nirspec');
  });
});
