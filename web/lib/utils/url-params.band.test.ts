import { describe, expect, it } from 'vitest';
import { DEFAULT_FILTERS, buildFilterParams, hasBandFilter } from '@/lib/actions/filter-params';
import { filtersToURLParams, parseFiltersFromURL } from '@/lib/utils/url-params';
import { normalizeFilters } from '@/lib/catalog-query/legacy';
import { OBJECTS_SORT_COLUMNS, SPECTRA_SORT_COLUMNS, isValidSortColumn } from '@/lib/actions/spectra-types';

describe('photometry band filter', () => {
  it('round-trips through the URL and only while a band is set', () => {
    const filters = {
      ...DEFAULT_FILTERS,
      band: 'f444w',
      band_mag_max: 27.5,
      band_snr_min: 5,
    };
    // the URL carries the tree (phase 3b): the pill is its exists node
    const params = filtersToURLParams(filters, 1, 50, 'object_id', 'asc', 'objects');
    const node = { exists: 'photometry', where: [{ col: 'band', op: 'eq', v: 'f444w' }, { col: 'mag', op: 'lte', v: 27.5 }, { col: 'snr', op: 'gte', v: 5 }] };
    expect(JSON.parse(params.get('q')!)).toEqual(node);
    expect(params.has('band')).toBe(false);

    // back from the URL it is a node of the tree, and the pill once normalized
    const back = parseFiltersFromURL(params);
    expect(back.conditions).toEqual([node]);
    const pills = normalizeFilters(back, 'objects');
    expect(pills.band).toBe('f444w');
    expect(pills.band_mag_max).toBe(27.5);
    expect(pills.band_mag_min).toBeNull();
    expect(pills.band_snr_min).toBe(5);
    expect(pills.band_snr_max).toBeNull();
    expect(pills.conditions).toEqual([]);

    // an older link's parameters read as the same node
    const legacy = parseFiltersFromURL(new URLSearchParams({ band: 'f444w', band_mag_max: '27.5', band_snr_min: '5' }));
    expect(legacy.conditions).toEqual([node]);

    // windows without a band are nothing
    const orphan = filtersToURLParams(
      { ...DEFAULT_FILTERS, band_mag_max: 27.5, band_snr_min: 5 },
      1, 50, 'object_id', 'asc', 'objects',
    );
    expect(orphan.has('q')).toBe(false);
  });

  it('sends the RPC parameters only while a band is set', () => {
    const none = buildFilterParams(DEFAULT_FILTERS, ['ember-uds']);
    expect('p_band' in none).toBe(false);
    expect(hasBandFilter(DEFAULT_FILTERS)).toBe(false);

    const some = buildFilterParams(
      { ...DEFAULT_FILTERS, band: 'f444w', band_mag_max: 27.5, band_snr_min: 5 },
      ['ember-uds'],
    );
    expect(some.p_band).toBe('f444w');
    expect(some.p_band_mag_min).toBeNull();
    expect(some.p_band_mag_max).toBe(27.5);
    expect(some.p_band_snr_min).toBe(5);
    expect(some.p_band_snr_max).toBeNull();
    expect(hasBandFilter({ band: 'f444w' })).toBe(true);
  });

  it('treats a blank or whitespace-only band as no filter', () => {
    for (const blank of ['', '   ']) {
      expect(parseFiltersFromURL(new URLSearchParams({ band: blank })).conditions).toEqual([]);
      expect('p_band' in buildFilterParams({ ...DEFAULT_FILTERS, band: blank }, ['ember-uds'])).toBe(false);
    }
  });

  it('passes an unrecognized band name straight through', () => {
    // Unlike a line, a band name has no fixed catalog to validate against —
    // it is whatever the field's photometry.toml calls it. An unknown name is
    // sent as-is and simply matches no photometry row.
    const params = buildFilterParams({ ...DEFAULT_FILTERS, band: 'not_a_band' }, ['ember-uds']);
    expect(params.p_band).toBe('not_a_band');
  });

  it('offers band_mag / band_snr as sort columns in both view modes', () => {
    for (const col of ['band_mag', 'band_snr'] as const) {
      expect(OBJECTS_SORT_COLUMNS).toContain(col);
      expect(SPECTRA_SORT_COLUMNS).toContain(col);
      expect(isValidSortColumn(col, 'objects')).toBe(true);
      expect(isValidSortColumn(col, 'spectra')).toBe(true);
    }
  });
});
