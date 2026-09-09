// Multi-band cutout figure (epic #337, Phase 5): one labeled North-up panel per
// band — optionally plus an RGB composite panel — composed into a single PNG:
// the classic postage-stamp strip for a quick look across an object's bands.
// Server-only (`sharp` composition); panel pixels come from the same engine
// path as the display routes (`bandToOutput` → stretch → colormap / RGB).
//
// Overlays (band labels, NIRSpec shutter footprints) are one SVG composited
// over the panels. Text is embedded glyph outlines (`./label-text`), never
// `<text>`: librsvg needs a system font for that, and the serverless host has
// none, which is how the labels went missing in production.

import sharp from 'sharp';
import {
  parseWcs,
  skyToPix,
  trilogyLevelsForBands,
  type BandWeight,
  type StretchMode,
  type ColormapName,
  type TrilogyParams,
  type TrilogyStats,
} from '@fitsgl/core';
import { getObservationColor } from '@/components/map/observation-colors';
import { shutterCorners } from '@/lib/utils/shutter-overlay';
import { bandToOutput } from './index';
import { labelAscent, labelSvg, labelWidth } from './label-text';
import {
  DEFAULT_SNR_RANGE,
  percentileLimits,
  renderRGB,
  renderSingleBand,
  renderWeightedTrilogy,
  snrLimits,
  type Limits,
  type Scaling,
} from './render';
import { northUpWcsHeader } from './reproject';
import type { FigureShutter } from './shutters';
import type { CompositeSource, FieldScienceSource, ScienceBand } from './source';

/** Gap between panels, px. */
export const FIGURE_GAP = 4;
/** Label inset from the panel's top-left corner, px. */
const LABEL_PAD = 8;
/** Stuck-closed shutters read red-dashed, as on the map. */
const SHUTTER_STUCK_COLOR = '#ef4444';

export interface FigureRgbRequest {
  /** `'trilogy'` (per-band levels from the precomputed stats) or a plain
   *  transfer curve over one shared percentile range, as the map's simple RGB. */
  stretch: StretchMode;
  /** Trilogy knob overrides on top of the producer's (trilogy only). */
  trilogy?: Partial<TrilogyParams>;
}

export interface FigureRequest {
  /** ICRS centre `[ra, dec]` in degrees. */
  center: [number, number];
  /** Square field of view in arcsec (every panel shows the same box). */
  fovArcsec: number;
  /** Panel edge in px. */
  panelSize: number;
  /** Panels per row; defaults to all panels in one row. */
  cols?: number;
  /** Single-band panel transfer curve (default linear) + colormap. */
  stretch?: StretchMode;
  colormap?: ColormapName;
  /** Single-band panel limits: `snr` (default; `median + [lo, hi]·σ` of the
   *  cutout's own noise, so every band reads alike) or `percentile`. */
  scaling?: Scaling;
  /** The SNR window in σ (default `[-5, 8]`). */
  snrRange?: readonly [number, number];
  /** Append an RGB composite panel from `src.rgb` (ignored when the source has none). */
  rgb?: FigureRgbRequest;
  /** Draw the band label on each panel (default true). */
  labels?: boolean;
  /** NIRSpec shutter footprints to draw on every panel. */
  shutters?: FigureShutter[];
}

/** The bands a trilogy composite stretches: the producer's weighted table when
 *  the request is the dataset default, else the `[R, G, B]` triple on pure
 *  per-channel weights — `trilogyComposite`'s two cases, as on the map. */
export function trilogyBands(rgb: CompositeSource): { bands: ScienceBand[]; weights: BandWeight[] } {
  if (rgb.weighted) return rgb.weighted;
  return { bands: [...rgb.triple], weights: [[1, 0, 0], [0, 1, 0], [0, 0, 1]] };
}

/** Trilogy composite possible: every participating band carries precomputed stats. */
export function rgbHasTrilogyStats(rgb: CompositeSource): boolean {
  return trilogyBands(rgb).bands.every((b) => b.trilogy !== undefined);
}

interface Panel {
  rgba: Uint8ClampedArray;
  /** Primary label (band name) and an optional smaller second line. */
  label: string;
  sublabel?: string;
}

function bandLabel(b: ScienceBand): string {
  return b.label ?? b.name.toUpperCase();
}

/** Render the per-band panels and compose them into one PNG. */
export async function renderFigurePng(src: FieldScienceSource, req: FigureRequest): Promise<Buffer> {
  const size = req.panelSize;
  const stretch = req.stretch ?? 'linear';
  const colormap = req.colormap ?? 'gray';
  const scaling = req.scaling ?? 'snr';
  const [snrLo, snrHi] = req.snrRange ?? DEFAULT_SNR_RANGE;
  const limitsFor = (data: Float32Array): Limits =>
    scaling === 'snr' ? snrLimits(data, snrLo, snrHi) : percentileLimits(data);
  const [ra, dec] = req.center;

  // A band serving both a single panel and an RGB channel is fetched and
  // reprojected once.
  const outputs = new Map<string, ReturnType<typeof bandToOutput>>();
  const outputFor = (band: ScienceBand) => {
    let p = outputs.get(band.name);
    if (!p) {
      p = bandToOutput(band, req.center, req.fovArcsec, size);
      outputs.set(band.name, p);
    }
    return p;
  };

  // Reproject + stretch every single-band panel on the same N-up output grid.
  const singles = src.bands.map(async (band): Promise<Panel> => {
    const out = await outputFor(band);
    const rgba = renderSingleBand(out.data, out.width, out.height, {
      limits: limitsFor(out.data),
      stretch,
      colormap,
    });
    return { rgba, label: bandLabel(band) };
  });

  // The composite reprojects each participating band independently onto that grid too.
  const composite = async (): Promise<Panel | null> => {
    if (!req.rgb || !src.rgb) return null;
    const rgb = src.rgb;
    let rgba: Uint8ClampedArray;
    let members: ScienceBand[];
    if (req.rgb.stretch === 'trilogy') {
      // Each band stretched by its OWN levels, channels as weighted averages —
      // the map's faithful trilogy, on the producer's full weight table for the
      // dataset default and on pure per-channel weights for an explicit triple.
      if (!rgbHasTrilogyStats(rgb)) {
        throw new Error('trilogy composite needs precomputed stats on every participating band');
      }
      const { bands, weights } = trilogyBands(rgb);
      members = bands;
      const outs = await Promise.all(bands.map(outputFor));
      const params: TrilogyParams = { ...src.trilogyParams, ...req.rgb.trilogy };
      const levels = trilogyLevelsForBands(bands.map((b) => b.trilogy as TrilogyStats), params);
      rgba = renderWeightedTrilogy(outs.map((o) => o.data), size, size, { levels, weights });
    } else {
      // Simple RGB is strictly the triple (as on the map): one SHARED range
      // (the envelope of the per-channel percentile cuts) so the composite
      // stays interpretable — the bands share flux units, exactly as the
      // map's simple-RGB shared handle.
      members = [...rgb.triple];
      const outs = await Promise.all(rgb.triple.map(outputFor));
      const channels = [outs[0].data, outs[1].data, outs[2].data] as const;
      const per = channels.map((c) => percentileLimits(c));
      const shared: Limits = {
        lo: Math.min(...per.map((l) => l.lo)),
        hi: Math.max(...per.map((l) => l.hi)),
      };
      rgba = renderRGB(channels, size, size, {
        limits: [shared, shared, shared],
        stretch: req.rgb.stretch,
      });
    }
    return {
      rgba,
      label: 'RGB',
      sublabel: members.map(bandLabel).join(' / '),
    };
  };

  const [singlePanels, rgbPanel] = await Promise.all([Promise.all(singles), composite()]);
  const panels: Panel[] = rgbPanel ? [...singlePanels, rgbPanel] : singlePanels;
  const n = panels.length;
  if (n === 0) throw new Error('figure: no panels');

  const cols = Math.max(1, Math.min(req.cols ?? n, n));
  const rows = Math.ceil(n / cols);
  const width = cols * size + (cols - 1) * FIGURE_GAP;
  const height = rows * size + (rows - 1) * FIGURE_GAP;

  // Shutter footprints projected through the panel's own N-up TAN WCS (the
  // one `reprojectToNorthUp` built), so they land exactly on the pixels.
  const shutterSvg = req.shutters && req.shutters.length > 0
    ? shuttersSvg(req.shutters, ra, dec, req.fovArcsec / size, size)
    : '';

  const composites: sharp.OverlayOptions[] = [];
  const overlays: string[] = [];
  const fontSize = Math.max(11, Math.round(size / 14));
  const subFontSize = Math.max(9, Math.round(fontSize * 0.75));

  panels.forEach((panel, i) => {
    const left = (i % cols) * (size + FIGURE_GAP);
    const top = Math.floor(i / cols) * (size + FIGURE_GAP);
    composites.push({
      input: Buffer.from(panel.rgba.buffer, panel.rgba.byteOffset, panel.rgba.byteLength),
      raw: { width: size, height: size, channels: 4 },
      left,
      top,
    });
    // A nested <svg> clips its content to the panel, so a footprint straddling
    // the edge never bleeds into the neighbour.
    const parts: string[] = [];
    if (shutterSvg) parts.push(shutterSvg);
    if (req.labels !== false) {
      const baseline = LABEL_PAD + labelAscent(fontSize);
      parts.push(labelSvg(panel.label, LABEL_PAD, baseline, { fontSize }));
      // The composite's channel list only when it fits the panel.
      if (panel.sublabel && labelWidth(panel.sublabel, subFontSize) <= size - 2 * LABEL_PAD) {
        const subBaseline = baseline + subFontSize * 1.35;
        parts.push(labelSvg(panel.sublabel, LABEL_PAD, subBaseline, { fontSize: subFontSize }));
      }
    }
    if (parts.length > 0) {
      overlays.push(`<svg x="${left}" y="${top}" width="${size}" height="${size}">${parts.join('')}</svg>`);
    }
  });
  if (overlays.length > 0) {
    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}">` +
      `${overlays.join('')}</svg>`;
    composites.push({ input: Buffer.from(svg), left: 0, top: 0 });
  }

  return sharp({
    create: { width, height, channels: 4, background: { r: 0, g: 0, b: 0, alpha: 255 } },
  })
    .composite(composites)
    .png()
    .toBuffer();
}

/**
 * SVG polygons for the shutters on one panel, in that panel's raster frame
 * (exported for tests). Colours follow the map: a stable per-observation hue
 * (from the full set, so the palette does not reshuffle with the FOV),
 * stuck-closed red dashed.
 */
export function shuttersSvg(
  shutters: readonly FigureShutter[],
  ra: number,
  dec: number,
  scaleArcsec: number,
  size: number,
): string {
  const wcs = parseWcs(northUpWcsHeader(ra, dec, scaleArcsec, size, size));
  if (!wcs) return '';
  const observations = [...new Set(shutters.map((s) => s.observation))].sort();
  const strokeWidth = Math.max(1, size / 300);
  const polys = shutters.map((s) => {
    const corners = shutterCorners(
      s.center_ra,
      s.center_dec,
      s.position_angle,
      s.aperture_width_arcsec,
      s.aperture_height_arcsec,
    );
    const points = corners
      .map((c) => {
        const p = skyToPix(wcs, c.ra, c.dec);
        // The WCS describes the FITS bottom-up array; the raster is top-down.
        return `${p.x.toFixed(2)},${(size - p.y).toFixed(2)}`;
      })
      .join(' ');
    const stuck = s.shutter_state === 'stuck_closed';
    const color = stuck ? SHUTTER_STUCK_COLOR : getObservationColor(s.observation, observations);
    return (
      `<polygon points="${points}" fill="${color}" fill-opacity="${stuck ? 0 : 0.15}" ` +
      `stroke="${color}" stroke-width="${stuck ? strokeWidth * 1.5 : strokeWidth}"` +
      (stuck ? ` stroke-dasharray="${3 * strokeWidth} ${2 * strokeWidth}"` : '') +
      '/>'
    );
  });
  return polys.join('');
}
