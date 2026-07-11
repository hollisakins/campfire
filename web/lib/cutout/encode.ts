// Server-only PNG/JPEG encoding for cutouts (epic #337, Phase 5). Kept out of the
// core (`./index`) so the engine stays free of the native `sharp` dependency and
// unit-tests without it. `sharp` here only encodes an already-rendered RGBA buffer
// (and optional flatten for JPEG) — not the retired PNG-tile compositing.

import sharp from 'sharp';
import type { CutoutRGBA } from './index';

/** Encode a rendered cutout's RGBA to a PNG buffer (transparency preserved). */
export function encodePng(cutout: CutoutRGBA): Promise<Buffer> {
  return sharp(Buffer.from(cutout.rgba.buffer, cutout.rgba.byteOffset, cutout.rgba.byteLength), {
    raw: { width: cutout.width, height: cutout.height, channels: 4 },
  })
    .png()
    .toBuffer();
}

/** Encode to JPEG, flattening no-data (transparent) pixels onto `background`. */
export function encodeJpeg(cutout: CutoutRGBA, background = '#000000', quality = 90): Promise<Buffer> {
  return sharp(Buffer.from(cutout.rgba.buffer, cutout.rgba.byteOffset, cutout.rgba.byteLength), {
    raw: { width: cutout.width, height: cutout.height, channels: 4 },
  })
    .flatten({ background })
    .jpeg({ quality })
    .toBuffer();
}
