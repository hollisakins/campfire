// Cutout pixel budgets — a leaf module with no imports, so the cutouts page
// can show the limits without dragging the science-cutout engine (and all of
// @fitsgl/core, ~45 kB br) into its client bundle (perf T1-7 / #503).

/** Per-band pixel budget (4096² ≈ 64 MB of float32). */
export const MAX_PIXELS_PER_BAND = 4096 * 4096;
/** Whole-request budget across bands (≈ 256 MB of float32). */
export const MAX_PIXELS_TOTAL = 4 * MAX_PIXELS_PER_BAND;
