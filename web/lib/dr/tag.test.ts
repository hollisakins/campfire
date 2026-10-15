import { describe, expect, it } from 'vitest';
import { drHeaderFor, drTagFromSearchParams, drTagFromUrl, parseDrTag, withDr, withoutDr } from './tag';

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
    expect(parseDrTag('a'.repeat(65))).toBeNull();
    expect(parseDrTag(null)).toBeNull();
    expect(parseDrTag(undefined)).toBeNull();
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

describe('drTagFromUrl / drTagFromSearchParams / drHeaderFor', () => {
  it('reads the parameter from a URL', () => {
    expect(drTagFromUrl('http://x/nirspec?dr=DR-seed')).toBe('DR-seed');
    expect(drTagFromUrl(new URL('http://x/nirspec?dr=bad%20tag'))).toBeNull();
    expect(drTagFromUrl('http://x/nirspec')).toBeNull();
  });
  it('reads a page searchParams record, refusing a repeated value', () => {
    expect(drTagFromSearchParams({ dr: 'DR1' })).toBe('DR1');
    expect(drTagFromSearchParams({ dr: ['DR1', 'DR2'] })).toBeNull();
    expect(drTagFromSearchParams(new URLSearchParams('dr=DR1'))).toBe('DR1');
    expect(drTagFromSearchParams(new URLSearchParams('dr=DR1&dr=DR2'))).toBeNull();
    expect(drTagFromSearchParams(null)).toBeNull();
  });
  it('stamps nothing while the pages flag is off', () => {
    const url = new URL('http://x/nirspec?dr=DR1');
    expect(drHeaderFor(url, 'off')).toBeNull();
    expect(drHeaderFor(url, 'admin')).toBe('DR1');
    expect(drHeaderFor(url, 'all')).toBe('DR1');
  });
});
