// The two-noun vocabulary (phase 3b): the cards write the pills' own node
// shapes, read every node back (or show it as advanced), edit by path, and
// keep their meaning when the view switches root.
import { describe, expect, it } from 'vitest';
import { legacyNodes, normalizeFilters } from './legacy';
import { validateNode, type TreeNode } from './tree';
import {
  addToCard, appendAt, cardContentsPath, compileChoice, compileMeasurement, compileProperty, describeItem, findProperty, lineWantsFresh, measurementKeys, nodeAt,
  propertyDef, readChoiceState, readConditions, removePaths, replaceAt, rerootConditions, setNegated, tableAtPath, tidyConditions, treeReadsColumn,
  type Item, type MeasurementItem, type PropertyItem, type SpectrumItem,
} from './vocabulary';
import { DEFAULT_FILTERS } from '@/lib/actions/filter-params';

const def = (scope: 'object' | 'spectrum' | 'line' | 'band', key: string) => {
  const d = propertyDef(scope, key);
  if (!d) throw new Error(`no ${scope}.${key}`);
  return d;
};

// the design doc's example (Figures 6 to 8), at the objects root
const figure6: TreeNode[] = [
  { col: 'field', op: 'in', v: ['goods-n'] },
  { exists: 'targets', where: [{ col: 'program_slug', op: 'in', v: ['jades'] }] },
  { col: 'redshift_quality', op: 'in', v: [3, 4] },
  { or: [{ col: 'redshift', op: 'between', v: [5, 8] }, { col: 'photo_z', op: 'between', v: [5, 8] }] },
  { exists: 'photometry', where: [{ col: 'band', op: 'eq', v: 'f444w' }, { col: 'mag', op: 'lte', v: 27 }, { col: 'snr', op: 'gte', v: 5 }] },
  { exists: 'spectra', where: [
    { col: 'grating', op: 'in', v: ['G395M'] },
    { col: 'signal_to_noise', op: 'gte', v: 3 },
    { exists: 'lines', where: [{ col: 'line', op: 'eq', v: 'Halpha' }, { col: 'snr', op: 'gte', v: 3 }] },
    { exists: 'lines', where: [{ col: 'line', op: 'eq', v: 'OIII5007' }, { col: 'snr', op: 'gte', v: 3 }] },
  ] },
  { exists: 'spectra', where: [{ col: 'grating', op: 'in', v: ['PRISM'] }], not: true },
  { exists: 'line_fits', where: [{ col: 'chi2', op: 'lt', v: 2 }] },
];

describe('compiling', () => {
  it('writes the legacy pills\' own shapes for the primary properties', () => {
    const o = (key: string, value: Parameters<typeof compileProperty>[1]) => compileProperty(def('object', key), value, 'objects');
    expect(o('program', { kind: 'choice', values: ['jades'] })).toEqual(legacyNodes({ programs: ['jades'] }, 'objects')[0]);
    expect(o('field', { kind: 'choice', values: ['cosmos'] })).toEqual(legacyNodes({ fields: ['cosmos'] }, 'objects')[0]);
    expect(o('observation', { kind: 'choice', values: ['o1'] })).toEqual(legacyNodes({ observations: ['o1'] }, 'objects')[0]);
    expect(o('redshift', { kind: 'range', min: 4.5, max: 6.5 })).toEqual(legacyNodes({ redshift_min: 4.5, redshift_max: 6.5 }, 'objects')[0]);
    expect(o('redshift', { kind: 'range', min: 4.5, max: null })).toEqual(legacyNodes({ redshift_min: 4.5 }, 'objects')[0]);
    expect(o('quality', { kind: 'choice', values: [3, 4] })).toEqual(legacyNodes({ redshift_quality: [3, 4] }, 'objects')[0]);
    expect(o('tag', { kind: 'choice', values: [1, 2], not: true })).toEqual(legacyNodes({ list_ids: [1, 2], list_ids_mode: 'none' }, 'objects')[0]);
    // coverage: the grating pill's three shapes come from one chip's state
    const grating = def('object', 'grating');
    expect(compileChoice(grating, { include: ['G395M'], mode: 'any', exclude: [] }, 'objects')).toEqual(legacyNodes({ gratings: ['G395M'], gratings_mode: 'any' }, 'objects'));
    expect(compileChoice(grating, { include: ['G395M', 'PRISM'], mode: 'all', exclude: [] }, 'objects')).toEqual(legacyNodes({ gratings: ['G395M', 'PRISM'], gratings_mode: 'all' }, 'objects'));
    expect(compileChoice(grating, { include: [], mode: 'any', exclude: ['G395M'] }, 'objects')).toEqual(legacyNodes({ gratings: ['G395M'], gratings_mode: 'none' }, 'objects'));
    // tags: A and B but not C, in one chip
    const tags = compileChoice(def('object', 'tag'), { include: [1, 2], mode: 'all', exclude: [3] }, 'objects');
    expect(tags).toEqual([
      { exists: 'object_list_members', where: [{ col: 'list_id', op: 'eq', v: 1 }] },
      { exists: 'object_list_members', where: [{ col: 'list_id', op: 'eq', v: 2 }] },
      { exists: 'object_list_members', where: [{ col: 'list_id', op: 'in', v: [3] }], not: true },
    ]);
    const tagItems = readConditions(tags, 'objects').items.filter((it): it is PropertyItem => it.kind === 'property' && it.def.key === 'tag');
    expect(readChoiceState(tagItems)).toEqual({ include: [1, 2], mode: 'all', exclude: [3] });
    // flags: any of two, none of a third
    const dq = def('spectrum', 'dq_flags');
    const flags = compileChoice(dq, { include: [1, 4], mode: 'any', exclude: [16] }, 'spectra');
    expect(flags).toEqual([{ col: 'dq_flags', op: 'anybits', v: 5 }, { col: 'dq_flags', op: 'nobits', v: 16 }]);
    expect(readChoiceState(readConditions(flags, 'spectra').items as PropertyItem[])).toEqual({ include: [1, 4], mode: 'any', exclude: [16] });
    expect(o('position', { kind: 'position', ra: 150.1, dec: 2.2, radius: 0.1 })).toEqual(legacyNodes({ coordinate_search: { ra: 150.1, dec: 2.2, radius: 6, radius_unit: 'arcmin' } }, 'objects')[0]);
    expect(o('inspected', { kind: 'bool', value: true })).toEqual(legacyNodes({ inspected_only: true }, 'objects')[0]);
    expect(o('needs_review', { kind: 'bool', value: true })).toEqual(legacyNodes({ needs_review: true }, 'objects')[0]);
    // the spectrum's, at the spectra root and inside a card alike
    const s = (key: string, value: Parameters<typeof compileProperty>[1]) => compileProperty(def('spectrum', key), value, 'spectra');
    expect(s('grating', { kind: 'choice', values: ['PRISM'] })).toEqual(legacyNodes({ gratings: ['PRISM'] }, 'spectra')[0]);
    expect(s('grating', { kind: 'choice', values: ['PRISM'], not: true })).toEqual(legacyNodes({ gratings: ['PRISM'], gratings_mode: 'none' }, 'spectra')[0]);
    expect(s('snr', { kind: 'range', min: 5, max: null })).toEqual(legacyNodes({ max_snr_min: 5 }, 'spectra')[0]);
    expect(s('dq_flags', { kind: 'flags', mode: 'none', mask: 5 })).toEqual(legacyNodes({ dq_flags: [1, 4], dq_flags_mode: 'none' }, 'spectra')[0]);
    expect(s('fresh', { kind: 'bool', value: true })).toEqual({ exists: 'line_fits', where: [{ col: 'stale_redshift', op: 'eq', v: false }] });
    // the object's, at the spectra root: the legacy shapes where one exists, else through its object
    const so = (key: string, value: Parameters<typeof compileProperty>[1]) => compileProperty(def('object', key), value, 'spectra');
    expect(so('field', { kind: 'choice', values: ['f'] })).toEqual(legacyNodes({ fields: ['f'] }, 'spectra')[0]);
    expect(so('redshift', { kind: 'range', min: 4, max: null })).toEqual(legacyNodes({ redshift_min: 4 }, 'spectra')[0]);
    expect(so('position', { kind: 'position', ra: 1, dec: 2, radius: 0.1 })).toEqual({ exists: 'targets', where: [{ cone: { ra: 1, dec: 2, radius: 0.1 } }] });
    // "observed in" goes through the object at the spectra root: a spectrum has one target
    expect(so('program', { kind: 'choice', values: ['p'] })).toEqual({ exists: 'objects', where: [{ exists: 'targets', where: [{ col: 'program_slug', op: 'in', v: ['p'] }] }] });
    expect(so('needs_review', { kind: 'bool', value: true })).toEqual({ exists: 'objects', where: [{ col: 'needs_review', op: 'eq', v: true }] });
    // nothing selects nothing
    expect(o('program', { kind: 'choice', values: [] })).toBeNull();
    expect(o('redshift', { kind: 'range', min: null, max: null })).toBeNull();
    expect(s('dq_flags', { kind: 'flags', mode: 'any', mask: 0 })).toBeNull();
  });

  it('writes measurements as the Line and Photometry pills did', () => {
    expect(compileMeasurement('line', 'CIII1908', [{ def: def('line', 'snr'), value: { kind: 'range', min: 5, max: null } }, { def: def('line', 'fresh'), value: { kind: 'bool', value: true } }]))
      .toEqual(legacyNodes({ line: 'CIII1908', line_snr_min: 5 }, 'objects')[0]);
    expect(compileMeasurement('band', 'f444w', [{ def: def('band', 'mag'), value: { kind: 'range', min: null, max: 25 } }]))
      .toEqual(legacyNodes({ band: 'f444w', band_mag_max: 25 }, 'objects')[0]);
    expect(compileMeasurement('line', 'Halpha', [], true)).toEqual({ exists: 'lines', where: [{ col: 'line', op: 'eq', v: 'Halpha' }], not: true });
  });

  it('is accepted by the validator and lifts into the pills', () => {
    const nodes = [
      compileProperty(def('object', 'program'), { kind: 'choice', values: ['jades'] }, 'objects')!,
      compileProperty(def('object', 'redshift'), { kind: 'range', min: 5, max: 8 }, 'objects')!,
      compileMeasurement('line', 'Halpha', [{ def: def('line', 'snr'), value: { kind: 'range', min: 3, max: null } }]),
      compileMeasurement('band', 'f444w', [{ def: def('band', 'mag'), value: { kind: 'range', min: null, max: 27 } }]),
    ];
    for (const n of nodes) expect(validateNode(n, 'objects')).toEqual(n);
    const f = normalizeFilters({ ...DEFAULT_FILTERS, conditions: nodes }, 'objects');
    expect(f.programs).toEqual(['jades']);
    expect(f.redshift_min).toBe(5);
    expect(f.line).toBe('Halpha');
    expect(f.band).toBe('f444w');
    expect(f.conditions).toEqual([]);
  });
});

describe('reading', () => {
  it('reads the design doc\'s example as its cards', () => {
    const { items } = readConditions(figure6, 'objects');
    expect(items.map((it) => it.kind)).toEqual(['property', 'property', 'property', 'anyOf', 'measurement', 'spectrum', 'property', 'advanced']);
    expect(items.every((it) => it.noun === 'object')).toBe(true);
    expect(findProperty(items, 'object', 'program')?.value).toEqual({ kind: 'choice', values: ['jades'] });
    expect(findProperty(items, 'object', 'field')?.path).toEqual([0]);
    const any = items[3];
    expect(any.kind === 'anyOf' && any.arms.map((a) => a.map((it) => it.kind === 'property' && it.def.key))).toEqual([['redshift'], ['photo_z']]);
    const band = items[4] as MeasurementItem;
    expect(band.key).toBe('f444w');
    expect(band.clauses.map((c) => c.kind === 'property' && [c.def.key, c.value])).toEqual([['mag', { kind: 'range', min: null, max: 27 }], ['snr', { kind: 'range', min: 5, max: null }]]);
    expect(band.clauses[1].path).toEqual([4, 2]);
    const card = items[5] as SpectrumItem;
    expect(card.not).toBe(false);
    expect(card.items.map((it) => (it.kind === 'property' ? it.def.key : it.kind === 'measurement' ? it.key : it.kind))).toEqual(['grating', 'snr', 'Halpha', 'OIII5007']);
    expect(card.items.every((it) => it.noun === 'spectrum')).toBe(true);
    expect((card.items[3] as MeasurementItem).clauses[0].path).toEqual([5, 3, 1]);
    // "no PRISM spectrum" is the Grating chip's exclusion, not a card
    const none = items[6] as PropertyItem;
    expect(none.def.key).toBe('grating');
    expect(none.value).toEqual({ kind: 'choice', values: ['PRISM'], not: true });
    // the fit's χ² is not vocabulary: advanced
    expect(items[7].kind).toBe('advanced');
  });

  it('reads a bare line or fit property on the object as the object\'s (any spectrum)', () => {
    const nodes: TreeNode[] = [
      { exists: 'lines', where: [{ col: 'line', op: 'eq', v: 'Halpha' }, { col: 'snr', op: 'gte', v: 3 }, { exists: 'line_fits', where: [{ col: 'stale_redshift', op: 'eq', v: false }] }], not: true },
      { exists: 'line_fits', where: [{ col: 'n_detected', op: 'gte', v: 2 }] },
      { exists: 'spectra', where: [{ col: 'grating', op: 'eq', v: 'G395M' }] },
    ];
    const { items } = readConditions(nodes, 'objects');
    const row = items[0] as MeasurementItem;
    expect(row.kind).toBe('measurement');
    expect(row.noun).toBe('object');
    expect(row.not).toBe(true);
    expect(row.path).toEqual([0]);
    expect(row.clauses.map((c) => c.kind === 'property' && [c.def.key, c.value])).toEqual([['snr', { kind: 'range', min: 3, max: null }], ['fresh', { kind: 'bool', value: true }]]);
    const fit = items[1] as PropertyItem;
    expect(fit.kind === 'property' && fit.def.key).toBe('n_detected');
    // the legacy "all" grating shape is a Grating chip value, not a card
    const g = items[2] as PropertyItem;
    expect(g.kind === 'property' && g.def.key).toBe('grating');
  });

  it('separates the spectrum\'s own from its object\'s at the spectra root', () => {
    const nodes: TreeNode[] = [
      { col: 'grating', op: 'in', v: ['G395M'] },
      { exists: 'lines', where: [{ col: 'line', op: 'eq', v: 'Halpha' }, { col: 'snr', op: 'gte', v: 3 }] },
      { exists: 'objects', where: [{ col: 'redshift', op: 'gte', v: 5 }, { exists: 'spectra', where: [{ col: 'grating', op: 'in', v: ['PRISM'] }], not: true }] },
      { exists: 'targets', where: [{ col: 'field', op: 'in', v: ['goods-n'] }] },
      { exists: 'photometry', where: [{ col: 'band', op: 'eq', v: 'f444w' }, { col: 'mag', op: 'lte', v: 27 }] },
      { col: 'program_slug', op: 'in', v: ['jades'] },
      { exists: 'object_list_members', where: [{ col: 'list_id', op: 'in', v: [7] }] },
    ];
    const { items } = readConditions(nodes, 'spectra');
    const spectrum = items.filter((it) => it.noun === 'spectrum');
    const object = items.filter((it) => it.noun === 'object');
    expect(spectrum.map((it) => (it.kind === 'property' ? it.def.key : it.kind === 'measurement' ? it.key : it.kind))).toEqual(['grating', 'Halpha', 'program']);
    expect(object.map((it) => (it.kind === 'property' ? it.def.key : it.kind === 'measurement' ? it.key : it.kind))).toEqual(['redshift', 'grating', 'field', 'f444w', 'tag']);
    expect(findProperty(object, 'object', 'redshift')?.path).toEqual([2, 0]);
    // "no PRISM spectrum" of its object is the object's Grating exclusion
    const none = object[1] as PropertyItem;
    expect(none.value).toEqual({ kind: 'choice', values: ['PRISM'], not: true });
    expect(none.path).toEqual([2, 1]);
    // inside a card the object's properties are not vocabulary
    const inCard = readConditions([{ exists: 'spectra', where: [{ exists: 'objects', where: [{ col: 'redshift', op: 'gte', v: 5 }] }] }], 'objects');
    expect((inCard.items[0] as SpectrumItem).items[0].kind).toBe('advanced');
  });

  it('shows what it cannot edit as advanced, never as nothing', () => {
    const odd: TreeNode[] = [
      { col: 'redshift', op: 'gt', v: 5 },
      { and: [{ col: 'field', op: 'in', v: ['a'] }, { exists: 'catalog_matches', where: [{ col: 'catalog_name', op: 'eq', v: 'x' }] }] },
      { exists: 'spectra', where: [{ col: 'cfpipe_version', op: 'ilike', v: '%1.%' }], not: true },
      { exists: 'photometry', where: [{ col: 'mag', op: 'lte', v: 25 }] },
    ];
    const { items } = readConditions(odd, 'objects');
    expect(items.map((it) => it.kind)).toEqual(['advanced', 'property', 'advanced', 'spectrum', 'advanced']);
    expect(items[1].path).toEqual([1, 0]);
    expect(items[2].path).toEqual([1, 1]);
    const card = items[3] as SpectrumItem;
    expect(card.items[0].kind).toBe('advanced');
    expect(card.items[0].path).toEqual([2, 0]);
  });

  it('round-trips every property through its node', () => {
    const cases: [Parameters<typeof def>[0], string, Parameters<typeof compileProperty>[1], 'objects' | 'spectra'][] = [
      ['object', 'program', { kind: 'choice', values: ['a', 'b'] }, 'objects'],
      ['object', 'tag', { kind: 'choice', values: [1], not: true }, 'objects'],
      ['object', 'redshift', { kind: 'range', min: null, max: null, unknown: true }, 'objects'],
      ['object', 'photo_z', { kind: 'range', min: 1, max: 2 }, 'objects'],
      ['object', 'inspected', { kind: 'bool', value: false }, 'objects'],
      ['object', 'has_photometry', { kind: 'bool', value: false }, 'objects'],
      ['object', 'position', { kind: 'position', ra: 1, dec: -2, radius: 0.05 }, 'objects'],
      ['object', 'grating', { kind: 'choice', values: ['G395M'], not: true }, 'objects'],
      ['object', 'grating', { kind: 'choice', values: ['G395M'] }, 'spectra'],
      ['object', 'max_snr', { kind: 'range', min: 5, max: null }, 'objects'],
      ['object', 'position', { kind: 'position', ra: 1, dec: -2, radius: 0.05 }, 'spectra'],
      ['object', 'field', { kind: 'choice', values: ['f'] }, 'spectra'],
      ['object', 'quality', { kind: 'choice', values: [4] }, 'spectra'],
      ['spectrum', 'grating', { kind: 'choice', values: ['PRISM'], not: true }, 'spectra'],
      ['spectrum', 'dq_flags', { kind: 'flags', mode: 'all', mask: 3 }, 'spectra'],
      ['spectrum', 'fresh', { kind: 'bool', value: false }, 'spectra'],
      ['spectrum', 'n_detected', { kind: 'range', min: 2, max: null }, 'spectra'],
      ['spectrum', 'exptime', { kind: 'range', min: 1000, max: 20000 }, 'spectra'],
    ];
    for (const [scope, key, value, table] of cases) {
      const node = compileProperty(def(scope, key), value, table)!;
      expect(validateNode(node, table), `${scope}.${key} validates`).toEqual(node);
      const { items } = readConditions([node], table);
      expect(items, `${scope}.${key} on ${table}`).toHaveLength(1);
      const it = items[0];
      expect(it.kind === 'property' && it.def.key === key && it.def.scope === scope, `${scope}.${key} reads back`).toBe(true);
      expect(it.kind === 'property' && it.value).toEqual(value);
    }
  });
});

describe('editing by path', () => {
  it('replaces, removes, appends and promotes', () => {
    let tree = figure6;
    // a clause of a row inside a card
    tree = replaceAt(tree, [5, 2, 1], { col: 'snr', op: 'gte', v: 5 });
    expect(nodeAt(tree, [5, 2, 1])).toEqual({ col: 'snr', op: 'gte', v: 5 });
    // removing the last arm of an OR removes the OR
    tree = replaceAt(tree, [3, 1], null);
    expect(nodeAt(tree, [3])).toEqual({ or: [{ col: 'redshift', op: 'between', v: [5, 8] }] });
    tree = replaceAt(tree, [3, 0], null);
    expect(tree).toHaveLength(figure6.length - 1);
    // a card emptied stays a card
    const prism = tree.length - 2;
    tree = replaceAt(tree, [prism, 0], null);
    expect(nodeAt(tree, [prism])).toEqual({ exists: 'spectra', not: true });
    // append at the top, inside a card, inside an OR arm
    tree = appendAt(tree, [], { col: 'needs_review', op: 'eq', v: true });
    expect(tree[tree.length - 1]).toEqual({ col: 'needs_review', op: 'eq', v: true });
    tree = appendAt(tree, [prism], { col: 'grating', op: 'in', v: ['PRISM'] });
    expect(nodeAt(tree, [prism])).toEqual({ exists: 'spectra', where: [{ col: 'grating', op: 'in', v: ['PRISM'] }], not: true });
    const withOr = appendAt([{ or: [{ col: 'redshift', op: 'gte', v: 5 }, { col: 'photo_z', op: 'gte', v: 5 }] }], [0, 1], { col: 'has_photometry', op: 'eq', v: true });
    expect(withOr[0]).toEqual({ or: [{ col: 'redshift', op: 'gte', v: 5 }, { and: [{ col: 'photo_z', op: 'gte', v: 5 }, { col: 'has_photometry', op: 'eq', v: true }] }] });
    const bare: TreeNode[] = [{ exists: 'lines', where: [{ col: 'line', op: 'eq', v: 'Halpha' }], not: true }];
    expect(setNegated(bare, [0], false)).toEqual([{ exists: 'lines', where: [{ col: 'line', op: 'eq', v: 'Halpha' }] }]);
    // several nodes removed at once, from the back so the paths stay valid
    const cardWhere = (figure6[5] as { where: TreeNode[] }).where;
    expect(removePaths(figure6, [[1], [5, 2], [6]])).toEqual([figure6[0], figure6[2], figure6[3], figure6[4],
      { exists: 'spectra', where: [cardWhere[0], cardWhere[1], cardWhere[3]] }, figure6[7]]);
  });

  it('keeps a card with one grating a card, apart from the Grating chip', () => {
    // a new card, then its first chip: the contents get their own group
    let nodes: TreeNode[] = [{ exists: 'spectra' }];
    expect(readConditions(nodes, 'objects').items[0].kind).toBe('spectrum');
    nodes = addToCard(nodes, [0], { col: 'grating', op: 'in', v: ['G395M'] });
    expect(nodes).toEqual([{ exists: 'spectra', where: [{ and: [{ col: 'grating', op: 'in', v: ['G395M'] }] }] }]);
    for (const n of nodes) expect(validateNode(n, 'objects')).toEqual(n);
    const card = readConditions(nodes, 'objects').items[0] as SpectrumItem;
    expect(card.kind).toBe('spectrum');
    expect(card.items.map((it) => it.kind === 'property' && [it.def.scope, it.def.key, it.path])).toEqual([['spectrum', 'grating', [0, 0, 0]]]);
    expect(cardContentsPath(nodes, [0])).toEqual([0, 0]);
    expect(tableAtPath(nodes, 'objects', [0, 0, 0])).toBe('spectra');
    // the bare shape is the chip
    expect(readConditions([{ exists: 'spectra', where: [{ col: 'grating', op: 'in', v: ['G395M'] }] }], 'objects').items[0].kind).toBe('property');
    // removing the chip leaves an empty card
    expect(replaceAt(nodes, [0, 0, 0], null)).toEqual([{ exists: 'spectra' }]);
    // and the card survives the view switch both ways
    const atSpectra = rerootConditions(nodes, 'objects', 'spectra');
    expect(atSpectra).toEqual([{ and: [{ col: 'grating', op: 'in', v: ['G395M'] }] }]);
    expect(readConditions(rerootConditions(atSpectra, 'spectra', 'objects'), 'objects').items[0].kind).toBe('spectrum');
  });

  it('knows the table a node sits in, so a replacement is written against it', () => {
    // an object's redshift at the spectra root lives inside `exists objects`
    const z = def('object', 'redshift');
    const atSpectra = [compileProperty(z, { kind: 'range', min: 3, max: null }, 'spectra')!];
    const item = readConditions(atSpectra, 'spectra').items[0];
    expect(item.path).toEqual([0, 0]);
    expect(tableAtPath(atSpectra, 'spectra', item.path)).toBe('objects');
    const edited = replaceAt(atSpectra, item.path, compileProperty(z, { kind: 'range', min: 4, max: null }, tableAtPath(atSpectra, 'spectra', item.path)));
    expect(edited).toEqual([{ exists: 'objects', where: [{ col: 'redshift', op: 'gte', v: 4 }] }]);
    expect(validateNode(edited[0], 'spectra')).toEqual(edited[0]);
    // inside a card, and inside an OR arm of a card
    expect(tableAtPath(figure6, 'objects', [5, 2])).toBe('spectra');
    expect(tableAtPath([{ exists: 'spectra', where: [{ or: [{ col: 'grating', op: 'eq', v: 'PRISM' }] }] }], 'objects', [0, 0, 0])).toBe('spectra');
    expect(tableAtPath(figure6, 'objects', [3, 1])).toBe('objects');
  });

  it('keeps a one-row card (it is what the user built) and drops empty wrappers and groups', () => {
    const card: TreeNode = { exists: 'spectra', where: [{ exists: 'lines', where: [{ col: 'line', op: 'eq', v: 'Halpha' }] }], not: true };
    expect(tidyConditions([card])).toEqual([card]);
    expect(tidyConditions([{ exists: 'objects', where: [] }, { exists: 'objects', where: [card] }, { or: [] }], 'spectra'))
      .toEqual([{ exists: 'objects', where: [card] }]);
  });
});

describe('the two roots', () => {
  it('moves the example to the spectra root and back without changing its meaning', () => {
    const atSpectra = rerootConditions(figure6, 'objects', 'spectra');
    for (const n of atSpectra) expect(validateNode(n, 'spectra')).toEqual(n);
    const { items } = readConditions(atSpectra, 'spectra');
    const spectrum = items.filter((it) => it.noun === 'spectrum');
    const object = items.filter((it) => it.noun === 'object');
    // the G395M card is the spectrum; its two lines came with it
    expect(spectrum.map((it) => (it.kind === 'property' ? it.def.key : it.kind === 'measurement' ? it.key : it.kind))).toEqual(['grating', 'snr', 'Halpha', 'OIII5007']);
    // "no PRISM" is its object's Grating chip; the χ² node rides along as advanced
    expect(object.map((it) => (it.kind === 'property' ? it.def.key : it.kind === 'measurement' ? it.key : it.kind)))
      .toEqual(['field', 'program', 'quality', 'anyOf', 'f444w', 'grating', 'advanced']);
    expect((object[5] as PropertyItem).value).toEqual({ kind: 'choice', values: ['PRISM'], not: true });
    // and back
    const back = rerootConditions(atSpectra, 'spectra', 'objects');
    expect(back).toEqual(figure6);
    expect(nodeAt(back, [5])).toEqual(figure6[5]);
    expect(nodeAt(back, [6])).toEqual(figure6[6]);
  });

  it('keeps "observed in all of A and B" an object\'s question at the spectra root', () => {
    const both = compileChoice(def('object', 'program'), { include: ['a', 'b'], mode: 'all', exclude: [] }, 'objects');
    const atSpectra = rerootConditions(both, 'objects', 'spectra');
    // through the object: a spectrum has one target, so two bare target predicates could never both hold
    expect(atSpectra).toEqual(both.map((n) => ({ exists: 'objects', where: [n] })));
    const items = readConditions(atSpectra, 'spectra').items;
    expect(items.every((it) => it.noun === 'object' && it.kind === 'property' && it.def.key === 'program')).toBe(true);
    expect(rerootConditions(atSpectra, 'spectra', 'objects')).toEqual(both);
    // the object's own column goes through the object too, and comes back
    const field = compileProperty(def('object', 'field'), { kind: 'choice', values: ['f'] }, 'objects')!;
    expect(rerootConditions([field], 'objects', 'spectra')).toEqual([{ exists: 'objects', where: [field] }]);
    expect(rerootConditions(rerootConditions([field], 'objects', 'spectra'), 'spectra', 'objects')).toEqual([field]);
    // a pasted spectra-root field through the target (the legacy shape) is the object's and stays bare
    const viaTarget: TreeNode = { exists: 'targets', where: [{ col: 'field', op: 'in', v: ['f'] }] };
    expect(rerootConditions(rerootConditions([viaTarget], 'spectra', 'objects'), 'objects', 'spectra')).toEqual([viaTarget]);
    // and the adder at the spectra root writes "observed in" through the object
    expect(compileProperty(def('object', 'program'), { kind: 'choice', values: ['a'] }, 'spectra')).toEqual({ exists: 'objects', where: [{ exists: 'targets', where: [{ col: 'program_slug', op: 'in', v: ['a'] }] }] });
  });

  it('keeps two cards as two spectra, not one', () => {
    const two: TreeNode[] = [
      { exists: 'spectra', where: [{ col: 'grating', op: 'in', v: ['G395M'] }, { exists: 'lines', where: [{ col: 'line', op: 'eq', v: 'Halpha' }] }] },
      { exists: 'spectra', where: [{ col: 'grating', op: 'in', v: ['G235M'] }, { exists: 'lines', where: [{ col: 'line', op: 'eq', v: 'CIII1908' }] }] },
    ];
    const atSpectra = rerootConditions(two, 'objects', 'spectra');
    expect(atSpectra).toEqual([
      { col: 'grating', op: 'in', v: ['G395M'] },
      { exists: 'lines', where: [{ col: 'line', op: 'eq', v: 'Halpha' }] },
      { exists: 'objects', where: [two[1]] },
    ]);
    expect(rerootConditions(atSpectra, 'spectra', 'objects')).toEqual(two);
    // an object's line row ("any spectrum") stays its object's at the spectra root, and comes back
    const lone: TreeNode[] = [{ exists: 'lines', where: [{ col: 'line', op: 'eq', v: 'Halpha' }] }, { col: 'redshift', op: 'gte', v: 5 }];
    expect(rerootConditions(lone, 'objects', 'spectra')).toEqual([{ exists: 'objects', where: [lone[0]] }, { exists: 'objects', where: [lone[1]] }]);
    expect(rerootConditions(rerootConditions(lone, 'objects', 'spectra'), 'spectra', 'objects')).toEqual(lone);
    // a line row at the spectra root is this spectrum's: a card at the objects root
    expect(rerootConditions([lone[0]], 'spectra', 'objects')).toEqual([{ exists: 'spectra', where: [lone[0]] }]);
  });
});

describe('columns and words', () => {
  it('knows whether a tree constrains a column anywhere (the inspection queue\'s default)', () => {
    expect(treeReadsColumn(figure6, 'objects', 'objects', 'redshift_quality')).toBe(true);
    expect(treeReadsColumn([{ col: 'redshift_quality', op: 'nin', v: [0] }], 'objects', 'objects', 'redshift_quality')).toBe(true);
    expect(treeReadsColumn([{ or: [{ col: 'redshift_quality', op: 'in', v: [4] }, { col: 'photo_z', op: 'gte', v: 5 }] }], 'objects', 'objects', 'redshift_quality')).toBe(true);
    expect(treeReadsColumn([{ exists: 'spectra', where: [{ col: 'grating', op: 'in', v: ['PRISM'] }] }], 'objects', 'objects', 'redshift_quality')).toBe(false);
    expect(treeReadsColumn([{ exists: 'objects', where: [{ col: 'redshift_quality', op: 'in', v: [4] }] }], 'spectra', 'objects', 'redshift_quality')).toBe(true);
  });

  it('names the measurements a tree carries and whether a line asks for a fresh fit', () => {
    expect(measurementKeys(figure6, 'line')).toEqual(['Halpha', 'OIII5007']);
    expect(measurementKeys(figure6, 'band')).toEqual(['f444w']);
    expect(lineWantsFresh(figure6, 'Halpha')).toBe(false);
    const fresh = compileMeasurement('line', 'Halpha', [{ def: def('line', 'fresh'), value: { kind: 'bool', value: true } }]);
    expect(lineWantsFresh([{ exists: 'spectra', where: [fresh] }], 'Halpha')).toBe(true);
  });

  it('describes items as the filter bar\'s chips', () => {
    const { items } = readConditions(figure6, 'objects');
    const words = items.map((it: Item) => describeItem(it));
    expect(words[1]).toBe('Program: jades');
    expect(words[3]).toBe('Redshift: 5 – 8 or Photo-z: 5 – 8');
    expect(words[4]).toBe('f444w mag ≤ 27 AB, S/N ≥ 5');
    expect(words[5]).toBe('Spectrum: Grating: G395M · S/N: ≥ 3 · Halpha S/N ≥ 3 · OIII5007 S/N ≥ 3');
    expect(words[6]).toBe('Grating: none of PRISM');
    expect(words[7]).toBe('has a line fit where χ² < 2');
  });
});
