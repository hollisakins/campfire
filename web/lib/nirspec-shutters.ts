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
  /** 1-indexed shutter ordinals marked stuck (nirspec_stuck_shutters, ``stuckOrdinals``). */
  stuckList: number[];
  /** STKSHTRS header from the s2d PRIMARY extension ('N/A' ⇒ pre-reprocessing). A
   *  reprocessed cell's list of removed ordinals sets its geometry: the pipeline plot
   *  reads the full local list (automatic calls included), which the web cannot. */
  stkshtrs: string | null | undefined;
  /** Number of spatial pixels (image height). */
  nRows: number;
}

/** The ordinals a reprocessed cell's STKSHTRS lists (stage 2a writes the list, e.g.
 * "[1, 3]"), or null when it is not a list. */
export function removedOrdinals(stkshtrs: string): number[] | null {
  const m = /^\s*\[([\d,\s]*)\]\s*$/.exec(stkshtrs);
  if (!m) return null;
  const out = m[1]!.split(',').map((s) => s.trim()).filter(Boolean).map(Number);
  return out.length ? out : null;
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
  const removed = reprocessed ? removedOrdinals(stkshtrs) : null;

  let nEffective: number;
  let ordinals: number[];
  if (!reprocessed) {
    nEffective = nCurrent;
    ordinals = Array.from({ length: nCurrent }, (_, k) => nCurrent - k);
  } else {
    // The shutters this cell's processing removed: its own STKSHTRS list when it is one
    // (the pipeline's automatic calls included, which never reach the database), else
    // the marks.
    const removedList = removed ?? stuckList;
    const nOriginal = nCurrent + removedList.length;
    const removedSet = new Set(removedList);
    const remaining: number[] = [];
    for (let s = 1; s <= nOriginal; s++) if (!removedSet.has(s)) remaining.push(s);
    if (remaining.length === 0) return [];
    const minRemain = remaining[0]!;
    const maxRemain = remaining[remaining.length - 1]!;
    nEffective = maxRemain - minRemain + 1;
    ordinals = Array.from({ length: nEffective }, (_, k) => maxRemain - k);
  }

  const stuckSet = new Set([...stuckList, ...(reprocessed ? removed ?? [] : [])]);
  return computeShutterRegions(nEffective, nRows).map((region, k) => {
    const ordinal = ordinals[k]!;
    return { ...region, ordinal, stuck: stuckSet.has(ordinal) };
  });
}

// ---------------------------------------------------------------------------
// Physical shutters (nirspec_stuck_shutters). A shutter is keyed per exposure root
// (MSA configuration) by "q<quadrant>r<row>c<column>", the metafile's
// shutter_quadrant / shutter_row / shutter_column — the same key as
// campfire_pipeline.nirspec.stuck_list and the table's CHECK.
// ---------------------------------------------------------------------------

export const SHUTTER_KEY_RE = /^q([1-4])r(\d+)c(\d+)$/;

/** An exposure root (MSA configuration), e.g. "jw07076001001_03101": the read route and
 * the write action of nirspec_stuck_shutters accept the same ones. The sequence's
 * activity counts in base 36 past the ninth ("jw12497004001_0410f"). */
export const EXPOSURE_ROOT_RE = /^jw\d{11}_[0-9a-z]{5}$/;

export interface ShutterKey { q: number; r: number; c: number }

export function parseShutterKey(key: string): ShutterKey | null {
  const m = SHUTTER_KEY_RE.exec(key);
  return m ? { q: Number(m[1]), r: Number(m[2]), c: Number(m[3]) } : null;
}

/**
 * The legacy ordinal frame of a source's slitlet: ordinal k (1-indexed) is the k-th
 * distinct shutter column in ascending order (stuck_list.source_columns), mapped to
 * every physical key at that column. `keys` is spectrum_exposures.msa_shutters.
 */
export function slitletOrdinals(keys: string[]): Map<number, string[]> {
  const byCol = new Map<number, string[]>();
  for (const key of keys) {
    const p = parseShutterKey(key);
    if (!p) continue;
    const list = byCol.get(p.c) ?? [];
    list.push(key);
    byCol.set(p.c, list);
  }
  const out = new Map<number, string[]>();
  [...byCol.keys()].sort((a, b) => a - b).forEach((c, i) => out.set(i + 1, byCol.get(c)!));
  return out;
}

/** The web's view of one row of nirspec_stuck_shutters (auto calls are never in the DB). */
export interface StuckShutterMark {
  origin: 'manual' | 'web';
  stuck: boolean;
}

/** The ordinals (overlay frame) of a slitlet whose shutters are marked stuck. */
export function stuckOrdinals(keys: string[], marks: Map<string, StuckShutterMark>): number[] {
  const out: number[] = [];
  for (const [ordinal, ks] of slitletOrdinals(keys)) {
    if (ks.some((k) => marks.get(k)?.stuck)) out.push(ordinal);
  }
  return out;
}

/**
 * The web mark a click on a cutout's shutter band sends for its physical shutters
 * (`rows`: their current marks). A band drawn open is marked stuck. A band drawn
 * stuck is cleared: its web rows are deleted when they are what makes it stuck, else
 * the pipeline's own call (in the cell's STKSHTRS, never in the database) is vetoed;
 * a band already vetoed has its veto deleted. `null` = delete the web rows.
 */
export function cutoutClickMark(drawnStuck: boolean, rows: (StuckShutterMark | undefined)[]): boolean | null {
  if (rows.length > 0 && rows.every((r) => r?.stuck === true)) return null;
  if (!drawnStuck) return true;
  return rows.length > 0 && rows.every((r) => r?.stuck === false) ? null : false;
}

/**
 * The web cycle for one shutter: no row → stuck → not stuck (a veto of the
 * pipeline's own call) → no row. `null` = delete the web row. A manual row is set
 * locally and never changed from the web: `undefined`.
 */
export function nextWebMark(cur: StuckShutterMark | undefined): boolean | null | undefined {
  if (cur?.origin === 'manual') return undefined;
  if (!cur) return true;
  return cur.stuck ? false : null;
}
