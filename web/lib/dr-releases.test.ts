import { describe, expect, it } from 'vitest';
import { formatArea, formatBytes, parseReleaseComponents, releaseCitation, releaseCount, releaseNextStep, releaseStateBadge, releaseSummary } from './dr-releases';

describe('releaseStateBadge', () => {
  it('labels the lifecycle', () => {
    expect(releaseStateBadge('published')).toEqual({ label: 'Published', tone: 'published' });
    expect(releaseStateBadge('withdrawn').tone).toBe('withdrawn');
    expect(releaseStateBadge('failed').tone).toBe('failed');
    expect(releaseStateBadge('ready')).toEqual({ label: 'Ready', tone: 'pending' });
    expect(releaseStateBadge('frozen').label).toBe('Frozen');
  });
});

describe('parseReleaseComponents', () => {
  it('names each component kind', () => {
    expect(parseReleaseComponents(['nirspec', 'nircam/cosmos', 'photometry/cosmos/COSMOS v1.2'])).toEqual([
      { kind: 'nirspec', label: 'NIRSpec', raw: 'nirspec' },
      { kind: 'nircam', label: 'NIRCam · cosmos', raw: 'nircam/cosmos' },
      { kind: 'photometry', label: 'Photometry · cosmos · COSMOS v1.2', raw: 'photometry/cosmos/COSMOS v1.2' },
    ]);
  });
  it('tolerates a malformed value', () => {
    expect(parseReleaseComponents(null)).toEqual([]);
    expect(parseReleaseComponents(['nirspec', 3])).toHaveLength(1);
  });
});

describe('releaseCount', () => {
  it('reads numbers only', () => {
    expect(releaseCount({ spectra: 12 }, 'spectra')).toBe(12);
    expect(releaseCount({ spectra: '12' }, 'spectra')).toBeNull();
    expect(releaseCount(null, 'spectra')).toBeNull();
  });
});

describe('releaseCitation', () => {
  const base = { tag: 'DR2026.1', doi: null, published_at: '2026-11-02T10:00:00Z', frozen_at: '2026-10-30T10:00:00Z' };

  it('cites the tag and link without a DOI', () => {
    const c = releaseCitation(base, 'https://campfire.example/');
    expect(c.text).toBe('CAMPFIRE Collaboration (2026). CAMPFIRE Data Release DR2026.1. https://campfire.example/releases/DR2026.1');
    expect(c.bibtex).toContain('@misc{campfire_DR2026_1,');
    expect(c.bibtex).not.toContain('doi');
  });
  it('cites the DOI once deposited', () => {
    const c = releaseCitation({ ...base, doi: '10.5281/zenodo.1' }, 'https://campfire.example');
    expect(c.text).toMatch(/doi:10\.5281\/zenodo\.1$/);
    expect(c.bibtex).toContain('doi          = {10.5281/zenodo.1},');
  });
  it('falls back to the freeze year, then n.d.', () => {
    expect(releaseCitation({ ...base, published_at: null }, 'x').text).toContain('(2026)');
    expect(releaseCitation({ ...base, published_at: null, frozen_at: null }, 'x').text).toContain('(n.d.)');
  });
});

describe('formatBytes', () => {
  it('scales by 1000', () => {
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(1_234_000)).toBe('1.2 MB');
    expect(formatBytes(null)).toBe('—');
  });
});

describe('releaseNextStep', () => {
  it('names the CLI step per state', () => {
    expect(releaseNextStep('frozen', 'DR1')).toBe('campfire dr archive --tag DR1');
    expect(releaseNextStep('ready', 'DR1')).toBe('campfire dr publish DR1');
    expect(releaseNextStep('published', 'DR1')).toBeNull();
  });
});

describe('releaseSummary', () => {
  const counts = {
    spectra: 163, objects: 101, nirspec_programs: 31, nirspec_fields: 14,
    by_component: { nirspec: { files: 671, bytes: 235_222_115 }, 'nircam/a2744': { files: 11, mosaics: 8, bytes: 32_718_585_600, area_arcmin2: 148.4 }, 'nircam/cosmos': { files: 2, bytes: 1000 } },
  };
  it('rolls components and counts up per instrument', () => {
    const s = releaseSummary(['nirspec', 'nircam/cosmos', 'nircam/a2744'], counts);
    expect(s.nirspec).toEqual({ spectra: 163, objects: 101, programs: 31, fields: 14, bytes: 235_222_115 });
    expect(s.nircam?.fields.map((f) => f.field)).toEqual(['a2744', 'cosmos']);
    expect(s.nircam?.mosaics).toBe(10); // 8 mosaics + a pre-P6b-3 component counted by files
    expect(s.nircam?.bytes).toBe(32_718_586_600);
    expect(s.nircam?.area_arcmin2).toBe(148.4);
    expect(s.photometry).toBeNull();
  });
  it('leaves out absent components and tolerates missing sizes', () => {
    const s = releaseSummary(['nircam/a2744'], { spectra: 5 });
    expect(s.nirspec).toBeNull();
    expect(s.nircam).toEqual({ fields: [{ field: 'a2744', mosaics: null, bytes: null, area_arcmin2: null }], mosaics: null, bytes: null, area_arcmin2: null });
  });
  it('lists photometry catalogs', () => {
    const s = releaseSummary(['nirspec', 'photometry/cosmos/COSMOS v1.2'], { photometry: 7 });
    expect(s.photometry).toEqual({ catalogs: [{ field: 'cosmos', catalog: 'COSMOS v1.2' }], rows: 7 });
  });
});

describe('formatArea', () => {
  it('switches units like the NIRCam pages', () => {
    expect(formatArea(148.36)).toBe('148 arcmin²');
    expect(formatArea(1959.5)).toBe('0.54 deg²');
    expect(formatArea(null)).toBe('—');
  });
});
