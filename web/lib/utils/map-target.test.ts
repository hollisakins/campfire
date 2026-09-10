import { describe, it, expect } from 'vitest';
import { formatTargetParam, parseTargetParam } from './map-target';

describe('formatTargetParam', () => {
  it('writes decimal degrees at fixed precision', () => {
    expect(formatTargetParam({ ra: 150.1234567, dec: -2.5 })).toBe('150.123457,-2.500000');
  });

  it('round-trips through parseTargetParam', () => {
    const t = { ra: 34.567891, dec: -5.432109 };
    expect(parseTargetParam(formatTargetParam(t))).toEqual(t);
  });

  it('is byte-stable for the same target, so a pan-triggered rewrite is a no-op', () => {
    const a = formatTargetParam({ ra: 150.1, dec: 2.2 });
    const b = formatTargetParam({ ra: 150.1, dec: 2.2 });
    expect(a).toBe(b);
  });
});

describe('parseTargetParam', () => {
  it('accepts comma- and whitespace-separated decimal degrees', () => {
    expect(parseTargetParam('150.5,-2.3')).toEqual({ ra: 150.5, dec: -2.3 });
    expect(parseTargetParam('150.5 -2.3')).toEqual({ ra: 150.5, dec: -2.3 });
    expect(parseTargetParam(' 150.5 , -2.3 ')).toEqual({ ra: 150.5, dec: -2.3 });
  });

  it('keeps the boundary values that are legal coordinates', () => {
    expect(parseTargetParam('0,0')).toEqual({ ra: 0, dec: 0 });
    expect(parseTargetParam('359.999999,90')).toEqual({ ra: 359.999999, dec: 90 });
    expect(parseTargetParam('0,-90')).toEqual({ ra: 0, dec: -90 });
  });

  it('returns null for an absent or non-string param', () => {
    expect(parseTargetParam(undefined)).toBeNull();
    // A repeated query param arrives as an array; there is no single target then.
    expect(parseTargetParam(['150,2', '151,3'])).toBeNull();
  });

  it('returns null for a malformed value rather than throwing', () => {
    expect(parseTargetParam('')).toBeNull();
    expect(parseTargetParam('150.5')).toBeNull(); // one component
    expect(parseTargetParam('150.5,2.3,4')).toBeNull(); // three
    expect(parseTargetParam('abc,def')).toBeNull();
    expect(parseTargetParam('10:00:00,+02:12:00')).toBeNull(); // sexagesimal is the box's job
    expect(parseTargetParam('NaN,2')).toBeNull();
    expect(parseTargetParam('Infinity,2')).toBeNull();
  });

  it('rejects out-of-range coordinates', () => {
    expect(parseTargetParam('360,0')).toBeNull();
    expect(parseTargetParam('-1,0')).toBeNull();
    expect(parseTargetParam('0,90.1')).toBeNull();
    expect(parseTargetParam('0,-90.1')).toBeNull();
  });
});
