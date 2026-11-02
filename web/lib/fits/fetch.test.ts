// The tile-compressed image reader against a fixture written by astropy
// (__fixtures__/compressed-gzip2.fits: PRIMARY, a plain BINTABLE, a BINTABLE with a
// variable-length heap, MBKG2D = GZIP_2 lossless float32 in 3 x 2 tiles so the edge
// tiles are partial, MBKG2DN = RICE_1 uint8). Pixel (x, y) of MBKG2D is
// 0.25 * (y * 7 + x) - 3, NaN where (y * 7 + x) % 11 == 0.

import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchCompressedImage, subtractImages, tformWidth, HduNotFoundError } from './fetch';

const FIXTURE = readFileSync(fileURLToPath(new URL('./__fixtures__/compressed-gzip2.fits', import.meta.url)));

/** Serve `bytes` like a range-capable server; records the requested ranges. */
function serve(bytes: Buffer) {
  const ranges: [number, number][] = [];
  vi.stubGlobal('fetch', async (_url: string, init?: RequestInit) => {
    const m = /bytes=(\d+)-(\d+)/.exec(new Headers(init?.headers).get('Range') ?? '');
    if (!m) return new Response(new Uint8Array(bytes), { status: 200 });
    const start = Number(m[1]);
    const end = Math.min(Number(m[2]), bytes.length - 1);
    ranges.push([start, end]);
    return new Response(new Uint8Array(bytes.subarray(start, end + 1)), { status: 206 });
  });
  return ranges;
}

afterEach(() => vi.unstubAllGlobals());

describe('fetchCompressedImage', () => {
  it('decodes a GZIP_2 image past tables with a heap, partial edge tiles included', async () => {
    const ranges = serve(FIXTURE);
    const img = await fetchCompressedImage('/f', 'MBKG2D');
    expect([img.width, img.height]).toEqual([7, 5]);
    for (let y = 0; y < 5; y++) {
      for (let x = 0; x < 7; x++) {
        const i = y * 7 + x;
        const v = img.data[i]!;
        if (i % 11 === 0) expect(v).toBeNaN();
        else expect(v).toBe(Math.fround(0.25 * i - 3));
      }
    }
    // the header window, then the HDU's data unit in one request
    expect(ranges).toHaveLength(2);
  });

  it('rejects compression other than GZIP_2', async () => {
    serve(FIXTURE);
    await expect(fetchCompressedImage('/f', 'MBKG2DN')).rejects.toThrow(/GZIP_2 only/);
  });

  it('reports a missing extension', async () => {
    serve(FIXTURE);
    await expect(fetchCompressedImage('/f', 'NOPE')).rejects.toBeInstanceOf(HduNotFoundError);
  });
});

describe('tformWidth', () => {
  it('sizes fixed and descriptor fields', () => {
    expect(tformWidth('1PB(3688)')).toBe(8);
    expect(tformWidth('1QB(10)')).toBe(16);
    expect(tformWidth('2E')).toBe(8);
    expect(tformWidth('D')).toBe(8);
    expect(tformWidth('20A')).toBe(20);
    expect(tformWidth('9X')).toBe(2);
  });
});

describe('subtractImages', () => {
  it('subtracts pixel by pixel and keeps NaN', () => {
    const a = { data: new Float32Array([3, NaN, 5]), width: 3, height: 1 };
    const b = { data: new Float32Array([1, 1, NaN]), width: 3, height: 1 };
    const d = subtractImages(a, b).data;
    expect(d[0]).toBe(2);
    expect(d[1]).toBeNaN();
    expect(d[2]).toBeNaN();
  });
});
