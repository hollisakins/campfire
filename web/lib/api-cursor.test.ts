// Opaque keyset cursors for /api/v1 list endpoints (perf T2-F, #511).
import { describe, it, expect } from 'vitest';
import {
  cursorFingerprint,
  decodeCursor,
  encodeNextCursor,
  cursorRpcParams,
  parseIncludeCount,
  OBJECTS_CURSOR_SCOPE,
  SPECTRA_CURSOR_SCOPE,
} from './api-cursor';

const O = OBJECTS_CURSOR_SCOPE;
const S = SPECTRA_CURSOR_SCOPE;

describe('cursorFingerprint', () => {
  it('ignores paging params and is order-independent', () => {
    const a = cursorFingerprint(new URLSearchParams('fields=cosmos&sort=ra&limit=10&cursor=abc&count=false'), O);
    const b = cursorFingerprint(new URLSearchParams('sort=ra&fields=cosmos&offset=500&include_count=true'), O);
    expect(a).toBe(b);
  });
  it('changes when a filter or the sort changes', () => {
    const base = cursorFingerprint(new URLSearchParams('fields=cosmos&sort=ra'), O);
    expect(cursorFingerprint(new URLSearchParams('fields=uds&sort=ra'), O)).not.toBe(base);
    expect(cursorFingerprint(new URLSearchParams('fields=cosmos&sort=dec'), O)).not.toBe(base);
    expect(cursorFingerprint(new URLSearchParams('fields=cosmos&sort=ra&sort_dir=desc'), O)).not.toBe(base);
  });
  it('differs between the two list endpoints for identical query params', () => {
    const q = new URLSearchParams('');
    expect(cursorFingerprint(q, O)).not.toBe(cursorFingerprint(q, S));
  });
});

describe('encodeNextCursor / decodeCursor', () => {
  const fp = cursorFingerprint(new URLSearchParams('sort=object_id'), O);
  const fpS = cursorFingerprint(new URLSearchParams('sort=object_id'), S);

  it('round-trips a text-sorted cursor', () => {
    const enc = encodeNextCursor({ has_more: true, next_sort_text: 'J1234+5678', next_sort_num: null, next_tiebreak: ['J1234+5678'] }, fp);
    expect(enc).toMatch(/^[A-Za-z0-9_-]+$/);
    const dec = decodeCursor(enc!, fp, O);
    expect(dec.ok).toBe(true);
    if (dec.ok) {
      expect(dec.cursor).toEqual({ v: 1, f: fp, t: 'J1234+5678', n: null, k: ['J1234+5678'] });
      expect(cursorRpcParams(dec.cursor)).toEqual({
        p_after_sort_text: 'J1234+5678', p_after_sort_num: null, p_after_tiebreak: ['J1234+5678'],
      });
    }
  });

  it('round-trips a numeric cursor, including a NULL sort value in the tail', () => {
    const enc = encodeNextCursor({ has_more: true, next_sort_text: null, next_sort_num: 150.0188513916, next_tiebreak: ['t1', 'prism', 't1_prism'] }, fpS);
    const dec = decodeCursor(enc!, fpS, S);
    expect(dec.ok && dec.cursor.n).toBe(150.0188513916);
    expect(dec.ok && dec.cursor.k).toEqual(['t1', 'prism', 't1_prism']);

    const tail = encodeNextCursor({ has_more: true, next_sort_text: null, next_sort_num: null, next_tiebreak: ['J9'] }, fp);
    const decTail = decodeCursor(tail!, fp, O);
    expect(decTail.ok && decTail.cursor.n).toBeNull();
    expect(decTail.ok && decTail.cursor.t).toBeNull();
  });

  it('accepts a numeric sort value serialized as a string', () => {
    const enc = encodeNextCursor({ has_more: true, next_sort_num: '3.5', next_tiebreak: ['a'] }, fp);
    const dec = decodeCursor(enc!, fp, O);
    expect(dec.ok && dec.cursor.n).toBe(3.5);
  });

  it('returns null when there is no next page', () => {
    expect(encodeNextCursor({ has_more: false, next_tiebreak: null }, fp)).toBeNull();
    expect(encodeNextCursor({ has_more: true, next_tiebreak: [] }, fp)).toBeNull();
  });

  it('rejects garbage, wrong versions and mismatched fingerprints', () => {
    expect(decodeCursor('not base64!!', fp, O).ok).toBe(false);
    expect(decodeCursor(Buffer.from('{"v":2}').toString('base64url'), fp, O).ok).toBe(false);
    expect(decodeCursor(Buffer.from('[1,2]').toString('base64url'), fp, O).ok).toBe(false);
    const other = encodeNextCursor({ has_more: true, next_sort_text: 'x', next_tiebreak: ['x'] }, 'deadbeefcafe');
    const dec = decodeCursor(other!, fp, O);
    expect(dec.ok).toBe(false);
    if (!dec.ok) expect(dec.error).toMatch(/does not match/);
  });

  it('rejects a cursor minted by the other endpoint, and any tiebreak of the wrong arity', () => {
    // Same (empty) query on both endpoints: the fingerprint alone must differ.
    const objQ = new URLSearchParams('');
    const objCursor = encodeNextCursor({ has_more: true, next_sort_text: 'J1', next_tiebreak: ['J1'] }, cursorFingerprint(objQ, O));
    const replayed = decodeCursor(objCursor!, cursorFingerprint(objQ, S), S);
    expect(replayed.ok).toBe(false);
    if (!replayed.ok) expect(replayed.error).toMatch(/does not match/);

    // A hand-built cursor with the right fingerprint but a spectra-shaped
    // tiebreak sent to the objects endpoint is invalid, not "no cursor".
    const forged = Buffer.from(JSON.stringify({ v: 1, f: fp, t: 'J1', n: null, k: ['t', 'g', 's'] })).toString('base64url');
    expect(decodeCursor(forged, fp, O)).toEqual({ ok: false, error: 'Invalid cursor' });
  });
});

describe('parseIncludeCount', () => {
  it('defaults to counting without a cursor and skipping with one', () => {
    expect(parseIncludeCount(new URLSearchParams(''), false)).toBe(true);
    expect(parseIncludeCount(new URLSearchParams(''), true)).toBe(false);
  });
  it('honours an explicit flag either way', () => {
    expect(parseIncludeCount(new URLSearchParams('count=false'), false)).toBe(false);
    expect(parseIncludeCount(new URLSearchParams('count=true'), true)).toBe(true);
    expect(parseIncludeCount(new URLSearchParams('include_count=0'), false)).toBe(false);
  });
});
