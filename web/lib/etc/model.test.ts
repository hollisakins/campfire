/**
 * Regression tests for the calculator core, pinned to the same published
 * 2026.09 depth-table numbers as `etc/tests/test_model.py` so the web
 * calculator and the Python package cannot drift apart silently.
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  READOUT_GROUP_S,
  abToUjy,
  continuum,
  line,
  makeExposure,
  nearestBin,
  recovery,
  resolvePlacement,
  timeForSnr,
  type NoiseModel,
} from './model';

const MODEL: NoiseModel = JSON.parse(
  readFileSync(path.join(__dirname, '..', '..', 'public', 'etc', 'models', 'nirspec-2026.09.json'), 'utf8')
);
const PRISM = MODEL.dispersers.prism_clear;

// From analysis/dispersers/prism_clear/payload.json (model 2026.09): T = 10 ks,
// t_exp = 1000 s, centred point source, at 0.65, 1.15, 1.65 um.
const PRISM_10KS = {
  wave: [0.65, 1.15, 1.65],
  sig_pix_nJy: [4.478753349136493, 2.3863491496791363, 2.7839844861158],
  sig_opt_nJy: [9.07684003911615, 3.7692652895902965, 4.598341659178076],
  sig_3px_nJy: [8.780724575228819, 3.7494817683726933, 4.525093007347401],
  ab5_pix: [27.257738284274907, 28.211933226226726, 27.996071898064727],
  ab5_res: [27.559081603874006, 28.638400953203544, 28.5125307029586],
  line5: [4.308436403031944e-18, 1.5890023017433559e-18, 9.71047231719418e-19],
};

const rel = (a: number, b: number, tol: number) => Math.abs(a - b) <= tol * Math.abs(b);

describe('model file', () => {
  it('is the 2026.09 model with all eight dispersers', () => {
    expect(MODEL.version).toBe('2026.09');
    expect(Object.keys(MODEL.dispersers).sort()).toEqual(
      ['g140h_f100lp', 'g140m_f070lp', 'g140m_f100lp', 'g235h_f170lp', 'g235m_f170lp', 'g395h_f290lp', 'g395m_f290lp', 'prism_clear']
    );
    expect(MODEL.readout_seconds_per_group).toEqual(READOUT_GROUP_S);
  });
});

describe('exposure timing', () => {
  it('builds from a readout setup or from explicit times', () => {
    const e = makeExposure({ readout: 'nrsirs2', ngroups: 13, nint: 1, nexp: 6 });
    expect(e.perExposureS).toBeCloseTo(13 * READOUT_GROUP_S.nrsirs2, 9);
    expect(e.totalS).toBeCloseTo(6 * e.perExposureS, 9);
    const d = makeExposure({ totalS: 10000, perExposureS: 1000 });
    expect(d.readout).toBeUndefined();
    expect(d.totalS).toBe(10000);
    // total time cannot be shorter than one exposure
    expect(makeExposure({ totalS: 500, perExposureS: 1000 }).totalS).toBe(1000);
    expect(() => makeExposure({ readout: 'bogus', ngroups: 10 })).toThrow();
  });
});

describe('PRISM depths', () => {
  const e = makeExposure({ totalS: 10000, perExposureS: 1000 });
  const opt = continuum(PRISM, e, { extraction: 'optimal' });
  const box = continuum(PRISM, e, { extraction: '3px' });

  it('match the published 10 ks tables to 1e-9', () => {
    PRISM_10KS.wave.forEach((w, k) => {
      const i = nearestBin(PRISM, w);
      const o = opt[i]!;
      const b = box[i]!;
      expect(o.wave).toBeCloseTo(w, 6);
      expect(rel(o.sigPix * 1e3, PRISM_10KS.sig_pix_nJy[k], 1e-9)).toBe(true);
      expect(rel(o.sig1d * 1e3, PRISM_10KS.sig_opt_nJy[k], 1e-9)).toBe(true);
      expect(rel(b.sig1d * 1e3, PRISM_10KS.sig_3px_nJy[k], 1e-9)).toBe(true);
      expect(Math.abs(o.ab5Pix - PRISM_10KS.ab5_pix[k])).toBeLessThan(1e-9);
      expect(Math.abs(o.ab5Res - PRISM_10KS.ab5_res[k])).toBeLessThan(1e-9);
      expect(rel(o.line5, PRISM_10KS.line5[k], 1e-9)).toBe(true);
    });
  });

  it('scale as T^-1/2 at fixed t_exp and favour longer exposures', () => {
    const i = nearestBin(PRISM, 2.0);
    const s1 = continuum(PRISM, e)[i]!.sig1d;
    const s4 = continuum(PRISM, makeExposure({ totalS: 40000, perExposureS: 1000 }))[i]!.sig1d;
    expect(s4).toBeCloseTo(s1 / 2, 12);
    const long = continuum(PRISM, makeExposure({ totalS: 10000, perExposureS: 2000 }))[i]!.sig1d;
    expect(long).toBeLessThan(s1);
    expect(timeForSnr(e, 2.5, 5)).toBeCloseTo(40000, 6);
  });

  it('gratings are more read-noise limited than PRISM', () => {
    const g = MODEL.dispersers.g395m_f290lp;
    const long = makeExposure({ totalS: 10000, perExposureS: 2000 });
    const ip = nearestBin(PRISM, 4.0);
    const ig = nearestBin(g, 4.0);
    const rPrism = continuum(PRISM, e)[ip]!.sig1d / continuum(PRISM, long)[ip]!.sig1d;
    const rG = continuum(g, e)[ig]!.sig1d / continuum(g, long)[ig]!.sig1d;
    expect(rG).toBeGreaterThan(rPrism);
  });
});

describe('source, placement and lines', () => {
  const e = makeExposure({ readout: 'nrsirs2', ngroups: 13, nexp: 6 });

  it('recovery is 1 for a point source and below 1 for galaxies', () => {
    expect(recovery(PRISM, null, 'optimal')).toBe(1);
    const typ = recovery(PRISM, 2.3, 'optimal');
    expect(typ).toBeGreaterThan(0.3);
    expect(typ).toBeLessThan(0.7);
    expect(recovery(PRISM, 2.3, '3px')).toBeLessThan(typ);
  });

  it('placement multipliers are ordered centred < typical < mean', () => {
    const c = resolvePlacement(PRISM, 'centred').mult;
    const t = resolvePlacement(PRISM, 'typical').mult;
    const m = resolvePlacement(PRISM, 'mean').mult;
    expect(c).toBe(1);
    expect(t).toBeGreaterThan(c);
    expect(m).toBeGreaterThan(t);
    expect(resolvePlacement(PRISM, { x: 0, y: 0 }).mult).toBeCloseTo(1, 12);
    expect(resolvePlacement(PRISM, { x: 0.4, y: 0 }).mult).toBeGreaterThan(1.5);
  });

  it('source photon noise raises the 1-D noise and S/N is flux over noise', () => {
    const F = abToUjy(25);
    const i = nearestBin(PRISM, 3.0);
    const bare = continuum(PRISM, e)[i]!;
    const withSrc = continuum(PRISM, e, { fluxSpecUjy: F })[i]!;
    expect(withSrc.sig1d).toBeGreaterThan(bare.sig1d);
    expect(withSrc.snrPix).toBeCloseTo(F / withSrc.sig1d, 12);
  });

  it('an emission line at its own 5σ limit has S/N ≈ 5 (a little less with its photon noise)', () => {
    const i = nearestBin(PRISM, 4.0);
    const row = continuum(PRISM, e)[i]!;
    const l = line(PRISM, e, row, row.line5, 0);
    expect(l.snr).toBeGreaterThan(4.5);
    expect(l.snr).toBeLessThanOrEqual(5.0001);
    expect(l.windowPx).toBeGreaterThanOrEqual(2);
    // a broad line spreads over more pixels and is harder to detect
    const broad = line(PRISM, e, row, row.line5, 3000);
    expect(broad.snr).toBeLessThan(l.snr);
  });
});
