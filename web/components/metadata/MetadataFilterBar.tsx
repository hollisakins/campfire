'use client';

import React, { useMemo } from 'react';
import { Search, X } from 'lucide-react';
import { FilterChip, type FilterOption } from '@/components/ui/FilterChip';
import type {
  MetadataFilters,
  MetadataTab,
} from '@/lib/actions/metadata-filters';
import type { ProgramOverview, ObservationOverview } from '@/lib/actions/programs';

interface MetadataFilterBarProps {
  tab: MetadataTab;
  filters: MetadataFilters;
  onChange: (next: MetadataFilters) => void;
  programs: ProgramOverview[];
  observations: ObservationOverview[];
  /** Buttons rendered to the right of the bar (e.g. bulk download actions). */
  rightSlot?: React.ReactNode;
}

const RECENCY_OPTIONS: FilterOption[] = [
  { value: 7, label: 'Last 7 days' },
  { value: 30, label: 'Last 30 days' },
  { value: 90, label: 'Last 90 days' },
];

function uniqueSorted<T extends string | number>(values: Array<T | null | undefined>): T[] {
  const set = new Set<T>();
  for (const v of values) {
    if (v !== null && v !== undefined && v !== '') set.add(v);
  }
  return Array.from(set).sort((a, b) => {
    if (typeof a === 'number' && typeof b === 'number') return a - b;
    return String(a).localeCompare(String(b));
  });
}

export const MetadataFilterBar: React.FC<MetadataFilterBarProps> = ({
  tab,
  filters,
  onChange,
  programs,
  observations,
  rightSlot,
}) => {
  // Derive option lists from the data being filtered.
  const cycleOptions: FilterOption[] = useMemo(
    () =>
      uniqueSorted(programs.map(p => p.cycle)).map(c => ({
        value: c as number,
        label: `Cycle ${c}`,
      })),
    [programs]
  );

  const piOptions: FilterOption[] = useMemo(
    () =>
      uniqueSorted(programs.map(p => p.pi_name)).map(name => ({
        value: name as string,
        label: name as string,
      })),
    [programs]
  );

  const fieldOptions: FilterOption[] = useMemo(() => {
    const source = tab === 'observations'
      ? observations.map(o => o.field)
      : programs.flatMap(p => p.fields);
    return uniqueSorted(source).map(f => ({ value: f as string, label: f as string }));
  }, [programs, observations, tab]);

  const gratingOptions: FilterOption[] = useMemo(() => {
    const source = tab === 'observations'
      ? observations.flatMap(o => o.gratings)
      : programs.flatMap(p => p.gratings);
    return uniqueSorted(source).map(g => ({ value: g as string, label: g as string }));
  }, [programs, observations, tab]);

  const programOptions: FilterOption[] = useMemo(
    () =>
      programs.map(p => ({
        value: p.slug,
        label: p.program_name || p.slug,
      })),
    [programs]
  );

  const reductionVersionOptions: FilterOption[] = useMemo(
    () =>
      uniqueSorted(observations.map(o => o.reduction_version)).map(v => ({
        value: v as string,
        label: v as string,
      })),
    [observations]
  );

  const crdsContextOptions: FilterOption[] = useMemo(
    () =>
      uniqueSorted(observations.map(o => o.crds_context)).map(v => ({
        value: v as string,
        label: v as string,
      })),
    [observations]
  );

  const update = (patch: Partial<MetadataFilters>) =>
    onChange({ ...filters, ...patch });

  return (
    <div className="flex flex-wrap items-center gap-2 mb-4">
      {/* Free-text search */}
      <div className="relative flex-1 min-w-[200px] max-w-md">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-text-secondary dark:text-slate-500" />
        <input
          type="text"
          value={filters.search}
          onChange={(e) => update({ search: e.target.value })}
          placeholder={
            tab === 'programs'
              ? 'Search programs, slug, PI, JWST PID…'
              : 'Search observation, program, field…'
          }
          className="w-full pl-9 pr-9 py-1.5 text-sm border border-border dark:border-slate-700 rounded-md bg-background dark:bg-slate-900 text-text-primary dark:text-slate-100 placeholder:text-text-secondary dark:placeholder:text-slate-500 focus:outline-none focus:ring-1 focus:ring-primary"
        />
        {filters.search && (
          <button
            onClick={() => update({ search: '' })}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-text-secondary dark:text-slate-500 hover:text-text-primary dark:hover:text-slate-200"
            aria-label="Clear search"
          >
            <X className="w-4 h-4" />
          </button>
        )}
      </div>

      {tab === 'programs' && (
        <>
          <FilterChip
            label="Cycle"
            options={cycleOptions}
            selected={filters.cycle}
            onChange={(s) => update({ cycle: s.map(Number) })}
          />
          <FilterChip
            label="PI"
            options={piOptions}
            selected={filters.pi}
            onChange={(s) => update({ pi: s.map(String) })}
            searchable
          />
          <FilterChip
            label="Public"
            options={[
              { value: 'true', label: 'Public only' },
              { value: 'false', label: 'Restricted only' },
            ]}
            selected={filters.is_public === null ? [] : [String(filters.is_public)]}
            onChange={(s) =>
              update({
                is_public: s.length === 0 ? null : s[0] === 'true',
              })
            }
            multiSelect={false}
          />
        </>
      )}

      {tab === 'observations' && (
        <>
          <FilterChip
            label="Program"
            options={programOptions}
            selected={filters.programs}
            onChange={(s) => update({ programs: s.map(String) })}
            searchable
          />
          <FilterChip
            label="Reduction"
            options={reductionVersionOptions}
            selected={filters.reduction_version}
            onChange={(s) => update({ reduction_version: s.map(String) })}
            searchable
          />
          <FilterChip
            label="CRDS"
            options={crdsContextOptions}
            selected={filters.crds_context}
            onChange={(s) => update({ crds_context: s.map(String) })}
            searchable
          />
          <FilterChip
            label="Patches"
            options={[
              { value: 'true', label: 'Has patches' },
              { value: 'false', label: 'No patches' },
            ]}
            selected={filters.has_patches === null ? [] : [String(filters.has_patches)]}
            onChange={(s) =>
              update({ has_patches: s.length === 0 ? null : s[0] === 'true' })
            }
            multiSelect={false}
          />
        </>
      )}

      <FilterChip
        label="Field"
        options={fieldOptions}
        selected={filters.fields}
        onChange={(s) => update({ fields: s.map(String) })}
        searchable={fieldOptions.length > 8}
      />
      <FilterChip
        label="Grating"
        options={gratingOptions}
        selected={filters.gratings}
        onChange={(s) => update({ gratings: s.map(String) })}
      />
      <FilterChip
        label="Recency"
        options={RECENCY_OPTIONS}
        selected={filters.recency_days === null ? [] : [filters.recency_days]}
        onChange={(s) =>
          update({ recency_days: s.length === 0 ? null : Number(s[0]) })
        }
        multiSelect={false}
      />

      {rightSlot && <div className="ml-auto flex items-center gap-2">{rightSlot}</div>}
    </div>
  );
};
