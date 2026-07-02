/**
 * IRAF ZScale display limits — a faithful port of astropy's
 * `astropy.visualization.ZScaleInterval.get_limits` (the same algorithm the
 * pipeline's `preview` step uses via `ZScaleInterval().get_limits`), so the
 * live in-browser render opens with the exact contrast reviewers already know
 * from the `_preview.png` thumbnails (epic #261, N4).
 *
 * The algorithm: subsample to ~`nsamples` finite values, sort, fit a line to
 * the sorted samples with iterative k-sigma rejection (dilating the reject mask
 * each pass), then set z1/z2 from the median and the slope/contrast around the
 * centre sample. Pure — no GL/DOM.
 */

export interface ZScaleParams {
  nsamples: number;
  contrast: number;
  maxReject: number;
  minNpixels: number;
  krej: number;
  maxIterations: number;
}

export const DEFAULT_ZSCALE: ZScaleParams = {
  nsamples: 1000,
  contrast: 0.25,
  maxReject: 0.5,
  minNpixels: 5,
  krej: 2.5,
  maxIterations: 5,
};

/** Least-squares slope/intercept of `y[i]` vs index `i`, over `good[i]` only. */
function fitLine(
  samples: Float64Array,
  good: Uint8Array,
): { slope: number; intercept: number } {
  let n = 0;
  let sx = 0;
  let sy = 0;
  for (let i = 0; i < samples.length; i++) {
    if (!good[i]) continue;
    n++;
    sx += i;
    sy += samples[i]!;
  }
  if (n === 0) return { slope: 0, intercept: 0 };
  const mx = sx / n;
  const my = sy / n;
  let sxx = 0;
  let sxy = 0;
  for (let i = 0; i < samples.length; i++) {
    if (!good[i]) continue;
    const dx = i - mx;
    sxx += dx * dx;
    sxy += dx * (samples[i]! - my);
  }
  const slope = sxx > 0 ? sxy / sxx : 0;
  return { slope, intercept: my - slope * mx };
}

/**
 * `[z1, z2]` ZScale limits over the finite values of `data`, or `null` when the
 * data has no finite values. Mirrors astropy's implementation, including the
 * `np.convolve(..., 'same')` dilation of the reject mask by `ngrow`.
 */
export function zscaleLimits(
  data: Float32Array | Float64Array,
  params: ZScaleParams = DEFAULT_ZSCALE,
): [number, number] | null {
  const { nsamples, contrast, maxReject, minNpixels, krej, maxIterations } = params;

  // Match astropy: filter to finite values FIRST, then subsample the finite
  // sequence with a fixed stride (`finite[::stride][:nsamples]`). Two-pass so we
  // never materialize the full finite array: count finite, then collect every
  // stride-th finite value.
  let finiteCount = 0;
  for (let i = 0; i < data.length; i++) if (Number.isFinite(data[i]!)) finiteCount++;
  if (finiteCount === 0) return null;
  const stride = Math.max(1, Math.floor(finiteCount / nsamples));
  const collected: number[] = [];
  let fidx = 0;
  for (let i = 0; i < data.length && collected.length < nsamples; i++) {
    const v = data[i]!;
    if (!Number.isFinite(v)) continue;
    if (fidx % stride === 0) collected.push(v);
    fidx++;
  }
  if (collected.length === 0) return null;

  const samples = Float64Array.from(collected);
  samples.sort();
  const npix = samples.length;
  const vmin = samples[0]!;
  const vmax = samples[npix - 1]!;
  if (npix < 3) return [vmin, vmax];

  const minpix = Math.max(minNpixels, Math.floor(npix * maxReject));
  const ngrow = Math.max(1, Math.floor(npix * 0.01));
  const half = Math.floor(ngrow / 2); // np.convolve 'same' offset for a length-ngrow kernel

  const bad = new Uint8Array(npix); // 0 = good
  let ngoodpix = npix;
  let lastNgoodpix = npix + 1;
  let fit = { slope: 0, intercept: 0 };

  for (let iter = 0; iter < maxIterations; iter++) {
    if (ngoodpix >= lastNgoodpix || ngoodpix < minpix) break;

    fit = fitLine(samples, invert(bad));
    // Residuals + std over the currently-good pixels.
    let sum = 0;
    let count = 0;
    const flat = new Float64Array(npix);
    for (let i = 0; i < npix; i++) {
      flat[i] = samples[i]! - (fit.slope * i + fit.intercept);
      if (!bad[i]) {
        sum += flat[i]!;
        count++;
      }
    }
    const mean = count > 0 ? sum / count : 0;
    let varSum = 0;
    for (let i = 0; i < npix; i++) {
      if (!bad[i]) varSum += (flat[i]! - mean) * (flat[i]! - mean);
    }
    const std = count > 0 ? Math.sqrt(varSum / count) : 0;
    const threshold = krej * std;

    // Mark new rejects, then dilate the reject mask by the length-ngrow kernel
    // exactly as np.convolve(mask, ones(ngrow), 'same') does.
    const flagged = new Uint8Array(npix);
    for (let i = 0; i < npix; i++) {
      if (flat[i]! < -threshold || flat[i]! > threshold) flagged[i] = 1;
    }
    for (let i = 0; i < npix; i++) {
      if (bad[i]) continue;
      // dilation: bad if any flagged pixel falls in the centered window
      let hit = false;
      for (let k = 0; k < ngrow; k++) {
        const j = i - half + k;
        if (j >= 0 && j < npix && flagged[j]) {
          hit = true;
          break;
        }
      }
      if (hit) bad[i] = 1;
    }

    lastNgoodpix = ngoodpix;
    ngoodpix = npix - countTrue(bad);
  }

  const centerPixel = Math.floor((npix - 1) / 2);
  const median = npix % 2 === 1
    ? samples[centerPixel]!
    : 0.5 * (samples[centerPixel]! + samples[centerPixel + 1]!);

  if (ngoodpix < minpix) return [vmin, vmax];

  let slope = fit.slope;
  if (contrast > 0) slope = slope / contrast;
  const z1 = Math.max(vmin, median - centerPixel * slope);
  const z2 = Math.min(vmax, median + (npix - 1 - centerPixel) * slope);
  if (!(z2 > z1)) return [vmin, vmax];
  return [z1, z2];
}

function invert(bad: Uint8Array): Uint8Array {
  const good = new Uint8Array(bad.length);
  for (let i = 0; i < bad.length; i++) good[i] = bad[i] ? 0 : 1;
  return good;
}

function countTrue(a: Uint8Array): number {
  let n = 0;
  for (let i = 0; i < a.length; i++) if (a[i]) n++;
  return n;
}
