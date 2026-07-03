'use client';

import React from 'react';
import { Search, X } from 'lucide-react';

// ---------------------------------------------------------------------------
// Declarative filter bar for admin lists, driven by the flat string facet
// model of useTableUrlState's flatFilterCodec ('' = unset). Three facet kinds
// cover the admin pages: pill rows for small enums (matching the existing
// deployments-page pills), labeled selects for larger option lists (matching
// the existing nircam-page dropdowns), and a debounced-by-the-hook search box.
// ---------------------------------------------------------------------------

export type FacetOption = { value: string; label: string };

export type FacetDescriptor =
  | { kind: 'pills'; key: string; options: FacetOption[]; allLabel?: string }
  | { kind: 'select'; key: string; label: string; options: FacetOption[]; allLabel?: string }
  | { kind: 'search'; key: string; placeholder: string };

interface AdminFilterBarProps {
  facets: FacetDescriptor[];
  values: Record<string, string>;
  onChange: (key: string, value: string) => void;
  onReset?: () => void;
  /** Right-aligned extra content (e.g. a Refresh button). */
  right?: React.ReactNode;
}

export function AdminFilterBar({ facets, values, onChange, onReset, right }: AdminFilterBarProps) {
  const anyActive = facets.some((f) => (values[f.key] ?? '') !== '');

  return (
    <div className="flex flex-wrap items-center gap-2 mb-4">
      {facets.map((facet) => {
        if (facet.kind === 'pills') {
          const current = values[facet.key] ?? '';
          const pills = [{ value: '', label: facet.allLabel ?? 'All' }, ...facet.options];
          return (
            <div key={facet.key} className="flex items-center gap-2">
              {pills.map((p) => (
                <button
                  key={p.value || '__all'}
                  onClick={() => onChange(facet.key, p.value)}
                  className={`px-3 py-1 rounded-full text-sm transition-colors ${
                    current === p.value
                      ? 'bg-primary text-on-primary'
                      : 'bg-card-hover text-text-secondary hover:text-text-primary'
                  }`}
                >
                  {p.label}
                </button>
              ))}
            </div>
          );
        }
        if (facet.kind === 'select') {
          return (
            <label key={facet.key} className="flex items-center gap-1.5 text-sm text-text-secondary">
              <span>{facet.label}</span>
              <select
                value={values[facet.key] ?? ''}
                onChange={(e) => onChange(facet.key, e.target.value)}
                className="px-2 py-1 border border-border-strong rounded-md bg-card text-text-primary text-sm focus:outline-none focus:ring-2 focus:ring-primary"
              >
                <option value="">{facet.allLabel ?? 'All'}</option>
                {facet.options.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </label>
          );
        }
        return (
          <div key={facet.key} className="relative">
            <Search className="w-4 h-4 text-text-secondary absolute left-2.5 top-1/2 -translate-y-1/2 pointer-events-none" />
            <input
              type="text"
              value={values[facet.key] ?? ''}
              onChange={(e) => onChange(facet.key, e.target.value)}
              placeholder={facet.placeholder}
              className="pl-8 pr-2 py-1 border border-border-strong rounded-md bg-card text-text-primary text-sm focus:outline-none focus:ring-2 focus:ring-primary w-56"
            />
          </div>
        );
      })}
      {anyActive && onReset && (
        <button
          onClick={onReset}
          className="flex items-center gap-1 px-2 py-1 text-sm text-text-secondary hover:text-text-primary transition-colors"
        >
          <X className="w-3.5 h-3.5" /> Clear
        </button>
      )}
      {right && <div className="ml-auto">{right}</div>}
    </div>
  );
}
