'use client';

/**
 * The cards of the filter UI (Catalog Read Model phase 3b, design doc
 * Figures 6 to 8): chips for a noun's properties, rows for its measurements,
 * one card per spectrum, "any of" groups, and an advanced block for whatever
 * the vocabulary cannot show. Everything reads from and writes to the tree
 * in `?q=` through lib/catalog-query/vocabulary.ts; nothing here keeps state
 * of its own beyond what a half-built chip needs.
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, X } from 'lucide-react';
import { FilterChip, type FilterOption } from '@/components/ui/FilterChip';
import { RangeFilterChip } from '@/components/ui/RangeFilterChip';
import { CoordinateSearchChip, type CoordinateSearchValue } from '@/components/ui/CoordinateSearchChip';
import { Popover, isInsidePopover } from '@/components/ui/Popover';
import { convertRadiusToDegrees } from '@/lib/utils/coordinate-parser';
import { describeNode, isExists, type TreeNode, type TreeRoot } from '@/lib/catalog-query/tree';
import {
  MEASUREMENTS, appendAt, compileMeasurement, compileProperty, describeItem, promoteImplicit, propertiesOf, replaceAt, setNegated,
  type AdvancedItem, type AnyOfItem, type FlagsMode, type Item, type MeasurementItem, type MeasurementKind, type Noun, type Path,
  type PropertyDef, type PropertyItem, type PropertyValue, type SpectrumItem, type ValueLabel,
} from '@/lib/catalog-query/vocabulary';
import { LINE_OPTIONS, optionsFor, type FilterChoices } from './filter-choices';

// ---------------------------------------------------------------- the editing context

/** Where a new node goes and against which table it is written. */
export interface Level {
  table: 'objects' | 'spectra';
  /** The container: the top level (empty) or a Spectrum card. */
  path: Path;
  /** The card is a bare row on the object: a new node makes it a card. */
  implicit?: boolean;
  /** At the spectra root, the spectrum's object: a new card is a sibling spectrum of it. */
  itsObject?: boolean;
}

export interface CardsContext {
  root: TreeRoot;
  nodes: TreeNode[];
  onChange: (nodes: TreeNode[]) => void;
  choices: FilterChoices;
  label: ValueLabel;
  showLine: string | null;
  showBand: string | null;
  onShowColumn: (kind: MeasurementKind, key: string | null) => void;
  /** Property keys the cards do not offer here (the map has its own position tools). */
  hideKeys?: string[];
}

function addToLevel(nodes: TreeNode[], level: Level, node: TreeNode): TreeNode[] {
  if (level.implicit) return promoteImplicit(nodes, level.path, node);
  if (level.itsObject && isExists(node) && node.exists === 'spectra') return appendAt(nodes, [], { exists: 'objects', where: [node] });
  return appendAt(nodes, level.path, node);
}

const CHIP = 'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm font-medium border transition-all duration-150';
const CHIP_IDLE = 'bg-card border-border text-text-secondary hover:border-text-secondary dark:hover:border-border-strong hover:text-text-primary';
const CHIP_ON = 'bg-primary/10 border-primary text-primary';
const ADD = `${CHIP} border-dashed bg-transparent text-text-secondary hover:text-text-primary hover:border-text-secondary cursor-pointer`;
const ADD_SELECT = `${ADD} appearance-none pr-7 bg-[length:12px] bg-[right_10px_center] bg-no-repeat bg-[url('data:image/svg+xml;utf8,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 24 24%22 fill=%22none%22 stroke=%22%2394a3b8%22 stroke-width=%222.5%22><path d=%22m6 9 6 6 6-6%22/></svg>')]`;
const INPUT = 'px-2 py-1 text-sm border border-border rounded-md bg-background text-text-primary placeholder:text-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/40 focus:border-transparent';
const SMALL_SELECT = `${INPUT} pr-6 py-0.5 text-xs`;
const EYEBROW = 'text-[11px] font-semibold uppercase tracking-wider text-text-tertiary';

const SEARCHABLE = new Set(['program', 'field', 'observation', 'list', 'line']);

// ---------------------------------------------------------------- chips

/** A summary chip: an item the bar or a group cannot edit in place. */
export function ItemChip({ text, onClick, onRemove, tone = 'default', title }: {
  text: string; onClick?: () => void; onRemove?: () => void; tone?: 'default' | 'spectrum' | 'advanced'; title?: string;
}) {
  const color = tone === 'spectrum'
    ? 'bg-teal-500/10 border-teal-600/60 text-teal-800 dark:text-teal-300'
    : tone === 'advanced'
      ? 'bg-amber-500/10 border-amber-600/60 text-amber-800 dark:text-amber-300'
      : CHIP_ON;
  return (
    <span className={`${CHIP} ${color} max-w-full`} title={title ?? text}>
      <button type="button" onClick={onClick} className="truncate max-w-[320px] text-left" disabled={!onClick}>{text}</button>
      {onRemove && (
        <button type="button" onClick={onRemove} className="shrink-0 hover:opacity-70" aria-label="Remove">
          <X className="w-3.5 h-3.5" />
        </button>
      )}
    </span>
  );
}

function positionToChip(v: PropertyValue & { kind: 'position' }): CoordinateSearchValue {
  const deg = v.radius;
  if (deg >= 1) return { ra: v.ra, dec: v.dec, radius: +deg.toPrecision(6), radius_unit: 'degrees' };
  if (deg * 60 >= 1) return { ra: v.ra, dec: v.dec, radius: +(deg * 60).toPrecision(6), radius_unit: 'arcmin' };
  return { ra: v.ra, dec: v.dec, radius: +(deg * 3600).toPrecision(6), radius_unit: 'arcsec' };
}

/** A bitmask property: any / all / none of a set of flags. */
function FlagsChip({ label, options, value, onChange }: {
  label: string; options: FilterOption[]; value: { mode: FlagsMode; mask: number } | null; onChange: (v: { mode: FlagsMode; mask: number } | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onDown = (e: MouseEvent) => { if (!isInsidePopover(e.target, ref.current)) setOpen(false); };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, []);
  const mode = value?.mode ?? 'any';
  const mask = value?.mask ?? 0;
  const count = options.filter((o) => (mask & Number(o.value)) !== 0).length;
  const word = mode === 'all' ? 'all of' : mode === 'none' ? 'none of' : 'any of';
  return (
    <div ref={ref} className="relative inline-block">
      <button type="button" onClick={() => setOpen(!open)} className={`${CHIP} ${count ? CHIP_ON : CHIP_IDLE}`}>
        <span>{count ? `${label}: ${word} ${count}` : label}</span>
        {count ? <X className="w-3.5 h-3.5 cursor-pointer" onClick={(e) => { e.stopPropagation(); onChange(null); }} /> : <ChevronDown className="w-3.5 h-3.5" />}
      </button>
      {open && (
        <Popover anchorRef={ref} open={open} className="min-w-[260px] max-w-[340px] bg-card border border-border rounded-lg shadow-lg p-2">
          <div className="inline-flex rounded-md border border-border overflow-hidden text-xs mb-2">
            {(['any', 'all', 'none'] as FlagsMode[]).map((m) => (
              <button key={m} type="button" onClick={() => onChange(mask ? { mode: m, mask } : null)}
                className={`px-2.5 py-1 ${mode === m ? 'bg-primary/10 text-primary font-medium' : 'text-text-secondary hover:bg-card-hover'}`}>
                {m === 'any' ? 'any of' : m === 'all' ? 'all of' : 'none of'}
              </button>
            ))}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {options.map((o) => {
              const on = (mask & Number(o.value)) !== 0;
              return (
                <button key={String(o.value)} type="button"
                  onClick={() => { const next = on ? mask & ~Number(o.value) : mask | Number(o.value); onChange(next ? { mode, mask: next } : null); }}
                  className={`px-2 py-0.5 text-xs rounded-full border transition-colors ${on ? CHIP_ON : 'border-border text-text-secondary hover:border-text-secondary'}`}>
                  {o.icon ? `${o.icon} ` : ''}{o.label}
                </button>
              );
            })}
          </div>
        </Popover>
      )}
    </div>
  );
}

/** A yes / no property as a chip: absent, yes, no. */
function BoolChip({ label, value, onChange }: { label: string; value: boolean | null; onChange: (v: boolean | null) => void }) {
  return (
    <span className={`${CHIP} ${value !== null ? CHIP_ON : CHIP_IDLE}`}>
      <button type="button" onClick={() => onChange(value === null ? true : !value)}>
        {value === null ? label : `${label}: ${value ? 'yes' : 'no'}`}
      </button>
      {value !== null ? <X className="w-3.5 h-3.5 cursor-pointer" onClick={() => onChange(null)} /> : <ChevronDown className="w-3.5 h-3.5" />}
    </span>
  );
}

/**
 * One property of a noun as the familiar chip: it reads its own node of the
 * tree (`item`) and writes it back, or, with no item yet, writes a new node
 * into `level` on the first value.
 */
export function PropertyChip({ def, item, level, ctx, extraCheckbox, onOpenAdvanced }: {
  def: PropertyDef; item?: PropertyItem; level: Level; ctx: CardsContext;
  extraCheckbox?: { label: string; checked: boolean; onChange: (checked: boolean) => void };
  onOpenAdvanced?: () => void;
}) {
  const commit = (value: PropertyValue | null) => {
    const node = value ? compileProperty(def, value, level.table) : null;
    if (item) ctx.onChange(replaceAt(ctx.nodes, item.path, node));
    else if (node) ctx.onChange(addToLevel(ctx.nodes, level, node));
  };
  const value = item?.value;
  switch (def.kind) {
    case 'choice': {
      const current = value?.kind === 'choice' ? value : null;
      const options = optionsFor(def.choices, ctx.choices);
      return (
        <FilterChip
          label={current?.not ? `${def.label} (none of)` : def.label}
          options={options}
          selected={current?.values.filter((v): v is string | number => v !== null && typeof v !== 'boolean') ?? []}
          onChange={(sel) => commit(sel.length ? { kind: 'choice', values: sel, ...(current?.not ? { not: true } : {}) } : null)}
          searchable={SEARCHABLE.has(def.choices ?? '')}
          footerLink={def.choices === 'program' || def.choices === 'observation' ? { label: 'Browse all programs', href: '/nirspec/metadata' } : undefined}
          extraCheckbox={extraCheckbox ?? (def.choices === 'quality' ? undefined : {
            label: 'None of these',
            checked: Boolean(current?.not),
            onChange: (checked) => { if (current) commit({ ...current, not: checked || undefined }); },
          })}
          shortcut={def.choices === 'quality' ? { label: 'All inspected', values: [1, 2, 3, 4] } : undefined}
        />
      );
    }
    case 'range': {
      const current = value?.kind === 'range' ? value : null;
      if (current?.unknown) {
        return <ItemChip text={`${def.label}: unknown`} onClick={onOpenAdvanced} onRemove={() => commit(null)} />;
      }
      const r = def.range;
      return (
        <RangeFilterChip
          label={def.label}
          min={current?.min ?? null}
          max={current?.max ?? null}
          onChange={(min, max) => commit(min === null && max === null ? null : { kind: 'range', min, max })}
          minBound={r?.min} maxBound={r?.max} step={r?.step} precision={r?.precision}
          quickRanges={r?.quick}
        />
      );
    }
    case 'flags': {
      const current = value?.kind === 'flags' ? value : null;
      return (
        <FlagsChip label={def.label} options={optionsFor(def.choices, ctx.choices)} value={current ? { mode: current.mode, mask: current.mask } : null}
          onChange={(v) => commit(v ? { kind: 'flags', ...v } : null)} />
      );
    }
    case 'bool': {
      const current = value?.kind === 'bool' ? value.value : null;
      return <BoolChip label={def.label} value={current} onChange={(v) => commit(v === null ? null : { kind: 'bool', value: v })} />;
    }
    case 'position': {
      const current = value?.kind === 'position' ? value : null;
      return (
        <CoordinateSearchChip
          value={current ? positionToChip(current) : null}
          onChange={(v) => commit(v ? { kind: 'position', ra: v.ra, dec: v.dec, radius: convertRadiusToDegrees(v.radius, v.radius_unit) } : null)}
        />
      );
    }
  }
}

// ---------------------------------------------------------------- adders

/** A native select styled as a dashed chip: never clipped by a scrolling parent. */
function AddSelect({ label, options, onPick, className = ADD_SELECT }: {
  label: string; options: { value: string; label: string; group?: string }[]; onPick: (value: string) => void; className?: string;
}) {
  if (options.length === 0) return null;
  const groups = [...new Set(options.map((o) => o.group ?? ''))];
  return (
    <select className={className} value="" onChange={(e) => { if (e.target.value) onPick(e.target.value); }} aria-label={label}>
      <option value="">{label}</option>
      {groups.map((g) => {
        const opts = options.filter((o) => (o.group ?? '') === g);
        const list = opts.map((o) => <option key={o.value} value={o.value}>{o.label}</option>);
        return g ? <optgroup key={g} label={g}>{list}</optgroup> : <React.Fragment key="_">{list}</React.Fragment>;
      })}
    </select>
  );
}

/** The Line / Band adder: a single-select pick adds one row. */
function AddMeasurementChip({ kind, level, ctx }: { kind: MeasurementKind; level: Level; ctx: CardsContext }) {
  const m = MEASUREMENTS[kind];
  const options = kind === 'line' ? LINE_OPTIONS : ctx.choices.bands;
  if (options.length === 0) return null;
  const add = (key: string) => {
    const snr = propertiesOf('line').find((d) => d.key === 'snr')!;
    const node = compileMeasurement(kind, key, kind === 'line' ? [{ def: snr, value: { kind: 'range', min: 3, max: null } }] : []);
    ctx.onChange(addToLevel(ctx.nodes, level, node));
  };
  return (
    <FilterChip label={`+ ${m.label}`} options={options} selected={[]} multiSelect={false} searchable
      onChange={(s) => { const key = s[0]; if (typeof key === 'string') add(key); }} className="[&>button]:border-dashed [&>button]:bg-transparent" />
  );
}

// ---------------------------------------------------------------- measurement rows

/** A two-ended range typed inline; the value leaves on blur or Enter. */
function RangeInline({ def, value, onCommit }: { def: PropertyDef; value: { min: number | null; max: number | null } | null; onCommit: (min: number | null, max: number | null) => void }) {
  const minRef = useRef<HTMLInputElement>(null);
  const maxRef = useRef<HTMLInputElement>(null);
  const read = (el: HTMLInputElement | null) => { const s = el?.value.trim() ?? ''; if (s === '') return null; const n = Number(s); return Number.isFinite(n) ? n : null; };
  const commit = () => onCommit(read(minRef.current), read(maxRef.current));
  const onKey = (e: React.KeyboardEvent<HTMLInputElement>) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); };
  const w = def.range && def.range.step < 1e-6 ? 'w-24' : 'w-16';
  const show = (n: number | null | undefined) => (n == null ? '' : String(n));
  return (
    <span className="inline-flex items-center gap-1 text-xs text-text-secondary whitespace-nowrap">
      <span className="font-medium text-text-primary">{def.label}</span>
      <span>≥</span>
      <input key={`min-${show(value?.min)}`} ref={minRef} type="number" step="any" defaultValue={show(value?.min)} placeholder="any"
        className={`${INPUT} ${w} py-0.5 text-xs font-mono text-right`} onBlur={commit} onKeyDown={onKey} />
      <span>≤</span>
      <input key={`max-${show(value?.max)}`} ref={maxRef} type="number" step="any" defaultValue={show(value?.max)} placeholder="any"
        className={`${INPUT} ${w} py-0.5 text-xs font-mono text-right`} onBlur={commit} onKeyDown={onKey} />
      {def.unit && <span className="text-text-tertiary">{def.unit}</span>}
    </span>
  );
}

/** One measurement of a noun: the key, its clauses inline, the column toggle. */
export function MeasurementRow({ item, ctx }: { item: MeasurementItem; ctx: CardsContext }) {
  const m = MEASUREMENTS[item.measure];
  const defs = propertiesOf(m.scope);
  const [pending, setPending] = useState<string[]>([]);
  const present = new Map(item.clauses.filter((c): c is PropertyItem => c.kind === 'property').map((c) => [c.def.key, c]));
  const visible = defs.filter((d) => d.primary || present.has(d.key) || pending.includes(d.key));
  const more = defs.filter((d) => !visible.includes(d));
  const commitClause = (def: PropertyDef, value: PropertyValue | null) => {
    const node = value ? compileProperty(def, value, m.table) : null;
    const clause = present.get(def.key);
    if (clause) ctx.onChange(replaceAt(ctx.nodes, clause.path, node));
    else if (node) ctx.onChange(appendAt(ctx.nodes, item.path, node));
    if (!node) setPending((p) => p.filter((k) => k !== def.key));
  };
  const shown = item.measure === 'line' ? ctx.showLine === item.key : ctx.showBand === item.key;
  const keyLabel = ctx.label(m.keyCol, item.key);
  return (
    <div className={`flex flex-wrap items-center gap-x-3 gap-y-1.5 py-1 ${item.not ? 'text-text-secondary' : ''}`}>
      <span className="inline-flex items-center gap-1.5 min-w-[7rem]">
        {item.not && <span className="text-[10px] font-semibold uppercase tracking-wider text-amber-700 dark:text-amber-400">no</span>}
        <span className="text-sm font-semibold font-mono text-text-primary">{keyLabel}</span>
      </span>
      {visible.map((def) => {
        const clause = present.get(def.key);
        const value = clause?.value;
        switch (def.kind) {
          case 'range':
            return <RangeInline key={def.key} def={def} value={value?.kind === 'range' ? value : null}
              onCommit={(min, max) => commitClause(def, min === null && max === null ? null : { kind: 'range', min, max })} />;
          case 'choice': {
            const options = optionsFor(def.choices, ctx.choices);
            const current = value?.kind === 'choice' ? String(value.values[0]) : '';
            return (
              <span key={def.key} className="inline-flex items-center gap-1 text-xs text-text-secondary">
                <span className="font-medium text-text-primary">{def.label}</span>
                <select className={SMALL_SELECT} value={current} onChange={(e) => commitClause(def, e.target.value ? { kind: 'choice', values: [e.target.value] } : null)}>
                  <option value="">any</option>
                  {options.map((o) => <option key={String(o.value)} value={String(o.value)}>{o.label}</option>)}
                </select>
              </span>
            );
          }
          case 'flags':
            return <FlagsChip key={def.key} label={def.label} options={optionsFor(def.choices, ctx.choices)}
              value={value?.kind === 'flags' ? { mode: value.mode, mask: value.mask } : null}
              onChange={(v) => commitClause(def, v ? { kind: 'flags', ...v } : null)} />;
          case 'bool':
            return <BoolChip key={def.key} label={def.label} value={value?.kind === 'bool' ? value.value : null}
              onChange={(v) => commitClause(def, v === null ? null : { kind: 'bool', value: v })} />;
          default:
            return null;
        }
      })}
      {item.clauses.filter((c): c is AdvancedItem => c.kind === 'advanced').map((c) => (
        <ItemChip key={c.path.join('.')} tone="advanced" text={describeNode(c.node, c.table)} onRemove={() => ctx.onChange(replaceAt(ctx.nodes, c.path, null))} />
      ))}
      <AddSelect label="more" className={`${SMALL_SELECT} appearance-none border-dashed cursor-pointer`}
        options={[
          ...more.map((d) => ({ value: d.key, label: d.label })),
          { value: '__not', label: item.not ? `has ${keyLabel}` : `has no ${keyLabel} like this` },
        ]}
        onPick={(v) => {
          if (v === '__not') { ctx.onChange(setNegated(ctx.nodes, item.path, !item.not)); return; }
          const def = defs.find((d) => d.key === v);
          if (!def) return;
          if (def.kind === 'bool') commitClause(def, { kind: 'bool', value: true });
          else setPending((p) => [...p, v]);
        }} />
      <span className="flex-1" />
      <button type="button" onClick={() => ctx.onShowColumn(item.measure, shown ? null : item.key)}
        className={`inline-flex items-center gap-1.5 text-xs ${shown ? 'text-primary' : 'text-text-tertiary hover:text-text-secondary'}`}
        title={shown ? 'Shown as a column of the table' : 'Show as a column of the table'}>
        <span className={`inline-block w-3 h-3 rounded border ${shown ? 'bg-primary border-primary' : 'border-border-strong'}`} />
        column
      </button>
      <button type="button" onClick={() => ctx.onChange(replaceAt(ctx.nodes, item.path, null))} className="p-1 rounded text-text-tertiary hover:text-text-primary hover:bg-card-hover" aria-label="Remove">
        <X className="w-3.5 h-3.5" />
      </button>
    </div>
  );
}

// ---------------------------------------------------------------- groups and blocks

/** An "any of" group: its arms as chips, a new arm from the property adder. */
function AnyOfGroup({ item, level, ctx, noun }: { item: AnyOfItem; level: Level; ctx: CardsContext; noun: Noun }) {
  const [pending, setPending] = useState<PropertyDef | null>(null);
  const armLevel: Level = { table: level.table, path: item.path };
  const defs = propertiesOf(noun).filter((d) => !(ctx.hideKeys ?? []).includes(d.key));
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-dashed border-border-strong px-3 py-2">
      <span className={`${EYEBROW} text-primary`}>any of</span>
      {item.arms.map((arm, i) => (
        <React.Fragment key={i}>
          {i > 0 && <span className="text-xs text-text-tertiary">or</span>}
          {arm.map((it) => (
            it.kind === 'property'
              ? <PropertyChip key={it.path.join('.')} def={it.def} item={it} level={armLevel} ctx={ctx} />
              : <ItemChip key={it.path.join('.')} text={describeItem(it, ctx.label)} tone={it.kind === 'advanced' ? 'advanced' : it.kind === 'spectrum' ? 'spectrum' : 'default'}
                  onRemove={() => ctx.onChange(replaceAt(ctx.nodes, it.path, null))} />
          ))}
        </React.Fragment>
      ))}
      {pending && (
        <>
          <span className="text-xs text-text-tertiary">or</span>
          <PropertyChip def={pending} level={armLevel} ctx={{ ...ctx, onChange: (n) => { setPending(null); ctx.onChange(n); } }} />
        </>
      )}
      <AddSelect label="+" options={defs.map((d) => ({ value: d.key, label: d.label }))} onPick={(k) => setPending(defs.find((d) => d.key === k) ?? null)} className={`${SMALL_SELECT} appearance-none border-dashed cursor-pointer`} />
      <button type="button" onClick={() => ctx.onChange(replaceAt(ctx.nodes, item.path, null))} className="p-1 rounded text-text-tertiary hover:text-text-primary hover:bg-card-hover" aria-label="Remove group">
        <X className="w-3.5 h-3.5" />
      </button>
    </div>
  );
}

/** A node the cards cannot show, in the same words, its JSON a click away. */
function AdvancedBlock({ item, ctx }: { item: AdvancedItem; ctx: CardsContext }) {
  const [json, setJson] = useState(false);
  return (
    <div className="rounded-lg border border-dashed border-amber-600/60 bg-amber-500/5 px-3 py-2 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-amber-700 dark:text-amber-400">advanced</span>
        <span className="text-text-primary">{describeNode(item.node, item.table)}</span>
        <span className="flex-1" />
        <button type="button" onClick={() => setJson(!json)} className="text-xs text-text-secondary hover:text-text-primary">{json ? 'hide JSON' : 'JSON'}</button>
        <button type="button" onClick={() => ctx.onChange(replaceAt(ctx.nodes, item.path, null))} className="p-1 rounded text-text-tertiary hover:text-text-primary hover:bg-card-hover" aria-label="Remove">
          <X className="w-3.5 h-3.5" />
        </button>
      </div>
      {json && <pre className="mt-2 text-xs font-mono whitespace-pre-wrap break-words text-text-secondary">{JSON.stringify(item.node, null, 1)}</pre>}
    </div>
  );
}

// ---------------------------------------------------------------- a noun's body

export interface NounBodyProps {
  noun: Noun;
  items: Item[];
  level: Level;
  ctx: CardsContext;
  /** Property keys shown only when present (the spectrum's object at the spectra root has no Program of its own). */
  demote?: string[];
}

/**
 * Everything of one noun at one level: its property chips (the primary ones
 * always, the rest when set or asked for), its "any of" groups, its
 * measurement rows, its Spectrum cards (an object's), and its advanced nodes.
 */
export function NounBody({ noun, items, level, ctx, demote = [] }: NounBodyProps) {
  const [pending, setPending] = useState<string[]>([]);
  const [pendingGroup, setPendingGroup] = useState<PropertyDef | null>(null);
  const hide = new Set(ctx.hideKeys ?? []);
  const defs = propertiesOf(noun).filter((d) => !hide.has(d.key));
  const props = items.filter((it): it is PropertyItem => it.kind === 'property');
  const present = new Map<string, PropertyItem>();
  for (const p of props) if (!present.has(p.def.key)) present.set(p.def.key, p);
  // a second node of a key the chip already shows is a chip of its own
  const duplicates = props.filter((p) => present.get(p.def.key) !== p);
  const visible = defs.filter((d) => (d.primary && !demote.includes(d.key)) || present.has(d.key) || pending.includes(d.key));
  const more = defs.filter((d) => !visible.includes(d));
  const groups = items.filter((it): it is AnyOfItem => it.kind === 'anyOf');
  const measure = MEASUREMENTS[noun === 'object' ? 'band' : 'line'];
  const rows = items.filter((it): it is MeasurementItem => it.kind === 'measurement' && it.measure === measure.kind);
  const cards = items.filter((it): it is SpectrumItem => it.kind === 'spectrum');
  const advanced = items.filter((it): it is AdvancedItem => it.kind === 'advanced');
  const needsReview = noun === 'object' ? present.get('needs_review') : undefined;
  const needsReviewDef = defs.find((d) => d.key === 'needs_review');
  const commitNeedsReview = (checked: boolean) => {
    if (!needsReviewDef) return;
    if (needsReview) ctx.onChange(replaceAt(ctx.nodes, needsReview.path, checked ? compileProperty(needsReviewDef, { kind: 'bool', value: true }, level.table) : null));
    else if (checked) { const n = compileProperty(needsReviewDef, { kind: 'bool', value: true }, level.table); if (n) ctx.onChange(addToLevel(ctx.nodes, level, n)); }
  };
  const addCard = () => ctx.onChange(addToLevel(ctx.nodes, level, { exists: 'spectra' }));
  const scopeNoun = useMemo(() => (noun === 'object' ? 'object' : 'spectrum'), [noun]);

  return (
    <div className="space-y-2.5">
      <div className="flex flex-wrap items-center gap-2">
        {visible.map((def) => {
          if (def.key === 'needs_review' && !pending.includes('needs_review') && noun === 'object') return null; // lives in the Quality chip
          return (
            <PropertyChip key={def.key} def={def} item={present.get(def.key)} level={level} ctx={ctx}
              extraCheckbox={def.key === 'quality' && needsReviewDef && !hide.has('needs_review')
                ? { label: 'Needs review only', checked: needsReview?.value.kind === 'bool' && needsReview.value.value, onChange: commitNeedsReview }
                : undefined} />
          );
        })}
        {duplicates.map((p) => (
          <PropertyChip key={p.path.join('.')} def={p.def} item={p} level={level} ctx={ctx} />
        ))}
        <AddSelect label="+ property"
          options={[
            ...more.filter((d) => !(d.key === 'needs_review' && noun === 'object')).map((d) => ({ value: d.key, label: d.label })),
            { value: '__anyof', label: 'Any of…', group: 'Groups' },
          ]}
          onPick={(k) => {
            if (k === '__anyof') { setPendingGroup(defs.find((d) => d.primary) ?? defs[0]); return; }
            setPending((p) => (p.includes(k) ? p : [...p, k]));
          }} />
      </div>

      {groups.map((g) => <AnyOfGroup key={g.path.join('.')} item={g} level={level} ctx={ctx} noun={scopeNoun} />)}
      {pendingGroup && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-dashed border-border-strong px-3 py-2">
          <span className={`${EYEBROW} text-primary`}>any of</span>
          <AddSelect label={pendingGroup.label} options={defs.map((d) => ({ value: d.key, label: d.label }))} onPick={(k) => setPendingGroup(defs.find((d) => d.key === k) ?? null)} className={`${SMALL_SELECT} appearance-none cursor-pointer`} />
          <GroupSeed key={pendingGroup.key} def={pendingGroup} level={level} ctx={ctx} onDone={() => setPendingGroup(null)} />
          <button type="button" onClick={() => setPendingGroup(null)} className="p-1 rounded text-text-tertiary hover:text-text-primary" aria-label="Cancel group"><X className="w-3.5 h-3.5" /></button>
        </div>
      )}

      {(rows.length > 0 || (measure.kind === 'line' ? LINE_OPTIONS.length : ctx.choices.bands.length) > 0) && (
        <div>
          <div className={`${EYEBROW} mb-1`}>{measure.plural}</div>
          <div className="divide-y divide-border/60">
            {rows.map((r) => <MeasurementRow key={r.path.join('.')} item={r} ctx={ctx} />)}
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            <AddMeasurementChip kind={measure.kind} level={level} ctx={ctx} />
          </div>
        </div>
      )}

      {noun === 'object' && (
        <div className="space-y-2">
          {cards.map((c) => <SpectrumCard key={c.path.join('.')} item={c} ctx={ctx} />)}
          <button type="button" onClick={addCard} className={ADD}>+ spectrum</button>
        </div>
      )}

      {advanced.map((a) => <AdvancedBlock key={a.path.join('.')} item={a} ctx={ctx} />)}
    </div>
  );
}

/** The chip that seeds an "any of" group: its first value writes `{ or: [node] }`. */
function GroupSeed({ def, level, ctx, onDone }: { def: PropertyDef; level: Level; ctx: CardsContext; onDone: () => void }) {
  return (
    <PropertyChip def={def} level={level} ctx={{
      ...ctx,
      onChange: (next) => {
        // the chip appended a node at the level's end: wrap that node into a group
        const added = findAppended(ctx.nodes, next, level);
        if (!added) { ctx.onChange(next); onDone(); return; }
        ctx.onChange(replaceAt(next, added.path, { or: [added.node] }));
        onDone();
      },
    }} />
  );
}

/** The node a chip just appended into `level`, with its path. */
function findAppended(before: TreeNode[], after: TreeNode[], level: Level): { node: TreeNode; path: Path } | null {
  const container = (nodes: TreeNode[]) => {
    if (level.path.length === 0) return nodes;
    let list: TreeNode[] = nodes;
    let node: TreeNode | undefined;
    for (const i of level.path) { node = list[i]; if (!node) return null; list = isExists(node) ? node.where ?? [] : 'and' in node ? node.and : 'or' in node ? node.or : []; }
    return list;
  };
  const b = container(before);
  const a = container(after);
  if (!a || !b || a.length !== b.length + 1) return null;
  return { node: a[a.length - 1], path: [...level.path, a.length - 1] };
}

/** One spectrum of an object: has / has no, its chips and its line rows. */
export function SpectrumCard({ item, ctx }: { item: SpectrumItem; ctx: CardsContext }) {
  const level: Level = { table: 'spectra', path: item.path, implicit: item.implicit };
  return (
    <div className={`rounded-lg border border-border bg-card p-3 border-l-[3px] ${item.not ? 'border-l-amber-600' : 'border-l-teal-600'}`}>
      <div className="flex items-center gap-2 mb-2">
        <select className={`${INPUT} pr-6 py-0.5 text-sm font-semibold`} value={item.not ? 'not' : 'has'} onChange={(e) => ctx.onChange(setNegated(ctx.nodes, item.path, e.target.value === 'not'))}>
          <option value="has">has a spectrum where</option>
          <option value="not">has no spectrum where</option>
        </select>
        <span className="flex-1" />
        <button type="button" onClick={() => ctx.onChange(replaceAt(ctx.nodes, item.path, null))} className="p-1 rounded text-text-tertiary hover:text-text-primary hover:bg-card-hover" aria-label="Remove spectrum">
          <X className="w-4 h-4" />
        </button>
      </div>
      <NounBody noun="spectrum" items={item.items} level={level} ctx={ctx} />
    </div>
  );
}
