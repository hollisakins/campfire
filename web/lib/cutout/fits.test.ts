// Structural tests for the minimal FITS writer (epic #337, Phase 5): parse the
// emitted bytes back with a tiny card reader and check HDU structure, card
// formatting, the CRPIX crop shift, and big-endian float32 round-tripping.
// (End-to-end astropy verification of route output happens in the live check.)

import { describe, it, expect } from 'vitest';
import { encodeFitsCutout, type FitsBandCutout } from './fits';

const CARD = 80;
const BLOCK = 2880;

/** Read the header cards of the HDU starting at `offset`; returns [cards, dataStart]. */
function readHeader(buf: Buffer, offset: number): [Map<string, string>, number] {
  const cards = new Map<string, string>();
  for (let pos = offset; ; pos += CARD) {
    const text = buf.subarray(pos, pos + CARD).toString('ascii');
    const key = text.slice(0, 8).trim();
    if (key === 'END') {
      return [cards, offset + Math.ceil((pos + CARD - offset) / BLOCK) * BLOCK];
    }
    if (text.slice(8, 10) === '= ') {
      cards.set(key, text.slice(10).split(' / ')[0].trim());
    }
  }
}

function num(cards: Map<string, string>, key: string): number {
  const raw = cards.get(key);
  expect(raw, `missing card ${key}`).toBeDefined();
  return parseFloat(raw!.replace('E', 'e'));
}

function makeBand(name: string, w: number, h: number, fill: (i: number) => number): FitsBandCutout {
  const data = new Float32Array(w * h);
  for (let i = 0; i < data.length; i++) data[i] = fill(i);
  return {
    name,
    data,
    width: w,
    height: h,
    wcs: {
      CRPIX1: 3932.5,
      CRPIX2: 5806.5,
      CDELT1: 8.3333333333333e-6,
      CDELT2: 8.3333333333333e-6,
      PC1_1: -1.0,
      CTYPE1: 'RA---TAN',
      CTYPE2: 'DEC--TAN',
      CRVAL1: 137.805,
      CRVAL2: 17.7999,
      RADESYS: 'ICRS',
    },
    origin: [100, 250],
    pixelScaleArcsec: 0.03,
    levelZ: 0,
  };
}

describe('encodeFitsCutout', () => {
  it('emits a block-aligned primary + one IMAGE extension per band', () => {
    const fits = encodeFitsCutout(
      [makeBand('f277w', 7, 5, (i) => i), makeBand('f444w', 7, 5, (i) => -i)],
      { primary: { FIELD: 'rj0911', RA_CEN: 137.788282 }, comments: ['test'] },
    );
    expect(fits.length % BLOCK).toBe(0);

    const [primary, ext1Start] = readHeader(fits, 0);
    expect(primary.get('SIMPLE')).toBe('T');
    expect(num(primary, 'BITPIX')).toBe(8);
    expect(num(primary, 'NAXIS')).toBe(0);
    expect(num(primary, 'NEXTEND')).toBe(2);
    expect(primary.get('FIELD')).toBe("'rj0911  '");
    expect(num(primary, 'RA_CEN')).toBeCloseTo(137.788282, 6);

    const [ext1, data1Start] = readHeader(fits, ext1Start);
    expect(ext1.get('XTENSION')).toBe("'IMAGE   '");
    expect(num(ext1, 'BITPIX')).toBe(-32);
    expect(num(ext1, 'NAXIS1')).toBe(7);
    expect(num(ext1, 'NAXIS2')).toBe(5);
    expect(ext1.get('EXTNAME')).toBe("'F277W   '");

    // Second extension follows the first's padded data unit.
    const ext2Start = data1Start + Math.ceil((7 * 5 * 4) / BLOCK) * BLOCK;
    const [ext2] = readHeader(fits, ext2Start);
    expect(ext2.get('EXTNAME')).toBe("'F444W   '");
  });

  it('shifts CRPIX by the crop origin and keeps the rest of the WCS verbatim', () => {
    const fits = encodeFitsCutout([makeBand('f444w', 4, 3, () => 1)]);
    const [, extStart] = readHeader(fits, 0);
    const [ext] = readHeader(fits, extStart);
    expect(num(ext, 'CRPIX1')).toBeCloseTo(3932.5 - 100, 9);
    expect(num(ext, 'CRPIX2')).toBeCloseTo(5806.5 - 250, 9);
    expect(num(ext, 'CRVAL1')).toBeCloseTo(137.805, 9);
    expect(num(ext, 'CDELT1')).toBeCloseTo(8.3333333333333e-6, 18);
    expect(num(ext, 'PC1_1')).toBe(-1);
    expect(ext.get('CTYPE1')).toBe("'RA---TAN'");
    expect(num(ext, 'LEVEL')).toBe(0);
    expect(num(ext, 'PIXSCALE')).toBeCloseTo(0.03, 9);
  });

  it('round-trips big-endian float32 data including NaN', () => {
    const band = makeBand('f150w', 3, 2, (i) => (i === 4 ? NaN : i * 1.5 - 2));
    const fits = encodeFitsCutout([band]);
    const [, extStart] = readHeader(fits, 0);
    const [, dataStart] = readHeader(fits, extStart);
    const view = new DataView(fits.buffer, fits.byteOffset + dataStart);
    for (let i = 0; i < band.data.length; i++) {
      const v = view.getFloat32(i * 4, false);
      if (Number.isNaN(band.data[i])) expect(Number.isNaN(v)).toBe(true);
      else expect(v).toBeCloseTo(band.data[i], 6);
    }
    // Data unit zero-padded to a full block.
    expect((fits.length - dataStart) % BLOCK).toBe(0);
  });

  it('rejects mismatched dims and empty band lists', () => {
    const band = makeBand('f444w', 4, 3, () => 0);
    band.width = 5;
    expect(() => encodeFitsCutout([band])).toThrow(/data length/);
    expect(() => encodeFitsCutout([])).toThrow(/no bands/);
  });
});
