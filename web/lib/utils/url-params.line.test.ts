import { describe, expect, it } from 'vitest';
import { DEFAULT_FILTERS, buildFilterParams, hasLineFilter } from '@/lib/actions/filter-params';
import { filtersToURLParams, parseFiltersFromURL } from '@/lib/utils/url-params';
import { normalizeFilters } from '@/lib/catalog-query/legacy';
import { LINE_FILTER_OPTIONS, isFilterableLine, lineLabel } from '@/lib/linelist';

describe('emission-line filter', () => {
  it('round-trips through the URL and only while a line is set', () => {
    const filters = { ...DEFAULT_FILTERS, line: 'CIII1908', line_snr_min: 3, line_include_stale: true };
    // the URL carries the tree (phase 3b): the pill is its exists node
    const params = filtersToURLParams(filters, 1, 50, 'object_id', 'asc', 'objects');
    const node = { exists: 'lines', where: [{ col: 'line', op: 'eq', v: 'CIII1908' }, { col: 'snr', op: 'gte', v: 3 }] };
    expect(JSON.parse(params.get('q')!)).toEqual(node);
    expect(params.has('line')).toBe(false);
    const back = parseFiltersFromURL(params);
    expect(back.conditions).toEqual([node]);
    const pills = normalizeFilters(back, 'objects');
    expect(pills.line).toBe('CIII1908');
    expect(pills.line_snr_min).toBe(3);
    expect(pills.line_snr_max).toBeNull();
    expect(pills.line_include_stale).toBe(true);
    // an older link's parameters read as the node, fresh fits only by default
    const legacy = parseFiltersFromURL(new URLSearchParams({ line: 'CIII1908', line_snr_min: '3' }));
    expect(legacy.conditions).toEqual([{ exists: 'lines', where: [...node.where, { exists: 'line_fits', where: [{ col: 'stale_redshift', op: 'eq', v: false }] }] }]);
    expect(normalizeFilters(legacy, 'objects').line_include_stale).toBe(false);
    // bounds without a line are nothing
    const orphan = filtersToURLParams({ ...DEFAULT_FILTERS, line_snr_min: 3 }, 1, 50, 'object_id', 'asc', 'objects');
    expect(orphan.has('q')).toBe(false);
    // an unknown or component name from a shared link is dropped, never echoed
    for (const bad of ['=HYPERLINK("x")', 'x,y', 'CIII1907', 'nope']) {
      expect(parseFiltersFromURL(new URLSearchParams({ line: bad })).conditions).toEqual([]);
      expect('p_line' in buildFilterParams({ ...DEFAULT_FILTERS, line: bad }, ['ember-uds'])).toBe(false);
    }
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
