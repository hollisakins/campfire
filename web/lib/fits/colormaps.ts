/**
 * Built-in single-band colormaps.
 *
 * The post-stretch scalar in [0,1] is mapped through a 1-D colormap LUT sampled
 * in the fragment shader. This module decodes the bundled base64 RGB tables
 * (`colormap-data.ts`) to the RGBA bytes uploaded as a `COLORMAP_SIZE × 1` LUT
 * texture, and adds a reversed-grey `Greys` (matplotlib's convention: low =
 * white) so the live render can match the `Greys` PNG previews the pipeline
 * emits.
 *
 * Vendored + adapted from `fitsgl` (`fitsgl-core/src/renderer/colormaps.ts`);
 * extension-less imports for the Next bundler (epic #261, N4).
 */

import { COLORMAP_RGB_B64, COLORMAP_SIZE } from './colormap-data';

export { COLORMAP_SIZE };

/** Names offered to the viewer. `Greys` is the reversed `gray` LUT (matplotlib). */
export const COLORMAP_NAMES = ['gray', 'Greys', 'viridis', 'magma', 'inferno'] as const;
export type ColormapName = (typeof COLORMAP_NAMES)[number];

export function isColormapName(value: string): value is ColormapName {
  return (COLORMAP_NAMES as readonly string[]).includes(value);
}

function decodeBase64(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** Reverse an N×3 RGB table in place-safe fashion (returns a new array). */
function reverseRGB(rgb: Uint8Array): Uint8Array {
  const n = rgb.length / 3;
  const out = new Uint8Array(rgb.length);
  for (let i = 0; i < n; i++) {
    const src = (n - 1 - i) * 3;
    out[i * 3 + 0] = rgb[src + 0]!;
    out[i * 3 + 1] = rgb[src + 1]!;
    out[i * 3 + 2] = rgb[src + 2]!;
  }
  return out;
}

const rgbCache = new Map<ColormapName, Uint8Array>();

/** Decoded N×3 row-major RGB bytes for a palette (cached; treat as read-only). */
function colormapRGB(name: ColormapName): Uint8Array {
  let rgb = rgbCache.get(name);
  if (rgb === undefined) {
    if (name === 'Greys') {
      rgb = reverseRGB(decodeBase64(COLORMAP_RGB_B64.gray));
    } else {
      rgb = decodeBase64(COLORMAP_RGB_B64[name]);
    }
    rgbCache.set(name, rgb);
  }
  return rgb;
}

/** Expand N×3 RGB bytes to N×4 RGBA (opaque). */
function rgbToRGBA(rgb: Uint8Array): Uint8Array {
  const n = rgb.length / 3;
  const rgba = new Uint8Array(n * 4);
  for (let i = 0; i < n; i++) {
    rgba[i * 4 + 0] = rgb[i * 3 + 0]!;
    rgba[i * 4 + 1] = rgb[i * 3 + 1]!;
    rgba[i * 4 + 2] = rgb[i * 3 + 2]!;
    rgba[i * 4 + 3] = 255;
  }
  return rgba;
}

/** Resolve a colormap name to the `{ rgba, size }` a LUT texture upload needs. */
export function resolveColormap(name: ColormapName): { rgba: Uint8Array; size: number } {
  const rgba = rgbToRGBA(colormapRGB(name));
  return { rgba, size: rgba.length / 4 };
}

/**
 * A representative background colour for NaN / no-data pixels under a palette:
 * the palette's colour at scalar 0 (so a `Greys` render clears to white, a
 * `gray`/`viridis` render to near-black), returned as `[r,g,b]` in [0,1].
 */
export function backgroundForColormap(name: ColormapName): [number, number, number] {
  const rgb = colormapRGB(name);
  return [rgb[0]! / 255, rgb[1]! / 255, rgb[2]! / 255];
}
