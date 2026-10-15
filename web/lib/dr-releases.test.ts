import { describe, expect, it } from 'vitest';
import { formatBytes, parseReleaseComponents, releaseCitation, releaseCount, releaseStateBadge } from './dr-releases';

describe('releaseStateBadge', () => {
  it('labels the lifecycle', () => {
    expect(releaseStateBadge('published')).toEqual({ label: 'Published', tone: 'published' });
    expect(releaseStateBadge('withdrawn').tone).toBe('withdrawn');
    expect(releaseStateBadge('failed').tone).toBe('failed');
    expect(releaseStateBadge('ready')).toEqual({ label: 'Ready · unpublished', tone: 'pending' });
    expect(releaseStateBadge('frozen').label).toBe('Frozen · unpublished');
  });
});

describe('parseReleaseComponents', () => {
  it('names each component kind', () => {
    expect(parseReleaseComponents(['nirspec', 'nircam/cosmos', 'photometry/cosmos/COSMOS v1.2'])).toEqual([
      { kind: 'nirspec', label: 'NIRSpec spectra', raw: 'nirspec' },
      { kind: 'nircam', label: 'NIRCam mosaics · cosmos', raw: 'nircam/cosmos' },
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
