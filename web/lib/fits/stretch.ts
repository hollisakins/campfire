/**
 * Non-linear display stretches.
 *
 * The viewer normalizes a raw pixel value to [0,1] over the display interval,
 * then applies one of these transfer functions before mapping to grey / a
 * colormap. The raw float texture is never modified — switching stretch only
 * changes this curve. The two non-linear curves match astropy.visualization
 * with fixed softening: `LogStretch(a=1000)` and `AsinhStretch(a=0.1)`.
 *
 * Vendored + trimmed from `fitsgl` (`fitsgl-core/src/renderer/stretch.ts`) —
 * single-band only (no trilogy/RGB composite), which is all a greyscale NIRCam
 * exposure needs (epic #261, N4). This module is the single source of truth for
 * the softening constants: the viewer's fragment shader inlines `LOG_SOFTENING`
 * / `ASINH_SOFTENING` from here, so the GLSL curve and this TS reference cannot
 * drift.
 */

/** Selectable transfer functions. `linear` is the identity. */
export type StretchMode = 'linear' | 'log' | 'sqrt' | 'asinh';

/** All stretch modes, in UI/declaration order. */
export const STRETCH_MODES: readonly StretchMode[] = ['linear', 'log', 'sqrt', 'asinh'];

/**
 * Integer ids handed to the shader's `u_stretchMode` uniform. Kept in lockstep
 * with the GLSL branch order. Ids match `fitsgl`'s numbering (3 = trilogy is
 * intentionally unused here) so the lifted shader body stays a faithful mirror.
 */
export const STRETCH_MODE_IDS: Record<StretchMode, number> = {
  linear: 0,
  log: 1,
  asinh: 2,
  sqrt: 4,
};

/** Fixed `log` softening — astropy `LogStretch` default. */
export const LOG_SOFTENING = 1000;
/** Fixed `asinh` softening — astropy `AsinhStretch` default. */
export const ASINH_SOFTENING = 0.1;

export function isStretchMode(value: string): value is StretchMode {
  return value === 'linear' || value === 'log' || value === 'sqrt' || value === 'asinh';
}

/**
 * Apply a stretch transfer function to an already-normalized value in [0,1];
 * the result is in [0,1]. Mirrors the GLSL `applyStretch` exactly.
 */
export function applyStretch(norm: number, mode: StretchMode): number {
  switch (mode) {
    case 'log':
      return Math.log(LOG_SOFTENING * norm + 1) / Math.log(LOG_SOFTENING + 1);
    case 'asinh':
      return Math.asinh(norm / ASINH_SOFTENING) / Math.asinh(1 / ASINH_SOFTENING);
    case 'sqrt':
      return Math.sqrt(norm);
    case 'linear':
      return norm;
  }
}

/**
 * Full single-channel transfer: linear-normalize `v` over [lo, hi], clamp to
 * [0,1], then apply the stretch. Mirrors the GLSL `scaleChannel`.
 */
export function scaleValue(v: number, lo: number, hi: number, mode: StretchMode): number {
  const norm = Math.min(1, Math.max(0, (v - lo) / (hi - lo)));
  return applyStretch(norm, mode);
}
