import { describe, expect, it } from 'vitest';
import { DEFAULT_FILTERS, buildFilterParams, hasLineFilter } from '@/lib/actions/filter-params';
import { filtersToURLParams, parseFiltersFromURL } from '@/lib/utils/url-params';
import { LINE_FILTER_OPTIONS, isFilterableLine, lineLabel } from '@/lib/linelist';

describe('emission-line filter', () => {
  it('round-trips through the URL and only while a line is set', () => {
    const filters = { ...DEFAULT_FILTERS, line: 'CIII1908', line_snr_min: 3, line_include_stale: true };
    const params = filtersToURLParams(filters, 1, 50, 'object_id', 'asc', 'objects');
    expect(params.get('line')).toBe('CIII1908');
    expect(params.get('line_snr_min')).toBe('3');
    expect(params.get('line_stale')).toBe('true');
    const back = parseFiltersFromURL(params);
    expect(back.line).toBe('CIII1908');
    expect(back.line_snr_min).toBe(3);
    expect(back.line_snr_max).toBeNull();
    expect(back.line_include_stale).toBe(true);
    // bounds without a line are dropped from the URL
    const orphan = filtersToURLParams({ ...DEFAULT_FILTERS, line_snr_min: 3 }, 1, 50, 'object_id', 'asc', 'objects');
    expect(orphan.has('line')).toBe(false);
    expect(orphan.has('line_snr_min')).toBe(false);
  });

  it('sends the RPC parameters only while a line is set', () => {
    const none = buildFilterParams(DEFAULT_FILTERS, ['ember-uds']);
    expect('p_line' in none).toBe(false);
    expect(hasLineFilter(DEFAULT_FILTERS)).toBe(false);
    const some = buildFilterParams({ ...DEFAULT_FILTERS, line: 'OII3727', line_snr_min: 5 }, ['ember-uds']);
    expect(some.p_line).toBe('OII3727');
    expect(some.p_line_snr_min).toBe(5);
    expect(some.p_line_snr_max).toBeNull();
    expect(some.p_line_include_stale).toBe(false);
    expect(hasLineFilter({ line: 'OII3727' })).toBe(true);
  });

  it('offers doublet totals and stand-alone lines, never components', () => {
    const values = LINE_FILTER_OPTIONS.map((o) => o.value);
    expect(values).toContain('CIII1908');
    expect(values).toContain('Halpha');
    expect(values).not.toContain('CIII1907');
    expect(values).not.toContain('SII6716');
    expect(isFilterableLine('SII6725')).toBe(true);
    expect(isFilterableLine('SII6731')).toBe(false);
    expect(lineLabel('Halpha_broad')).toBe('Hα (broad)');
    expect(lineLabel('CIII1908')).toBe('CIII]λλ1907,1909');
    // sorted by rest wavelength
    const waves = LINE_FILTER_OPTIONS.map((o) => o.wave);
    expect([...waves].sort((a, b) => a - b)).toEqual(waves);
  });
});
