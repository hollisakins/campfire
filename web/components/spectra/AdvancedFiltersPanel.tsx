'use client';

/**
 * The Advanced modal (Catalog Read Model phase 3b): every filter of the
 * catalog as the familiar pills — the basic ones the filter bar also shows,
 * kept in step since both edit the same state — plus the conditions zone,
 * where the predicate tree's free nodes are edited. One centred dialog on
 * the table page and the map.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Check, Copy, X } from 'lucide-react';
import { FilterChip, type FilterOption } from '@/components/ui/FilterChip';
import { FilterChipWithMode } from '@/components/ui/FilterChipWithMode';
import { RangeFilterChip } from '@/components/ui/RangeFilterChip';
import { CoordinateSearchChip } from '@/components/ui/CoordinateSearchChip';
import { DQ_FLAGS, REDSHIFT_QUALITY } from '@/lib/flags';
import { GRATINGS, type Program } from '@/lib/types';
import type { ViewMode } from '@/lib/actions/spectra-types';
import { DEFAULT_FILTERS, type FilterOptions } from '@/lib/actions/filter-params';
import { OBSERVATION_COLORS } from '@/components/map/observation-colors';
import { LINE_FILTER_OPTIONS } from '@/lib/linelist';
import { getAvailableLists } from '@/lib/actions/lists';
import { filterNodes, rootOf } from '@/lib/catalog-query/legacy';
import { treeOf } from '@/lib/catalog-query/tree';
import { fetchJson } from '@/lib/fetch-json';
import type { CatalogQuerySqlResponse } from '@/app/api/catalog-query/sql/route';
import { ConditionsEditor, type ConditionChoices } from './ConditionsEditor';

export type AdvancedFilterOptions = FilterOptions;

interface AdvancedFiltersPanelProps {
  isOpen: boolean;
  onClose: () => void;
  filters: FilterOptions;
  onFiltersChange: (filters: FilterOptions) => void;
  availablePrograms?: Program[];
  availableFields?: string[];
  availableObservations?: string[];
  /** Photometry bands the band filter can offer (/api/filter-options); none
   *  hides the band pill, since band names are per-field catalog config. */
  availableBands?: string[];
  viewMode?: ViewMode;
  /** The map has its own position tools: no position pill there. */
  hidePosition?: boolean;
  title?: string;
}

// Shortcut for "All inspected" quality values (everything except "Not Inspected")
const INSPECTED_QUALITY_VALUES = [1, 2, 3, 4];

/** The filters the modal owns beyond the filter bar's basics, for its badge. */
export function advancedFilterCount(filters: FilterOptions): number {
  return (
    (filters.coordinate_search !== null ? 1 : 0) +
    (filters.gratings?.length ?? 0) +
    (filters.max_snr_min !== null || filters.max_snr_max !== null ? 1 : 0) +
    (filters.max_exposure_time_min !== null || filters.max_exposure_time_max !== null ? 1 : 0) +
    (filters.line ? 1 : 0) +
    (filters.band ? 1 : 0) +
    (filters.list_ids?.length ?? 0) +
    (filters.dq_flags?.length ?? 0) +
    (filters.conditions?.length ?? 0)
  );
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="px-5 py-3.5 border-b border-border last:border-b-0">
      <div className="text-[11px] font-semibold uppercase tracking-wider text-text-tertiary mb-2">{label}</div>
      <div className="flex flex-wrap items-center gap-2">{children}</div>
    </div>
  );
}

function TogglePill({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <button type="button" onClick={() => onChange(!checked)}
      className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm font-medium border transition-all duration-150 ${
        checked ? 'bg-primary/10 border-primary text-primary' : 'bg-card border-border text-text-secondary hover:border-text-secondary dark:hover:border-border-strong hover:text-text-primary'
      }`}>
      {checked && <Check className="w-3.5 h-3.5" />}
      {label}
    </button>
  );
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

  const update = <K extends keyof FilterOptions>(key: K, value: FilterOptions[K]) => onFiltersChange({ ...filters, [key]: value });

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

  const activeCount =
    filters.programs.length + filters.fields.length + filters.observations.length + filters.redshift_quality.length +
    (filters.redshift_min !== null || filters.redshift_max !== null ? 1 : 0) + (filters.needs_review ? 1 : 0) +
    advancedFilterCount(filters);

  // "Copy as SQL": the same compiler the queries run, for the whole state
  const [copied, setCopied] = useState<'sql' | null>(null);
  const [sqlError, setSqlError] = useState<string | null>(null);
  const nodes = filterNodes(filters, root);
  const copySql = async () => {
    const tree = treeOf(nodes);
    if (!tree) return;
    setSqlError(null);
    try {
      const res = await fetchJson<CatalogQuerySqlResponse>(`/api/catalog-query/sql?root=${root}&q=${encodeURIComponent(JSON.stringify(tree))}`);
      await navigator.clipboard.writeText(`SELECT r.*\nFROM public.${root} r\nWHERE ${res.sql};`);
      setCopied('sql');
      setTimeout(() => setCopied(null), 1500);
    } catch (e) {
      setSqlError(e instanceof Error ? e.message : 'Could not compile');
    }
  };

  const clearAll = () => onFiltersChange({ ...DEFAULT_FILTERS, search: filters.search, search_scope: filters.search_scope });

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-[10000]">
      <div className="absolute inset-0 bg-slate-900/60 dark:bg-black/70" onClick={onClose} />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-[min(920px,94vw)] max-h-[88vh] flex flex-col bg-background border border-border rounded-2xl shadow-2xl overflow-hidden"
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-border bg-surface-2">
          <div className="flex items-center gap-2">
            <h2 className="text-base font-semibold text-text-primary">{title}</h2>
            {activeCount > 0 && (
              <span className="px-1.5 py-0.5 text-[10px] font-bold rounded bg-primary text-on-primary">{activeCount}</span>
            )}
          </div>
          <button onClick={onClose} className="p-2 rounded-lg text-text-secondary hover:bg-card-hover hover:text-text-primary transition-colors" aria-label="Close">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto pb-24">
          <Section label="Catalog">
            <FilterChip label="Program" options={programOptions} selected={filters.programs}
              onChange={(s) => update('programs', s as string[])} searchable
              footerLink={{ label: 'Browse all programs', href: '/nirspec/metadata' }} />
            <FilterChip label="Field" options={fieldOptions} selected={filters.fields}
              onChange={(s) => update('fields', s as string[])} searchable />
            <FilterChip label="Observation" options={observationOptions} selected={filters.observations}
              onChange={(s) => update('observations', s as string[])} searchable />
            {listOptions.length > 0 && (
              <FilterChipWithMode label="Tags" options={listOptions} selected={filters.list_ids}
                onChange={(s) => update('list_ids', s as number[])}
                mode={filters.list_ids_mode} onModeChange={(m) => update('list_ids_mode', m)} />
            )}
          </Section>

          <Section label="Redshift">
            <RangeFilterChip label="Redshift" min={filters.redshift_min} max={filters.redshift_max}
              onChange={(min, max) => onFiltersChange({ ...filters, redshift_min: min, redshift_max: max })}
              minBound={0} maxBound={15} step={0.1} precision={2} />
            <FilterChip label="Quality" options={qualityOptions} selected={filters.redshift_quality}
              onChange={(s) => update('redshift_quality', s as number[])}
              shortcut={{ label: 'All inspected', values: INSPECTED_QUALITY_VALUES }}
              extraCheckbox={{
                label: 'Needs review only',
                checked: filters.needs_review === true,
                onChange: (checked) => update('needs_review', checked ? true : null),
              }} />
            {!hidePosition && (
              <CoordinateSearchChip value={filters.coordinate_search} onChange={(v) => update('coordinate_search', v)} />
            )}
          </Section>

          <Section label="Spectra">
            <FilterChipWithMode label="Gratings" options={gratingOptions} selected={filters.gratings}
              onChange={(s) => update('gratings', s as string[])}
              mode={filters.gratings_mode} onModeChange={(m) => update('gratings_mode', m)}
              showModeToggle={!isSpectra} />
            <RangeFilterChip label={isSpectra ? 'S/N' : 'Max S/N'} min={filters.max_snr_min} max={filters.max_snr_max}
              onChange={(min, max) => onFiltersChange({ ...filters, max_snr_min: min, max_snr_max: max })}
              minBound={0} maxBound={100000} step={1} precision={0}
              quickRanges={[{ label: '≥ 3', min: 3, max: null }, { label: '≥ 5', min: 5, max: null }, { label: '≥ 10', min: 10, max: null }, { label: '≥ 20', min: 20, max: null }]} />
            <RangeFilterChip label={isSpectra ? 'Exp. time (s)' : 'Max exp. time (s)'} min={filters.max_exposure_time_min} max={filters.max_exposure_time_max}
              onChange={(min, max) => onFiltersChange({ ...filters, max_exposure_time_min: min, max_exposure_time_max: max })}
              minBound={0} maxBound={50000} step={100} precision={0}
              quickRanges={[{ label: '≥ 1000', min: 1000, max: null }, { label: '≥ 5000', min: 5000, max: null }, { label: '≥ 10000', min: 10000, max: null }]} />
            {isSpectra && (
              <FilterChipWithMode label="Quality flags" options={dqFlagOptions} selected={filters.dq_flags}
                onChange={(s) => update('dq_flags', s as number[])}
                mode={filters.dq_flags_mode} onModeChange={(m) => update('dq_flags_mode', m)} />
            )}
          </Section>

          <Section label="Measurements">
            <FilterChip label="Line" options={lineOptions} selected={filters.line ? [filters.line] : []} multiSelect={false} searchable
              onChange={(s) => {
                const line = (s[0] as string | undefined) ?? null;
                onFiltersChange({ ...filters, line, ...(line ? {} : { line_snr_min: null, line_snr_max: null, line_include_stale: false }) });
              }} />
            {filters.line && (
              <>
                <RangeFilterChip label="Line S/N" min={filters.line_snr_min} max={filters.line_snr_max}
                  onChange={(min, max) => onFiltersChange({ ...filters, line_snr_min: min, line_snr_max: max })}
                  minBound={-10} maxBound={10000} step={0.5} precision={1}
                  quickRanges={[{ label: '≥ 3', min: 3, max: null }, { label: '≥ 5', min: 5, max: null }, { label: '≥ 10', min: 10, max: null }, { label: '< 3', min: null, max: 3 }]} />
                <TogglePill label="Include stale fits" checked={filters.line_include_stale} onChange={(v) => update('line_include_stale', v)} />
              </>
            )}
            {bandOptions.length > 0 && (
              <FilterChip label="Band" options={bandOptions} selected={filters.band ? [filters.band] : []} multiSelect={false} searchable
                onChange={(s) => {
                  const band = (s[0] as string | undefined) ?? null;
                  onFiltersChange({ ...filters, band, ...(band ? {} : { band_mag_min: null, band_mag_max: null, band_snr_min: null, band_snr_max: null }) });
                }} />
            )}
            {filters.band && (
              <>
                <RangeFilterChip label="Mag (AB)" min={filters.band_mag_min} max={filters.band_mag_max}
                  onChange={(min, max) => onFiltersChange({ ...filters, band_mag_min: min, band_mag_max: max })}
                  minBound={0} maxBound={40} step={0.5} precision={2}
                  quickRanges={[{ label: '< 24', min: null, max: 24 }, { label: '< 25', min: null, max: 25 }, { label: '< 26', min: null, max: 26 }, { label: '< 27', min: null, max: 27 }]} />
                <RangeFilterChip label="Band S/N" min={filters.band_snr_min} max={filters.band_snr_max}
                  onChange={(min, max) => onFiltersChange({ ...filters, band_snr_min: min, band_snr_max: max })}
                  minBound={-100} maxBound={100000} step={0.5} precision={1}
                  quickRanges={[{ label: '≥ 3', min: 3, max: null }, { label: '≥ 5', min: 5, max: null }, { label: '≥ 10', min: 10, max: null }]} />
              </>
            )}
          </Section>

          <div className="px-5 py-3.5">
            <div className="flex items-baseline justify-between mb-2">
              <div className="text-[11px] font-semibold uppercase tracking-wider text-text-tertiary">Conditions</div>
              <div className="text-xs text-text-tertiary">Combined with the filters above</div>
            </div>
            <ConditionsEditor root={root} conditions={filters.conditions} onChange={(c) => update('conditions', c)} choices={choices} />
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center gap-2 px-5 py-3 border-t border-border bg-card">
          <button onClick={clearAll} disabled={activeCount === 0}
            className="px-3 py-2 text-sm font-medium rounded-lg border border-border text-text-secondary hover:bg-card-hover disabled:opacity-50 disabled:cursor-not-allowed transition-all">
            Clear all
          </button>
          <button onClick={copySql} disabled={nodes.length === 0}
            className="inline-flex items-center gap-1.5 px-3 py-2 text-sm font-medium rounded-lg border border-border text-text-secondary hover:bg-card-hover disabled:opacity-50 disabled:cursor-not-allowed transition-all"
            title="The SQL this query compiles to">
            {copied === 'sql' ? <Check className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
            {copied === 'sql' ? 'Copied' : 'Copy as SQL'}
          </button>
          {sqlError && <span className="text-xs text-red-500 truncate">{sqlError}</span>}
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
