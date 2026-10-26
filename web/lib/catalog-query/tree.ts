/**
 * The predicate tree (Catalog Read Model phase 3): the JSON the portal
 * carries as `?q=`, the API as `q=`, and `catalog_tree_sql` compiles. Three
 * node kinds plus a cone:
 *
 *   { and: [...] } / { or: [...] }                     a group
 *   { col, op, v }                                     a predicate on the table in scope
 *   { exists: <child>, where: [...], not?: true }      a correlated child
 *   { cone: { ra, dec, radius } }                      a sky cone, degrees
 *
 * Everything here is structural: the database is the authority on
 * semantics and refuses what it does not know (SQLSTATE 22023). The
 * validator mirrors the whitelist so a shared link never sends junk, and so
 * the builder can show a node it did not write.
 */
import {
  CATALOGUE, CORRELATIONS, OPS_BY_TYPE, OP_LABEL, TABLE_NOUN, hasSkyPosition, isTreeOp, isTreeTable, leafDef, opArity,
  type LeafDef, type LeafType, type TreeOp, type TreeRoot, type TreeTable,
} from './catalogue';

export type Scalar = number | string | boolean | null;
export type PredicateValue = Scalar | Scalar[];

export interface PredicateNode { col: string; op: TreeOp; v?: PredicateValue }
export interface ExistsNode { exists: TreeTable; where?: TreeNode[]; not?: boolean }
export interface AndNode { and: TreeNode[] }
export interface OrNode { or: TreeNode[] }
export interface ConeNode { cone: { ra: number; dec: number; radius: number } }
export type TreeNode = PredicateNode | ExistsNode | AndNode | OrNode | ConeNode;

/** The limits catalog_tree_sql enforces. */
export const MAX_TREE_BYTES = 20000;
export const MAX_GROUP_SIZE = 50;
export const MAX_TREE_DEPTH = 6;

export const isAnd = (n: TreeNode): n is AndNode => 'and' in n;
export const isOr = (n: TreeNode): n is OrNode => 'or' in n;
export const isGroup = (n: TreeNode): n is AndNode | OrNode => isAnd(n) || isOr(n);
export const isExists = (n: TreeNode): n is ExistsNode => 'exists' in n;
export const isCone = (n: TreeNode): n is ConeNode => 'cone' in n;
export const isPredicate = (n: TreeNode): n is PredicateNode => 'col' in n;

function isRecord(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null && !Array.isArray(x);
}

function scalarOk(v: unknown, type: LeafType): v is Scalar {
  switch (type) {
    case 'num': return typeof v === 'number' && Number.isFinite(v);
    case 'int': case 'bits': return typeof v === 'number' && Number.isInteger(v);
    case 'text': case 'ts': return typeof v === 'string';
    case 'bool': return typeof v === 'boolean';
    case 'text[]': return typeof v === 'string';
    case 'int[]': return typeof v === 'number' && Number.isInteger(v);
  }
}

function valueOk(v: unknown, op: TreeOp, type: LeafType): v is PredicateValue {
  switch (opArity(op)) {
    case 'none': return v === undefined || v === null;
    case 'one': return scalarOk(v, type);
    case 'pair':
      return Array.isArray(v) && v.length === 2 && !(v[0] === null && v[1] === null)
        && v.every((x) => x === null || scalarOk(x, type));
    case 'many': return Array.isArray(v) && v.length > 0 && v.every((x) => scalarOk(x, type));
  }
}

/**
 * The node `x` is, validated against `table`, as a clean copy (recognised
 * keys only, a `table.col` prefix stripped), or null when it is not one.
 */
export function validateNode(x: unknown, table: TreeTable, depth = 0): TreeNode | null {
  if (!isRecord(x) || depth > MAX_TREE_DEPTH) return null;
  if ('and' in x || 'or' in x) {
    const key = 'and' in x ? 'and' : 'or';
    const raw = x[key];
    if (!Array.isArray(raw) || raw.length === 0 || raw.length > MAX_GROUP_SIZE) return null;
    const children: TreeNode[] = [];
    for (const c of raw) {
      const node = validateNode(c, table, depth + 1);
      if (!node) return null;
      children.push(node);
    }
    return key === 'and' ? { and: children } : { or: children };
  }
  if ('cone' in x) {
    const c = x.cone;
    if (!hasSkyPosition(table) || !isRecord(c)) return null;
    const { ra, dec, radius } = c;
    if (![ra, dec, radius].every((v) => typeof v === 'number' && Number.isFinite(v))) return null;
    if ((radius as number) <= 0 || (radius as number) > 10) return null;
    return { cone: { ra: ra as number, dec: dec as number, radius: radius as number } };
  }
  if ('exists' in x) {
    const child = x.exists;
    if (!isTreeTable(child) || !CORRELATIONS[table].includes(child)) return null;
    const node: ExistsNode = { exists: child };
    if (x.where !== undefined) {
      if (!Array.isArray(x.where) || x.where.length > MAX_GROUP_SIZE) return null;
      const where: TreeNode[] = [];
      for (const w of x.where) {
        const n = validateNode(w, child, depth + 1);
        if (!n) return null;
        where.push(n);
      }
      if (where.length > 0) node.where = where;
    }
    if (x.not !== undefined) {
      if (typeof x.not !== 'boolean') return null;
      if (x.not) node.not = true;
    }
    return node;
  }
  if ('col' in x && 'op' in x) {
    const rawCol = x.col;
    if (typeof rawCol !== 'string') return null;
    let col: string = rawCol;
    if (col.includes('.')) {
      const dot = col.indexOf('.');
      if (col.slice(0, dot) !== table) return null;
      col = col.slice(dot + 1);
    }
    const leaf = leafDef(table, col);
    if (!leaf) return null;
    const op = x.op;
    if (!isTreeOp(op) || !OPS_BY_TYPE[leaf.type].includes(op)) return null;
    if (!valueOk(x.v, op, leaf.type)) return null;
    const node: PredicateNode = { col, op };
    if (opArity(op) !== 'none') node.v = x.v;
    return node;
  }
  return null;
}

/** Every node of `nodes` validated against `root`, or null if any is not one. */
export function validateNodes(nodes: unknown, root: TreeTable): TreeNode[] | null {
  if (!Array.isArray(nodes)) return null;
  const out: TreeNode[] = [];
  for (const n of nodes) {
    const v = validateNode(n, root);
    if (!v) return null;
    out.push(v);
  }
  return out;
}

/**
 * The conditions a `?q=` carries: a single node, or the children of a root
 * `and` group (its optional version key ignored). Null for anything else.
 */
export function parseConditions(raw: string | null | undefined, root: TreeRoot): TreeNode[] | null {
  if (!raw || raw.length > MAX_TREE_BYTES) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isRecord(parsed)) return null;
  if ('and' in parsed && !('or' in parsed)) {
    const { v, ...rest } = parsed;
    if (v !== undefined && v !== 1 && v !== '1') return null;
    const group = validateNode(rest, root);
    return group && isAnd(group) ? group.and : null;
  }
  const node = validateNode(parsed, root);
  return node ? [node] : null;
}

/** The `?q=` text for these conditions: one node bare, several as an `and`. */
export function encodeConditions(nodes: TreeNode[]): string | null {
  if (nodes.length === 0) return null;
  return JSON.stringify(nodes.length === 1 ? nodes[0] : { and: nodes });
}

/** The tree the RPCs take: a versioned root group, or null when empty. */
export function treeOf(nodes: TreeNode[]): (AndNode & { v: 1 }) | null {
  return nodes.length === 0 ? null : { v: 1, and: nodes };
}

/**
 * The same conditions read against another root (the view switched between
 * objects and spectra): a predicate or cone on the old root becomes an
 * exists over it, an exists over the new root flattens into the root itself,
 * and every other exists is kept where the new root correlates to it.
 */
export function rerootNodes(nodes: TreeNode[], from: TreeRoot, to: TreeRoot): TreeNode[] {
  if (from === to) return nodes;
  return nodes.flatMap((n) => rerootNode(n, from, to));
}

function rerootNode(node: TreeNode, from: TreeRoot, to: TreeRoot): TreeNode[] {
  if (isAnd(node)) return [{ and: rerootNodes(node.and, from, to) }];
  if (isOr(node)) {
    // every arm must stay one node
    return [{ or: node.or.map((c) => { const r = rerootNode(c, from, to); return r.length === 1 ? r[0] : { and: r }; }) }];
  }
  if (isExists(node)) {
    if (node.exists === to && !node.not) return node.where ?? [];
    if (CORRELATIONS[to].includes(node.exists)) return [node];
  }
  return [{ exists: from, where: [node] }];
}

// ---------------------------------------------------------------- reading the tree

/** Depth-first walk over every node, children after their parent. */
export function* walkNodes(nodes: TreeNode[], table: TreeTable): Generator<{ node: TreeNode; table: TreeTable }> {
  for (const node of nodes) {
    yield { node, table };
    if (isGroup(node)) yield* walkNodes(isAnd(node) ? node.and : node.or, table);
    else if (isExists(node) && node.where) yield* walkNodes(node.where, node.exists);
  }
}

function wherePredicate(node: ExistsNode, col: string): PredicateNode | undefined {
  return node.where?.find((w): w is PredicateNode => isPredicate(w) && w.col === col);
}

/** The first line an exists-over-lines condition names (`line = X`). */
export function firstLineOf(nodes: TreeNode[], root: TreeTable): string | null {
  for (const { node } of walkNodes(nodes, root)) {
    if (isExists(node) && node.exists === 'lines' && !node.not) {
      const p = wherePredicate(node, 'line');
      if (p && p.op === 'eq' && typeof p.v === 'string') return p.v;
    }
  }
  return null;
}

/** The first band an exists-over-photometry condition names (`band = X`). */
export function firstBandOf(nodes: TreeNode[], root: TreeTable): string | null {
  for (const { node } of walkNodes(nodes, root)) {
    if (isExists(node) && node.exists === 'photometry' && !node.not) {
      const p = wherePredicate(node, 'band');
      if (p && p.op === 'eq' && typeof p.v === 'string') return p.v;
    }
  }
  return null;
}

/** The first cone's centre, the distance reference for the distance sort. */
export function firstConeOf(nodes: TreeNode[], root: TreeTable): { ra: number; dec: number; radius: number } | null {
  for (const { node } of walkNodes(nodes, root)) {
    if (isCone(node)) return node.cone;
  }
  return null;
}

// ---------------------------------------------------------------- describing the tree

export interface DescribeContext {
  /** Display names for choice values: a line's label, a tag's name, a program's name. */
  valueLabel?: (leaf: LeafDef, value: Scalar) => string | undefined;
}

function fmtScalar(v: Scalar, leaf: LeafDef | undefined, ctx: DescribeContext): string {
  if (v === null) return 'null';
  if (leaf && ctx.valueLabel) {
    const l = ctx.valueLabel(leaf, v);
    if (l) return l;
  }
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : String(+v.toPrecision(6));
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  return v;
}

/** One line of prose for a node, as the builder shows it. */
export function describeNode(node: TreeNode, table: TreeTable, ctx: DescribeContext = {}): string {
  if (isAnd(node)) return node.and.map((c) => describeNode(c, table, ctx)).join(' and ');
  if (isOr(node)) return 'any of: ' + node.or.map((c) => describeNode(c, table, ctx)).join('; ');
  if (isCone(node)) {
    const r = node.cone.radius;
    const radius = r >= 1 ? `${+r.toPrecision(4)}°` : r * 60 >= 1 ? `${+(r * 60).toPrecision(4)}′` : `${+(r * 3600).toPrecision(4)}″`;
    return `within ${radius} of ${node.cone.ra.toFixed(5)}, ${node.cone.dec.toFixed(5)}`;
  }
  if (isExists(node)) {
    const noun = TABLE_NOUN[node.exists];
    const head = node.not ? negateExists(noun.exists) : noun.exists;
    if (!node.where || node.where.length === 0) return head.replace(/ where$/, '');
    return `${head} ${node.where.map((w) => describeNode(w, node.exists, ctx)).join(' and ')}`;
  }
  const leaf = leafDef(table, node.col);
  const label = leaf?.label ?? node.col;
  const unit = leaf?.unit && leaf.type !== 'text' ? ` ${leaf.unit}` : '';
  const op = OP_LABEL[node.op];
  switch (opArity(node.op)) {
    case 'none': return `${label} ${op}`;
    case 'pair': {
      const [lo, hi] = node.v as Scalar[];
      if (lo === null) return `${label} ≤ ${fmtScalar(hi, leaf, ctx)}${unit}`;
      if (hi === null) return `${label} ≥ ${fmtScalar(lo, leaf, ctx)}${unit}`;
      return `${label} ${fmtScalar(lo, leaf, ctx)} – ${fmtScalar(hi, leaf, ctx)}${unit}`;
    }
    case 'many': return `${label} ${op} ${(node.v as Scalar[]).map((v) => fmtScalar(v, leaf, ctx)).join(', ')}`;
    default: return `${label} ${op} ${fmtScalar(node.v as Scalar, leaf, ctx)}${unit}`;
  }
}

/** "has a line where" → "has no line where", "is in a tag where" → "is in no tag where". */
export function negateExists(phrase: string): string {
  return phrase.replace(/^has (an? )?/, 'has no ').replace(/^is in a /, 'is in no ');
}

/** The leaf a predicate names, for callers that render values. */
export function predicateLeaf(node: PredicateNode, table: TreeTable): LeafDef | undefined {
  return leafDef(table, node.col);
}

export { CATALOGUE, CORRELATIONS, OP_LABEL, OPS_BY_TYPE, TABLE_NOUN, opArity };
export type { LeafDef, LeafType, TreeOp, TreeRoot, TreeTable };
