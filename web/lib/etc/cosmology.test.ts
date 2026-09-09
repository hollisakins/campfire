import { describe, expect, it } from 'vitest';
import { apparentFromAbsolute, distanceModulus, luminosityDistanceMpc } from './cosmology';

describe('flat ΛCDM distances (Planck18)', () => {
  it('reduces to Hubble\'s law at low z', () => {
    // D_L ≈ cz/H0 (1 + z/2 (1 - q0)) — at z = 0.01 the correction is ~0.6%
    expect(luminosityDistanceMpc(0.01)).toBeCloseTo(44.6, 0);
  });

  it('matches astropy Planck18 luminosity distances to 0.3% (radiation neglected; < 0.01 mag)', () => {
    // astropy.cosmology.Planck18.luminosity_distance(z).value
    const ref: [number, number][] = [
      [0.5, 2919.6],
      [1.0, 6791.3],
      [3.0, 26016.0],
      [6.0, 58975.7],
      [10.0, 105999.1],
    ];
    for (const [z, dl] of ref) {
      expect(Math.abs(luminosityDistanceMpc(z) / dl - 1)).toBeLessThan(0.003);
    }
  });

  it('turns an absolute magnitude into an apparent one', () => {
    // astropy: -20 + Planck18.distmod(6) - 2.5 log10(7) = 26.7406
    expect(apparentFromAbsolute(-20, 6)).toBeCloseTo(26.7406, 2);
    expect(distanceModulus(0)).toBe(-Infinity);
  });
});
