'use client';

/**
 * The cards of the filter UI (Catalog Read Model phase 3b, design doc
 * Figures 6 to 8): chips for a noun's properties, tables for its
 * measurements, one card per spectrum, "any of" groups, and an advanced block
 * for whatever the vocabulary cannot show. Everything reads from and writes
 * to the tree in `?q=` through lib/catalog-query/vocabulary.ts; nothing here
 * keeps state of its own beyond what a half-built chip needs.
 *
 * At the object level a spectrum's grating and a line are questions about
 * any of the object's spectra; a Spectrum card is the one place where
 * several things must hold on the same spectrum.
 */
import React, { useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, X } from 'lucide-react';
import { FilterChip, type FilterOption } from '@/components/ui/FilterChip';
import { RangeFilterChip } from '@/components/ui/RangeFilterChip';
import { CoordinateSearchChip, type CoordinateSearchValue } from '@/components/ui/CoordinateSearchChip';
import { Popover, isInsidePopover } from '@/components/ui/Popover';
import { convertRadiusToDegrees } from '@/lib/utils/coordinate-parser';
import { describeNode, isExists, type TreeNode, type TreeRoot } from '@/lib/catalog-query/tree';
import {
  MEASUREMENTS, appendAt, compileChoice, compileMeasurement, compileProperty, describeItem, propertiesOf, readChoiceState, removePaths,
  replaceAt, setNegated,
  type AdvancedItem, type AnyOfItem, type Item, type MeasurementItem, type MeasurementKind, type Noun, type Path,
  type PropertyDef, type PropertyItem, type PropertyValue, type SpectrumItem, type ValueLabel,
} from '@/lib/catalog-query/vocabulary';
import { LINE_OPTIONS, optionsFor, type FilterChoices } from './filter-choices';

// ---------------------------------------------------------------- the editing context

/** Where a new node goes and against which table it is written. */
export interface Level {
  table: 'objects' | 'spectra';
  /** The container: the top level (empty) or a Spectrum card. */
  path: Path;
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
  if (level.itsObject && isExists(node) && node.exists === 'spectra') return appendAt(nodes, [], { exists: 'objects', where: [node] });
  return appendAt(nodes, level.path, node);
}

function addManyToLevel(nodes: TreeNode[], level: Level, added: TreeNode[]): TreeNode[] {
  return added.reduce((acc, n) => addToLevel(acc, level, n), nodes);
}

const CHIP = 'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm font-medium border transition-all duration-150';
const CHIP_IDLE = 'bg-card border-border text-text-secondary hover:border-text-secondary dark:hover:border-border-strong hover:text-text-primary';
const CHIP_ON = 'bg-primary/10 border-primary text-primary';
const ADD = `${CHIP} border-dashed bg-transparent text-text-secondary hover:text-text-primary hover:border-text-secondary cursor-pointer`;
const CARET = `appearance-none pr-7 bg-[length:12px] bg-[right_10px_center] bg-no-repeat bg-[url('data:image/svg+xml;utf8,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 24 24%22 fill=%22none%22 stroke=%22%2394a3b8%22 stroke-width=%222.5%22><path d=%22m6 9 6 6 6-6%22/></svg>')]`;
const ADD_SELECT = `${ADD} ${CARET}`;
const INPUT = 'px-2 py-1 text-sm border border-border rounded-md bg-background text-text-primary placeholder:text-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/40 focus:border-transparent';
const SMALL_SELECT = `${INPUT} ${CARET} pr-6 py-0.5 text-xs`;
const EYEBROW = 'text-[11px] font-semibold uppercase tracking-wider text-text-tertiary';
const ICON_BTN = 'p-1 rounded text-text-tertiary hover:text-text-primary hover:bg-card-hover';

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

/** A yes / no property: a two-option dropdown. */
function YesNoChip({ label, value, onChange }: { label: string; value: boolean | null; onChange: (v: boolean | null) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onDown = (e: MouseEvent) => { if (!isInsidePopover(e.target, ref.current)) setOpen(false); };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, []);
  return (
    <div ref={ref} className="relative inline-block">
      <button type="button" onClick={() => setOpen(!open)} className={`${CHIP} ${value !== null ? CHIP_ON : CHIP_IDLE}`}>
        <span>{value === null ? label : `${label}: ${value ? 'yes' : 'no'}`}</span>
        {value !== null
          ? <X className="w-3.5 h-3.5 cursor-pointer" onClick={(e) => { e.stopPropagation(); onChange(null); }} />
          : <ChevronDown className={`w-3.5 h-3.5 transition-transform ${open ? 'rotate-180' : ''}`} />}
      </button>
      {open && (
        <Popover anchorRef={ref} open={open} className="min-w-[160px] bg-card border border-border rounded-lg shadow-lg p-1 text-sm">
          {[true, false].map((v) => (
            <button key={String(v)} type="button" onClick={() => { onChange(v); setOpen(false); }}
              className={`w-full text-left px-3 py-2 rounded-md hover:bg-card-hover ${value === v ? 'text-primary font-medium' : 'text-text-primary'}`}>
              {v ? 'yes' : 'no'}
            </button>
          ))}
        </Popover>
      )}
    </div>
  );
}

/**
 * One property of a noun as the familiar chip. A choice or flags chip owns
 * every node of its key at the level (its included values, any or all of
 * them, and its excluded values); the others own one node. With no node yet
 * the first value writes a new one into `level`.
 */
export function PropertyChip({ def, items, level, ctx, extraCheckbox, onOpenAdvanced }: {
  def: PropertyDef; items: PropertyItem[]; level: Level; ctx: CardsContext;
  extraCheckbox?: { label: string; checked: boolean; onChange: (checked: boolean) => void };
  onOpenAdvanced?: () => void;
}) {
  const first = items[0];
  const commitOne = (value: PropertyValue | null) => {
    const node = value ? compileProperty(def, value, level.table) : null;
    if (first) ctx.onChange(replaceAt(ctx.nodes, first.path, node));
    else if (node) ctx.onChange(addToLevel(ctx.nodes, level, node));
  };
  if (def.kind === 'choice' || def.kind === 'flags') {
    const state = readChoiceState(items);
    const commitState = (next: { include?: (string | number)[]; exclude?: (string | number)[]; mode?: 'any' | 'all' }) => {
      const merged = { include: next.include ?? state.include, exclude: next.exclude ?? state.exclude, mode: next.mode ?? state.mode };
      const nodes = compileChoice(def, merged, level.table);
      ctx.onChange(addManyToLevel(removePaths(ctx.nodes, items.map((it) => it.path)), level, nodes));
    };
    const scalars = (v: unknown[]) => v.filter((x): x is string | number => typeof x === 'string' || typeof x === 'number');
    return (
      <FilterChip
        label={def.label}
        options={optionsFor(def.choices, ctx.choices)}
        selected={scalars(state.include)}
        onChange={(sel) => commitState({ include: sel })}
        excluded={scalars(state.exclude)}
        onExcludedChange={(ex) => commitState({ exclude: ex })}
        mode={state.mode}
        onModeChange={def.multi || def.kind === 'flags' ? (mode) => commitState({ mode }) : undefined}
        searchable={SEARCHABLE.has(def.choices ?? '')}
        footerLink={def.choices === 'program' || def.choices === 'observation' ? { label: 'Browse all programs', href: '/nirspec/metadata' } : undefined}
        extraCheckbox={extraCheckbox}
        shortcut={def.choices === 'quality' ? { label: 'All inspected', values: [1, 2, 3, 4] } : undefined}
      />
    );
  }
  const value = first?.value;
  switch (def.kind) {
    case 'range': {
      const current = value?.kind === 'range' ? value : null;
      if (current?.unknown) {
        return <ItemChip text={`${def.label}: unknown`} onClick={onOpenAdvanced} onRemove={() => commitOne(null)} />;
      }
      const r = def.range;
      return (
        <RangeFilterChip
          label={def.label}
          min={current?.min ?? null}
          max={current?.max ?? null}
          onChange={(min, max) => commitOne(min === null && max === null ? null : { kind: 'range', min, max })}
          minBound={r?.min} maxBound={r?.max} step={r?.step} precision={r?.precision}
          quickRanges={r?.quick}
        />
      );
    }
    case 'bool': {
      const current = value?.kind === 'bool' ? value.value : null;
      return <YesNoChip label={def.label} value={current} onChange={(v) => commitOne(v === null ? null : { kind: 'bool', value: v })} />;
    }
    case 'position': {
      const current = value?.kind === 'position' ? value : null;
      return (
        <CoordinateSearchChip
          value={current ? positionToChip(current) : null}
          onChange={(v) => commitOne(v ? { kind: 'position', ra: v.ra, dec: v.dec, radius: convertRadiusToDegrees(v.radius, v.radius_unit) } : null)}
        />
      );
    }
    default:
      return null;
  }
}

// ---------------------------------------------------------------- adders

/** A native select styled as a dashed chip: never clipped by a scrolling parent. */
function AddSelect({ label, options, onPick, className = ADD_SELECT }: {
  label: string; options: { value: string; label: string }[]; onPick: (value: string) => void; className?: string;
}) {
  if (options.length === 0) return null;
  return (
    <select className={className} value="" onChange={(e) => { if (e.target.value) onPick(e.target.value); }} aria-label={label}>
      <option value="">{label}</option>
      {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );
}

// ---------------------------------------------------------------- measurement tables

/** A min – max pair typed inline; the value leaves on blur or Enter. */
function RangeCell({ def, value, onCommit }: { def: PropertyDef; value: { min: number | null; max: number | null } | null; onCommit: (min: number | null, max: number | null) => void }) {
  const minRef = useRef<HTMLInputElement>(null);
  const maxRef = useRef<HTMLInputElement>(null);
  const read = (el: HTMLInputElement | null) => { const s = el?.value.trim() ?? ''; if (s === '') return null; const n = Number(s); return Number.isFinite(n) ? n : null; };
  const commit = () => onCommit(read(minRef.current), read(maxRef.current));
  const onKey = (e: React.KeyboardEvent<HTMLInputElement>) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); };
  const w = def.range && def.range.step < 1e-6 ? 'w-[5.5rem]' : 'w-14';
  const show = (n: number | null | undefined) => (n == null ? '' : String(n));
  const cls = `${INPUT} ${w} py-0.5 text-xs font-mono text-right`;
  return (
    <span className="inline-flex items-center gap-1 whitespace-nowrap">
      <input key={`min-${show(value?.min)}`} ref={minRef} type="number" step="any" defaultValue={show(value?.min)} placeholder="min" className={cls} onBlur={commit} onKeyDown={onKey} aria-label={`${def.label} min`} />
      <span className="text-text-tertiary text-xs">–</span>
      <input key={`max-${show(value?.max)}`} ref={maxRef} type="number" step="any" defaultValue={show(value?.max)} placeholder="max" className={cls} onBlur={commit} onKeyDown={onKey} aria-label={`${def.label} max`} />
    </span>
  );
}

/** The columns of a measurement table: the primary clauses of its scope. */
function tableColumns(kind: MeasurementKind): PropertyDef[] {
  return propertiesOf(MEASUREMENTS[kind].scope).filter((d) => d.primary);
}

/** One row: the key, a cell per column, the details for a line, the column toggle. */
function MeasurementRowCells({ item, ctx, columns }: { item: MeasurementItem; ctx: CardsContext; columns: PropertyDef[] }) {
  const m = MEASUREMENTS[item.measure];
  const [open, setOpen] = useState(false);
  const present = new Map(item.clauses.filter((c): c is PropertyItem => c.kind === 'property').map((c) => [c.def.key, c]));
  const commitClause = (def: PropertyDef, value: PropertyValue | null) => {
    const node = value ? compileProperty(def, value, m.table) : null;
    const clause = present.get(def.key);
    if (clause) ctx.onChange(replaceAt(ctx.nodes, clause.path, node));
    else if (node) ctx.onChange(appendAt(ctx.nodes, item.path, node));
  };
  const shown = item.measure === 'line' ? ctx.showLine === item.key : ctx.showBand === item.key;
  const keyLabel = ctx.label(m.keyCol, item.key);
  const details = propertiesOf(m.scope).filter((d) => !d.primary);
  const advanced = item.clauses.filter((c): c is AdvancedItem => c.kind === 'advanced');
  const detailCount = details.filter((d) => present.has(d.key)).length + advanced.length + (item.not ? 1 : 0);
  const hasDetails = item.measure === 'line' || advanced.length > 0 || item.not;
  const cell = (def: PropertyDef) => {
    const value = present.get(def.key)?.value;
    switch (def.kind) {
      case 'range':
        return <RangeCell def={def} value={value?.kind === 'range' ? value : null} onCommit={(min, max) => commitClause(def, min === null && max === null ? null : { kind: 'range', min, max })} />;
      case 'choice': {
        const current = value?.kind === 'choice' ? String(value.values[0]) : '';
        return (
          <select className={SMALL_SELECT} value={current} onChange={(e) => commitClause(def, e.target.value ? { kind: 'choice', values: [e.target.value] } : null)}>
            <option value="">any</option>
            {optionsFor(def.choices, ctx.choices).map((o) => <option key={String(o.value)} value={String(o.value)}>{o.label}</option>)}
          </select>
        );
      }
      case 'bool': {
        const current = value?.kind === 'bool' ? value.value : null;
        return (
          <select className={SMALL_SELECT} value={current === null ? '' : String(current)} onChange={(e) => commitClause(def, e.target.value === '' ? null : { kind: 'bool', value: e.target.value === 'true' })}>
            <option value="">any</option>
            <option value="true">yes</option>
            <option value="false">no</option>
          </select>
        );
      }
      case 'flags': {
        // one clause, one node: the included flags, any or all of them
        const bits = value?.kind === 'flags' && value.mode !== 'none' ? readChoiceState([present.get(def.key)!]).include.filter((x): x is number => typeof x === 'number') : [];
        const mode = value?.kind === 'flags' && value.mode === 'all' ? 'all' : 'any';
        return (
          <FilterChip label={def.label} options={optionsFor(def.choices, ctx.choices)} className="[&>button]:py-0.5 [&>button]:text-xs"
            selected={bits} mode={mode}
            onChange={(sel) => { const mask = sel.reduce<number>((a, b) => a | Number(b), 0); commitClause(def, mask ? { kind: 'flags', mode, mask } : null); }}
            onModeChange={(m) => { const mask = bits.reduce((a, b) => a | b, 0); if (mask) commitClause(def, { kind: 'flags', mode: m, mask }); }} />
        );
      }
      default:
        return null;
    }
  };
  return (
    <>
      <tr className={`align-middle ${item.not ? 'text-text-secondary' : ''}`}>
        <td className="py-1 pr-3 whitespace-nowrap">
          <span className="inline-flex items-center gap-1.5">
            {hasDetails && (
              <button type="button" onClick={() => setOpen(!open)} className={ICON_BTN} aria-label="Details">
                {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
              </button>
            )}
            {item.not && <span className="text-[10px] font-semibold uppercase tracking-wider text-amber-700 dark:text-amber-400">absent</span>}
            <span className="text-sm font-semibold font-mono text-text-primary">{keyLabel}</span>
            {!open && detailCount > 0 && <span className="text-[10px] text-text-tertiary">+{detailCount}</span>}
          </span>
        </td>
        {columns.map((def) => <td key={def.key} className="py-1 pr-3">{cell(def)}</td>)}
        <td className="py-1 pr-2 text-right whitespace-nowrap">
          <button type="button" onClick={() => ctx.onShowColumn(item.measure, shown ? null : item.key)}
            className={`inline-flex items-center gap-1.5 text-xs ${shown ? 'text-primary' : 'text-text-tertiary hover:text-text-secondary'}`}
            title={shown ? 'Shown as a column of the table' : 'Show as a column of the table'}>
            <span className={`inline-block w-3 h-3 rounded border ${shown ? 'bg-primary border-primary' : 'border-border-strong'}`} />
          </button>
        </td>
        <td className="py-1 text-right">
          <button type="button" onClick={() => ctx.onChange(replaceAt(ctx.nodes, item.path, null))} className={ICON_BTN} aria-label="Remove">
            <X className="w-3.5 h-3.5" />
          </button>
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={columns.length + 3} className="pb-2 pl-6">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-text-secondary">
              {details.map((def) => (
                <span key={def.key} className="inline-flex items-center gap-1.5">
                  <span className="font-medium text-text-primary">{def.label}</span>
                  {cell(def)}
                  {def.unit && def.kind === 'range' && <span className="text-text-tertiary">{def.unit}</span>}
                </span>
              ))}
              <label className="inline-flex items-center gap-1.5 cursor-pointer">
                <input type="checkbox" checked={item.not} onChange={(e) => ctx.onChange(setNegated(ctx.nodes, item.path, e.target.checked))} className="accent-primary" />
                <span>must be absent</span>
              </label>
              {advanced.map((c) => (
                <ItemChip key={c.path.join('.')} tone="advanced" text={describeNode(c.node, c.table)} onRemove={() => ctx.onChange(replaceAt(ctx.nodes, c.path, null))} />
              ))}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

/** A noun's measurements of one kind as a table, with a multi-select picker
 *  that adds and removes rows. */
export function MeasurementTable({ kind, items, level, ctx, title }: { kind: MeasurementKind; items: MeasurementItem[]; level: Level; ctx: CardsContext; title: string }) {
  const m = MEASUREMENTS[kind];
  const options: FilterOption[] = kind === 'line' ? LINE_OPTIONS : ctx.choices.bands;
  if (options.length === 0 && items.length === 0) return null;
  const columns = tableColumns(kind);
  const keys = items.map((it) => it.key);
  const onPick = (selected: (string | number)[]) => {
    const next = selected.map(String);
    let nodes = ctx.nodes;
    const gone = items.filter((it) => !next.includes(it.key));
    if (gone.length) nodes = removePaths(nodes, gone.map((it) => it.path));
    for (const key of next) {
      if (keys.includes(key)) continue;
      const snr = propertiesOf('line').find((d) => d.key === 'snr')!;
      nodes = addToLevel(nodes, level, compileMeasurement(kind, key, kind === 'line' ? [{ def: snr, value: { kind: 'range', min: 3, max: null } }] : []));
    }
    ctx.onChange(nodes);
  };
  return (
    <div>
      <div className="flex items-center gap-3 mb-1">
        <span className={EYEBROW}>{title}</span>
        <FilterChip label={`+ ${m.label}`} options={options} selected={keys} onChange={onPick} searchable={kind === 'line'}
          className="[&>button]:py-0.5 [&>button]:text-xs [&>button]:border-dashed [&>button]:bg-transparent" />
      </div>
      {items.length > 0 && (
        <table className="text-sm">
          <thead>
            <tr className="text-[11px] uppercase tracking-wider text-text-tertiary">
              <th className="font-medium text-left pr-3 pb-1">{m.label}</th>
              {columns.map((d) => <th key={d.key} className="font-medium text-left pr-3 pb-1">{d.label}{d.unit ? ` (${d.unit})` : ''}</th>)}
              <th className="font-medium text-right pr-2 pb-1">column</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {items.map((it) => <MeasurementRowCells key={it.path.join('.')} item={it} ctx={ctx} columns={columns} />)}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- groups and blocks

/** An "any of" group: alternatives across properties, each arm a chip. */
function AnyOfGroup({ item, level, ctx, noun }: { item: AnyOfItem; level: Level; ctx: CardsContext; noun: Noun }) {
  const [pending, setPending] = useState<PropertyDef | null>(null);
  const armLevel: Level = { table: level.table, path: item.path };
  const used = new Set(item.arms.flat().filter((it): it is PropertyItem => it.kind === 'property').map((it) => it.def.key));
  const defs = propertiesOf(noun).filter((d) => !(ctx.hideKeys ?? []).includes(d.key) && !used.has(d.key) && d.key !== 'needs_review');
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-dashed border-border-strong px-3 py-2">
      <span className={`${EYEBROW} text-primary`}>any of</span>
      {item.arms.map((arm, i) => (
        <React.Fragment key={i}>
          {i > 0 && <span className="text-xs text-text-tertiary">or</span>}
          {arm.map((it) => (
            it.kind === 'property'
              ? <PropertyChip key={it.path.join('.')} def={it.def} items={[it]} level={armLevel} ctx={ctx} />
              : <ItemChip key={it.path.join('.')} text={describeItem(it, ctx.label)} tone={it.kind === 'advanced' ? 'advanced' : it.kind === 'spectrum' ? 'spectrum' : 'default'}
                  onRemove={() => ctx.onChange(replaceAt(ctx.nodes, it.path, null))} />
          ))}
        </React.Fragment>
      ))}
      {pending && (
        <>
          <span className="text-xs text-text-tertiary">or</span>
          <PropertyChip def={pending} items={[]} level={armLevel} ctx={{ ...ctx, onChange: (n) => { setPending(null); ctx.onChange(n); } }} />
        </>
      )}
      <AddSelect label="+ alternative" options={defs.map((d) => ({ value: d.key, label: d.label }))} onPick={(k) => setPending(defs.find((d) => d.key === k) ?? null)} className={`${SMALL_SELECT} border-dashed cursor-pointer`} />
      <button type="button" onClick={() => ctx.onChange(replaceAt(ctx.nodes, item.path, null))} className={ICON_BTN} aria-label="Remove group">
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
        <button type="button" onClick={() => ctx.onChange(replaceAt(ctx.nodes, item.path, null))} className={ICON_BTN} aria-label="Remove">
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
 * measurement tables, its Spectrum cards (an object's), and its advanced
 * nodes.
 */
export function NounBody({ noun, items, level, ctx, demote = [] }: NounBodyProps) {
  const [pending, setPending] = useState<string[]>([]);
  const [pendingGroup, setPendingGroup] = useState<PropertyDef | null>(null);
  const hide = new Set(ctx.hideKeys ?? []);
  const defs = propertiesOf(noun).filter((d) => !hide.has(d.key));
  const props = items.filter((it): it is PropertyItem => it.kind === 'property');
  const byKey = new Map<string, PropertyItem[]>();
  for (const p of props) byKey.set(p.def.key, [...(byKey.get(p.def.key) ?? []), p]);
  const visible = defs.filter((d) => (d.primary && !demote.includes(d.key)) || byKey.has(d.key) || pending.includes(d.key));
  const more = defs.filter((d) => !visible.includes(d) && d.key !== 'needs_review');
  const groups = items.filter((it): it is AnyOfItem => it.kind === 'anyOf');
  const rows = (kind: MeasurementKind) => items.filter((it): it is MeasurementItem => it.kind === 'measurement' && it.measure === kind);
  const cards = items.filter((it): it is SpectrumItem => it.kind === 'spectrum');
  const advanced = items.filter((it): it is AdvancedItem => it.kind === 'advanced');
  const needsReview = noun === 'object' ? byKey.get('needs_review')?.[0] : undefined;
  const needsReviewDef = defs.find((d) => d.key === 'needs_review');
  const commitNeedsReview = (checked: boolean) => {
    if (!needsReviewDef) return;
    const node = checked ? compileProperty(needsReviewDef, { kind: 'bool', value: true }, level.table) : null;
    if (needsReview) ctx.onChange(replaceAt(ctx.nodes, needsReview.path, node));
    else if (node) ctx.onChange(addToLevel(ctx.nodes, level, node));
  };
  const addCard = () => ctx.onChange(addToLevel(ctx.nodes, level, { exists: 'spectra' }));

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        {visible.map((def) => {
          if (def.key === 'needs_review' && noun === 'object' && !hide.has('quality')) return null; // lives in the Quality chip
          return (
            <PropertyChip key={def.key} def={def} items={byKey.get(def.key) ?? []} level={level} ctx={ctx}
              extraCheckbox={def.key === 'quality' && needsReviewDef
                ? { label: 'Needs review only', checked: needsReview?.value.kind === 'bool' && needsReview.value.value, onChange: commitNeedsReview }
                : undefined} />
          );
        })}
        <AddSelect label="+ property"
          options={[...more.map((d) => ({ value: d.key, label: d.label })), { value: '__anyof', label: 'Any of… (an OR across properties)' }]}
          onPick={(k) => {
            if (k === '__anyof') { setPendingGroup(defs.find((d) => d.primary) ?? defs[0]); return; }
            setPending((p) => (p.includes(k) ? p : [...p, k]));
          }} />
      </div>

      {groups.map((g) => <AnyOfGroup key={g.path.join('.')} item={g} level={level} ctx={ctx} noun={noun} />)}
      {pendingGroup && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-dashed border-border-strong px-3 py-2">
          <span className={`${EYEBROW} text-primary`}>any of</span>
          <AddSelect label={pendingGroup.label} options={defs.filter((d) => d.key !== 'needs_review').map((d) => ({ value: d.key, label: d.label }))} onPick={(k) => setPendingGroup(defs.find((d) => d.key === k) ?? null)} className={`${SMALL_SELECT} cursor-pointer`} />
          <GroupSeed key={pendingGroup.key} def={pendingGroup} level={level} ctx={ctx} onDone={() => setPendingGroup(null)} />
          <button type="button" onClick={() => setPendingGroup(null)} className={ICON_BTN} aria-label="Cancel group"><X className="w-3.5 h-3.5" /></button>
        </div>
      )}

      {noun === 'object' && <MeasurementTable kind="band" items={rows('band')} level={level} ctx={ctx} title="Photometry" />}
      <MeasurementTable kind="line" items={rows('line')} level={level} ctx={ctx} title={noun === 'object' ? 'Lines, in any spectrum' : 'Lines'} />

      {noun === 'object' && (
        <div className="space-y-2">
          {cards.map((c) => <SpectrumCard key={c.path.join('.')} item={c} ctx={ctx} />)}
          <button type="button" onClick={addCard} className={ADD} title="Conditions that must hold on one and the same spectrum">+ spectrum</button>
        </div>
      )}

      {advanced.map((a) => <AdvancedBlock key={a.path.join('.')} item={a} ctx={ctx} />)}
    </div>
  );
}

/** The chip that seeds an "any of" group: its first value writes `{ or: [node] }`. */
function GroupSeed({ def, level, ctx, onDone }: { def: PropertyDef; level: Level; ctx: CardsContext; onDone: () => void }) {
  return (
    <PropertyChip def={def} items={[]} level={level} ctx={{
      ...ctx,
      onChange: (next) => {
        // the chip appended a node at the level's end: wrap that node into a group
        const added = findAppended(ctx.nodes, next, level);
        ctx.onChange(added ? replaceAt(next, added.path, { or: [added.node] }) : next);
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

/** One spectrum of an object: everything inside holds on the same spectrum. */
export function SpectrumCard({ item, ctx }: { item: SpectrumItem; ctx: CardsContext }) {
  const level: Level = { table: 'spectra', path: item.path };
  return (
    <div className={`rounded-lg border border-border bg-card p-3 border-l-[3px] ${item.not ? 'border-l-amber-600' : 'border-l-teal-600'}`}>
      <div className="flex items-center gap-2 mb-2">
        <select className={`${INPUT} ${CARET} py-0.5 text-sm font-semibold`} value={item.not ? 'not' : 'has'} onChange={(e) => ctx.onChange(setNegated(ctx.nodes, item.path, e.target.value === 'not'))}>
          <option value="has">one spectrum with all of</option>
          <option value="not">no spectrum with all of</option>
        </select>
        <span className="flex-1" />
        <button type="button" onClick={() => ctx.onChange(replaceAt(ctx.nodes, item.path, null))} className={ICON_BTN} aria-label="Remove spectrum">
          <X className="w-4 h-4" />
        </button>
      </div>
      <NounBody noun="spectrum" items={item.items} level={level} ctx={ctx} />
    </div>
  );
}
