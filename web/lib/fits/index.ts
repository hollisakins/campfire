/**
 * In-browser FITS render engine for the admin NIRCam exposure viewer (epic
 * #261, N4). A small single-image WebGL2 viewer built from primitives vendored
 * out of the owner's `fitsgl` project — no tile pyramid, no npm dependency.
 */

export { FitsImageViewer, type PixelReadout } from './viewer';
export { fetchSciImage, type SciImage } from './fetch';
export { STRETCH_MODES, type StretchMode, isStretchMode } from './stretch';
export { COLORMAP_NAMES, type ColormapName, isColormapName } from './colormaps';
export { zscaleLimits, DEFAULT_ZSCALE } from './zscale';
