// Chunked key authorization. The regression this guards: an unbounded `.in()`
// list overflows the PostgREST request URL, the query fails as a whole, and the
// caller can't tell "the query broke" from "you may download none of these" —
// which is how the NIRCam download script came out holding only the exposure
// maps (the one short key list that fit) under a whole-selection byte total.
import { describe, it, expect, vi } from 'vitest';

vi.mock('server-only', () => ({}));

import { authorizeKeysInChunks, KEY_AUTH_CHUNK } from './authorized-keys';

const keys = (n: number, prefix = 'k') =>
  Array.from({ length: n }, (_, i) => `${prefix}${i}`);

describe('authorizeKeysInChunks', () => {
  it('never sends more than the chunk size in one query', async () => {
    const sent: string[][] = [];
    const { keys: got, error } = await authorizeKeysInChunks(
      keys(233),
      'storage_key',
      async (chunk) => {
        sent.push(chunk);
        return { data: chunk.map((storage_key) => ({ storage_key })), error: null };
      },
    );

    expect(error).toBeNull();
    expect(sent).toHaveLength(Math.ceil(233 / KEY_AUTH_CHUNK));
    expect(Math.max(...sent.map((c) => c.length))).toBeLessThanOrEqual(KEY_AUTH_CHUNK);
    // Every requested key comes back exactly once, in input order.
    expect(got).toEqual(keys(233));
  });

  it('returns only the keys the DB actually returned', async () => {
    const authorized = new Set(['k1', 'k3']);
    const { keys: got } = await authorizeKeysInChunks(
      keys(5),
      'storage_key',
      async (chunk) => ({
        data: chunk.filter((k) => authorized.has(k)).map((storage_key) => ({ storage_key })),
        error: null,
      }),
    );
    expect(got).toEqual(['k1', 'k3']);
  });

  it('deduplicates the requested keys', async () => {
    const sent: string[][] = [];
    const { keys: got } = await authorizeKeysInChunks(
      ['a', 'b', 'a', 'b'],
      'storage_key',
      async (chunk) => {
        sent.push(chunk);
        return { data: chunk.map((storage_key) => ({ storage_key })), error: null };
      },
    );
    expect(sent).toEqual([['a', 'b']]);
    expect(got).toEqual(['a', 'b']);
  });

  it('fails the whole call on a chunk error rather than authorizing a subset', async () => {
    let call = 0;
    const { keys: got, error } = await authorizeKeysInChunks(
      keys(KEY_AUTH_CHUNK * 2),
      'storage_key',
      async (chunk) => {
        call += 1;
        if (call === 2) return { data: null, error: { message: 'URI too long' } };
        return { data: chunk.map((storage_key) => ({ storage_key })), error: null };
      },
    );
    expect(error).toBe('URI too long');
    expect(got).toEqual([]);
  });

  it('short-circuits on an empty request', async () => {
    const runQuery = vi.fn();
    const { keys: got, error } = await authorizeKeysInChunks([], 'storage_key', runQuery);
    expect(got).toEqual([]);
    expect(error).toBeNull();
    expect(runQuery).not.toHaveBeenCalled();
  });
});
