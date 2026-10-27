// The predicate tree's web side (Catalog Read Model phase 3b): the leaf
// catalogue mirrors the database whitelist, the pills' tree shapes are the
// ones the SQL guard proves at parity, and a `?q=` round-trips.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { CATALOGUE, CORRELATIONS, OPS_BY_TYPE, TREE_TABLES } from './catalogue';
import { describeNode, encodeConditions, parseConditions, rerootNodes, validateNode, type TreeNode } from './tree';
import { activeLineOf, distanceReferenceOf, filterNodes, legacyNodes, liftConditions, needsTreePath, normalizeFilters } from './legacy';
import { DEFAULT_FILTERS } from '@/lib/actions/filter-params';
import { filtersToURLParams, parseFiltersFromURL } from '@/lib/utils/url-params';

const functionsSql = readFileSync(fileURLToPath(new URL('../../../supabase/schemas/functions.sql', import.meta.url)), 'utf8');

function jsonBody(fn: string): Record<string, Record<string, string>> {
  const start = functionsSql.indexOf(`CREATE OR REPLACE FUNCTION public.${fn}()`);
  const open = functionsSql.indexOf('$j$', start) + 3;
  const close = functionsSql.indexOf('$j$', open);
  return JSON.parse(functionsSql.slice(open, close));
}

describe('the leaf catalogue mirrors the whitelist', () => {
  it('has the same tables, columns and types as catalog_query_whitelist()', () => {
    const whitelist = jsonBody('catalog_query_whitelist');
    expect(Object.keys(whitelist).sort()).toEqual([...TREE_TABLES].sort());
    for (const table of TREE_TABLES) {
      const ours = Object.fromEntries(Object.entries(CATALOGUE[table]).map(([c, d]) => [c, d.type]));
      expect(ours, table).toEqual(whitelist[table]);
    }
  });

  it('has the same correlations as catalog_query_correlations()', () => {
    const correlations = jsonBody('catalog_query_correlations');
    for (const table of TREE_TABLES) {
      expect([...CORRELATIONS[table]].sort(), table).toEqual(Object.keys(correlations[table] ?? {}).sort());
    }
  });

  it('offers every operator the compiler takes for a type, and no other', () => {
    const body = functionsSql.slice(functionsSql.indexOf('CREATE OR REPLACE FUNCTION public.catalog_query_node_sql('));
    const ops = (type: string) => {
      const section = body.slice(body.indexOf(`WHEN '${type}'`), body.indexOf('ELSE RAISE EXCEPTION', body.indexOf(`WHEN '${type}'`)));
      return [...section.matchAll(/WHEN '([a-z_]+)'\s+THEN/g)].map((m) => m[1]).filter((op) => op !== type);
    };
    for (const [type, compiled] of [['num', ops('num')], ['text', ops('text')], ['bool', ops('bool')], ['bits', ops('bits')]] as const) {
      const ours = OPS_BY_TYPE[type].filter((op) => op !== 'is_null' && op !== 'not_null');
      expect([...ours].sort(), type).toEqual([...new Set(compiled)].sort());
    }
  });
});

describe('validateNode', () => {
  it('accepts the documented shapes and cleans them', () => {
    expect(validateNode({ col: 'objects.redshift', op: 'gt', v: 4.9, junk: 1 }, 'objects')).toEqual({ col: 'redshift', op: 'gt', v: 4.9 });
    expect(validateNode({ exists: 'lines', where: [{ col: 'line', op: 'eq', v: 'CIII1908' }], not: false }, 'objects'))
      .toEqual({ exists: 'lines', where: [{ col: 'line', op: 'eq', v: 'CIII1908' }] });
    expect(validateNode({ cone: { ra: 150.1, dec: 2.2, radius: 0.1 } }, 'objects')).toEqual({ cone: { ra: 150.1, dec: 2.2, radius: 0.1 } });
    expect(validateNode({ or: [{ col: 'needs_review', op: 'eq', v: true }, { col: 'redshift', op: 'is_null', v: null }] }, 'objects'))
      .toEqual({ or: [{ col: 'needs_review', op: 'eq', v: true }, { col: 'redshift', op: 'is_null' }] });
  });

  it('refuses what the whitelist does not know', () => {
    expect(validateNode({ col: 'passwd', op: 'eq', v: 1 }, 'objects')).toBeNull();
    expect(validateNode({ col: 'redshift', op: 'ilike', v: 'x' }, 'objects')).toBeNull();
    expect(validateNode({ col: 'redshift', op: 'gt', v: 'high' }, 'objects')).toBeNull();
    expect(validateNode({ col: 'redshift', op: 'between', v: [null, null] }, 'objects')).toBeNull();
    expect(validateNode({ exists: 'photometry' }, 'lines')).toBeNull();
    expect(validateNode({ cone: { ra: 1, dec: 2, radius: 11 } }, 'objects')).toBeNull();
    expect(validateNode({ cone: { ra: 1, dec: 2, radius: 1 } }, 'spectra')).toBeNull();
    expect(validateNode({ and: [] }, 'objects')).toBeNull();
    expect(validateNode({ exists: 'lines', where: [{ col: 'redshift', op: 'gt', v: 1 }] }, 'objects')).toBeNull();
  });
});

describe('the pills as a tree', () => {
  it('writes the shapes the SQL guard proves at parity with the legacy RPCs', () => {
    expect(legacyNodes({ redshift_min: 4.5, redshift_max: 6.5, redshift_quality: [3, 4] }, 'objects')).toEqual([
      { col: 'redshift_quality', op: 'in', v: [3, 4] },
      { col: 'redshift', op: 'between', v: [4.5, 6.5] },
    ]);
    expect(legacyNodes({ line: 'CIII1908', line_snr_min: 5 }, 'objects')).toEqual([
      { exists: 'lines', where: [{ col: 'line', op: 'eq', v: 'CIII1908' }, { col: 'snr', op: 'gte', v: 5 }, { exists: 'line_fits', where: [{ col: 'stale_redshift', op: 'eq', v: false }] }] },
    ]);
    expect(legacyNodes({ line: 'CIII1908', line_include_stale: true }, 'objects')).toEqual([
      { exists: 'lines', where: [{ col: 'line', op: 'eq', v: 'CIII1908' }] },
    ]);
    expect(legacyNodes({ band: 'f444w', band_mag_max: 25 }, 'objects')).toEqual([
      { exists: 'photometry', where: [{ col: 'band', op: 'eq', v: 'f444w' }, { col: 'mag', op: 'lte', v: 25 }] },
    ]);
    expect(legacyNodes({ gratings: ['G395M'], gratings_mode: 'any' }, 'objects')).toEqual([
      { exists: 'spectra', where: [{ col: 'grating', op: 'in', v: ['G395M'] }] },
    ]);
    expect(legacyNodes({ gratings: ['G395M', 'PRISM'], gratings_mode: 'all' }, 'objects')).toEqual([
      { exists: 'spectra', where: [{ col: 'grating', op: 'eq', v: 'G395M' }] },
      { exists: 'spectra', where: [{ col: 'grating', op: 'eq', v: 'PRISM' }] },
    ]);
    expect(legacyNodes({ gratings: ['G395M'], gratings_mode: 'none' }, 'objects')).toEqual([
      { exists: 'spectra', where: [{ col: 'grating', op: 'in', v: ['G395M'] }], not: true },
    ]);
    expect(legacyNodes({ fields: ['zzz_cq_field'], inspected_only: true }, 'objects')).toEqual([
      { col: 'field', op: 'in', v: ['zzz_cq_field'] },
      { col: 'redshift_quality', op: 'gt', v: 0 },
    ]);
    expect(legacyNodes({ coordinate_search: { ra: 150.1, dec: 2.2, radius: 6, radius_unit: 'arcmin' } }, 'objects')).toEqual([
      { cone: { ra: 150.1, dec: 2.2, radius: 0.1 } },
    ]);
    expect(legacyNodes({ dq_flags: [1, 4], dq_flags_mode: 'none' }, 'objects')).toEqual([]);
  });

  it('moves to the spectra root with the spectra view', () => {
    expect(legacyNodes({ programs: ['p'], fields: ['f'], gratings: ['PRISM'], max_snr_min: 5, redshift_min: 4, dq_flags: [1, 4], dq_flags_mode: 'any' }, 'spectra')).toEqual([
      { col: 'program_slug', op: 'in', v: ['p'] },
      { exists: 'targets', where: [{ col: 'field', op: 'in', v: ['f'] }] },
      { exists: 'objects', where: [{ col: 'redshift', op: 'gte', v: 4 }] },
      { col: 'signal_to_noise', op: 'gte', v: 5 },
      { col: 'grating', op: 'in', v: ['PRISM'] },
      { col: 'dq_flags', op: 'anybits', v: 5 },
    ]);
  });

  it('is accepted by the validator for every pill, on both roots', () => {
    const all = {
      ...DEFAULT_FILTERS, programs: ['p'], fields: ['f'], observations: ['o'], redshift_quality: [3, 4], redshift_min: 1, redshift_max: 2,
      max_snr_min: 3, max_exposure_time_max: 5000, gratings: ['PRISM', 'G395M'], gratings_mode: 'all' as const, list_ids: [1, 2], list_ids_mode: 'none' as const,
      dq_flags: [1], inspected_only: true, needs_review: true, has_photometry: false,
      coordinate_search: { ra: 1, dec: 2, radius: 1, radius_unit: 'arcmin' as const }, line: 'OIII5007', line_snr_min: 3, band: 'f444w', band_snr_min: 5,
    };
    for (const root of ['objects', 'spectra'] as const) {
      for (const node of legacyNodes(all, root)) expect(validateNode(node, root), `${root}: ${JSON.stringify(node)}`).toEqual(node);
    }
  });
});

describe('free conditions', () => {
  // a line cut the Line pill cannot hold (it has a component clause), so it stays a condition
  const line: TreeNode = { exists: 'lines', where: [{ col: 'line', op: 'eq', v: 'OIII5007' }, { col: 'snr', op: 'gte', v: 3 }, { col: 'component', op: 'eq', v: 'narrow' }] };
  const either: TreeNode = { or: [line, { exists: 'photometry', where: [{ col: 'band', op: 'eq', v: 'f444w' }, { col: 'mag', op: 'lte', v: 24 }] }] };

  it('is the state: the URL carries the tree, older links read as nodes', () => {
    const filters = { ...DEFAULT_FILTERS, conditions: [{ col: 'redshift', op: 'gte', v: 5 } as TreeNode, either] };
    const params = filtersToURLParams(filters, 1, 50, 'object_id', 'asc', 'objects');
    expect(params.has('z_min')).toBe(false);
    expect(JSON.parse(params.get('q')!)).toEqual({ and: filters.conditions });
    // the columns follow the rows the tree is sure of: `either` is an OR, so neither of its rows is one
    expect(parseFiltersFromURL(params)).toEqual(filters);
    const one = filtersToURLParams({ ...DEFAULT_FILTERS, conditions: [either] }, 1, 50, 'object_id', 'asc', 'objects');
    expect(JSON.parse(one.get('q')!)).toEqual(either);
    expect(encodeConditions([])).toBeNull();
    // a pill a caller still carries in its legacy slot is folded into the tree
    const folded = filtersToURLParams({ ...DEFAULT_FILTERS, fields: ['cosmos'], conditions: [line] }, 1, 50, 'object_id', 'asc', 'objects');
    expect(JSON.parse(folded.get('q')!)).toEqual({ and: [{ col: 'field', op: 'in', v: ['cosmos'] }, line] });
    // an older link: its parameters lead, then its q
    const old = parseFiltersFromURL(new URLSearchParams({ programs: 'jades,ceers', z_min: '4', quality: '3,4', q: JSON.stringify(line) }));
    expect(old.conditions).toEqual([
      { exists: 'targets', where: [{ col: 'program_slug', op: 'in', v: ['jades', 'ceers'] }] },
      { col: 'redshift_quality', op: 'in', v: [3, 4] },
      { col: 'redshift', op: 'gte', v: 4 },
      line,
    ]);
    expect(old.programs).toEqual([]);
  });

  it('normalizes into the pills the legacy RPCs take, and keeps the rest', () => {
    const q = JSON.stringify({ v: 1, and: [
      { col: 'redshift', op: 'gte', v: 4.9 },
      { exists: 'lines', where: [{ col: 'line', op: 'eq', v: 'CIII1908' }, { col: 'snr', op: 'gte', v: 5 }] },
      line,
      { exists: 'spectra', where: [{ col: 'grating', op: 'in', v: ['G395M'] }] },
    ] });
    const f = normalizeFilters(parseFiltersFromURL(new URLSearchParams({ q })), 'objects');
    expect(f.redshift_min).toBe(4.9);
    expect(f.line).toBe('CIII1908');
    expect(f.line_snr_min).toBe(5);
    expect(f.line_include_stale).toBe(true);
    expect(f.gratings).toEqual(['G395M']);
    expect(f.conditions).toEqual([line]);
    // and what lifted comes back as the same tree (in the pills' order)
    expect(filterNodes(f, 'objects')).toEqual([
      { col: 'redshift', op: 'gte', v: 4.9 },
      { exists: 'spectra', where: [{ col: 'grating', op: 'in', v: ['G395M'] }] },
      { exists: 'lines', where: [{ col: 'line', op: 'eq', v: 'CIII1908' }, { col: 'snr', op: 'gte', v: 5 }] },
      line,
    ]);
    // a node the pill holds exactly is the pill: `> 4.9` is not `≥ 4.9`, so it stays a condition
    const strict = normalizeFilters(parseFiltersFromURL(new URLSearchParams({ q: '{"col":"redshift","op":"gt","v":4.9}' })), 'objects');
    expect(strict.redshift_min).toBeNull();
    expect(strict.conditions).toEqual([{ col: 'redshift', op: 'gt', v: 4.9 }]);
    // a second node of the same pill shape stays a condition too (ANDed)
    const twice = normalizeFilters({ ...DEFAULT_FILTERS, conditions: [{ col: 'field', op: 'in', v: ['a'] }, { col: 'field', op: 'in', v: ['b'] }] }, 'objects');
    expect(twice.fields).toEqual(['a']);
    expect(twice.conditions).toEqual([{ col: 'field', op: 'in', v: ['b'] }]);
    // a boolean false fills its slot: a contradiction stays a contradiction
    const contra = normalizeFilters({ ...DEFAULT_FILTERS, conditions: [{ col: 'has_photometry', op: 'eq', v: false }, { col: 'has_photometry', op: 'eq', v: true }] }, 'objects');
    expect(contra.has_photometry).toBe(false);
    expect(contra.conditions).toEqual([{ col: 'has_photometry', op: 'eq', v: true }]);
  });

  it('drops an unreadable q whole', () => {
    expect(parseFiltersFromURL(new URLSearchParams({ q: '{"col":"passwd","op":"eq","v":1}', z_min: '2' })).conditions).toEqual([{ col: 'redshift', op: 'gte', v: 2 }]);
    expect(parseFiltersFromURL(new URLSearchParams({ q: 'not json' })).conditions).toEqual([]);
    expect(parseFiltersFromURL(new URLSearchParams({ q: '{"and":[{"col":"redshift","op":"gt","v":1},{"col":"nope","op":"gt","v":1}]}' })).conditions).toEqual([]);
    expect(parseConditions('{"v":2,"and":[{"col":"redshift","op":"gt","v":1}]}', 'objects')).toBeNull();
  });

  it('leaves a spectra-view grating exclusion in the tree (the spectra RPCs ignore the mode)', () => {
    const f = normalizeFilters({ ...DEFAULT_FILTERS, conditions: [{ col: 'grating', op: 'nin', v: ['PRISM'] }, { col: 'grating', op: 'in', v: ['G395M'] }] }, 'spectra');
    expect(f.gratings).toEqual(['G395M']);
    expect(f.conditions).toEqual([{ col: 'grating', op: 'nin', v: ['PRISM'] }]);
    expect(needsTreePath(f)).toBe(true);
  });

  it('read against the view root from the URL', () => {
    const f = parseFiltersFromURL(new URLSearchParams({ view: 'spectra', q: '{"col":"signal_to_noise","op":"gte","v":5}' }));
    expect(normalizeFilters(f, 'spectra').max_snr_min).toBe(5);
    // a caller that queries objects (prev / next, the inspection queue) reads a spectra link as a card
    const asObjects = parseFiltersFromURL(new URLSearchParams({ view: 'spectra', q: '{"col":"signal_to_noise","op":"gte","v":5}' }), 'objects');
    expect(asObjects.conditions).toEqual([{ exists: 'spectra', where: [{ col: 'signal_to_noise', op: 'gte', v: 5 }] }]);
    expect(normalizeFilters(asObjects, 'objects').conditions).toEqual(asObjects.conditions);
    expect(parseFiltersFromURL(new URLSearchParams({ q: '{"col":"signal_to_noise","op":"gte","v":5}' })).conditions).toEqual([]);
  });

  it('re-roots when the view switches', () => {
    const fromObjects = rerootNodes([{ col: 'redshift', op: 'gt', v: 5 }, line, { cone: { ra: 1, dec: 2, radius: 0.1 } }], 'objects', 'spectra');
    expect(fromObjects).toEqual([
      { exists: 'objects', where: [{ col: 'redshift', op: 'gt', v: 5 }] },
      line,
      { exists: 'objects', where: [{ cone: { ra: 1, dec: 2, radius: 0.1 } }] },
    ]);
    for (const n of fromObjects) expect(validateNode(n, 'spectra')).toEqual(n);
    // and back: the exists over the new root flattens
    expect(rerootNodes(fromObjects, 'spectra', 'objects')).toEqual([{ col: 'redshift', op: 'gt', v: 5 }, line, { cone: { ra: 1, dec: 2, radius: 0.1 } }]);
    const lifted = liftConditions(rerootNodes(fromObjects, 'spectra', 'objects'), DEFAULT_FILTERS, 'objects');
    expect(lifted.coordinate_search).toEqual({ ra: 1, dec: 2, radius: 0.1, radius_unit: 'degrees' });
    expect(lifted.conditions).toEqual([{ col: 'redshift', op: 'gt', v: 5 }, line]);
  });

  it('show the column a row asks for, never one a filter implies', () => {
    expect(activeLineOf({ ...DEFAULT_FILTERS, conditions: [either] })).toBeNull();
    expect(activeLineOf({ ...DEFAULT_FILTERS, show_line: 'Halpha' })).toBe('Halpha');
    expect(activeLineOf({ ...DEFAULT_FILTERS, show_line: 'CIII1907' })).toBeNull();
    expect(distanceReferenceOf({ ...DEFAULT_FILTERS, conditions: [{ cone: { ra: 150, dec: 2, radius: 0.1 } }] }, 'objects')).toEqual({ ra: 150, dec: 2 });
    expect(distanceReferenceOf(DEFAULT_FILTERS, 'objects')).toBeNull();
    // the columns follow the rows: the first line and the first band filtered on, from any link
    const rows = { ...DEFAULT_FILTERS, conditions: [...legacyNodes({ band: 'f444w', band_mag_max: 27 }, 'objects'), line, either] };
    const params = filtersToURLParams(rows, 1, 50, 'object_id', 'asc', 'objects');
    expect(params.has('col_line')).toBe(false);
    expect(parseFiltersFromURL(params).show_band).toBe('f444w');
    // the first line row the tree is sure of; the one inside `either` (an OR) is not a column
    expect(parseFiltersFromURL(params).show_line).toBe('OIII5007');
    expect(parseFiltersFromURL(new URLSearchParams({ q: JSON.stringify(either) })).show_line).toBeNull();
    expect(parseFiltersFromURL(new URLSearchParams({ line: 'Halpha' })).show_line).toBe('Halpha');
    expect(parseFiltersFromURL(new URLSearchParams({ z_min: '4' })).show_line).toBeNull();
    // a column the pill cannot carry sends the state down the tree path
    const pill = normalizeFilters({ ...DEFAULT_FILTERS, conditions: legacyNodes({ line: 'Halpha', line_snr_min: 3 }, 'objects') }, 'objects');
    expect(needsTreePath(pill)).toBe(false);
    expect(needsTreePath({ ...pill, show_line: 'Halpha' })).toBe(false);
    expect(needsTreePath({ ...pill, show_line: 'OIII5007' })).toBe(true);
    expect(needsTreePath({ ...pill, show_band: 'f444w' })).toBe(true);
  });

  it('describe themselves', () => {
    expect(describeNode(either, 'objects')).toBe('any of: has a line where Line = OIII5007 and S/N ≥ 3 and Component = narrow; has photometry where Band = f444w and Magnitude ≤ 24 AB');
    expect(describeNode({ exists: 'spectra', not: true, where: [{ col: 'grating', op: 'eq', v: 'G395M' }] }, 'objects')).toBe('has no spectrum where Grating = G395M');
    expect(describeNode({ cone: { ra: 150.1, dec: 2.2, radius: 1 / 60 } }, 'objects')).toBe('within 1′ of 150.10000, 2.20000');
  });
});
