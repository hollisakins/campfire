'use client';

/**
 * The filter bar of the NIRSpec table (Catalog Read Model phase 3b, design
 * doc Figure 8): the search box, the familiar chips for the primary
 * properties, then one chip per other card of the tree (a measurement, a
 * Spectrum card, an "any of" group, an advanced node), so nothing active is
 * ever folded into a count. The chips read and write the tree in `?q=`; the
 * Filters button opens the modal over the same state.
 */
import React, { useState, useEffect, useMemo, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { X, Search, ChevronDown, SlidersHorizontal, Map } from 'lucide-react';
import { FiltersModal } from '@/components/filters/FiltersModal';
import { ItemChip, PropertyChip, type CardsContext, type Level } from '@/components/filters/FilterCards';
import { buildChoices, valueLabelFor } from '@/components/filters/filter-choices';
import { applyConditions } from '@/components/filters/filter-state';
import { rootOf } from '@/lib/catalog-query/legacy';
import {
  compileProperty, describeItem, findProperty, propertyDef, readConditions, replaceAt, rerootConditions,
  type Item, type PropertyItem,
} from '@/lib/catalog-query/vocabulary';
import { filtersToURLParams } from '@/lib/utils/url-params';
import type { Program } from '@/lib/types';
import { DEFAULT_FILTERS } from '@/lib/actions/filter-params';
import type { FilterOptions, FilterMode, SearchScope } from '@/lib/actions/filter-params';
import type { ViewMode } from '@/lib/actions/spectra-types';

// Re-export canonical types for backwards compatibility
export type { FilterOptions, FilterMode, SearchScope };
export type AdvancedFilterOptions = FilterOptions;
export { DEFAULT_FILTERS };

// Search scope options with labels and placeholders
function getSearchScopeOptions(viewMode?: ViewMode): { value: SearchScope; label: string; placeholder: string }[] {
  const idLabel = viewMode === 'objects' ? 'Object ID' : 'Target ID';
  return [
    { value: 'target_id', label: idLabel, placeholder: `Search by ${idLabel}...` },
    { value: 'my_comments', label: 'My Comments', placeholder: 'Search my comments...' },
    { value: 'all_comments', label: 'All Comments', placeholder: 'Search all comments...' },
  ];
}

interface SpectraFilterBarProps {
  filters: AdvancedFilterOptions;
  onFiltersChange: (filters: AdvancedFilterOptions) => void;
  availablePrograms: Program[];
  availableFields: string[];
  availableObservations: string[];
  /** Photometry bands the Photometry rows can offer (/api/filter-options). */
  availableBands?: string[];
  className?: string;
  isSearchDebouncing?: boolean;
  viewMode?: ViewMode;
}

/** The chips the bar always shows, in order. */
const BAR_KEYS = ['program', 'field', 'observation', 'redshift', 'quality'] as const;

export const SpectraFilterBar: React.FC<SpectraFilterBarProps> = ({
  filters,
  onFiltersChange,
  availablePrograms,
  availableFields,
  availableObservations,
  availableBands = [],
  className = '',
  viewMode,
}) => {
  const router = useRouter();
  // Local state for search input to keep it responsive during typing
  const [localSearch, setLocalSearch] = useState(filters.search);
  // Local state for scope dropdown
  const [scopeDropdownOpen, setScopeDropdownOpen] = useState(false);
  const scopeDropdownRef = useRef<HTMLDivElement>(null);
  // Modal state
  const [panelOpen, setPanelOpen] = useState(false);

  // Track the last value WE sent to the parent to distinguish our echoes from external changes
  const lastSentValueRef = useRef(filters.search);

  // Sync from props ONLY for external changes (e.g., URL navigation), not our own echoes
  useEffect(() => {
    if (filters.search === lastSentValueRef.current) return;
    setLocalSearch(filters.search);
    lastSentValueRef.current = filters.search;
  }, [filters.search]);

  // Debounce updates to parent - only propagate after 300ms of no typing
  useEffect(() => {
    if (localSearch === filters.search) return;
    const timer = setTimeout(() => {
      lastSentValueRef.current = localSearch;
      onFiltersChange({ ...filters, search: localSearch });
    }, 300);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [localSearch]); // Intentionally exclude filters and onFiltersChange to prevent loops

  // Close scope dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (scopeDropdownRef.current && !scopeDropdownRef.current.contains(event.target as Node)) {
        setScopeDropdownOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const searchScopeOptions = getSearchScopeOptions(viewMode);
  const currentScope = searchScopeOptions.find(s => s.value === filters.search_scope) || searchScopeOptions[0];
  const handleScopeChange = (scope: SearchScope) => {
    onFiltersChange({ ...filters, search_scope: scope });
    setScopeDropdownOpen(false);
  };

  // The tree is the state: the bar is one reading of it
  const root = rootOf(viewMode);
  const isSpectra = root === 'spectra';
  const reading = useMemo(() => readConditions(filters.conditions, root), [filters.conditions, root]);
  const choices = useMemo(
    () => buildChoices({ programs: availablePrograms, fields: availableFields, observations: availableObservations, bands: availableBands }),
    [availablePrograms, availableFields, availableObservations, availableBands],
  );
  const label = useMemo(() => valueLabelFor(choices), [choices]);
  const ctx: CardsContext = {
    root,
    nodes: filters.conditions,
    onChange: (next) => onFiltersChange(applyConditions(filters, next, root)),
    choices,
    label,
    showLine: filters.show_line,
    showBand: filters.show_band,
    onShowColumn: (kind, key) => onFiltersChange(kind === 'line' ? { ...filters, show_line: key } : { ...filters, show_band: key }),
    hideKeys: ['tag'],
  };

  // the bar's own chips: at the spectra root Program and Observation are the
  // spectrum's, the rest its object's
  const objectLevel: Level = isSpectra ? { table: 'spectra', path: [], itsObject: true } : { table: 'objects', path: [] };
  const barChips = BAR_KEYS.map((key) => {
    const scope = isSpectra && (key === 'program' || key === 'observation') ? 'spectrum' : 'object';
    const def = propertyDef(scope, key)!;
    const item = findProperty(reading.items, scope, key);
    const level: Level = scope === 'spectrum' ? { table: 'spectra', path: [] } : objectLevel;
    return { def, item, level };
  });
  const needsReview = findProperty(reading.items, 'object', 'needs_review');
  const barItems = new Set<Item>(barChips.map((c) => c.item).filter((it): it is PropertyItem => it !== undefined));
  if (needsReview) barItems.add(needsReview);
  const otherItems = reading.items.filter((it) => !barItems.has(it));

  const setNeedsReview = (checked: boolean) => {
    const def = propertyDef('object', 'needs_review')!;
    const node = checked ? compileProperty(def, { kind: 'bool', value: true }, objectLevel.table) : null;
    if (needsReview) ctx.onChange(replaceAt(filters.conditions, needsReview.path, node));
    else if (node) ctx.onChange([...filters.conditions, node]);
  };

  const hasActiveFilters = filters.conditions.length > 0 || filters.search.length > 0;
  const handleClearAll = () => {
    setLocalSearch('');
    onFiltersChange(DEFAULT_FILTERS);
  };
  const toneOf = (it: Item) => (it.kind === 'spectrum' ? 'spectrum' : it.kind === 'advanced' ? 'advanced' : 'default');

  return (
    <div className={`space-y-3 ${className}`}>
      {/* Search bar with scope dropdown */}
      <div className="relative max-w-md flex" ref={scopeDropdownRef}>
        <div className="relative">
          <button
            onClick={() => setScopeDropdownOpen(!scopeDropdownOpen)}
            className="flex items-center gap-1 px-3 py-2 text-sm border border-r-0 border-border rounded-l-lg bg-card text-text-primary hover:bg-card-hover transition-colors"
          >
            <span className="whitespace-nowrap">{currentScope.label}</span>
            <ChevronDown className={`w-3.5 h-3.5 transition-transform ${scopeDropdownOpen ? 'rotate-180' : ''}`} />
          </button>
          {scopeDropdownOpen && (
            <div className="absolute top-full left-0 mt-1 w-40 bg-background border border-border rounded-lg shadow-lg z-50 py-1">
              {searchScopeOptions.map((option) => (
                <button
                  key={option.value}
                  onClick={() => handleScopeChange(option.value)}
                  className={`w-full text-left px-3 py-2 text-sm hover:bg-card-hover transition-colors ${
                    filters.search_scope === option.value ? 'text-primary font-medium' : 'text-text-primary'
                  }`}
                >
                  {option.label}
                </button>
              ))}
            </div>
          )}
        </div>
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-text-secondary" />
          <input
            type="text"
            value={localSearch}
            onChange={(e) => setLocalSearch(e.target.value)}
            placeholder={currentScope.placeholder}
            className="w-full pl-10 pr-10 py-2 text-sm border border-border rounded-r-lg bg-background text-text-primary placeholder:text-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
          />
          {localSearch && (
            <button
              onClick={() => setLocalSearch('')}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-text-secondary hover:text-text-primary"
            >
              <X className="w-4 h-4" />
            </button>
          )}
        </div>
      </div>

      {/* The chips */}
      <div className="flex flex-wrap items-center gap-2">
        {barChips.map(({ def, item, level }, i) => (
          <React.Fragment key={`${def.scope}.${def.key}`}>
            {i === 3 && <div className="h-6 w-px bg-border mx-1" />}
            <PropertyChip def={def} item={item} level={level} ctx={ctx}
              extraCheckbox={def.key === 'quality' ? {
                label: 'Needs review only',
                checked: needsReview?.value.kind === 'bool' && needsReview.value.value,
                onChange: setNeedsReview,
              } : undefined} />
          </React.Fragment>
        ))}

        {otherItems.length > 0 && <div className="h-6 w-px bg-border mx-1" />}
        {otherItems.map((it) => (
          <ItemChip key={it.path.join('.')} text={describeItem(it, label)} tone={toneOf(it)}
            onClick={() => setPanelOpen(true)}
            onRemove={() => ctx.onChange(replaceAt(filters.conditions, it.path, null))} />
        ))}

        <div className="h-6 w-px bg-border mx-1" />

        {/* Filters: the modal over the same state */}
        <button
          onClick={() => setPanelOpen(true)}
          className="inline-flex items-center gap-1.5 px-3 h-8 rounded-full text-sm font-medium border border-border bg-card text-text-secondary hover:border-text-secondary dark:hover:border-border-strong hover:text-text-primary transition-all duration-200"
        >
          <SlidersHorizontal className="w-4 h-4" />
          <span>Filters</span>
        </button>

        {/* Show on Map button */}
        <button
          onClick={() => {
            // the map queries objects: conditions written against spectra move over
            const mapFilters = isSpectra
              ? { ...filters, conditions: rerootConditions(filters.conditions, 'spectra', 'objects') }
              : filters;
            const filterParams = filtersToURLParams(mapFilters);
            // If exactly one field is selected, also set it as the map field param
            const field = findProperty(reading.items, 'object', 'field');
            if (field && field.value.kind === 'choice' && field.value.values.length === 1 && !field.value.not) {
              filterParams.set('field', String(field.value.values[0]));
            }
            const qs = filterParams.toString();
            router.push(`/map${qs ? `?${qs}` : ''}`);
          }}
          className="inline-flex items-center gap-1.5 px-3 h-8 rounded-full text-sm font-medium border border-border bg-card text-text-secondary hover:border-text-secondary dark:hover:border-border-strong hover:text-text-primary transition-all duration-200"
          title="Show filtered objects on map"
        >
          <Map className="w-4 h-4" />
          <span>Map</span>
        </button>

        {hasActiveFilters && (
          <>
            <div className="h-6 w-px bg-border mx-1" />
            <button
              onClick={handleClearAll}
              className="inline-flex items-center gap-1 px-3 py-1.5 text-sm text-text-secondary hover:text-text-primary transition-colors"
            >
              <X className="w-3.5 h-3.5" />
              Clear all
            </button>
          </>
        )}
      </div>

      <FiltersModal
        isOpen={panelOpen}
        onClose={() => setPanelOpen(false)}
        filters={filters}
        onApply={onFiltersChange}
        availablePrograms={availablePrograms}
        availableFields={availableFields}
        availableObservations={availableObservations}
        availableBands={availableBands}
        viewMode={viewMode}
      />
    </div>
  );
};
