import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchSciImage } from './fetch';

// A synthetic FITS file in memory, served with byte ranges the way the FITS
// proxy and the delivery front serve a canonical exposure.

const BLOCK = 2880;

function card(key: string, value: string): string {
  return `${key.padEnd(8)}= ${value.padStart(20)}`.padEnd(80);
}

function header(cards: string[]): Uint8Array {
  const text = [...cards, 'END'.padEnd(80)].join('');
  const padded = text.padEnd(Math.ceil(text.length / BLOCK) * BLOCK);
  return new TextEncoder().encode(padded);
}

function imageHdu(
  extname: string, width: number, height: number, bitpix: -32 | 16,
  value: (x: number, y: number) => number,
): Uint8Array {
  const head = header([
    card('XTENSION', "'IMAGE   '"), card('BITPIX', String(bitpix)), card('NAXIS', '2'),
    card('NAXIS1', String(width)), card('NAXIS2', String(height)),
    card('PCOUNT', '0'), card('GCOUNT', '1'), card('EXTNAME', `'${extname.padEnd(8)}'`),
  ]);
  const bytesPer = Math.abs(bitpix) / 8;
  const dataBytes = width * height * bytesPer;
  const data = new Uint8Array(Math.ceil(dataBytes / BLOCK) * BLOCK);
  const view = new DataView(data.buffer);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const off = (y * width + x) * bytesPer;
      if (bitpix === -32) view.setFloat32(off, value(x, y), false);   // FITS is big-endian
      else view.setInt16(off, value(x, y), false);
    }
  }
  const out = new Uint8Array(head.length + data.length);
  out.set(head, 0);
  out.set(data, head.length);
  return out;
}

function concat(parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let off = 0;
  for (const p of parts) { out.set(p, off); off += p.length; }
  return out;
}

const PRIMARY = header([card('SIMPLE', 'T'), card('BITPIX', '8'), card('NAXIS', '0')]);

/** Serve `file` at any url with HTTP Range; returns the list of ranges asked for. */
function serve(file: Uint8Array): string[] {
  const asked: string[] = [];
  vi.stubGlobal('fetch', async (_url: string, init?: { headers?: Record<string, string> }) => {
    const range = init?.headers?.Range ?? '';
    asked.push(range);
    const m = /bytes=(\d+)-(\d+)/.exec(range);
    if (!m) return new Response(file.slice().buffer, { status: 200 });
    const start = Number(m[1]);
    if (start >= file.length) return new Response(null, { status: 416 });
    const end = Math.min(Number(m[2]), file.length - 1);
    return new Response(file.slice(start, end + 1).buffer, { status: 206 });
  });
  return asked;
}

afterEach(() => vi.unstubAllGlobals());

// Two chips of 6 x 4 as ACS/WFC lays them out: SCI, ERR, DQ, SCI, ERR, DQ.
// Chip 1 holds 100 + x + 10 y, chip 2 holds 200 + x + 10 y.
function twoChipFile(): Uint8Array {
  const chip = (base: number) => [
    imageHdu('SCI', 6, 4, -32, (x, y) => base + x + 10 * y),
    imageHdu('ERR', 6, 4, -32, () => 1),
    imageHdu('DQ', 6, 4, 16, () => 0),
  ];
  return concat([PRIMARY, ...chip(100), ...chip(200)]);
}

describe('fetchSciImage', () => {
  it('reads the first SCI extension and nothing past it by default', async () => {
    const asked = serve(twoChipFile());
    const image = await fetchSciImage('/fits');
    expect([image.width, image.height]).toEqual([6, 4]);
    expect(image.data[0]).toBe(100);
    expect(image.data[6 * 3 + 5]).toBe(135);
    // one header window, one data block
    expect(asked).toHaveLength(2);
  });

  it('stacks two chips bottom to top: [SCI,1] the lower rows, [SCI,2] the upper', async () => {
    serve(twoChipFile());
    const image = await fetchSciImage('/fits', undefined, { chips: 2 });
    expect([image.width, image.height]).toEqual([6, 8]);
    expect(image.data).toHaveLength(48);
    // row 0 is chip 1's first row; row 4 is chip 2's first row
    expect(image.data[0]).toBe(100);
    expect(image.data[6 * 3 + 5]).toBe(135);
    expect(image.data[6 * 4]).toBe(200);
    expect(image.data[6 * 7 + 5]).toBe(235);
  });

  it('refuses a file with fewer SCI extensions than the detector has chips', async () => {
    const oneChip = concat([
      PRIMARY,
      imageHdu('SCI', 6, 4, -32, (x) => x),
      imageHdu('ERR', 6, 4, -32, () => 1),
      imageHdu('DQ', 6, 4, 16, () => 0),
    ]);
    serve(oneChip);
    await expect(fetchSciImage('/fits', undefined, { chips: 2 })).rejects.toThrow();
  });

  it('refuses chips of different widths', async () => {
    const file = concat([
      PRIMARY,
      imageHdu('SCI', 6, 4, -32, (x) => x),
      imageHdu('SCI', 5, 4, -32, (x) => x),
    ]);
    serve(file);
    await expect(fetchSciImage('/fits', undefined, { chips: 2 }))
      .rejects.toThrow(/differ in width/);
  });
});
