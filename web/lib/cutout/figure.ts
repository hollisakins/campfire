// Multi-band cutout figure (epic #337, Phase 5): one labeled North-up panel per
// band, composed into a single PNG — the classic postage-stamp strip for a
// quick look across an object's bands. Server-only (`sharp` composition);
// panel pixels come from the same engine path as the display routes
// (`bandToOutput` → per-band percentile stretch → colormap).

import sharp from 'sharp';
import type { StretchMode, ColormapName } from '@fitsgl/core';
import { bandToOutput } from './index';
import { percentileLimits, renderSingleBand } from './render';
import type { FieldScienceSource } from './source';

/** Gap between panels, px. */
const GAP = 4;
/** Label inset from the panel's top-left corner, px. */
const LABEL_PAD = 8;

export interface FigureRequest {
  /** ICRS centre `[ra, dec]` in degrees. */
  center: [number, number];
  /** Square field of view in arcsec (every panel shows the same box). */
  fovArcsec: number;
  /** Panel edge in px. */
  panelSize: number;
  /** Panels per row; defaults to all bands in one row. */
  cols?: number;
  stretch?: StretchMode;
  colormap?: ColormapName;
  /** Draw the band label on each panel (default true). */
  labels?: boolean;
}

/** Escape a band label for the SVG text overlay. */
function esc(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/** Render the per-band panels and compose them into one PNG. */
export async function renderFigurePng(src: FieldScienceSource, req: FigureRequest): Promise<Buffer> {
  const n = src.bands.length;
  if (n === 0) throw new Error('figure: no bands');
  const size = req.panelSize;
  const cols = Math.max(1, Math.min(req.cols ?? n, n));
  const rows = Math.ceil(n / cols);
  const stretch = req.stretch ?? 'asinh';
  const colormap = req.colormap ?? 'gray';

  // Reproject + stretch every band on the same N-up output grid.
  const panels = await Promise.all(
    src.bands.map(async (band) => {
      const out = await bandToOutput(band, req.center, req.fovArcsec, size);
      const rgba = renderSingleBand(out.data, out.width, out.height, {
        limits: percentileLimits(out.data),
        stretch,
        colormap,
      });
      return { rgba, label: band.label ?? band.name.toUpperCase() };
    }),
  );

  const width = cols * size + (cols - 1) * GAP;
  const height = rows * size + (rows - 1) * GAP;
  const composites: sharp.OverlayOptions[] = [];
  const labelSpans: string[] = [];
  const fontSize = Math.max(11, Math.round(size / 14));

  panels.forEach((panel, i) => {
    const left = (i % cols) * (size + GAP);
    const top = Math.floor(i / cols) * (size + GAP);
    composites.push({
      input: Buffer.from(panel.rgba.buffer, panel.rgba.byteOffset, panel.rgba.byteLength),
      raw: { width: size, height: size, channels: 4 },
      left,
      top,
    });
    if (req.labels !== false) {
      labelSpans.push(
        `<text x="${left + LABEL_PAD}" y="${top + LABEL_PAD + fontSize}" ` +
          `font-family="Helvetica, Arial, sans-serif" font-size="${fontSize}" font-weight="600" ` +
          `fill="#ffffff" stroke="#000000" stroke-width="${fontSize / 8}" paint-order="stroke">` +
          `${esc(panel.label)}</text>`,
      );
    }
  });
  if (labelSpans.length > 0) {
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}">${labelSpans.join('')}</svg>`;
    composites.push({ input: Buffer.from(svg), left: 0, top: 0 });
  }

  return sharp({
    create: { width, height, channels: 4, background: { r: 0, g: 0, b: 0, alpha: 255 } },
  })
    .composite(composites)
    .png()
    .toBuffer();
}
