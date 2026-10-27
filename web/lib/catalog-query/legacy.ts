/**
 * The legacy filter contract as a predicate tree (phase 3b). The pills keep
 * editing `FilterOptions`; when a query also carries free conditions, the
 * whole filter state is evaluated as one tree, so every pill must have a
 * tree form that selects what its RPC parameter selects. The shapes here are
 * the ones check_catalog_query.sql proves at parity with the legacy RPCs
 * (section 3) as a viewer.
 *
 * `liftConditions` is the inverse for the shapes the pills write: a tree a
 * user pastes from the API docs lands in the pills where it can, and the
 * rest stays a condition.
 */
import { DEFAULT_FILTERS, type FilterOptions } from '@/lib/actions/filter-params';
import { convertRadiusToDegrees } from '@/lib/utils/coordinate-parser';
import { isFilterableLine } from '@/lib/linelist';
import { encodeBitmask } from '@/lib/flags';
import {
  firstConeOf, isCone, isExists, isPredicate,
  type ExistsNode, type PredicateNode, type Scalar, type TreeNode, type TreeRoot,
} from './tree';
import { lineWantsFresh } from './vocabulary';

/** The root a view mode queries. */
export function rootOf(viewMode: 'objects' | 'spectra' | undefined): TreeRoot {
  return viewMode === 'spectra' ? 'spectra' : 'objects';
}

function between(col: string, lo: number | null | undefined, hi: number | null | undefined): PredicateNode | null {
  const a = lo ?? null, b = hi ?? null;
  if (a === null && b === null) return null;
  if (b === null) return { col, op: 'gte', v: a };
  if (a === null) return { col, op: 'lte', v: b };
  return { col, op: 'between', v: [a, b] };
}

function onObjects(root: TreeRoot, node: TreeNode | null): TreeNode | null {
  if (!node) return null;
  return root === 'objects' ? node : { exists: 'objects', where: [node] };
}

/** The tree form of every legacy filter set in `filters`, against `root`. */
export function legacyNodes(filters: Partial<FilterOptions> | undefined, root: TreeRoot): TreeNode[] {
  const f = filters ?? {};
  const out: (TreeNode | null)[] = [];
  const push = (n: TreeNode | null) => { if (n) out.push(n); };
  const spectraRoot = root === 'spectra';

  if (f.programs?.length) {
    push(spectraRoot
      ? { col: 'program_slug', op: 'in', v: f.programs }
      : { exists: 'targets', where: [{ col: 'program_slug', op: 'in', v: f.programs }] });
  }
  if (f.fields?.length) {
    push(spectraRoot
      ? { exists: 'targets', where: [{ col: 'field', op: 'in', v: f.fields }] }
      : { col: 'field', op: 'in', v: f.fields });
  }
  if (f.observations?.length) {
    push(spectraRoot
      ? { col: 'observation', op: 'in', v: f.observations }
      : { exists: 'targets', where: [{ col: 'observation', op: 'in', v: f.observations }] });
  }
  if (f.redshift_quality?.length) push(onObjects(root, { col: 'redshift_quality', op: 'in', v: f.redshift_quality }));
  push(onObjects(root, between('redshift', f.redshift_min, f.redshift_max)));
  push(spectraRoot
    ? between('signal_to_noise', f.max_snr_min, f.max_snr_max)
    : between('max_snr', f.max_snr_min, f.max_snr_max));
  push(spectraRoot
    ? between('exposure_time', f.max_exposure_time_min, f.max_exposure_time_max)
    : between('max_exposure_time', f.max_exposure_time_min, f.max_exposure_time_max));
  if (f.inspected_only != null) push(onObjects(root, { col: 'redshift_quality', op: f.inspected_only ? 'gt' : 'eq', v: 0 }));
  if (f.needs_review != null) push(onObjects(root, { col: 'needs_review', op: 'eq', v: f.needs_review }));
  if (f.has_photometry != null) push(onObjects(root, { col: 'has_photometry', op: 'eq', v: f.has_photometry }));

  if (f.gratings?.length) {
    const mode = f.gratings_mode ?? 'any';
    if (spectraRoot) {
      // the spectra view has no all-mode: a row carries one grating
      push({ col: 'grating', op: mode === 'none' ? 'nin' : 'in', v: f.gratings });
    } else if (mode === 'all') {
      for (const g of f.gratings) push({ exists: 'spectra', where: [{ col: 'grating', op: 'eq', v: g }] });
    } else {
      push({ exists: 'spectra', where: [{ col: 'grating', op: 'in', v: f.gratings }], ...(mode === 'none' ? { not: true } : {}) });
    }
  }
  if (f.list_ids?.length) {
    const mode = f.list_ids_mode ?? 'any';
    if (mode === 'all') {
      for (const id of f.list_ids) push({ exists: 'object_list_members', where: [{ col: 'list_id', op: 'eq', v: id }] });
    } else {
      push({ exists: 'object_list_members', where: [{ col: 'list_id', op: 'in', v: f.list_ids }], ...(mode === 'none' ? { not: true } : {}) });
    }
  }
  // the DQ flags live on the spectrum (and the target); the objects list has
  // never taken them, so they filter the spectra view only
  if (spectraRoot && f.dq_flags?.length) {
    const mode = f.dq_flags_mode ?? 'any';
    push({ col: 'dq_flags', op: mode === 'all' ? 'allbits' : mode === 'none' ? 'nobits' : 'anybits', v: encodeBitmask(f.dq_flags) });
  }
  if (f.coordinate_search) {
    const cone = {
      cone: {
        ra: f.coordinate_search.ra,
        dec: f.coordinate_search.dec,
        radius: convertRadiusToDegrees(f.coordinate_search.radius, f.coordinate_search.radius_unit),
      },
    };
    push(spectraRoot ? { exists: 'targets', where: [cone] } : cone);
  }
  const line = f.line?.trim();
  if (line && isFilterableLine(line)) {
    const where: TreeNode[] = [{ col: 'line', op: 'eq', v: line }];
    const snr = between('snr', f.line_snr_min, f.line_snr_max);
    if (snr) where.push(snr);
    if (!f.line_include_stale) where.push({ exists: 'line_fits', where: [{ col: 'stale_redshift', op: 'eq', v: false }] });
    push({ exists: 'lines', where });
  }
  const band = f.band?.trim();
  if (band) {
    const where: TreeNode[] = [{ col: 'band', op: 'eq', v: band }];
    const mag = between('mag', f.band_mag_min, f.band_mag_max);
    if (mag) where.push(mag);
    const snr = between('snr', f.band_snr_min, f.band_snr_max);
    if (snr) where.push(snr);
    push({ exists: 'photometry', where });
  }
  return out.filter((n): n is TreeNode => n !== null);
}

/** The whole filter state as tree nodes: the pills' nodes, then the conditions. */
export function filterNodes(filters: Partial<FilterOptions> | undefined, root: TreeRoot): TreeNode[] {
  return [...legacyNodes(filters, root), ...(filters?.conditions ?? [])];
}

/**
 * True when the legacy RPC parameters cannot carry the (normalized) state:
 * it holds conditions beyond the pills' shapes, or asks for a line or band
 * column other than the one its pill filters on.
 */
export function needsTreePath(filters: Partial<FilterOptions> | undefined): boolean {
  if ((filters?.conditions?.length ?? 0) > 0) return true;
  const line = activeLineOf(filters);
  if (line && line !== (filters?.line?.trim() || null)) return true;
  const band = activeBandOf(filters);
  if (band && band !== (filters?.band?.trim() || null)) return true;
  return false;
}

/** The line whose S/N the table shows and sorts on: the `column` toggle of a
 *  line row (`show_line`), never implied by a filter. */
export function activeLineOf(filters: Partial<FilterOptions> | undefined): string | null {
  const line = filters?.show_line?.trim();
  return line && isFilterableLine(line) ? line : null;
}

/** The band whose magnitude and S/N the table shows (`show_band`). */
export function activeBandOf(filters: Partial<FilterOptions> | undefined): string | null {
  return filters?.show_band?.trim() || null;
}

/** Whether the shown line's column should count stale fits: it does unless a
 *  row for that line asks for a fresh fit. */
export function lineColumnIncludesStale(filters: Partial<FilterOptions> | undefined, root: TreeRoot): boolean {
  const line = activeLineOf(filters);
  return !(line && lineWantsFresh(filterNodes(filters, root), line));
}

/** The position the distance column measures from: the position pill, else
 *  the first cone condition. */
export function distanceReferenceOf(filters: Partial<FilterOptions> | undefined, root: TreeRoot = 'objects'): { ra: number; dec: number } | null {
  if (filters?.coordinate_search) return { ra: filters.coordinate_search.ra, dec: filters.coordinate_search.dec };
  const cone = firstConeOf(filters?.conditions ?? [], root);
  return cone ? { ra: cone.ra, dec: cone.dec } : null;
}

// ---------------------------------------------------------------- lifting

type Slot = Partial<FilterOptions>;

function strings(v: unknown): string[] | null {
  if (typeof v === 'string') return [v];
  return Array.isArray(v) && v.every((x) => typeof x === 'string') ? (v as string[]) : null;
}
function ints(v: unknown): number[] | null {
  if (typeof v === 'number') return [v];
  return Array.isArray(v) && v.every((x) => typeof x === 'number') ? (v as number[]) : null;
}
function bounds(p: PredicateNode): [number | null, number | null] | null {
  if (p.op === 'gte' && typeof p.v === 'number') return [p.v, null];
  if (p.op === 'lte' && typeof p.v === 'number') return [null, p.v];
  if (p.op === 'between' && Array.isArray(p.v) && p.v.length === 2) {
    const [a, b] = p.v as Scalar[];
    if ((a === null || typeof a === 'number') && (b === null || typeof b === 'number')) return [a, b];
  }
  return null;
}
function inPredicate(node: TreeNode, col: string): PredicateNode | null {
  return isPredicate(node) && node.col === col && (node.op === 'in' || node.op === 'eq') ? node : null;
}
function soleWhere(node: TreeNode, table: string): TreeNode | null {
  return isExists(node) && node.exists === table && !node.not && node.where?.length === 1 ? node.where[0] : null;
}

/** The slot a single node fills when that slot is empty, or null. */
function liftOne(node: TreeNode, root: TreeRoot, f: Slot): Slot | null {
  const spectraRoot = root === 'spectra';
  const empty = (k: keyof FilterOptions) => {
    const v = f[k];
    return v == null || v === false || (Array.isArray(v) && v.length === 0);
  };
  // a predicate on the objects row, wherever the root puts it
  const objectsPred = (n: TreeNode): PredicateNode | null => {
    if (spectraRoot) {
      const w = soleWhere(n, 'objects');
      return w && isPredicate(w) ? w : null;
    }
    return isPredicate(n) ? n : null;
  };
  const op = objectsPred(node);
  if (op) {
    let p: PredicateNode | null;
    if ((p = inPredicate(op, 'redshift_quality')) && empty('redshift_quality')) {
      const v = ints(p.v); if (v) return { redshift_quality: v };
    }
    if (op.col === 'redshift' && empty('redshift_min') && empty('redshift_max')) {
      const b = bounds(op); if (b) return { redshift_min: b[0], redshift_max: b[1] };
    }
    if (op.col === 'needs_review' && op.op === 'eq' && op.v === true && empty('needs_review')) return { needs_review: true };
    if (op.col === 'has_photometry' && op.op === 'eq' && typeof op.v === 'boolean' && empty('has_photometry')) return { has_photometry: op.v };
    if (!spectraRoot) {
      if ((p = inPredicate(op, 'field')) && empty('fields')) { const v = strings(p.v); if (v) return { fields: v }; }
      if (op.col === 'max_snr' && empty('max_snr_min') && empty('max_snr_max')) { const b = bounds(op); if (b) return { max_snr_min: b[0], max_snr_max: b[1] }; }
      if (op.col === 'max_exposure_time' && empty('max_exposure_time_min') && empty('max_exposure_time_max')) { const b = bounds(op); if (b) return { max_exposure_time_min: b[0], max_exposure_time_max: b[1] }; }
    }
  }
  if (spectraRoot && isPredicate(node)) {
    let p: PredicateNode | null;
    if ((p = inPredicate(node, 'program_slug')) && empty('programs')) { const v = strings(p.v); if (v) return { programs: v }; }
    if ((p = inPredicate(node, 'observation')) && empty('observations')) { const v = strings(p.v); if (v) return { observations: v }; }
    if ((p = inPredicate(node, 'grating')) && empty('gratings')) { const v = strings(p.v); if (v) return { gratings: v, gratings_mode: 'any' }; }
    if (node.col === 'grating' && node.op === 'nin' && empty('gratings')) { const v = strings(node.v); if (v) return { gratings: v, gratings_mode: 'none' }; }
    if (node.col === 'signal_to_noise' && empty('max_snr_min') && empty('max_snr_max')) { const b = bounds(node); if (b) return { max_snr_min: b[0], max_snr_max: b[1] }; }
    if (node.col === 'exposure_time' && empty('max_exposure_time_min') && empty('max_exposure_time_max')) { const b = bounds(node); if (b) return { max_exposure_time_min: b[0], max_exposure_time_max: b[1] }; }
  }
  // exists over targets
  const t = soleWhere(node, 'targets');
  if (t) {
    let p: PredicateNode | null;
    if (!spectraRoot && (p = inPredicate(t, 'program_slug')) && empty('programs')) { const v = strings(p.v); if (v) return { programs: v }; }
    if (!spectraRoot && (p = inPredicate(t, 'observation')) && empty('observations')) { const v = strings(p.v); if (v) return { observations: v }; }
    if (spectraRoot && (p = inPredicate(t, 'field')) && empty('fields')) { const v = strings(p.v); if (v) return { fields: v }; }
    if (spectraRoot && isCone(t) && empty('coordinate_search')) return { coordinate_search: { ra: t.cone.ra, dec: t.cone.dec, radius: t.cone.radius, radius_unit: 'degrees' } };
  }
  if (!spectraRoot && isCone(node) && empty('coordinate_search')) {
    return { coordinate_search: { ra: node.cone.ra, dec: node.cone.dec, radius: node.cone.radius, radius_unit: 'degrees' } };
  }
  // exists over spectra: the gratings pill (objects root)
  if (!spectraRoot && isExists(node) && node.exists === 'spectra' && node.where?.length === 1 && empty('gratings')) {
    const p = inPredicate(node.where[0], 'grating');
    const v = p && strings(p.v);
    if (v) return { gratings: v, gratings_mode: node.not ? 'none' : 'any' };
  }
  // tags
  if (isExists(node) && node.exists === 'object_list_members' && node.where?.length === 1 && empty('list_ids')) {
    const p = inPredicate(node.where[0], 'list_id');
    const v = p && ints(p.v);
    if (v) return { list_ids: v, list_ids_mode: node.not ? 'none' : 'any' };
  }
  // the line pill: line = X [, S/N bounds] [, a fresh fit]
  if (isExists(node) && node.exists === 'lines' && !node.not && node.where && empty('line')) {
    const lineP = node.where.find((w) => isPredicate(w) && w.col === 'line' && w.op === 'eq') as PredicateNode | undefined;
    if (lineP && typeof lineP.v === 'string' && isFilterableLine(lineP.v)) {
      const rest = node.where.filter((w) => w !== lineP);
      let snr: [number | null, number | null] | null = null;
      let fresh = false;
      let ok = true;
      for (const w of rest) {
        if (isPredicate(w) && w.col === 'snr' && snr === null && (snr = bounds(w))) continue;
        if (isExists(w) && w.exists === 'line_fits' && !w.not && w.where?.length === 1 && isPredicate(w.where[0])
            && w.where[0].col === 'stale_redshift' && w.where[0].op === 'eq' && w.where[0].v === false && !fresh) { fresh = true; continue; }
        ok = false; break;
      }
      if (ok) return { line: lineP.v, line_snr_min: snr?.[0] ?? null, line_snr_max: snr?.[1] ?? null, line_include_stale: !fresh };
    }
  }
  // the band pill: band = X [, mag bounds] [, S/N bounds]
  if (isExists(node) && node.exists === 'photometry' && !node.not && node.where && empty('band')) {
    const bandP = node.where.find((w) => isPredicate(w) && w.col === 'band' && w.op === 'eq') as PredicateNode | undefined;
    if (bandP && typeof bandP.v === 'string') {
      let mag: [number | null, number | null] | null = null;
      let snr: [number | null, number | null] | null = null;
      let ok = true;
      for (const w of node.where.filter((w) => w !== bandP)) {
        if (isPredicate(w) && w.col === 'mag' && mag === null && (mag = bounds(w))) continue;
        if (isPredicate(w) && w.col === 'snr' && snr === null && (snr = bounds(w))) continue;
        ok = false; break;
      }
      if (ok) return { band: bandP.v, band_mag_min: mag?.[0] ?? null, band_mag_max: mag?.[1] ?? null, band_snr_min: snr?.[0] ?? null, band_snr_max: snr?.[1] ?? null };
    }
  }
  return null;
}

/** What the tree holds as pills: the lifted slots, the nodes that fed them, and the rest. */
export interface LiftResult {
  filters: FilterOptions;
  /** For each lifted node, its index in the input and the slot keys it filled. */
  lifted: { index: number; keys: (keyof FilterOptions)[] }[];
  residual: TreeNode[];
}

/**
 * The pills a tree holds (phase 3b: the tree is the state, the pills read and
 * write its canonical nodes). Each root node that is one of the pills' own
 * shapes fills that pill when it is still empty; the rest is residual.
 */
export function liftTree(nodes: TreeNode[], root: TreeRoot, base: FilterOptions = DEFAULT_FILTERS): LiftResult {
  let filters: FilterOptions = { ...base, conditions: [] };
  const lifted: LiftResult['lifted'] = [];
  const residual: TreeNode[] = [];
  nodes.forEach((node, index) => {
    const slot = liftOne(node, root, filters);
    if (slot) {
      filters = { ...filters, ...slot };
      lifted.push({ index, keys: Object.keys(slot) as (keyof FilterOptions)[] });
    } else {
      residual.push(node);
    }
  });
  return { filters: { ...filters, conditions: residual }, lifted, residual };
}

/**
 * Conditions a shared link carries, lifted into the pills they belong to
 * (when those pills are empty); what does not lift stays a condition.
 */
export function liftConditions(nodes: TreeNode[], filters: FilterOptions, root: TreeRoot): FilterOptions {
  const { filters: out } = liftTree(nodes, root, filters);
  return { ...out, conditions: [...(filters.conditions ?? []), ...out.conditions] };
}

/**
 * The filter state as the server reads it: whatever of the tree the legacy
 * RPC parameters can carry moves into their slots, so a tree made only of
 * pill shapes takes the legacy path and only a real residual needs the tree.
 */
export function normalizeFilters(filters: Partial<FilterOptions> | undefined, root: TreeRoot): FilterOptions {
  const full: FilterOptions = { ...DEFAULT_FILTERS, ...(filters ?? {}) };
  return liftConditions(full.conditions ?? [], { ...full, conditions: [] }, root);
}

export type { ExistsNode };
