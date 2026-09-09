import { describe, it, expect } from 'vitest';
import sharp from 'sharp';
import { shuttersSvg } from './figure';
import { labelSvg } from './label-text';
import type { FigureShutter } from './shutters';

const CENTER = { ra: 150.1, dec: 2.2 };

function shutter(over: Partial<FigureShutter> = {}): FigureShutter {
  return {
    object_id: 't1',
    observation: 'obs-a',
    center_ra: CENTER.ra,
    center_dec: CENTER.dec,
    position_angle: 0,
    shutter_state: 'open',
    aperture_width_arcsec: 0.2,
    aperture_height_arcsec: 0.46,
    ...over,
  };
}

/** Parse the polygon points of the first <polygon> in `svg`. */
function firstPolygon(svg: string): Array<[number, number]> {
  const m = svg.match(/points="([^"]+)"/);
  if (!m) throw new Error('no polygon');
  return m[1].split(' ').map((p) => p.split(',').map(Number) as [number, number]);
}

describe('shuttersSvg', () => {
  const size = 200;
  const scale = 10 / size; // 10" FOV on 200 px ⇒ 0.05"/px

  it('projects a centred shutter onto the panel centre with the right extents', () => {
    const svg = shuttersSvg([shutter()], CENTER.ra, CENTER.dec, scale, size);
    const pts = firstPolygon(svg);
    const xs = pts.map((p) => p[0]);
    const ys = pts.map((p) => p[1]);
    const cx = (Math.min(...xs) + Math.max(...xs)) / 2;
    const cy = (Math.min(...ys) + Math.max(...ys)) / 2;
    expect(cx).toBeCloseTo(size / 2, 1);
    expect(cy).toBeCloseTo(size / 2, 1);
    // PA 0: the 0.46" along-slit axis runs North–South ⇒ taller than wide.
    expect(Math.max(...xs) - Math.min(...xs)).toBeCloseTo(0.2 / scale, 1);
    expect(Math.max(...ys) - Math.min(...ys)).toBeCloseTo(0.46 / scale, 1);
  });

  it('is North-up, East-left in the raster frame', () => {
    const cosDec = Math.cos((CENTER.dec * Math.PI) / 180);
    // 2" East and 3" North of the centre.
    const s = shutter({
      center_ra: CENTER.ra + 2 / 3600 / cosDec,
      center_dec: CENTER.dec + 3 / 3600,
    });
    const pts = firstPolygon(shuttersSvg([s], CENTER.ra, CENTER.dec, scale, size));
    const cx = pts.reduce((a, p) => a + p[0], 0) / pts.length;
    const cy = pts.reduce((a, p) => a + p[1], 0) / pts.length;
    expect(cx).toBeCloseTo(size / 2 - 2 / scale, 0); // East ⇒ left
    expect(cy).toBeCloseTo(size / 2 - 3 / scale, 0); // North ⇒ up (smaller y)
  });

  it('styles stuck-closed shutters red dashed and keeps per-observation colours stable', () => {
    const a = shutter({ observation: 'obs-a' });
    const b = shutter({ observation: 'obs-b', shutter_state: 'stuck_closed' });
    const svg = shuttersSvg([a, b], CENTER.ra, CENTER.dec, scale, size);
    const polys = svg.split('<polygon').slice(1);
    expect(polys).toHaveLength(2);
    expect(polys[1]).toContain('#ef4444');
    expect(polys[1]).toContain('stroke-dasharray');
    expect(polys[0]).not.toContain('stroke-dasharray');
    // Given the field's full observation list, a colour never depends on
    // which other observations are in view.
    const field = ['obs-0', 'obs-a', 'obs-b', 'obs-c'];
    const colorOf = (s: string) => s.match(/stroke="(#[0-9a-f]{6})"/)![1];
    const withB = shuttersSvg([a, shutter({ observation: 'obs-c' })], CENTER.ra, CENTER.dec, scale, size, field);
    const alone = shuttersSvg([a], CENTER.ra, CENTER.dec, scale, size, field);
    expect(colorOf(alone)).toBe(colorOf(withB.split('<polygon')[1]));
    // Without the list the index space is the in-view set (documented fallback).
    expect(colorOf(shuttersSvg([a], CENTER.ra, CENTER.dec, scale, size))).toBe(colorOf(svg.split('<polygon')[1]));
  });
});

describe('figure overlay rasterization', () => {
  it('rasterizes a glyph-outline label without any system font', async () => {
    const size = 120;
    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}">` +
      labelSvg('F444W', 8, 24, { fontSize: 16 }) +
      '</svg>';
    const { data, info } = await sharp({
      create: { width: size, height: size, channels: 4, background: { r: 0, g: 0, b: 0, alpha: 255 } },
    })
      .composite([{ input: Buffer.from(svg), left: 0, top: 0 }])
      .raw()
      .toBuffer({ resolveWithObject: true });
    // White fill pixels exist inside the label box, none far below it.
    let inLabel = 0;
    let below = 0;
    for (let y = 0; y < info.height; y++) {
      for (let x = 0; x < info.width; x++) {
        const o = (y * info.width + x) * 4;
        const white = data[o] > 200 && data[o + 1] > 200 && data[o + 2] > 200;
        if (!white) continue;
        if (y >= 8 && y <= 28 && x >= 8 && x <= 80) inLabel++;
        if (y > 60) below++;
      }
    }
    expect(inLabel).toBeGreaterThan(50);
    expect(below).toBe(0);
  });
});

describe('shared shutter overlay', () => {
  it('rasterizes one <defs> geometry into every panel through <use>', async () => {
    const size = 60;
    const gap = 4;
    const inner = shuttersSvg(
      [shutter({ aperture_width_arcsec: 1.5, aperture_height_arcsec: 1.5 })],
      CENTER.ra, CENTER.dec, 3 / size, size,
    );
    // Same structure renderFigurePng emits: defs at the root, a <use> per nested panel svg.
    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="${2 * size + gap}" height="${size}">` +
      `<defs><g id="shutters">${inner}</g></defs>` +
      `<svg x="0" y="0" width="${size}" height="${size}"><use href="#shutters" xlink:href="#shutters"/></svg>` +
      `<svg x="${size + gap}" y="0" width="${size}" height="${size}"><use href="#shutters" xlink:href="#shutters"/></svg>` +
      '</svg>';
    const { data, info } = await sharp({
      create: { width: 2 * size + gap, height: size, channels: 4, background: { r: 0, g: 0, b: 0, alpha: 255 } },
    })
      .composite([{ input: Buffer.from(svg), left: 0, top: 0 }])
      .raw()
      .toBuffer({ resolveWithObject: true });
    const lit = (x: number, y: number) => {
      const o = (y * info.width + x) * 4;
      return data[o] + data[o + 1] + data[o + 2] > 20; // the footprint fill is 15% opaque
    };
    // The shutter's centre and its left edge (stroke) are lit in BOTH panels; the gap is not.
    expect(lit(size / 2, size / 2)).toBe(true);
    expect(lit(size / 4, size / 2)).toBe(true);
    expect(lit(size + gap + size / 2, size / 2)).toBe(true);
    expect(lit(size + gap + size / 4, size / 2)).toBe(true);
    expect(lit(size + 1, size / 2)).toBe(false);
  });
});
