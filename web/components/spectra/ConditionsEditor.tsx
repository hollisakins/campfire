'use client';

/**
 * The live predicate tree of the Advanced modal (Catalog Read Model phase
 * 3b). The tree is the filter state: every pill drops a node into it, and
 * every node is edited here in place — a property of the row, an exists
 * over a child table ("has a line where …", negated as "has no line"), an
 * OR/AND group, a cone. Everything written is one of
 * lib/catalog-query/tree.ts's node shapes, so the URL, the API and the
 * database read it unchanged.
 */
import React from 'react';
import { X } from 'lucide-react';
import {
  CATALOGUE, CORRELATIONS, OPS_BY_TYPE, OP_LABEL, TABLE_NOUN, hasSkyPosition, opArity,
  type LeafChoices, type LeafDef, type TreeOp, type TreeRoot, type TreeTable,
} from '@/lib/catalog-query/catalogue';
import {
  isAnd, isCone, isExists, isPredicate, negateExists,
  type AndNode, type ConeNode, type ExistsNode, type OrNode, type PredicateNode, type Scalar, type TreeNode,
} from '@/lib/catalog-query/tree';
import { DQ_FLAGS, LINE_FLAGS, REDSHIFT_QUALITY } from '@/lib/flags';
import { GRATINGS } from '@/lib/types';
import { LINE_FILTER_OPTIONS } from '@/lib/linelist';

export interface ChoiceOption { value: string | number; label: string }

/** The option lists the page carries for the choice-valued leaves. */
export interface ConditionChoices {
  programs: ChoiceOption[];
  observations: ChoiceOption[];
  fields: ChoiceOption[];
  bands: ChoiceOption[];
  lists: ChoiceOption[];
}

const INPUT = 'px-2 py-1 text-sm border border-border rounded-md bg-background text-text-primary placeholder:text-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/40 focus:border-transparent';
const SELECT = `${INPUT} pr-6`;
const ADD = 'px-2 py-1 text-xs rounded-md border border-dashed border-border text-text-secondary hover:text-text-primary hover:border-text-secondary bg-transparent cursor-pointer';

const DEFAULT_COLUMN: Record<TreeTable, string> = {
  objects: 'redshift', targets: 'max_snr', spectra: 'signal_to_noise', lines: 'snr', line_fits: 'n_detected',
  photometry: 'mag', catalog_matches: 'match_distance_arcsec', object_list_members: 'list_id',
};

function defaultOp(leaf: LeafDef): TreeOp {
  switch (leaf.type) {
    case 'num': case 'int': case 'ts': return 'gte';
    case 'bool': return 'eq';
    case 'bits': return 'anybits';
    case 'text[]': case 'int[]': return 'has';
    default: return leaf.choices ? 'eq' : 'ilike';
  }
}

function defaultValue(leaf: LeafDef, op: TreeOp, choices: ConditionChoices): PredicateNode['v'] {
  const arity = opArity(op);
  if (arity === 'none') return undefined;
  if (arity === 'pair') return [null, null];
  const first = choiceOptions(leaf, choices)[0]?.value;
  const one: Scalar = leaf.type === 'bool' ? true
    : leaf.type === 'bits' ? 0
    : leaf.type === 'num' || leaf.type === 'int' || leaf.type === 'int[]' ? (typeof first === 'number' ? first : 0)
    : op === 'ilike' ? '%'
    : (typeof first === 'string' ? first : '');
  return arity === 'many' ? [one] : one;
}

export function defaultPredicate(table: TreeTable, choices: ConditionChoices, col = DEFAULT_COLUMN[table]): PredicateNode {
  const leaf = CATALOGUE[table][col];
  const op = defaultOp(leaf);
  const v = defaultValue(leaf, op, choices);
  return v === undefined ? { col, op } : { col, op, v };
}

/** The pick list for a choice-valued leaf, from the catalogue or the page. */
function choiceOptions(leaf: LeafDef, choices: ConditionChoices): ChoiceOption[] {
  switch (leaf.choices) {
    case 'line': return LINE_FILTER_OPTIONS.map((o) => ({ value: o.value, label: o.label }));
    case 'band': return choices.bands;
    case 'grating': return GRATINGS.map((g) => ({ value: g, label: g }));
    case 'program': return choices.programs;
    case 'observation': return choices.observations;
    case 'field': return choices.fields;
    case 'quality': return REDSHIFT_QUALITY.map((q) => ({ value: q.value, label: q.label }));
    case 'component': return ['narrow', 'broad', 'doublet'].map((c) => ({ value: c, label: c }));
    case 'deploy_status': return ['published', 'draft', 'revoked'].map((c) => ({ value: c, label: c }));
    case 'list': return choices.lists;
    case 'dq_flags': return DQ_FLAGS.map((f) => ({ value: f.value, label: f.label }));
    case 'line_flags': return LINE_FLAGS.map((f) => ({ value: f.value, label: f.label }));
    default: return [];
  }
}

const BITS_CHOICES: LeafChoices[] = ['dq_flags', 'line_flags'];

// ---------------------------------------------------------------- values

function parseScalar(raw: string, leaf: LeafDef): Scalar | undefined {
  const s = raw.trim();
  if (leaf.type === 'num' || leaf.type === 'int' || leaf.type === 'int[]' || leaf.type === 'bits') {
    if (s === '') return undefined;
    const n = Number(s);
    if (!Number.isFinite(n)) return undefined;
    return leaf.type === 'num' ? n : Math.trunc(n);
  }
  if (leaf.type === 'bool') return s === 'true';
  return s;
}

/** "words" shown for a `%words%` pattern, and back. */
function patternToText(v: Scalar | undefined): string {
  return typeof v === 'string' ? v.replace(/^%/, '').replace(/%$/, '') : '';
}
function textToPattern(s: string): string {
  const t = s.trim();
  return t.includes('%') || t.includes('_') ? t : `%${t}%`;
}

function ScalarInput({ leaf, value, onChange, choices, placeholder, pattern = false }: {
  leaf: LeafDef; value: Scalar | undefined; onChange: (v: Scalar | undefined) => void; choices: ConditionChoices; placeholder?: string;
  /** The value is an ILIKE pattern: shown without its % wrapper, stored with one. */
  pattern?: boolean;
}) {
  const options = choiceOptions(leaf, choices);
  if (leaf.type === 'bool') {
    return (
      <select className={SELECT} value={value === false ? 'false' : 'true'} onChange={(e) => onChange(e.target.value === 'true')}>
        <option value="true">yes</option>
        <option value="false">no</option>
      </select>
    );
  }
  if (options.length > 0 && leaf.type !== 'bits' && !pattern) {
    const known = options.some((o) => String(o.value) === String(value));
    return (
      <select className={SELECT} value={known ? String(value) : ''}
        onChange={(e) => { const o = options.find((x) => String(x.value) === e.target.value); onChange(o ? o.value : undefined); }}>
        {!known && <option value="">{value === undefined || value === null || value === '' ? 'choose…' : String(value)}</option>}
        {options.map((o) => <option key={String(o.value)} value={String(o.value)}>{o.label}</option>)}
      </select>
    );
  }
  return <FreeScalarInput leaf={leaf} value={value} onChange={onChange} placeholder={placeholder} pattern={pattern} />;
}

/** A typed free input; the value leaves on blur or Enter (remounted on a new value). */
function FreeScalarInput({ leaf, value, onChange, placeholder, pattern = false }: {
  leaf: LeafDef; value: Scalar | undefined; onChange: (v: Scalar | undefined) => void; placeholder?: string; pattern?: boolean;
}) {
  const numeric = leaf.type === 'num' || leaf.type === 'int' || leaf.type === 'int[]' || leaf.type === 'bits';
  const shown = pattern ? patternToText(value) : value == null ? '' : String(value);
  return (
    <input
      key={shown}
      type={numeric ? 'number' : 'text'}
      step={leaf.type === 'num' ? 'any' : 1}
      className={`${INPUT} ${numeric ? 'w-24' : 'w-40'}`}
      defaultValue={shown}
      placeholder={placeholder ?? (pattern ? 'contains…' : leaf.type === 'ts' ? '2026-01-01' : leaf.unit ?? '')}
      onBlur={(e) => onChange(pattern ? textToPattern(e.target.value) : parseScalar(e.target.value, leaf))}
      onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
    />
  );
}

/** A set of toggles for a bitmask or a multi-valued choice. */
function PickMany({ options, selected, onChange }: { options: ChoiceOption[]; selected: Set<string>; onChange: (next: Set<string>) => void }) {
  return (
    <span className="inline-flex flex-wrap gap-1">
      {options.map((o) => {
        const on = selected.has(String(o.value));
        return (
          <button key={String(o.value)} type="button"
            onClick={() => { const next = new Set(selected); if (on) next.delete(String(o.value)); else next.add(String(o.value)); onChange(next); }}
            className={`px-2 py-0.5 text-xs rounded-full border transition-colors ${on ? 'bg-primary/10 border-primary text-primary' : 'border-border text-text-secondary hover:border-text-secondary'}`}>
            {o.label}
          </button>
        );
      })}
    </span>
  );
}

function ValueEditor({ leaf, op, value, onChange, choices }: {
  leaf: LeafDef; op: TreeOp; value: PredicateNode['v']; onChange: (v: PredicateNode['v']) => void; choices: ConditionChoices;
}) {
  const arity = opArity(op);
  if (arity === 'none') return null;
  if (arity === 'pair') {
    const [lo, hi] = Array.isArray(value) && value.length === 2 ? (value as Scalar[]) : [null, null];
    return (
      <span className="inline-flex items-center gap-1">
        <ScalarInput leaf={leaf} value={lo ?? undefined} choices={choices} placeholder="min" onChange={(v) => onChange([v ?? null, hi])} />
        <span className="text-text-tertiary">–</span>
        <ScalarInput leaf={leaf} value={hi ?? undefined} choices={choices} placeholder="max" onChange={(v) => onChange([lo, v ?? null])} />
        {leaf.unit && <span className="text-xs text-text-tertiary">{leaf.unit}</span>}
      </span>
    );
  }
  if (leaf.type === 'bits' && leaf.choices && BITS_CHOICES.includes(leaf.choices)) {
    const options = choiceOptions(leaf, choices);
    const mask = typeof value === 'number' ? value : 0;
    const selected = new Set(options.filter((o) => (mask & Number(o.value)) !== 0).map((o) => String(o.value)));
    return <PickMany options={options} selected={selected} onChange={(next) => onChange([...next].reduce((m, v) => m | Number(v), 0))} />;
  }
  if (arity === 'many') {
    const options = choiceOptions(leaf, choices);
    const values = Array.isArray(value) ? (value as Scalar[]) : [];
    if (options.length > 0) {
      const selected = new Set(values.map(String));
      return <PickMany options={options} selected={selected}
        onChange={(next) => onChange(options.filter((o) => next.has(String(o.value))).map((o) => o.value))} />;
    }
    return (
      <input key={values.join(',')} type="text" className={`${INPUT} w-56`} defaultValue={values.join(', ')} placeholder="a, b, c"
        onBlur={(e) => onChange(e.target.value.split(',').map((s) => parseScalar(s, leaf)).filter((s): s is Scalar => s !== undefined && s !== ''))}
        onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }} />
    );
  }
  return (
    <span className="inline-flex items-center gap-1">
      <ScalarInput leaf={leaf} value={value as Scalar | undefined} choices={choices} onChange={(v) => onChange(v)} pattern={op === 'ilike'} />
      {leaf.unit && leaf.type !== 'text' && <span className="text-xs text-text-tertiary">{leaf.unit}</span>}
    </span>
  );
}

// ---------------------------------------------------------------- nodes

function PredicateRow({ node, table, onChange, choices }: {
  node: PredicateNode; table: TreeTable; onChange: (n: PredicateNode) => void; choices: ConditionChoices;
}) {
  const columns = CATALOGUE[table];
  const leaf = columns[node.col] ?? columns[DEFAULT_COLUMN[table]];
  const ops = OPS_BY_TYPE[leaf.type];
  const setColumn = (col: string) => {
    const next = columns[col];
    const op = OPS_BY_TYPE[next.type].includes(node.op) ? node.op : defaultOp(next);
    const v = defaultValue(next, op, choices);
    onChange(v === undefined ? { col, op } : { col, op, v });
  };
  const setOp = (op: TreeOp) => {
    const sameShape = opArity(op) === opArity(node.op) && opArity(op) !== 'none' && !(op === 'ilike' || node.op === 'ilike');
    const v = sameShape ? node.v : defaultValue(leaf, op, choices);
    onChange(v === undefined ? { col: node.col, op } : { col: node.col, op, v });
  };
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <select className={`${SELECT} font-medium`} value={node.col in columns ? node.col : DEFAULT_COLUMN[table]} onChange={(e) => setColumn(e.target.value)}>
        {Object.entries(columns).map(([col, def]) => <option key={col} value={col}>{def.label}</option>)}
      </select>
      <select className={SELECT} value={node.op} onChange={(e) => setOp(e.target.value as TreeOp)}>
        {ops.map((op) => <option key={op} value={op}>{OP_LABEL[op]}</option>)}
      </select>
      <ValueEditor leaf={leaf} op={node.op} value={node.v} choices={choices}
        onChange={(v) => onChange(v === undefined ? { col: node.col, op: node.op } : { col: node.col, op: node.op, v })} />
    </span>
  );
}

function ConeRow({ node, onChange }: { node: ConeNode; onChange: (n: ConeNode) => void }) {
  const num = (v: number, set: (n: number) => void, w = 'w-28') => (
    <input type="number" step="any" className={`${INPUT} ${w}`} defaultValue={v} key={v}
      onBlur={(e) => { const n = Number(e.target.value); if (Number.isFinite(n)) set(n); }}
      onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }} />
  );
  const c = node.cone;
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5 text-sm text-text-secondary">
      <span className="font-medium text-text-primary">within</span>
      {num(+(c.radius * 60).toPrecision(6), (arcmin) => onChange({ cone: { ...c, radius: Math.min(10, Math.max(1e-6, arcmin / 60)) } }), 'w-20')}
      <span className="text-xs text-text-tertiary">arcmin of</span>
      RA {num(c.ra, (ra) => onChange({ cone: { ...c, ra } }))}
      Dec {num(c.dec, (dec) => onChange({ cone: { ...c, dec } }))}
    </span>
  );
}

/** A native select styled as a button: never clipped by a scrolling parent. */
function AddSelect({ table, onAdd, choices, label, allowGroups = true, className = ADD }: {
  table: TreeTable; onAdd: (n: TreeNode) => void; choices: ConditionChoices; label: string; allowGroups?: boolean; className?: string;
}) {
  return (
    <select className={className} value="" onChange={(e) => {
      const v = e.target.value;
      if (v === 'prop') onAdd(defaultPredicate(table, choices));
      else if (v.startsWith('exists:')) { const child = v.slice(7) as TreeTable; onAdd({ exists: child, where: [defaultPredicate(child, choices)] }); }
      else if (v === 'cone') onAdd({ cone: { ra: 150.1, dec: 2.2, radius: 1 / 60 } });
      else if (v === 'or') onAdd({ or: [defaultPredicate(table, choices), defaultPredicate(table, choices)] });
      else if (v === 'and') onAdd({ and: [defaultPredicate(table, choices), defaultPredicate(table, choices)] });
    }}>
      <option value="">{label}</option>
      <option value="prop">A property of the {TABLE_NOUN[table].singular}</option>
      {CORRELATIONS[table].map((child) => (
        <option key={child} value={`exists:${child}`}>{TABLE_NOUN[child].exists.replace(/ where$/, '')} …</option>
      ))}
      {hasSkyPosition(table) && <option value="cone">Within a cone on the sky</option>}
      {allowGroups && <option value="or">Any of … (OR)</option>}
      {allowGroups && <option value="and">All of … (AND)</option>}
    </select>
  );
}

function Remove({ onClick }: { onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} className="shrink-0 p-1 rounded text-text-tertiary hover:text-text-primary hover:bg-card-hover" aria-label="Remove">
      <X className="w-3.5 h-3.5" />
    </button>
  );
}

function NodeList({ nodes, table, onChange, choices, joiner, depth }: {
  nodes: TreeNode[]; table: TreeTable; onChange: (nodes: TreeNode[]) => void; choices: ConditionChoices; joiner: 'and' | 'or'; depth: number;
}) {
  const replace = (i: number, n: TreeNode) => onChange(nodes.map((x, j) => (j === i ? n : x)));
  const remove = (i: number) => onChange(nodes.filter((_, j) => j !== i));
  return (
    <div className="space-y-1.5">
      {nodes.map((node, i) => (
        <div key={i} className="flex items-start gap-2">
          <span className="w-8 shrink-0 pt-1.5 text-[11px] uppercase tracking-wider text-text-tertiary text-right">{i === 0 ? '' : joiner}</span>
          <div className="flex-1 min-w-0">
            <NodeEditor node={node} table={table} onChange={(n) => replace(i, n)} choices={choices} depth={depth} />
          </div>
          <Remove onClick={() => remove(i)} />
        </div>
      ))}
    </div>
  );
}

function NodeEditor({ node, table, onChange, choices, depth }: {
  node: TreeNode; table: TreeTable; onChange: (n: TreeNode) => void; choices: ConditionChoices; depth: number;
}) {
  if (isPredicate(node)) return <PredicateRow node={node} table={table} onChange={onChange} choices={choices} />;
  if (isCone(node)) return <ConeRow node={node} onChange={onChange} />;
  if (isExists(node)) return <ExistsEditor node={node} onChange={onChange} choices={choices} depth={depth} />;
  const group = node as AndNode | OrNode;
  const children = isAnd(group) ? group.and : group.or;
  const joiner = isAnd(group) ? 'and' : 'or';
  const set = (next: TreeNode[]) => onChange(isAnd(group) ? { and: next } : { or: next });
  return (
    <div className="rounded-lg border border-border bg-surface-2/40 p-2">
      <div className="flex items-center justify-between gap-2 mb-1.5">
        <select className={`${SELECT} font-medium`} value={joiner} onChange={(e) => onChange(e.target.value === 'and' ? { and: children } : { or: children })}>
          <option value="and">all of</option>
          <option value="or">any of</option>
        </select>
        {depth < 4 && <AddSelect table={table} choices={choices} label="+ Add" onAdd={(n) => set([...children, n])} />}
      </div>
      <NodeList nodes={children} table={table} onChange={set} choices={choices} joiner={joiner} depth={depth + 1} />
    </div>
  );
}

function ExistsEditor({ node, onChange, choices, depth }: {
  node: ExistsNode; onChange: (n: ExistsNode) => void; choices: ConditionChoices; depth: number;
}) {
  const where = node.where ?? [];
  const set = (next: TreeNode[]) => {
    const out: ExistsNode = { exists: node.exists };
    if (next.length > 0) out.where = next;
    if (node.not) out.not = true;
    onChange(out);
  };
  const noun = TABLE_NOUN[node.exists].exists.replace(/ where$/, '');
  // simple clauses (predicates, cones) read inline; anything nested below
  const inline = where.filter((w) => isPredicate(w) || isCone(w));
  const nested = where.filter((w) => !(isPredicate(w) || isCone(w)));
  const replaceWhere = (target: TreeNode, next: TreeNode) => set(where.map((w) => (w === target ? next : w)));
  const removeWhere = (target: TreeNode) => set(where.filter((w) => w !== target));
  return (
    <div className={`rounded-lg border border-border p-2 ${node.not ? 'bg-red-500/5' : 'bg-surface-2/40'}`}>
      <div className="flex flex-wrap items-center gap-1.5">
        <select className={`${SELECT} font-medium`} value={node.not ? 'not' : 'has'}
          onChange={(e) => { const out: ExistsNode = { exists: node.exists, ...(where.length ? { where } : {}) }; if (e.target.value === 'not') out.not = true; onChange(out); }}>
          <option value="has">{noun}</option>
          <option value="not">{negateExists(noun)}</option>
        </select>
        {where.length > 0 && <span className="text-sm text-text-secondary">where</span>}
        {inline.map((w, i) => (
          <React.Fragment key={i}>
            {i > 0 && <span className="text-[11px] uppercase tracking-wider text-text-tertiary">and</span>}
            <span className="inline-flex items-center gap-0.5">
              {isCone(w)
                ? <ConeRow node={w} onChange={(n) => replaceWhere(w, n)} />
                : <PredicateRow node={w as PredicateNode} table={node.exists} onChange={(n) => replaceWhere(w, n)} choices={choices} />}
              <Remove onClick={() => removeWhere(w)} />
            </span>
          </React.Fragment>
        ))}
        {depth < 4 && <AddSelect table={node.exists} choices={choices} label="+ and" onAdd={(n) => set([...where, n])} />}
      </div>
      {nested.length > 0 && (
        <div className="mt-1.5 pl-2">
          <NodeList nodes={nested} table={node.exists} onChange={(next) => set([...inline, ...next])} choices={choices} joiner="and" depth={depth + 1} />
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- the tree

export interface TreeEditorProps {
  root: TreeRoot;
  conditions: TreeNode[];
  onChange: (conditions: TreeNode[]) => void;
  choices: ConditionChoices;
}

/** The whole tree, its root nodes ANDed. */
export function TreeEditor({ root, conditions, onChange, choices }: TreeEditorProps) {
  return (
    <div>
      {conditions.length > 0 ? (
        <NodeList nodes={conditions} table={root} onChange={onChange} choices={choices} joiner="and" depth={0} />
      ) : (
        <p className="text-sm text-text-tertiary">No conditions. Every {TABLE_NOUN[root].singular} the catalog shows you.</p>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <AddSelect table={root} choices={choices} label="+ Add condition" onAdd={(n) => onChange([...conditions, n])} />
      </div>
    </div>
  );
}

export { AddSelect, choiceOptions };
