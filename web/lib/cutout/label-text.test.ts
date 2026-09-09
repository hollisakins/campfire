import { describe, it, expect } from 'vitest';
import { labelSvg, labelWidth, layoutLabel } from './label-text';

describe('label-text', () => {
  it('lays out every printable ASCII character as outline geometry', () => {
    const text = 'F444W NRC_F200W-2 (0.03") 10h00m+02d';
    const { glyphs, width } = layoutLabel(text, 12);
    // Every non-space character contributes an outline; spaces only advance.
    const visible = text.replace(/ /g, '').length;
    expect(glyphs.length).toBe(visible);
    expect(width).toBeGreaterThan(0);
    for (const g of glyphs) expect(g.d).toMatch(/^M-?\d+ -?\d+/);
  });

  it('advances monotonically and scales with the font size', () => {
    const w12 = labelWidth('F444W', 12);
    const w24 = labelWidth('F444W', 24);
    expect(w24).toBeCloseTo(2 * w12, 6);
    const { glyphs } = layoutLabel('AB', 12);
    expect(glyphs[1].dx).toBeGreaterThan(glyphs[0].dx);
  });

  it('never drops a character: non-ASCII falls back to "?"', () => {
    expect(layoutLabel('µm', 12).glyphs.length).toBe(2);
    expect(layoutLabel('µm', 12).width).toBe(layoutLabel('?m', 12).width);
  });

  it('emits <path> only — no <text> and no font dependency — halo under fill', () => {
    const svg = labelSvg('F115W', 8, 20, { fontSize: 14 });
    expect(svg).not.toContain('<text');
    expect(svg).not.toContain('font-family');
    expect(svg).toContain('<path');
    // Two passes: the stroked halo group precedes the filled group.
    const halo = svg.indexOf('stroke="#000000"');
    const fill = svg.indexOf('fill="#ffffff"');
    expect(halo).toBeGreaterThan(-1);
    expect(fill).toBeGreaterThan(halo);
    // Outlines are y-up font units: the frame flips them into the raster.
    expect(svg).toContain('scale(');
    expect(svg).toMatch(/scale\([0-9.e-]+ -[0-9.e-]+\)/);
  });

  it('escapes nothing dangerous into attributes', () => {
    const svg = labelSvg('a"<b>', 0, 0, { fontSize: 10 });
    expect(svg).not.toMatch(/d="[^"]*<[^"]*"/);
  });
});
