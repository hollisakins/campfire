'use client';

/**
 * The Advanced modal (Catalog Read Model phase 3b). The predicate tree is the
 * filter state and is shown live; the pills along the top are fast ways to
 * drop a node into it (Program, Field, … edit their own node in the tree,
 * Line and Photometry add one each time, so two lines are two clicks). The
 * same dialog serves the table page and the map; a toggle shows the SQL the
 * tree compiles to.
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Check, Copy, X } from 'lucide-react';
import { FilterChip, type FilterOption } from '@/components/ui/FilterChip';
import { RangeFilterChip } from '@/components/ui/RangeFilterChip';
import { CoordinateSearchChip } from '@/components/ui/CoordinateSearchChip';
import { DQ_FLAGS, REDSHIFT_QUALITY } from '@/lib/flags';
import { GRATINGS, type Program } from '@/lib/types';
import type { ViewMode } from '@/lib/actions/spectra-types';
import type { FilterOptions } from '@/lib/actions/filter-params';
import { OBSERVATION_COLORS } from '@/components/map/observation-colors';
import { LINE_FILTER_OPTIONS } from '@/lib/linelist';
import { getAvailableLists } from '@/lib/actions/lists';
import { rootOf, setSlots, slotFilters } from '@/lib/catalog-query/legacy';
import { treeOf, type TreeNode } from '@/lib/catalog-query/tree';
import { fetchJson } from '@/lib/fetch-json';
import type { CatalogQuerySqlResponse } from '@/app/api/catalog-query/sql/route';
import { AddSelect, TreeEditor, type ConditionChoices } from './ConditionsEditor';

export type AdvancedFilterOptions = FilterOptions;

interface AdvancedFiltersPanelProps {
  isOpen: boolean;
  onClose: () => void;
  filters: FilterOptions;
  onFiltersChange: (filters: FilterOptions) => void;
  availablePrograms?: Program[];
  availableFields?: string[];
  availableObservations?: string[];
  /** Photometry bands the Photometry pill can offer (/api/filter-options). */
  availableBands?: string[];
  viewMode?: ViewMode;
  /** The map has its own position tools: no position pill there. */
  hidePosition?: boolean;
  title?: string;
}

// Shortcut for "All inspected" quality values (everything except "Not Inspected")
const INSPECTED_QUALITY_VALUES = [1, 2, 3, 4];

/** The main filter bar's own pills; the Advanced badge counts the rest of the tree. */
export const BAR_SLOT_KEYS: (keyof FilterOptions)[] = ['programs', 'fields', 'observations', 'redshift_quality', 'redshift_min', 'redshift_max', 'needs_review'];

const PILL = 'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm font-medium border transition-all duration-150 bg-card border-border text-text-secondary hover:border-text-secondary dark:hover:border-border-strong hover:text-text-primary';
const ADD_SELECT = `${PILL} appearance-none pr-7 bg-[length:12px] bg-[right_10px_center] bg-no-repeat cursor-pointer`;

function Divider() {
  return <div className="h-6 w-px bg-border mx-1" />;
}

export function AdvancedFiltersPanel({
  isOpen,
  onClose,
  filters,
  onFiltersChange,
  availablePrograms = [],
  availableFields = [],
  availableObservations = [],
  availableBands = [],
  viewMode = 'objects',
  hidePosition = false,
  title = 'Advanced',
}: AdvancedFiltersPanelProps) {
  const root = rootOf(viewMode);
  const isSpectra = viewMode === 'spectra';
  const conditions = filters.conditions;
  const setConditions = (next: TreeNode[]) => onFiltersChange({ ...filters, conditions: next });
  // the pills read their own node of the tree and write it back
  const slots = useMemo(() => slotFilters(conditions, root), [conditions, root]);
  const patch = (p: Partial<FilterOptions>) => setConditions(setSlots(conditions, p, root));
  const add = (node: TreeNode) => setConditions([...conditions, node]);

  // the viewer's tags, once per opening
  const [listOptions, setListOptions] = useState<FilterOption[]>([]);
  useEffect(() => {
    if (!isOpen) return;
    getAvailableLists().then(({ lists }) => {
      setListOptions(lists.map((l) => ({ value: l.id, label: l.name, color: l.color ?? undefined, icon: l.icon ?? undefined })));
    });
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = '';
    };
  }, [isOpen, onClose]);

  const programOptions: FilterOption[] = useMemo(() => [...availablePrograms]
    .sort((a, b) => (a.jwst_pids?.length ? Math.max(...a.jwst_pids) : 0) - (b.jwst_pids?.length ? Math.max(...b.jwst_pids) : 0))
    .map((p) => ({ value: p.slug, label: p.program_name || p.slug })), [availablePrograms]);
  const fieldOptions: FilterOption[] = useMemo(() => availableFields.map((f) => ({ value: f, label: f.toUpperCase() })), [availableFields]);
  const observationOptions: FilterOption[] = useMemo(() => availableObservations.map((o, idx) => ({
    value: o, label: o, color: OBSERVATION_COLORS[idx % OBSERVATION_COLORS.length],
  })), [availableObservations]);
  const qualityOptions: FilterOption[] = REDSHIFT_QUALITY.map((q) => ({ value: q.value, label: q.label, icon: q.icon, color: q.color }));
  const gratingOptions: FilterOption[] = GRATINGS.map((g) => ({ value: g, label: g }));
  const dqFlagOptions: FilterOption[] = DQ_FLAGS.map((f) => ({ value: f.value, label: f.label, icon: f.icon, color: f.color }));
  const lineOptions: FilterOption[] = LINE_FILTER_OPTIONS.map((o) => ({
    value: o.value,
    label: `${o.label}  ${o.wave < 10000 ? o.wave.toFixed(0) : (o.wave / 1e4).toFixed(2) + ' µm'}`,
    short: o.label,
  }));
  const bandOptions: FilterOption[] = availableBands.map((b) => ({ value: b, label: b.toUpperCase() }));

  const choices: ConditionChoices = useMemo(() => ({
    programs: programOptions.map((o) => ({ value: o.value, label: o.label })),
    observations: observationOptions.map((o) => ({ value: o.value, label: o.label })),
    fields: availableFields.map((f) => ({ value: f, label: f.toUpperCase() })),
    bands: availableBands.map((b) => ({ value: b, label: b.toUpperCase() })),
    lists: listOptions.map((o) => ({ value: o.value, label: o.label })),
  }), [programOptions, observationOptions, availableFields, availableBands, listOptions]);

  // Tree | SQL: the same compiler the queries run
  const [view, setView] = useState<'tree' | 'sql'>('tree');
  const [sql, setSql] = useState<{ text: string; error?: string } | null>(null);
  const sqlRequest = useRef(0);
  const tree = useMemo(() => treeOf(conditions), [conditions]);
  const sqlText = sql?.text ? `SELECT r.*\nFROM public.${root} r\nWHERE ${sql.text};` : '';
  useEffect(() => {
    if (!isOpen || view !== 'sql') return;
    if (!tree) { setSql({ text: '' }); return; }
    const id = ++sqlRequest.current;
    const t = setTimeout(() => {
      fetchJson<CatalogQuerySqlResponse>(`/api/catalog-query/sql?root=${root}&q=${encodeURIComponent(JSON.stringify(tree))}`)
        .then((res) => { if (sqlRequest.current === id) setSql({ text: res.sql }); })
        .catch((e) => { if (sqlRequest.current === id) setSql({ text: '', error: e instanceof Error ? e.message : 'Could not compile' }); });
    }, 250);
    return () => clearTimeout(t);
  }, [isOpen, view, tree, root]);

  const [copied, setCopied] = useState(false);
  const copySql = async () => {
    if (!sqlText) { setView('sql'); return; }
    await navigator.clipboard.writeText(sqlText);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-[10000]">
      <div className="absolute inset-0 bg-slate-900/60 dark:bg-black/70" onClick={onClose} />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-[min(960px,94vw)] max-h-[88vh] flex flex-col bg-background border border-border rounded-2xl shadow-2xl overflow-hidden"
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-border bg-surface-2">
          <div className="flex items-center gap-2">
            <h2 className="text-base font-semibold text-text-primary">{title}</h2>
            {conditions.length > 0 && (
              <span className="px-1.5 py-0.5 text-[10px] font-bold rounded bg-primary text-on-primary">{conditions.length}</span>
            )}
          </div>
          <button onClick={onClose} className="p-2 rounded-lg text-text-secondary hover:bg-card-hover hover:text-text-primary transition-colors" aria-label="Close">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* The pills: each drops a node into the tree */}
        <div className="px-5 py-3 border-b border-border">
          <div className="flex flex-wrap items-center gap-2">
            <FilterChip label="Program" options={programOptions} selected={slots.programs}
              onChange={(s) => patch({ programs: s as string[] })} searchable
              footerLink={{ label: 'Browse all programs', href: '/nirspec/metadata' }} />
            <FilterChip label="Field" options={fieldOptions} selected={slots.fields}
              onChange={(s) => patch({ fields: s as string[] })} searchable />
            <FilterChip label="Observation" options={observationOptions} selected={slots.observations}
              onChange={(s) => patch({ observations: s as string[] })} searchable />
            <RangeFilterChip label="Redshift" min={slots.redshift_min} max={slots.redshift_max}
              onChange={(min, max) => patch({ redshift_min: min, redshift_max: max })}
              minBound={0} maxBound={15} step={0.1} precision={2} />
            <FilterChip label="Quality" options={qualityOptions} selected={slots.redshift_quality}
              onChange={(s) => patch({ redshift_quality: s as number[] })}
              shortcut={{ label: 'All inspected', values: INSPECTED_QUALITY_VALUES }}
              extraCheckbox={{
                label: 'Needs review only',
                checked: slots.needs_review === true,
                onChange: (checked) => patch({ needs_review: checked ? true : null }),
              }} />
            {!hidePosition && (
              <CoordinateSearchChip value={slots.coordinate_search} onChange={(v) => patch({ coordinate_search: v })} />
            )}
            <Divider />
            <FilterChip label="Grating" options={gratingOptions} selected={slots.gratings}
              onChange={(s) => patch({ gratings: s as string[], gratings_mode: 'any' })} />
            <RangeFilterChip label={isSpectra ? 'S/N' : 'Max S/N'} min={slots.max_snr_min} max={slots.max_snr_max}
              onChange={(min, max) => patch({ max_snr_min: min, max_snr_max: max })}
              minBound={0} maxBound={100000} step={1} precision={0}
              quickRanges={[{ label: '≥ 3', min: 3, max: null }, { label: '≥ 5', min: 5, max: null }, { label: '≥ 10', min: 10, max: null }, { label: '≥ 20', min: 20, max: null }]} />
            <RangeFilterChip label={isSpectra ? 'Exp. time (s)' : 'Max exp. time (s)'} min={slots.max_exposure_time_min} max={slots.max_exposure_time_max}
              onChange={(min, max) => patch({ max_exposure_time_min: min, max_exposure_time_max: max })}
              minBound={0} maxBound={50000} step={100} precision={0}
              quickRanges={[{ label: '≥ 1000', min: 1000, max: null }, { label: '≥ 5000', min: 5000, max: null }, { label: '≥ 10000', min: 10000, max: null }]} />
            {listOptions.length > 0 && (
              <FilterChip label="Tags" options={listOptions} selected={slots.list_ids}
                onChange={(s) => patch({ list_ids: s as number[], list_ids_mode: 'any' })} />
            )}
            {isSpectra && (
              <FilterChip label="Quality flags" options={dqFlagOptions} selected={slots.dq_flags}
                onChange={(s) => patch({ dq_flags: s as number[], dq_flags_mode: 'any' })} />
            )}
            <Divider />
            {/* adders: one node per pick, so several lines are several picks */}
            <FilterChip label="Line" options={lineOptions} selected={[]} multiSelect={false} searchable
              onChange={(s) => { const line = s[0] as string | undefined; if (line) add({ exists: 'lines', where: [{ col: 'line', op: 'eq', v: line }, { col: 'snr', op: 'gte', v: 3 }] }); }} />
            {bandOptions.length > 0 && (
              <FilterChip label="Photometry" options={bandOptions} selected={[]} multiSelect={false} searchable
                onChange={(s) => { const band = s[0] as string | undefined; if (band) add({ exists: 'photometry', where: [{ col: 'band', op: 'eq', v: band }, { col: 'mag', op: 'lte', v: 27 }] }); }} />
            )}
            <AddSelect table={root} choices={choices} label="More…" onAdd={add}
              className={`${ADD_SELECT} bg-[url('data:image/svg+xml;utf8,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 24 24%22 fill=%22none%22 stroke=%22%2394a3b8%22 stroke-width=%222.5%22><path d=%22m6 9 6 6 6-6%22/></svg>')]`} />
          </div>
        </div>

        {/* The tree, live, or its SQL */}
        <div className="flex-1 overflow-y-auto px-5 py-4">
          <div className="flex items-center justify-between mb-3">
            <div className="text-[11px] font-semibold uppercase tracking-wider text-text-tertiary">
              Query · {isSpectra ? 'spectra' : 'objects'} where all of
            </div>
            <div className="inline-flex rounded-md border border-border overflow-hidden text-xs">
              {(['tree', 'sql'] as const).map((v) => (
                <button key={v} type="button" onClick={() => setView(v)}
                  className={`px-2.5 py-1 ${view === v ? 'bg-primary/10 text-primary font-medium' : 'text-text-secondary hover:bg-card-hover'}`}>
                  {v === 'tree' ? 'Tree' : 'SQL'}
                </button>
              ))}
            </div>
          </div>
          {view === 'tree' ? (
            <TreeEditor root={root} conditions={conditions} onChange={setConditions} choices={choices} />
          ) : (
            <pre className="text-xs leading-relaxed whitespace-pre-wrap break-words rounded-lg border border-border bg-surface-2/40 p-3 font-mono text-text-primary min-h-[80px]">
              {sql?.error ? `-- ${sql.error}` : tree ? (sqlText || '-- compiling…') : `-- no conditions\nSELECT r.* FROM public.${root} r;`}
            </pre>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center gap-2 px-5 py-3 border-t border-border bg-card">
          <button onClick={() => setConditions([])} disabled={conditions.length === 0}
            className="px-3 py-2 text-sm font-medium rounded-lg border border-border text-text-secondary hover:bg-card-hover disabled:opacity-50 disabled:cursor-not-allowed transition-all">
            Clear
          </button>
          <button onClick={copySql} disabled={!tree}
            className="inline-flex items-center gap-1.5 px-3 py-2 text-sm font-medium rounded-lg border border-border text-text-secondary hover:bg-card-hover disabled:opacity-50 disabled:cursor-not-allowed transition-all"
            title="The SQL this query compiles to">
            {copied ? <Check className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
            {copied ? 'Copied' : 'Copy SQL'}
          </button>
          <div className="flex-1" />
          <button onClick={onClose}
            className="px-5 py-2 text-sm font-medium rounded-lg bg-primary text-on-primary hover:bg-primary-hover shadow-sm hover:shadow transition-all">
            Done
          </button>
        </div>
      </div>
    </div>
  );
}
