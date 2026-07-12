'use client';

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { ChevronDown, Search } from 'lucide-react';
import type { NircamFieldCard } from '@/lib/types';

// Compact volume label for the dropdown sublines (e.g. "743.6 GB").
const formatVolume = (bytes: number): string => {
  if (!bytes) return '—';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
};

interface FieldSelectorDropdownProps {
  fields: NircamFieldCard[];
  /** The currently-open field (route param). */
  current: string;
  /** Route for a chosen field; defaults to the field detail page. Lets
   *  sub-routes (e.g. cutouts) switch fields without leaving the tool. */
  linkTo?: (field: string) => string;
  className?: string;
}

/**
 * Searchable single-select field switcher for /nircam/[field] — sits top-left,
 * above the field header (wireframe: "Field COSMOS ▾"). Choosing a field
 * navigates to its detail route (or a sub-route via `linkTo`).
 */
export const FieldSelectorDropdown: React.FC<FieldSelectorDropdownProps> = ({
  fields,
  current,
  linkTo = (field) => `/nircam/${encodeURIComponent(field)}`,
  className = '',
}) => {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const rootRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  const currentCard = fields.find((f) => f.field === current);

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onClick);
    return () => document.removeEventListener('mousedown', onClick);
  }, [open]);

  // Focus search when opened
  useEffect(() => {
    if (open) {
      setQuery('');
      searchRef.current?.focus();
    }
  }, [open]);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return fields;
    return fields.filter(
      (f) =>
        f.field.toLowerCase().includes(q) ||
        f.display_name.toLowerCase().includes(q),
    );
  }, [fields, query]);

  const choose = (field: string) => {
    setOpen(false);
    if (field !== current) router.push(linkTo(field));
  };

  return (
    <div ref={rootRef} className={`relative inline-block ${className}`}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg bg-surface-2 border border-border-strong text-sm font-semibold text-text-primary hover:bg-card-hover transition-colors"
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span className="text-[11px] font-medium uppercase tracking-wider text-text-tertiary">
          Field
        </span>
        <span>{currentCard?.display_name ?? current.toUpperCase()}</span>
        <ChevronDown className="w-3.5 h-3.5 text-text-tertiary" />
      </button>

      {open && (
        <div className="absolute left-0 top-full mt-1.5 w-72 bg-card border border-border-strong rounded-xl shadow-xl z-30 overflow-hidden">
          <div className="flex items-center gap-2 border-b border-border px-3">
            <Search className="w-3.5 h-3.5 text-text-tertiary flex-none" />
            <input
              ref={searchRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Escape') setOpen(false);
                if (e.key === 'Enter' && matches.length === 1) choose(matches[0].field);
              }}
              placeholder="Search fields…"
              className="w-full bg-transparent py-2.5 text-sm text-text-primary placeholder:text-text-tertiary outline-none"
            />
          </div>
          <div className="max-h-72 overflow-auto p-1" role="listbox">
            {matches.length === 0 ? (
              <div className="px-3 py-2 text-sm text-text-tertiary">No match</div>
            ) : (
              matches.map((f) => {
                const isCurrent = f.field === current;
                return (
                  <button
                    key={f.field}
                    type="button"
                    role="option"
                    aria-selected={isCurrent}
                    onClick={() => choose(f.field)}
                    className={`w-full flex items-center justify-between gap-3 px-2.5 py-2 rounded-lg text-sm text-left transition-colors ${
                      isCurrent
                        ? 'bg-primary/10 text-primary'
                        : 'text-text-primary hover:bg-card-hover'
                    }`}
                  >
                    <span className="font-medium">{f.display_name}</span>
                    <span className={`font-mono text-[11px] ${isCurrent ? 'text-primary/75' : 'text-text-tertiary'}`}>
                      {f.n_filters} filt · {formatVolume(f.total_bytes)}
                    </span>
                  </button>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
};
