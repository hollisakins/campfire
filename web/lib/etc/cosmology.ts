/**
 * Minimal flat ΛCDM distances for the ETC page: turning an absolute AB
 * magnitude and a redshift into the apparent magnitude the calculator needs.
 * Planck 2018 parameters (astropy's `Planck18`); radiation is neglected, which
 * is a ≤ 0.2% effect on D_L at z ≤ 10 (< 0.005 mag).
 */

export const H0_KM_S_MPC = 67.66;
export const OMEGA_M = 0.30966;
export const OMEGA_L = 1 - OMEGA_M;
const C_KM_S = 299792.458;

function invE(z: number): number {
  return 1 / Math.sqrt(OMEGA_M * (1 + z) ** 3 + OMEGA_L);
}

/** Line-of-sight comoving distance [Mpc] (Simpson's rule, 400 panels). */
export function comovingDistanceMpc(z: number): number {
  if (!(z > 0)) return 0;
  const n = 400;
  const h = z / n;
  let s = invE(0) + invE(z);
  for (let i = 1; i < n; i++) s += (i % 2 ? 4 : 2) * invE(i * h);
  return ((C_KM_S / H0_KM_S_MPC) * h * s) / 3;
}

/** Luminosity distance [Mpc]. */
export function luminosityDistanceMpc(z: number): number {
  return (1 + z) * comovingDistanceMpc(z);
}

/** Distance modulus 5 log10(D_L / 10 pc). */
export function distanceModulus(z: number): number {
  const dl = luminosityDistanceMpc(z);
  return dl > 0 ? 5 * Math.log10(dl) + 25 : -Infinity;
}

/**
 * Apparent AB magnitude of a source with absolute AB magnitude `M` at
 * redshift `z`, for a flat-f_ν continuum: m = M + DM(z) − 2.5 log10(1+z).
 * (The −2.5 log10(1+z) is the bandwidth term of the K-correction; it is the
 * whole K-correction when f_ν is flat, which is what the calculator assumes
 * when it applies one magnitude across the disperser's band.)
 */
export function apparentFromAbsolute(M: number, z: number): number {
  return M + distanceModulus(z) - 2.5 * Math.log10(1 + z);
}
