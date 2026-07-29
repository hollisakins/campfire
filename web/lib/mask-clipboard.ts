/**
 * Session-scoped clipboard for copying mask polygons between NIRCam exposures.
 *
 * Mask coordinates are per-exposure DS9 image pixels, not sky coordinates
 * (the browser can't do GWCS — see python/campfire/deploy/nircam_masks.py),
 * so a paste is a raw pixel-frame copy: right for detector-fixed artifacts
 * (bad columns, amp glow, snowball-prone regions), wrong for sky-fixed
 * features across dithers. The stored width/height let callers refuse a paste
 * onto an exposure with different dimensions, where the coordinates would be
 * silently wrong.
 *
 * Backed by sessionStorage (per tab, survives reloads) with an in-memory
 * mirror so reads are synchronous and cheap enough to call during render.
 */

import type { MaskPolygon } from '@/lib/types';

export interface MaskClipboard {
  /** Polygons in the canonical storage frame (DS9 image coords, 1-indexed). */
  polygons: MaskPolygon[];
  imageWidth: number;
  imageHeight: number;
  /** Filename of the exposure the polygons were copied from (provenance). */
  sourceFilename: string;
  copiedAt: string;
}

const STORAGE_KEY = 'campfire:nircam-mask-clipboard';

let mem: MaskClipboard | null = null;
let loaded = false;

export function getMaskClipboard(): MaskClipboard | null {
  if (!loaded) {
    loaded = true;
    try {
      const raw = sessionStorage.getItem(STORAGE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw) as MaskClipboard;
        if (Array.isArray(parsed.polygons) && parsed.polygons.length > 0) {
          mem = parsed;
        }
      }
    } catch {
      // Storage unavailable (SSR, privacy mode) or corrupt — treat as empty.
    }
  }
  return mem;
}

export function setMaskClipboard(entry: MaskClipboard): void {
  mem = entry;
  loaded = true;
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(entry));
  } catch {
    // The in-memory mirror still serves this tab until reload.
  }
}
