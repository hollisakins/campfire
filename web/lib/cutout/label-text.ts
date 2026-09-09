// Font-free text for server-rendered figures (epic #337, Phase 5). `sharp`
// rasterizes SVG through librsvg, whose `<text>` needs a system font: on a
// serverless host with no fontconfig fonts the label silently renders as
// nothing (or as fallback boxes), which is how the figure's band labels went
// missing in production. Glyph outlines are embedded instead, so a label is
// plain `<path>` geometry that rasterizes identically everywhere.
//
// `label-font.json` holds printable-ASCII outlines of DejaVu Sans Bold
// (Bitstream Vera licence, free to embed) in font units, y-up, plus advance
// widths. Regenerate with opentype.js if the glyph set ever needs to grow.

import font from './label-font.json';

interface LabelFont {
  unitsPerEm: number;
  ascender: number;
  descender: number;
  /** char → [advance width, SVG path data] in font units (y-up). */
  glyphs: Record<string, [number, string]>;
}

const FONT = font as unknown as LabelFont;
const FALLBACK_GLYPH = '?';

/** Escape for an SVG attribute value. */
function escAttr(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/** Layout `text` at `fontSize` px: per-glyph outlines + x offsets, and the total advance. */
export function layoutLabel(text: string, fontSize: number): { glyphs: Array<{ d: string; dx: number }>; width: number; scale: number } {
  const scale = fontSize / FONT.unitsPerEm;
  const glyphs: Array<{ d: string; dx: number }> = [];
  let pen = 0;
  for (const ch of text) {
    // Sanitize non-ASCII / control characters to '?' rather than dropping them,
    // so a label never silently loses a character.
    const g = FONT.glyphs[ch] ?? FONT.glyphs[FALLBACK_GLYPH];
    if (!g) continue;
    const [advance, d] = g;
    if (d) glyphs.push({ d, dx: pen });
    pen += advance;
  }
  return { glyphs, width: pen * scale, scale };
}

export interface LabelStyle {
  fontSize: number;
  fill?: string;
  /** Halo colour drawn under the fill for legibility over imagery. */
  stroke?: string;
  strokeWidth?: number;
}

/**
 * SVG markup for `text` with its baseline's left end at `(x, y)` in raster
 * (y-down) coordinates. Two passes — a stroked halo, then the fill — instead
 * of `paint-order`, which older librsvg builds ignore (drawing the halo on
 * top of the fill and turning the text into black blobs).
 */
export function labelSvg(text: string, x: number, y: number, style: LabelStyle): string {
  const { glyphs, scale } = layoutLabel(text, style.fontSize);
  if (glyphs.length === 0) return '';
  const fill = style.fill ?? '#ffffff';
  const stroke = style.stroke ?? '#000000';
  const strokeWidth = style.strokeWidth ?? style.fontSize / 8;
  const paths = glyphs
    .map((g) => `<path transform="translate(${g.dx} 0)" d="${escAttr(g.d)}"/>`)
    .join('');
  // Outlines are y-up font units: flip and scale into the raster frame.
  const frame = `transform="translate(${x} ${y}) scale(${scale} ${-scale})"`;
  // Stroke width is specified in the (scaled) group's user units.
  const haloWidth = strokeWidth / scale;
  return (
    `<g ${frame} fill="none" stroke="${stroke}" stroke-width="${haloWidth}" stroke-linejoin="round">${paths}</g>` +
    `<g ${frame} fill="${fill}">${paths}</g>`
  );
}

/** Pixel width of `text` at `fontSize` (for right-aligning or fitting a label). */
export function labelWidth(text: string, fontSize: number): number {
  return layoutLabel(text, fontSize).width;
}

/** Cap height-ish metric: how far the tallest glyph rises above the baseline, px. */
export function labelAscent(fontSize: number): number {
  // DejaVu's ascender includes diacritic headroom; the cap height is ~0.73 em.
  return 0.73 * fontSize;
}
