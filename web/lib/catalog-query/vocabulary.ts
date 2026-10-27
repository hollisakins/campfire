/**
 * The two-noun vocabulary over the predicate tree (Catalog Read Model phase
 * 3b, design doc "The vocabulary: two nouns"). A user names what was
 * measured, never where it is stored:
 *
 *   Object    properties (redshift, quality, position, field, program, tag, …)
 *             and measurements keyed by band (magnitude, S/N, flux)
 *   Spectrum  properties (grating, S/N, exposure time, quality flags, …)
 *             and measurements keyed by line (S/N, flux, rest EW, …)
 *
 * An object has many spectra; a spectrum has exactly one object. At the
 * object level a spectrum's property or a line is a question about any of
 * its spectra (grating coverage, "Hα detected"); scope is containment, so
 * everything inside one Spectrum card holds on the same row of `spectra`. The tree in `?q=` stays the state; this module is its reading
 * (`readConditions`) and its writing (`compileProperty`,
 * `compileMeasurement`, the path edits). Whatever the cards cannot show
 * reads as an `advanced` item, never as nothing.
 *
 * Every shape written here for a primary property is the legacy pill's own
 * node (lib/catalog-query/legacy.ts), so a tree made only of cards still
 * takes the legacy RPCs.
 */
import type { LeafChoices, TreeRoot, TreeTable } from './catalogue';
import {
  describeNode, isAnd, isCone, isExists, isOr, isPredicate,
  type ConeNode, type ExistsNode, type PredicateNode, type Scalar, type TreeNode,
} from './tree';

export type Noun = 'object' | 'spectrum';
/** Where a property lives: on a noun, or among the clauses of a measurement. */
export type Scope = Noun | 'line' | 'band';
export type PropertyKind = 'choice' | 'range' | 'flags' | 'bool' | 'position';
export type MeasurementKind = 'line' | 'band';
export type FlagsMode = 'any' | 'all' | 'none';

/** The table a scope's properties are predicates on. */
export const SCOPE_TABLE: Record<Scope, TreeTable> = { object: 'objects', spectrum: 'spectra', line: 'lines', band: 'photometry' };

interface Shape {
  /** Child tables walked by sole-where exists from the scope's table to the predicate's. */
  via: TreeTable[];
  /** The column, or '' for a cone. */
  col: string;
  /** A bool whose two values are two different predicates (inspected = quality > 0 / = 0). */
  bool?: { true: Omit<PredicateNode, 'col'>; false: Omit<PredicateNode, 'col'> };
  /** A bool stored inverted (a fresh fit is `stale_redshift = false`). */
  invert?: boolean;
}

export interface RangeHints {
  min: number; max: number; step: number; precision: number;
  quick?: { label: string; min: number | null; max: number | null }[];
  /** The bound a one-sided default asks for (S/N ≥, magnitude ≤). */
  side?: 'min' | 'max';
}

export interface PropertyDef {
  key: string;
  scope: Scope;
  label: string;
  kind: PropertyKind;
  /** Offered on the first list of its scope (always shown as a chip). */
  primary?: boolean;
  /** A relation an object has several of (tags, gratings, programs): the
   *  included values take an any / all switch. */
  multi?: boolean;
  choices?: LeafChoices;
  unit?: string;
  range?: RangeHints;
  /** How it is written against each table it can be evaluated from. */
  shapes: Partial<Record<TreeTable, Shape>>;
}

export type PropertyValue =
  | { kind: 'choice'; values: Scalar[]; not?: boolean }
  | { kind: 'range'; min: number | null; max: number | null; unknown?: boolean }
  | { kind: 'flags'; mode: FlagsMode; mask: number }
  | { kind: 'bool'; value: boolean }
  | { kind: 'position'; ra: number; dec: number; radius: number };

const direct = (col: string): Shape => ({ via: [], col });
const via = (tables: TreeTable[], col: string): Shape => ({ via: tables, col });
const snrQuick = [{ label: '≥ 3', min: 3, max: null }, { label: '≥ 5', min: 5, max: null }, { label: '≥ 10', min: 10, max: null }, { label: '≥ 20', min: 20, max: null }];

/** The properties of the four scopes, in the order the cards show them. */
export const PROPERTIES: PropertyDef[] = [
  // Object
  { key: 'program', scope: 'object', label: 'Program', kind: 'choice', primary: true, multi: true, choices: 'program', shapes: { objects: via(['targets'], 'program_slug'), spectra: via(['targets'], 'program_slug') } },
  { key: 'field', scope: 'object', label: 'Field', kind: 'choice', primary: true, choices: 'field', shapes: { objects: direct('field'), spectra: via(['targets'], 'field') } },
  { key: 'observation', scope: 'object', label: 'Observation', kind: 'choice', primary: true, multi: true, choices: 'observation', shapes: { objects: via(['targets'], 'observation'), spectra: via(['targets'], 'observation') } },
  { key: 'redshift', scope: 'object', label: 'Redshift', kind: 'range', primary: true, range: { min: 0, max: 15, step: 0.1, precision: 2 }, shapes: { objects: direct('redshift') } },
  // before quality: `redshift_quality = 0` is "not inspected", not a quality pick
  { key: 'inspected', scope: 'object', label: 'Inspected', kind: 'bool', shapes: { objects: { via: [], col: 'redshift_quality', bool: { true: { op: 'gt', v: 0 }, false: { op: 'eq', v: 0 } } } } },
  { key: 'quality', scope: 'object', label: 'Quality', kind: 'choice', primary: true, choices: 'quality', shapes: { objects: direct('redshift_quality') } },
  { key: 'position', scope: 'object', label: 'Position', kind: 'position', primary: true, shapes: { objects: direct(''), spectra: via(['targets'], '') } },
  { key: 'tag', scope: 'object', label: 'Tags', kind: 'choice', primary: true, multi: true, choices: 'list', shapes: { objects: via(['object_list_members'], 'list_id'), spectra: via(['object_list_members'], 'list_id') } },
  // coverage: any of the object's spectra (the legacy grating pill's three shapes)
  { key: 'grating', scope: 'object', label: 'Grating', kind: 'choice', primary: true, multi: true, choices: 'grating', shapes: { objects: via(['spectra'], 'grating'), spectra: { via: ['objects', 'spectra'], col: 'grating' } } },
  { key: 'max_snr', scope: 'object', label: 'Max S/N', kind: 'range', range: { min: 0, max: 100000, step: 1, precision: 0, quick: snrQuick, side: 'min' }, shapes: { objects: direct('max_snr') } },
  { key: 'max_exptime', scope: 'object', label: 'Max exp. time', kind: 'range', unit: 's', range: { min: 0, max: 50000, step: 100, precision: 0, side: 'min' }, shapes: { objects: direct('max_exposure_time') } },
  { key: 'needs_review', scope: 'object', label: 'Needs review', kind: 'bool', shapes: { objects: direct('needs_review') } },
  { key: 'photo_z', scope: 'object', label: 'Photo-z', kind: 'range', range: { min: 0, max: 15, step: 0.1, precision: 2 }, shapes: { objects: direct('photo_z') } },
  { key: 'has_photometry', scope: 'object', label: 'Has photometry', kind: 'bool', shapes: { objects: direct('has_photometry') } },
  { key: 'redshift_auto', scope: 'object', label: 'Auto redshift', kind: 'range', range: { min: 0, max: 15, step: 0.1, precision: 2 }, shapes: { objects: direct('redshift_auto') } },
  { key: 'redshift_inspected', scope: 'object', label: 'Inspected redshift', kind: 'range', range: { min: 0, max: 15, step: 0.1, precision: 2 }, shapes: { objects: direct('redshift_inspected') } },
  { key: 'n_detected', scope: 'object', label: 'Lines detected (any spectrum)', kind: 'range', range: { min: 0, max: 50, step: 1, precision: 0, side: 'min' }, shapes: { objects: via(['line_fits'], 'n_detected') } },
  { key: 'fresh', scope: 'object', label: 'Fresh line fit (any spectrum)', kind: 'bool', shapes: { objects: { via: ['line_fits'], col: 'stale_redshift', invert: true } } },
  // Spectrum
  { key: 'grating', scope: 'spectrum', label: 'Grating', kind: 'choice', primary: true, choices: 'grating', shapes: { spectra: direct('grating') } },
  { key: 'snr', scope: 'spectrum', label: 'S/N', kind: 'range', primary: true, range: { min: 0, max: 100000, step: 1, precision: 0, quick: snrQuick, side: 'min' }, shapes: { spectra: direct('signal_to_noise') } },
  { key: 'exptime', scope: 'spectrum', label: 'Exp. time', kind: 'range', primary: true, unit: 's', range: { min: 0, max: 50000, step: 100, precision: 0, side: 'min', quick: [{ label: '≥ 1000', min: 1000, max: null }, { label: '≥ 5000', min: 5000, max: null }, { label: '≥ 10000', min: 10000, max: null }] }, shapes: { spectra: direct('exposure_time') } },
  { key: 'program', scope: 'spectrum', label: 'Program', kind: 'choice', primary: true, choices: 'program', shapes: { spectra: direct('program_slug') } },
  { key: 'observation', scope: 'spectrum', label: 'Observation', kind: 'choice', primary: true, choices: 'observation', shapes: { spectra: direct('observation') } },
  { key: 'dq_flags', scope: 'spectrum', label: 'Quality flags', kind: 'flags', primary: true, choices: 'dq_flags', shapes: { spectra: direct('dq_flags') } },
  { key: 'n_detected', scope: 'spectrum', label: 'Lines detected', kind: 'range', range: { min: 0, max: 50, step: 1, precision: 0, side: 'min' }, shapes: { spectra: via(['line_fits'], 'n_detected') } },
  { key: 'fresh', scope: 'spectrum', label: 'Fresh line fit', kind: 'bool', shapes: { spectra: { via: ['line_fits'], col: 'stale_redshift', invert: true } } },
  { key: 'confidence', scope: 'spectrum', label: 'Redshift fit confidence', kind: 'range', range: { min: 0, max: 1, step: 0.01, precision: 2, side: 'min' }, shapes: { spectra: direct('confidence') } },
  { key: 'redshift_auto', scope: 'spectrum', label: 'Auto redshift', kind: 'range', range: { min: 0, max: 15, step: 0.1, precision: 2 }, shapes: { spectra: direct('redshift_auto') } },
  { key: 'z_fit', scope: 'spectrum', label: 'Fitted redshift', kind: 'range', range: { min: 0, max: 15, step: 0.1, precision: 3 }, shapes: { spectra: via(['line_fits'], 'z_fit') } },
  { key: 'sigma_v', scope: 'spectrum', label: 'Velocity dispersion', kind: 'range', unit: 'km/s', range: { min: 0, max: 5000, step: 10, precision: 0 }, shapes: { spectra: via(['line_fits'], 'sigma_v') } },
  // Line (the clauses of a line measurement)
  { key: 'snr', scope: 'line', label: 'S/N', kind: 'range', primary: true, range: { min: 0, max: 1000, step: 1, precision: 1, side: 'min' }, shapes: { lines: direct('snr') } },
  { key: 'flux', scope: 'line', label: 'Flux', kind: 'range', unit: 'erg/s/cm²', range: { min: 0, max: 1e-12, step: 1e-18, precision: 3, side: 'min' }, shapes: { lines: direct('flux') } },
  { key: 'ew_rest', scope: 'line', label: 'Rest EW', kind: 'range', unit: 'Å', range: { min: 0, max: 10000, step: 1, precision: 1, side: 'min' }, shapes: { lines: direct('ew_rest') } },
  { key: 'component', scope: 'line', label: 'Component', kind: 'choice', choices: 'component', shapes: { lines: direct('component') } },
  { key: 'dv', scope: 'line', label: 'Velocity offset', kind: 'range', unit: 'km/s', range: { min: -5000, max: 5000, step: 10, precision: 0 }, shapes: { lines: direct('dv') } },
  { key: 'sigma_v', scope: 'line', label: 'Velocity dispersion', kind: 'range', unit: 'km/s', range: { min: 0, max: 5000, step: 10, precision: 0 }, shapes: { lines: direct('sigma_v') } },
  { key: 'flags', scope: 'line', label: 'Line flags', kind: 'flags', choices: 'line_flags', shapes: { lines: direct('flags') } },
  { key: 'fresh', scope: 'line', label: 'Fresh fit', kind: 'bool', shapes: { lines: { via: ['line_fits'], col: 'stale_redshift', invert: true } } },
  // Band (the clauses of a band measurement)
  { key: 'mag', scope: 'band', label: 'mag', kind: 'range', primary: true, unit: 'AB', range: { min: 15, max: 35, step: 0.1, precision: 1, side: 'max' }, shapes: { photometry: direct('mag') } },
  { key: 'snr', scope: 'band', label: 'S/N', kind: 'range', primary: true, range: { min: 0, max: 10000, step: 1, precision: 1, side: 'min' }, shapes: { photometry: direct('snr') } },
  { key: 'flux', scope: 'band', label: 'Flux', kind: 'range', unit: 'µJy', range: { min: 0, max: 1e6, step: 0.01, precision: 3, side: 'min' }, shapes: { photometry: direct('flux') } },
];

export function propertiesOf(scope: Scope): PropertyDef[] {
  return PROPERTIES.filter((p) => p.scope === scope);
}

export function propertyDef(scope: Scope, key: string): PropertyDef | undefined {
  return PROPERTIES.find((p) => p.scope === scope && p.key === key);
}

export interface MeasurementDef {
  kind: MeasurementKind;
  /** The noun the measurement belongs to. */
  noun: Noun;
  table: 'lines' | 'photometry';
  keyCol: 'line' | 'band';
  scope: 'line' | 'band';
  label: string;
  plural: string;
}

export const MEASUREMENTS: Record<MeasurementKind, MeasurementDef> = {
  line: { kind: 'line', noun: 'spectrum', table: 'lines', keyCol: 'line', scope: 'line', label: 'Line', plural: 'Lines' },
  band: { kind: 'band', noun: 'object', table: 'photometry', keyCol: 'band', scope: 'band', label: 'Band', plural: 'Photometry' },
};

// ---------------------------------------------------------------- compiling

function between(col: string, lo: number | null, hi: number | null): PredicateNode | null {
  if (lo === null && hi === null) return null;
  if (hi === null) return { col, op: 'gte', v: lo };
  if (lo === null) return { col, op: 'lte', v: hi };
  return { col, op: 'between', v: [lo, hi] };
}

/** The shape a property takes against `table`: its own, or its objects shape
 *  reached through the spectrum's object. */
function shapesFor(def: PropertyDef, table: TreeTable): Shape[] {
  const out: Shape[] = [];
  const own = def.shapes[table];
  if (own) out.push(own);
  if (table === 'spectra' && def.scope === 'object' && def.shapes.objects) {
    const o = def.shapes.objects;
    out.push({ ...o, via: ['objects', ...o.via] });
  }
  return out;
}

function wrap(leaf: TreeNode, chain: TreeTable[], not: boolean): TreeNode {
  let node = leaf;
  for (let i = chain.length - 1; i >= 0; i--) {
    const ex: ExistsNode = { exists: chain[i], where: [node] };
    if (i === 0 && not) ex.not = true;
    node = ex;
  }
  return node;
}

/** The node a property value compiles to against `table`, or null when the
 *  value selects nothing (an empty choice, an open range). */
export function compileProperty(def: PropertyDef, value: PropertyValue, table: TreeTable): TreeNode | null {
  const shape = shapesFor(def, table)[0];
  if (!shape) return null;
  let leaf: TreeNode | null = null;
  let not = false;
  switch (value.kind) {
    case 'choice': {
      if (value.values.length === 0) return null;
      if (shape.via.length === 0) leaf = { col: shape.col, op: value.not ? 'nin' : 'in', v: value.values };
      else { leaf = { col: shape.col, op: 'in', v: value.values }; not = Boolean(value.not); }
      break;
    }
    case 'range':
      leaf = value.unknown ? { col: shape.col, op: 'is_null' } : between(shape.col, value.min, value.max);
      break;
    case 'flags':
      if (!value.mask) return null;
      leaf = { col: shape.col, op: value.mode === 'all' ? 'allbits' : value.mode === 'none' ? 'nobits' : 'anybits', v: value.mask };
      break;
    case 'bool':
      if (shape.bool) leaf = { col: shape.col, ...(value.value ? shape.bool.true : shape.bool.false) };
      else leaf = { col: shape.col, op: 'eq', v: shape.invert ? !value.value : value.value };
      break;
    case 'position':
      leaf = { cone: { ra: value.ra, dec: value.dec, radius: value.radius } };
      break;
  }
  if (!leaf) return null;
  return wrap(leaf, shape.via, not);
}

/** What one choice or flags chip holds: its included values (any of them,
 *  or all of them for a multi-valued relation) and its excluded values. */
export interface ChoiceState { include: Scalar[]; mode: 'any' | 'all'; exclude: Scalar[] }

function bitsOf(mask: number): number[] {
  const out: number[] = [];
  for (let b = 0; b < 31; b++) if (mask & (1 << b)) out.push(1 << b);
  return out;
}

/** The nodes a chip's state compiles to: one include node (or one per value
 *  for "all of"), one exclude node. */
export function compileChoice(def: PropertyDef, state: ChoiceState, table: TreeTable): TreeNode[] {
  const out: TreeNode[] = [];
  const push = (n: TreeNode | null) => { if (n) out.push(n); };
  if (def.kind === 'flags') {
    const mask = (v: Scalar[]) => v.reduce<number>((m, x) => m | (typeof x === 'number' ? x : 0), 0);
    if (state.include.length) push(compileProperty(def, { kind: 'flags', mode: state.mode, mask: mask(state.include) }, table));
    if (state.exclude.length) push(compileProperty(def, { kind: 'flags', mode: 'none', mask: mask(state.exclude) }, table));
    return out;
  }
  if (state.include.length) {
    if (state.mode === 'all' && def.multi && state.include.length > 1) {
      // the legacy "all" shape: one node per value, `= v`
      for (const v of state.include) {
        const n = compileProperty(def, { kind: 'choice', values: [v] }, table);
        push(n && asEq(n));
      }
    } else {
      push(compileProperty(def, { kind: 'choice', values: state.include }, table));
    }
  }
  if (state.exclude.length) push(compileProperty(def, { kind: 'choice', values: state.exclude, not: true }, table));
  return out;
}

/** `in [v]` written as `= v`, at the leaf of its chain. */
function asEq(node: TreeNode): TreeNode {
  if (isExists(node) && node.where?.length === 1) return { ...node, where: [asEq(node.where[0])] };
  if (isPredicate(node) && node.op === 'in' && Array.isArray(node.v) && node.v.length === 1) return { col: node.col, op: 'eq', v: node.v[0] as Scalar };
  return node;
}

/** The state the items of one key at one level amount to. */
export function readChoiceState(items: PropertyItem[]): ChoiceState {
  const include: Scalar[] = [];
  const exclude: Scalar[] = [];
  let positives = 0;
  let allSingle = true;
  for (const it of items) {
    if (it.value.kind === 'choice') {
      const target = it.value.not ? exclude : include;
      for (const v of it.value.values) if (!target.includes(v)) target.push(v);
      if (!it.value.not) { positives++; if (it.value.values.length !== 1) allSingle = false; }
    } else if (it.value.kind === 'flags') {
      const target = it.value.mode === 'none' ? exclude : include;
      for (const v of bitsOf(it.value.mask)) if (!target.includes(v)) target.push(v);
      if (it.value.mode === 'all') { positives += 2; }
      else if (it.value.mode === 'any') { positives++; allSingle = allSingle && bitsOf(it.value.mask).length === 1; }
    }
  }
  return { include, mode: positives > 1 && allSingle ? 'all' : 'any', exclude };
}

/** A measurement: `exists lines|photometry where key = X and clauses`. */
export function compileMeasurement(kind: MeasurementKind, key: string, clauses: { def: PropertyDef; value: PropertyValue }[], not = false): ExistsNode {
  const m = MEASUREMENTS[kind];
  const where: TreeNode[] = [{ col: m.keyCol, op: 'eq', v: key }];
  for (const c of clauses) {
    const n = compileProperty(c.def, c.value, m.table);
    if (n) where.push(n);
  }
  const node: ExistsNode = { exists: m.table, where };
  if (not) node.not = true;
  return node;
}

// ---------------------------------------------------------------- reading

export type Path = number[];

export interface PropertyItem { kind: 'property'; noun: Noun; path: Path; def: PropertyDef; value: PropertyValue }
export interface MeasurementItem {
  kind: 'measurement'; noun: Noun; path: Path; measure: MeasurementKind; key: string; not: boolean;
  clauses: (PropertyItem | AdvancedItem)[];
}
export interface SpectrumItem { kind: 'spectrum'; noun: 'object'; path: Path; not: boolean; items: Item[] }
export interface AnyOfItem { kind: 'anyOf'; noun: Noun; path: Path; arms: Item[][] }
export interface AdvancedItem { kind: 'advanced'; noun: Noun; path: Path; node: TreeNode; table: TreeTable }
export type Item = PropertyItem | MeasurementItem | SpectrumItem | AnyOfItem | AdvancedItem;

export interface Reading {
  root: TreeRoot;
  /** Every item at the root level, in tree order, each tagged with its noun:
   *  at the objects root all are `object` (a Spectrum card is an object's);
   *  at the spectra root the spectrum's own are `spectrum`, its object's `object`. */
  items: Item[];
}

function rangeBounds(p: PredicateNode): [number | null, number | null] | null {
  if (p.op === 'gte' && typeof p.v === 'number') return [p.v, null];
  if (p.op === 'lte' && typeof p.v === 'number') return [null, p.v];
  if (p.op === 'between' && Array.isArray(p.v) && p.v.length === 2) {
    const [a, b] = p.v as Scalar[];
    if ((a === null || typeof a === 'number') && (b === null || typeof b === 'number')) return [a, b];
  }
  return null;
}

/** The leaf a shape's chain leads to from `node`, with the outer negation. */
function unwrap(node: TreeNode, shape: Shape): { leaf: TreeNode; not: boolean } | null {
  let cur = node;
  let not = false;
  for (let i = 0; i < shape.via.length; i++) {
    if (!isExists(cur) || cur.exists !== shape.via[i] || cur.where?.length !== 1) return null;
    if (cur.not) { if (i !== 0) return null; not = true; }
    cur = cur.where[0];
  }
  return { leaf: cur, not };
}

function readValue(def: PropertyDef, shape: Shape, leaf: TreeNode, not: boolean): PropertyValue | null {
  if (def.kind === 'position') {
    return !not && isCone(leaf) ? { kind: 'position', ...leaf.cone } : null;
  }
  if (!isPredicate(leaf) || leaf.col !== shape.col) return null;
  switch (def.kind) {
    case 'choice': {
      if (leaf.op === 'in' && Array.isArray(leaf.v)) return { kind: 'choice', values: leaf.v as Scalar[], ...(not ? { not: true } : {}) };
      if (leaf.op === 'eq' && !Array.isArray(leaf.v)) return { kind: 'choice', values: [leaf.v as Scalar], ...(not ? { not: true } : {}) };
      if (shape.via.length === 0 && !not) {
        if (leaf.op === 'nin' && Array.isArray(leaf.v)) return { kind: 'choice', values: leaf.v as Scalar[], not: true };
        if (leaf.op === 'neq' && !Array.isArray(leaf.v)) return { kind: 'choice', values: [leaf.v as Scalar], not: true };
      }
      return null;
    }
    case 'range': {
      if (not) return null;
      if (leaf.op === 'is_null') return { kind: 'range', min: null, max: null, unknown: true };
      const b = rangeBounds(leaf);
      return b ? { kind: 'range', min: b[0], max: b[1] } : null;
    }
    case 'flags': {
      if (not || typeof leaf.v !== 'number') return null;
      const mode: FlagsMode | null = leaf.op === 'anybits' ? 'any' : leaf.op === 'allbits' ? 'all' : leaf.op === 'nobits' ? 'none' : null;
      return mode ? { kind: 'flags', mode, mask: leaf.v } : null;
    }
    case 'bool': {
      if (not) return null;
      if (shape.bool) {
        const same = (a: Omit<PredicateNode, 'col'>) => a.op === leaf.op && JSON.stringify(a.v ?? null) === JSON.stringify(leaf.v ?? null);
        if (same(shape.bool.true)) return { kind: 'bool', value: true };
        if (same(shape.bool.false)) return { kind: 'bool', value: false };
        return null;
      }
      if (leaf.op === 'eq' && typeof leaf.v === 'boolean') return { kind: 'bool', value: shape.invert ? !leaf.v : leaf.v };
      return null;
    }
  }
  return null;
}

/** The property a node is, read against `table` among the given scopes. */
export function readProperty(node: TreeNode, table: TreeTable, scopes: Scope[]): { def: PropertyDef; value: PropertyValue } | null {
  for (const def of PROPERTIES) {
    if (!scopes.includes(def.scope)) continue;
    for (const shape of shapesFor(def, table)) {
      const u = unwrap(node, shape);
      if (!u) continue;
      const value = readValue(def, shape, u.leaf, u.not);
      if (value) return { def, value };
    }
  }
  return null;
}

function measurementKey(node: ExistsNode, def: MeasurementDef): { key: string; index: number } | null {
  const i = (node.where ?? []).findIndex((w) => isPredicate(w) && w.col === def.keyCol && w.op === 'eq' && typeof w.v === 'string');
  return i < 0 ? null : { key: (node.where![i] as PredicateNode).v as string, index: i };
}

function readMeasurement(node: ExistsNode, kind: MeasurementKind, noun: Noun, path: Path, not: boolean): MeasurementItem | null {
  const def = MEASUREMENTS[kind];
  const k = measurementKey(node, def);
  if (!k) return null;
  const clauses: (PropertyItem | AdvancedItem)[] = [];
  (node.where ?? []).forEach((w, i) => {
    if (i === k.index) return;
    const p = readProperty(w, def.table, [def.scope]);
    const cpath = [...path, i];
    clauses.push(p ? { kind: 'property', noun, path: cpath, def: p.def, value: p.value } : { kind: 'advanced', noun, path: cpath, node: w, table: def.table });
  });
  return { kind: 'measurement', noun, path, measure: kind, key: k.key, not, clauses };
}

interface ReadCtx {
  /** Inside a Spectrum card: the object's own properties are not vocabulary there. */
  inCard: boolean;
}

function readArm(arm: TreeNode, table: 'objects' | 'spectra', path: Path, ctx: ReadCtx): Item[] {
  if (isAnd(arm)) return arm.and.flatMap((c, j) => readNode(c, table, [...path, j], ctx));
  return readNode(arm, table, path, ctx);
}

function readNode(node: TreeNode, table: 'objects' | 'spectra', path: Path, ctx: ReadCtx): Item[] {
  const noun: Noun = table === 'objects' ? 'object' : 'spectrum';
  const advanced = (): Item[] => [{ kind: 'advanced', noun, path, node, table }];
  if (isAnd(node)) return node.and.flatMap((c, i) => readNode(c, table, [...path, i], ctx));
  if (isOr(node)) {
    const arms = node.or.map((arm, i) => readArm(arm, table, [...path, i], ctx));
    // an OR whose arms are all the object's reads as the object's
    const nouns = new Set(arms.flat().map((it) => it.noun));
    const armNoun: Noun = nouns.size === 1 ? [...nouns][0] : noun;
    return [{ kind: 'anyOf', noun: armNoun, path, arms }];
  }
  if (table === 'objects') {
    // the object's own, including what it asks of any of its spectra
    // (grating coverage, a fit property) before a card
    const p = readProperty(node, 'objects', ['object']);
    if (p) return [{ kind: 'property', noun: 'object', path, def: p.def, value: p.value }];
    if (isExists(node)) {
      if (node.exists === 'spectra') {
        return [{ kind: 'spectrum', noun: 'object', path, not: Boolean(node.not), items: readLevel(node.where ?? [], 'spectra', path, { inCard: true }) }];
      }
      if (node.exists === 'lines') {
        // a line of any of its spectra
        const m = readMeasurement(node, 'line', 'object', path, Boolean(node.not));
        return m ? [m] : advanced();
      }
      if (node.exists === 'photometry') {
        const m = readMeasurement(node, 'band', 'object', path, Boolean(node.not));
        return m ? [m] : advanced();
      }
    }
    return advanced();
  }
  // table === 'spectra'
  if (isExists(node) && node.exists === 'lines') {
    const m = readMeasurement(node, 'line', 'spectrum', path, Boolean(node.not));
    return m ? [m] : advanced();
  }
  const sp = readProperty(node, 'spectra', ['spectrum']);
  if (sp) return [{ kind: 'property', noun: 'spectrum', path, def: sp.def, value: sp.value }];
  if (!ctx.inCard) {
    // the spectrum's object, at the spectra root
    if (isExists(node) && node.exists === 'objects' && !node.not) {
      return readLevel(node.where ?? [], 'objects', path, { inCard: false });
    }
    if (isExists(node) && node.exists === 'photometry') {
      const m = readMeasurement(node, 'band', 'object', path, Boolean(node.not));
      return m ? [m] : advanced();
    }
    const op = readProperty(node, 'spectra', ['object']);
    if (op) return [{ kind: 'property', noun: 'object', path, def: op.def, value: op.value }];
  }
  return advanced();
}

function readLevel(nodes: TreeNode[], table: 'objects' | 'spectra', path: Path, ctx: ReadCtx): Item[] {
  return nodes.flatMap((n, i) => readNode(n, table, [...path, i], ctx));
}

/** The cards a tree holds, read against its root. */
export function readConditions(nodes: TreeNode[], root: TreeRoot): Reading {
  const table = root === 'spectra' ? 'spectra' : 'objects';
  return { root, items: readLevel(nodes, table, [], { inCard: false }) };
}

/** Every property item at a level, keyed by scope and key (first wins). */
export function findProperty(items: Item[], scope: Scope, key: string): PropertyItem | undefined {
  return items.find((it): it is PropertyItem => it.kind === 'property' && it.def.scope === scope && it.def.key === key);
}

// ---------------------------------------------------------------- path edits

function childrenOf(node: TreeNode): TreeNode[] | null {
  if (isExists(node)) return node.where ?? [];
  if (isAnd(node)) return node.and;
  if (isOr(node)) return node.or;
  return null;
}

function withChildren(node: TreeNode, children: TreeNode[]): TreeNode {
  if (isExists(node)) {
    const out: ExistsNode = { exists: node.exists };
    if (children.length > 0) out.where = children;
    if (node.not) out.not = true;
    return out;
  }
  if (isAnd(node)) return { and: children };
  return { or: children };
}

/** The table the node at `path` is evaluated against: its nearest exists
 *  ancestor's child table, else the root. */
export function tableAtPath(nodes: TreeNode[], root: TreeRoot, path: Path): TreeTable {
  let table: TreeTable = root;
  let list: TreeNode[] | null = nodes;
  for (let i = 0; i < path.length - 1; i++) {
    const node: TreeNode | undefined = list?.[path[i]];
    if (!node) return table;
    if (isExists(node)) table = node.exists;
    list = childrenOf(node);
  }
  return table;
}

export function nodeAt(nodes: TreeNode[], path: Path): TreeNode | undefined {
  let list: TreeNode[] | null = nodes;
  let node: TreeNode | undefined;
  for (const i of path) {
    if (!list) return undefined;
    node = list[i];
    if (!node) return undefined;
    list = childrenOf(node);
  }
  return node;
}

/** `nodes` with the node at `path` replaced (or removed when `next` is null). */
export function replaceAt(nodes: TreeNode[], path: Path, next: TreeNode | null): TreeNode[] {
  if (path.length === 0) return nodes;
  const [i, ...rest] = path;
  if (i < 0 || i >= nodes.length) return nodes;
  if (rest.length === 0) {
    return next ? nodes.map((n, j) => (j === i ? next : n)) : nodes.filter((_, j) => j !== i);
  }
  const parent = nodes[i];
  const children = childrenOf(parent);
  if (!children) return nodes;
  const updated = replaceAt(children, rest, next);
  // a container emptied by a removal goes with its last child, except a
  // Spectrum card, which stays as "has a spectrum" until it is removed itself
  if (updated.length === 0 && next === null && !(isExists(parent) && parent.exists === 'spectra')) {
    return nodes.filter((_, j) => j !== i);
  }
  return nodes.map((n, j) => (j === i ? withChildren(parent, updated) : n));
}

/** `nodes` with `node` appended inside the container at `path` (the top level
 *  for an empty path). A plain node in an OR arm becomes an AND of the two. */
export function appendAt(nodes: TreeNode[], path: Path, node: TreeNode): TreeNode[] {
  if (path.length === 0) return [...nodes, node];
  const target = nodeAt(nodes, path);
  if (!target) return nodes;
  const children = childrenOf(target);
  if (children) return replaceAt(nodes, path, withChildren(target, [...children, node]));
  return replaceAt(nodes, path, { and: [target, node] });
}

/**
 * A Spectrum card keeps its contents in one `and` group inside the exists
 * (`exists spectra where [{and: [...]}]`), so a card holding a grating alone
 * is never the Grating chip's bare `exists spectra where grating in [...]`.
 * The path of a card's contents, and the card with a node added to them.
 */
export function cardContentsPath(nodes: TreeNode[], cardPath: Path): Path {
  const card = nodeAt(nodes, cardPath);
  if (card && isExists(card) && card.where?.length === 1 && isAnd(card.where[0])) return [...cardPath, 0];
  return cardPath;
}

export function addToCard(nodes: TreeNode[], cardPath: Path, node: TreeNode): TreeNode[] {
  const card = nodeAt(nodes, cardPath);
  if (!card || !isExists(card)) return nodes;
  const where = card.where ?? [];
  const inner = where.length === 1 && isAnd(where[0]) ? where[0].and : where;
  const next: ExistsNode = { exists: card.exists, where: [{ and: [...inner, node] }] };
  if (card.not) next.not = true;
  return replaceAt(nodes, cardPath, next);
}

/** `nodes` without the nodes at `paths` (deepest / last first, so the other
 *  paths stay valid). */
export function removePaths(nodes: TreeNode[], paths: Path[]): TreeNode[] {
  const order = [...paths].sort((a, b) => {
    for (let i = 0; i < Math.min(a.length, b.length); i++) if (a[i] !== b[i]) return b[i] - a[i];
    return b.length - a.length;
  });
  let out = nodes;
  for (const p of order) out = replaceAt(out, p, null);
  return out;
}

/** The exists at `path` with its negation set. */
export function setNegated(nodes: TreeNode[], path: Path, not: boolean): TreeNode[] {
  const target = nodeAt(nodes, path);
  if (!target || !isExists(target)) return nodes;
  const out: ExistsNode = { exists: target.exists };
  if (target.where) out.where = target.where;
  if (not) out.not = true;
  return replaceAt(nodes, path, out);
}

/** The tree tidied after an edit: an `exists objects` wrapper with nothing
 *  left inside goes, an empty group goes. */
export function tidyConditions(nodes: TreeNode[], table: TreeTable = 'objects'): TreeNode[] {
  const out: TreeNode[] = [];
  for (const node of nodes) {
    if (isAnd(node)) { const and = tidyConditions(node.and, table); if (and.length) out.push({ and }); continue; }
    if (isOr(node)) { const or = tidyConditions(node.or, table); if (or.length) out.push({ or }); continue; }
    if (!isExists(node)) { out.push(node); continue; }
    const where = tidyConditions(node.where ?? [], node.exists);
    if (node.exists === 'objects' && !node.not && where.length === 0) continue;
    out.push(withChildren(node, where));
  }
  return out;
}

// ---------------------------------------------------------------- the two roots

/** Whether a node at the spectra root is the spectrum's own or its object's. */
function spectrumScopedAtSpectraRoot(node: TreeNode): boolean {
  if (isPredicate(node) || isCone(node)) return true;
  if (isExists(node)) {
    if (node.exists === 'lines' || node.exists === 'line_fits') return true;
    if (node.exists === 'targets') {
      // what the vocabulary writes through the target at this root is the
      // object's (field, position, "observed in"); anything else stays with
      // the spectrum
      const w = node.where?.length === 1 ? node.where[0] : null;
      return !(w && (isCone(w) || (isPredicate(w) && ['field', 'program_slug', 'observation'].includes(w.col))));
    }
    return false; // objects, photometry, catalog_matches, list members
  }
  return false;
}

/** A Spectrum card at the objects root: an `exists spectra` that is not the
 *  Grating chip's own node (a sole grating predicate, which is coverage). */
function isCardAtObjectsRoot(node: TreeNode): boolean {
  if (!isExists(node) || node.exists !== 'spectra') return false;
  const w = node.where ?? [];
  return !(w.length === 1 && isPredicate(w[0]) && w[0].col === 'grating');
}

function flattenAnds(nodes: TreeNode[]): TreeNode[] {
  return nodes.flatMap((n) => (isAnd(n) ? flattenAnds(n.and) : [n]));
}

/**
 * The same conditions against the other root, keeping what they mean. From
 * objects to spectra the first (non-negated) Spectrum card becomes the
 * spectrum itself and every other card a sibling of the same object; from
 * spectra to objects everything that is the spectrum's own becomes one card.
 */
export function rerootConditions(nodes: TreeNode[], from: TreeRoot, to: TreeRoot): TreeNode[] {
  if (from === to) return nodes;
  const flat = flattenAnds(nodes);
  if (from === 'objects' && to === 'spectra') {
    // the spectrum is the first card; every other node, cards included, is its object's
    const chosen = flat.findIndex((n) => isCardAtObjectsRoot(n) && !(n as ExistsNode).not && ((n as ExistsNode).where?.length ?? 0) > 0);
    const out: TreeNode[] = [];
    flat.forEach((n, i) => {
      if (i === chosen) out.push(...((n as ExistsNode).where ?? []));
      else if (isExists(n) && (n.exists === 'photometry' || n.exists === 'object_list_members' || n.exists === 'catalog_matches' || n.exists === 'targets')) out.push(n);
      else out.push({ exists: 'objects', where: [n] });
    });
    return out;
  }
  if (from === 'spectra' && to === 'objects') {
    const top: TreeNode[] = [];
    const card: TreeNode[] = [];
    let cardAt = -1;
    for (const n of flat) {
      if (isOr(n)) {
        const arms = n.or.map((a) => (isExists(a) && a.exists === 'objects' && !a.not && a.where?.length === 1 ? a.where[0] : null));
        if (arms.every((a): a is TreeNode => a !== null)) top.push({ or: arms });
        else if (n.or.every((a) => spectrumScopedAtSpectraRoot(a))) { if (cardAt < 0) cardAt = top.length; card.push(n); }
        else top.push({ exists: 'spectra', where: [n] });
        continue;
      }
      if (isExists(n) && n.exists === 'objects' && !n.not) { top.push(...(n.where ?? [])); continue; }
      if (spectrumScopedAtSpectraRoot(n)) { if (cardAt < 0) cardAt = top.length; card.push(n); }
      else top.push(n);
    }
    if (card.length === 0) return top;
    // a card of one grating predicate would read as the Grating chip: keep it a card
    const lone = card.length === 1 && isPredicate(card[0]) && card[0].col === 'grating';
    top.splice(cardAt, 0, { exists: 'spectra', where: lone ? [{ and: card }] : card });
    return top;
  }
  // targets is not a vocabulary root: wrap generically
  return nodes.map((n) => ({ exists: from, where: [n] }));
}

// ---------------------------------------------------------------- columns

/** Every key of a measurement kind the tree names anywhere. */
export function measurementKeys(nodes: TreeNode[], kind: MeasurementKind): string[] {
  const def = MEASUREMENTS[kind];
  const out: string[] = [];
  const walk = (list: TreeNode[]) => {
    for (const n of list) {
      if (isExists(n) && n.exists === def.table) {
        const k = measurementKey(n, def);
        if (k && !out.includes(k.key)) out.push(k.key);
      }
      const c = childrenOf(n);
      if (c) walk(c);
    }
  };
  walk(nodes);
  return out;
}

/** True when some line row for `line` asks for a fresh fit, so its column
 *  should hide stale fits too. */
export function lineWantsFresh(nodes: TreeNode[], line: string): boolean {
  let found = false;
  const walk = (list: TreeNode[]) => {
    for (const n of list) {
      if (isExists(n) && n.exists === 'lines' && !n.not) {
        const k = measurementKey(n, MEASUREMENTS.line);
        if (k && k.key === line && (n.where ?? []).some((w) => {
          const p = readProperty(w, 'lines', ['line']);
          return p?.def.key === 'fresh' && p.value.kind === 'bool' && p.value.value;
        })) found = true;
      }
      const c = childrenOf(n);
      if (c) walk(c);
    }
  };
  walk(nodes);
  return found;
}

// ---------------------------------------------------------------- words

export type ValueLabel = (choices: LeafChoices | undefined, value: Scalar) => string;

const plain: ValueLabel = (_c, v) => String(v);

function fmtNum(n: number, precision?: number): string {
  if (Number.isInteger(n)) return String(n);
  return precision !== undefined && Math.abs(n) >= 1e-3 ? String(+n.toFixed(precision)) : String(+n.toPrecision(4));
}

export function describeValue(def: PropertyDef, value: PropertyValue, label: ValueLabel = plain): string {
  switch (value.kind) {
    case 'choice': {
      const names = value.values.map((v) => label(def.choices, v));
      const list = names.length > 3 ? `${names.slice(0, 3).join(', ')} +${names.length - 3}` : names.join(', ');
      return value.not ? `none of ${list}` : list;
    }
    case 'range': {
      const u = def.unit ? ` ${def.unit}` : '';
      const p = def.range?.precision;
      if (value.unknown) return 'unknown';
      if (value.min !== null && value.max !== null) return `${fmtNum(value.min, p)} – ${fmtNum(value.max, p)}${u}`;
      if (value.min !== null) return `≥ ${fmtNum(value.min, p)}${u}`;
      if (value.max !== null) return `≤ ${fmtNum(value.max, p)}${u}`;
      return 'any';
    }
    case 'flags': {
      const word = value.mode === 'all' ? 'all of' : value.mode === 'none' ? 'none of' : 'any of';
      return `${word} ${label(def.choices, value.mask)}`;
    }
    case 'bool': return value.value ? 'yes' : 'no';
    case 'position': {
      const r = value.radius;
      const radius = r >= 1 ? `${fmtNum(r)}°` : r * 60 >= 1 ? `${fmtNum(r * 60)}′` : `${fmtNum(r * 3600)}″`;
      return `within ${radius} of ${value.ra.toFixed(4)}, ${value.dec.toFixed(4)}`;
    }
  }
}

/** One short phrase for an item, as a chip on the filter bar reads. */
export function describeItem(item: Item, label: ValueLabel = plain): string {
  switch (item.kind) {
    case 'property': {
      const v = describeValue(item.def, item.value, label);
      if (item.def.kind === 'position') return v;
      if (item.value.kind === 'bool') return item.value.value ? item.def.label : `not ${item.def.label.toLowerCase()}`;
      return `${item.def.label}: ${v}`;
    }
    case 'measurement': {
      const key = label(MEASUREMENTS[item.measure].keyCol, item.key);
      const parts = item.clauses.map((c) => (c.kind === 'property' ? `${c.def.label} ${describeValue(c.def, c.value, label)}` : describeNode(c.node, c.table)));
      const body = parts.length ? `${key} ${parts.join(', ')}` : key;
      return item.not ? `no ${body}` : body;
    }
    case 'spectrum': {
      const inner = item.items.map((it) => describeItem(it, label)).join(' · ');
      return `${item.not ? 'No spectrum' : 'Spectrum'}${inner ? `: ${inner}` : ''}`;
    }
    case 'anyOf':
      return item.arms.map((arm) => arm.map((it) => describeItem(it, label)).join(' and ')).join(' or ');
    case 'advanced':
      return describeNode(item.node, item.table);
  }
}

export type { ConeNode };
