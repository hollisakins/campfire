// Shutter-region geometry for the NIRSpec nods overlay (P6). A faithful port of
// two pipeline functions so the web overlay matches the *_nods.pdf exactly:
//   - _compute_shutter_regions  (stuck_shutters.py:_compute_shutter_regions)
//   - _annotate_stuck_shutters  (plots.py:_annotate_stuck_shutters)
// Pure module (no 'use server'); imported by NodCell for drawing and by tests.

// JWST NIRSpec MSA slit geometry (must match stuck_shutters.py constants).
const MSA_MARGIN = 1.05; // 0.55 (half open area) + 0.50 (padding beyond outer shutters)
const MSA_PITCH = 1.15; // slit_frame units per shutter pitch
const PADDING = 0.5; // padding zone beyond the outer shutters, excluded from regions

/**
 * numpy-compatible round-half-away-from-zero. NB: numpy actually rounds
 * half-to-even; for the fractional pixel positions here an exact .5 is
 * vanishingly unlikely, so Math.round (half-up) matches in practice. Kept as a
 * named helper to flag the (negligible) discrepancy — the vitest oracle catches
 * any real divergence.
 */
function npRound(x: number): number {
  return Math.round(x);
}

export interface ShutterRegion {
  rowStart: number;
  rowEnd: number;
}

/**
 * Pixel row boundaries for each shutter region in an s2d image. Port of
 * `_compute_shutter_regions`. Regions are ordered from the BOTTOM of the s2d
 * (region 0 = highest shutter_column = shutter N) to the top (region N-1 =
 * shutter 1). The 0.50 padding beyond the outer shutters and the sub-pixel bar
 * rows between shutters are excluded.
 */
export function computeShutterRegions(nShutters: number, nRows: number): ShutterRegion[] {
  const total = (nShutters - 1) * MSA_PITCH + 2 * MSA_MARGIN;
  const outerMarginPix = npRound((PADDING / total) * (nRows - 1));

  const boundaries: number[] = [outerMarginPix];
  for (let k = 0; k < nShutters - 1; k++) {
    const frac = (MSA_MARGIN + (k + 0.5) * MSA_PITCH) / total;
    boundaries.push(npRound(frac * (nRows - 1)));
  }
  boundaries.push(nRows - outerMarginPix);

  const regions: ShutterRegion[] = [];
  for (let i = 0; i < nShutters; i++) {
    const start = boundaries[i]! + (i > 0 ? 1 : 0) + 1;
    const end = boundaries[i + 1]!;
    regions.push({ rowStart: start, rowEnd: end });
  }
  return regions;
}

export interface ShutterOverlayRegion extends ShutterRegion {
  ordinal: number; // 1-indexed slitlet ordinal
  stuck: boolean; // whether this ordinal is flagged stuck
}

export interface ShutterOverlayInput {
  /** SHUTSTA header string from the s2d SCI extension; its length is the current shutter count. */
  shutsta: string | null | undefined;
  /** 1-indexed shutter ordinals flagged as stuck (from nirspec_source_review.stuck_shutters). */
  stuckList: number[];
  /** STKSHTRS header from the s2d PRIMARY extension ('N/A' ⇒ pre-reprocessing). */
  stkshtrs: string | null | undefined;
  /** Number of spatial pixels (image height). */
  nRows: number;
}

/**
 * Map an s2d cell's shutter geometry to labelled, clickable overlay regions. Port
 * of `_annotate_stuck_shutters` — handles pre-reprocessing (stuck shutters still in
 * SHUTSTA) and post-reprocessing (removed from the metafile) geometry. Returns [] if
 * the cell has no shutters or the reprocessed window is empty.
 */
export function shutterOverlayRegions(input: ShutterOverlayInput): ShutterOverlayRegion[] {
  const { shutsta, stuckList, stkshtrs, nRows } = input;
  const nCurrent = shutsta ? shutsta.length : 0;
  if (nCurrent < 1) return [];

  // STKSHTRS distinguishes pre- vs post-reprocessing; 'N/A' (or absent) ⇒ pre.
  const reprocessed = !!stkshtrs && stkshtrs !== 'N/A';

  let nEffective: number;
  let ordinals: number[];
  if (!reprocessed) {
    nEffective = nCurrent;
    ordinals = Array.from({ length: nCurrent }, (_, k) => nCurrent - k);
  } else {
    const nOriginal = nCurrent + stuckList.length;
    const stuckSet = new Set(stuckList);
    const remaining: number[] = [];
    for (let s = 1; s <= nOriginal; s++) if (!stuckSet.has(s)) remaining.push(s);
    if (remaining.length === 0) return [];
    const minRemain = remaining[0]!;
    const maxRemain = remaining[remaining.length - 1]!;
    nEffective = maxRemain - minRemain + 1;
    ordinals = Array.from({ length: nEffective }, (_, k) => maxRemain - k);
  }

  const stuckSet = new Set(stuckList);
  return computeShutterRegions(nEffective, nRows).map((region, k) => {
    const ordinal = ordinals[k]!;
    return { ...region, ordinal, stuck: stuckSet.has(ordinal) };
  });
}
