'use client';

import React, { useMemo, useRef, useState, useEffect } from 'react';
import { Search, Telescope, MapPin, Users, X } from 'lucide-react';
import { useRouter } from 'next/navigation';
import type { ProgramOverview, ObservationOverview } from '@/lib/actions/programs';

type ResultKind = 'program' | 'observation' | 'pi';

interface SearchResult {
  kind: ResultKind;
  label: string;
  sublabel?: string;
  // Action target — interpreted by the consumer.
  programSlug?: string;
  observationName?: string;
  piName?: string;
}

interface MetadataSearchBarProps {
  programs: ProgramOverview[];
  observations: ObservationOverview[];
  /**
   * Current search text. Controlled so it stays in sync with the filter
   * state (typing here drives `filters.search` directly — there is no
   * second search bar in the filter row).
   */
  value: string;
  onChange: (value: string) => void;
  onSelectObservation: (obsName: string) => void;
  onSelectPi: (piName: string) => void;
}

const MAX_RESULTS = 8;

function matches(haystack: string | number | null | undefined, needle: string): boolean {
  if (haystack === null || haystack === undefined) return false;
  return String(haystack).toLowerCase().includes(needle);
}

export const MetadataSearchBar: React.FC<MetadataSearchBarProps> = ({
  programs,
  observations,
  value,
  onChange,
  onSelectObservation,
  onSelectPi,
}) => {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const piNames = useMemo(() => {
    const set = new Set<string>();
    for (const p of programs) if (p.pi_name) set.add(p.pi_name);
    return Array.from(set);
  }, [programs]);

  const results: SearchResult[] = useMemo(() => {
    const q = value.trim().toLowerCase();
    if (q.length < 1) return [];
    const out: SearchResult[] = [];

    for (const p of programs) {
      if (out.length >= MAX_RESULTS * 3) break;
      if (
        matches(p.program_name, q) ||
        matches(p.slug, q) ||
        p.jwst_pids.some((pid) => matches(pid, q))
      ) {
        out.push({
          kind: 'program',
          label: p.program_name || p.slug,
          sublabel: `${p.slug}${p.pi_name ? ` · PI ${p.pi_name}` : ''}`,
          programSlug: p.slug,
        });
      }
    }

    for (const o of observations) {
      if (out.length >= MAX_RESULTS * 3) break;
      if (matches(o.observation, q)) {
        out.push({
          kind: 'observation',
          label: o.observation,
          sublabel: `${o.program_name || o.program_slug} · ${o.field}`,
          observationName: o.observation,
        });
      }
    }

    for (const name of piNames) {
      if (out.length >= MAX_RESULTS * 3) break;
      if (matches(name, q)) {
        out.push({
          kind: 'pi',
          label: name,
          sublabel: 'Filter Programs by this PI',
          piName: name,
        });
      }
    }

    return out.slice(0, MAX_RESULTS);
  }, [value, programs, observations, piNames]);

  useEffect(() => {
    setHighlight(0);
  }, [value]);

  const select = (r: SearchResult) => {
    onChange('');
    setOpen(false);
    if (r.kind === 'program' && r.programSlug) {
      router.push(`/nirspec/metadata/programs/${r.programSlug}`);
    } else if (r.kind === 'observation' && r.observationName) {
      onSelectObservation(r.observationName);
    } else if (r.kind === 'pi' && r.piName) {
      onSelectPi(r.piName);
    }
  };

  const handleKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (!open || results.length === 0) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setHighlight((h) => Math.min(h + 1, results.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setHighlight((h) => Math.max(h - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      select(results[highlight]);
    } else if (e.key === 'Escape') {
      setOpen(false);
    }
  };

  const Icon = (kind: ResultKind) =>
    kind === 'program' ? Telescope : kind === 'observation' ? MapPin : Users;

  return (
    <div ref={containerRef} className="relative w-full max-w-xl">
      <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-text-secondary dark:text-slate-500" />
      <input
        type="text"
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={handleKey}
        placeholder="Search programs, observations, PIs, or JWST PIDs…"
        className="w-full pl-9 pr-9 py-2 text-sm border border-border dark:border-slate-700 rounded-md bg-background dark:bg-slate-900 text-text-primary dark:text-slate-100 placeholder:text-text-secondary dark:placeholder:text-slate-500 focus:outline-none focus:ring-1 focus:ring-primary"
      />
      {value && (
        <button
          onClick={() => {
            onChange('');
            setOpen(false);
          }}
          className="absolute right-3 top-1/2 -translate-y-1/2 text-text-secondary dark:text-slate-500 hover:text-text-primary dark:hover:text-slate-200"
          aria-label="Clear search"
        >
          <X className="w-4 h-4" />
        </button>
      )}

      {open && value.trim() && (
        <div className="absolute z-40 mt-1 w-full bg-background dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg shadow-lg overflow-hidden">
          {results.length === 0 ? (
            <div className="px-3 py-3 text-sm text-text-secondary dark:text-slate-500 text-center">
              No matches
            </div>
          ) : (
            <ul className="max-h-[320px] overflow-y-auto divide-y divide-border dark:divide-slate-700">
              {results.map((r, i) => {
                const ResultIcon = Icon(r.kind);
                return (
                  <li key={`${r.kind}:${r.label}:${i}`}>
                    <button
                      type="button"
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => select(r)}
                      onMouseEnter={() => setHighlight(i)}
                      className={`w-full flex items-start gap-3 px-3 py-2 text-left transition-colors ${
                        i === highlight
                          ? 'bg-card-hover dark:bg-slate-700/60'
                          : 'hover:bg-card-hover dark:hover:bg-slate-700/40'
                      }`}
                    >
                      <ResultIcon className="w-4 h-4 mt-0.5 flex-shrink-0 text-text-secondary dark:text-slate-400" />
                      <div className="min-w-0 flex-1">
                        <div className="text-sm font-medium text-text-primary dark:text-slate-100 truncate">
                          {r.label}
                        </div>
                        {r.sublabel && (
                          <div className="text-xs text-text-secondary dark:text-slate-400 truncate">
                            {r.sublabel}
                          </div>
                        )}
                      </div>
                      <span className="text-[10px] uppercase tracking-wider text-text-secondary dark:text-slate-500 self-center">
                        {r.kind}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
};
