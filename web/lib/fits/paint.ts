/**
 * CPU float32 → ImageData paint for the NIRSpec nods renderer (P5).
 *
 * The nods grid shows many small rectified S2D slitlet cutouts at once. A WebGL
 * canvas per cell would exhaust the browser's ~16-context ceiling, and the PDF
 * uses ONE shared ZScale across all cells (not per-image auto-stretch), so we
 * paint on a plain 2D canvas: normalize+stretch each pixel through the SAME
 * `scaleValue` transfer the WebGL shader uses, map through the colormap LUT, and
 * write ImageData. Cheap here — slitlets are hundreds of px, not 2048².
 *
 * origin='lower' (matching viewer.ts): FITS row 0 is painted at the BOTTOM of the
 * image. NaN/non-finite pixels take the colormap's background colour.
 */

import { scaleValue, type StretchMode } from './stretch';
import { resolveColormap, backgroundForColormap, type ColormapName } from './colormaps';

export function paintToImageData(
  data: Float32Array,
  width: number,
  height: number,
  vmin: number,
  vmax: number,
  stretch: StretchMode,
  colormap: ColormapName,
): ImageData {
  const { rgba, size } = resolveColormap(colormap);
  const [br, bg, bb] = backgroundForColormap(colormap);
  const out = new Uint8ClampedArray(width * height * 4);

  for (let row = 0; row < height; row++) {
    const destRow = height - 1 - row; // origin='lower': row 0 → bottom
    for (let col = 0; col < width; col++) {
      const v = data[row * width + col]!;
      const di = (destRow * width + col) * 4;
      if (!Number.isFinite(v)) {
        out[di] = br; out[di + 1] = bg; out[di + 2] = bb; out[di + 3] = 255;
        continue;
      }
      const t = scaleValue(v, vmin, vmax, stretch); // [0,1]
      let idx = Math.round(t * (size - 1));
      if (idx < 0) idx = 0;
      else if (idx >= size) idx = size - 1;
      const ci = idx * 4;
      out[di] = rgba[ci]!;
      out[di + 1] = rgba[ci + 1]!;
      out[di + 2] = rgba[ci + 2]!;
      out[di + 3] = 255;
    }
  }
  return new ImageData(out, width, height);
}
