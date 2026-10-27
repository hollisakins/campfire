'use client';

import React, { useState, useRef, useEffect } from 'react';
import { ChevronDown, X, Check, Minus, Search, HelpCircle } from 'lucide-react';
import Link from 'next/link';
import { Popover, isInsidePopover } from './Popover';

export interface FilterOption {
  value: string | number;
  label: string;
  short?: string;
  icon?: string;
  color?: string;
}

// Utility function to darken and saturate a hex color
function darkenColor(hex: string, percent: number): string {
  // Remove # if present
  const color = hex.replace('#', '');

  // Parse RGB components (0-255)
  const num = parseInt(color, 16);
  let r = (num >> 16) & 0xff;
  let g = (num >> 8) & 0xff;
  let b = num & 0xff;

  // Convert RGB to HSL
  r /= 255;
  g /= 255;
  b /= 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  let h = 0, s = 0, l = (max + min) / 2;

  if (max !== min) {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    switch (max) {
      case r: h = ((g - b) / d + (g < b ? 6 : 0)) / 6; break;
      case g: h = ((b - r) / d + 2) / 6; break;
      case b: h = ((r - g) / d + 4) / 6; break;
    }
  }

  // Increase saturation by 20% and darken by the specified percent
  s = Math.min(1, s * 1.2);
  l = l * (1 - percent / 100);

  // Convert HSL back to RGB
  const hue2rgb = (p: number, q: number, t: number) => {
    if (t < 0) t += 1;
    if (t > 1) t -= 1;
    if (t < 1/6) return p + (q - p) * 6 * t;
    if (t < 1/2) return q;
    if (t < 2/3) return p + (q - p) * (2/3 - t) * 6;
    return p;
  };

  let rOut, gOut, bOut;
  if (s === 0) {
    rOut = gOut = bOut = l;
  } else {
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;
    rOut = hue2rgb(p, q, h + 1/3);
    gOut = hue2rgb(p, q, h);
    bOut = hue2rgb(p, q, h - 1/3);
  }

  // Convert back to 0-255 range and hex
  const rHex = Math.round(rOut * 255);
  const gHex = Math.round(gOut * 255);
  const bHex = Math.round(bOut * 255);

  return '#' + ((rHex << 16) | (gHex << 8) | bHex).toString(16).padStart(6, '0');
}

interface ShortcutButton {
  label: string;
  values: (string | number)[];
}

interface FooterLink {
  label: string;
  href: string;
}

interface ExtraCheckbox {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}

interface FilterChipProps {
  label: string;
  options: FilterOption[];
  selected: (string | number)[];
  onChange: (selected: (string | number)[]) => void;
  multiSelect?: boolean;
  className?: string;
  disabled?: boolean;
  shortcut?: ShortcutButton;
  searchable?: boolean;
  footerLink?: FooterLink;
  /** Kept for callers; the dropdown now places itself from the viewport. */
  dropdownPlacement?: 'bottom' | 'top';
  /** Optional toggleable checkbox rendered below the options list, for filters that are
   *  conceptually related to the main filter but aren't one of its discrete values. */
  extraCheckbox?: ExtraCheckbox;
  /** Three-state options (phase 3b): a click includes a value, a second click
   *  excludes it, a third clears it. `excluded` is the excluded set, and
   *  every change reports both sets at once (one state, one commit). */
  excluded?: (string | number)[];
  onExcludedChange?: (selected: (string | number)[], excluded: (string | number)[]) => void;
  /** For a relation with several values per row: whether the included values
   *  are any of them or all of them. Shown once two are included. */
  mode?: 'any' | 'all';
  onModeChange?: (mode: 'any' | 'all') => void;
  /** Mount with the dropdown open (a chip the user just asked for). */
  defaultOpen?: boolean;
}

export const FilterChip: React.FC<FilterChipProps> = ({
  label,
  options,
  selected,
  onChange,
  multiSelect = true,
  className = '',
  disabled = false,
  shortcut,
  searchable = false,
  footerLink,
  extraCheckbox,
  excluded = [],
  onExcludedChange,
  mode = 'any',
  onModeChange,
  defaultOpen = false,
}) => {
  const triState = Boolean(onExcludedChange);
  const [isOpen, setIsOpen] = useState(defaultOpen);
  const [searchTerm, setSearchTerm] = useState('');
  const containerRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);

  // Close dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (!isInsidePopover(event.target, containerRef.current)) {
        setIsOpen(false);
      }
    };

    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  // Reset search when dropdown closes
  useEffect(() => {
    if (!isOpen) setSearchTerm('');
  }, [isOpen]);

  // Filter options by search term, and sort selected to top when searchable.
  // Never in a three-state chip: an option that moves under the pointer
  // breaks the second click.
  const filteredOptions = (() => {
    let opts = options;
    if (searchable && searchTerm) {
      opts = opts.filter(opt => opt.label.toLowerCase().includes(searchTerm.toLowerCase()));
    }
    if (searchable && !triState && selected.length > 0 && !searchTerm) {
      const selectedSet = new Set(selected);
      opts = [...opts].sort((a, b) => {
        const aSelected = selectedSet.has(a.value) ? 0 : 1;
        const bSelected = selectedSet.has(b.value) ? 0 : 1;
        return aSelected - bSelected;
      });
    }
    return opts;
  })();

  const handleToggle = (value: string | number) => {
    if (triState && onExcludedChange) {
      // none → included → excluded → none, reported as one change
      if (selected.includes(value)) onExcludedChange(selected.filter(v => v !== value), [...excluded, value]);
      else if (excluded.includes(value)) onExcludedChange(selected, excluded.filter(v => v !== value));
      else onExcludedChange([...selected, value], excluded);
      return;
    }
    if (multiSelect) {
      if (selected.includes(value)) {
        onChange(selected.filter(v => v !== value));
      } else {
        onChange([...selected, value]);
      }
    } else {
      onChange(selected.includes(value) ? [] : [value]);
      setIsOpen(false);
    }
  };

  const handleClear = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (onExcludedChange) onExcludedChange([], []); else onChange([]);
    if (extraCheckbox?.checked) extraCheckbox.onChange(false);
  };

  const activeCount = selected.length + excluded.length + (extraCheckbox?.checked ? 1 : 0);
  const isActive = activeCount > 0;
  // a single-select chip names its pick; a multi-select counts them
  const soleOption = !multiSelect && selected.length === 1 ? options.find((o) => o.value === selected[0]) : undefined;
  const nameOf = (v: string | number) => { const o = options.find((x) => x.value === v); return o ? (o.short ?? o.label) : String(v); };
  const displayText = soleOption
    ? `${label}: ${soleOption.short ?? soleOption.label}`
    : triState && isActive && selected.length + excluded.length <= 2
      ? `${label}: ${[...selected.map(nameOf), ...excluded.map((v) => `not ${nameOf(v)}`)].join(selected.length > 1 && mode === 'all' ? ' & ' : ', ')}`
      : isActive
        ? `${label} (${selected.length}${excluded.length ? `, not ${excluded.length}` : ''}${extraCheckbox?.checked ? ' +1' : ''})`
        : label;

  return (
    <div
      ref={containerRef}
      className={`relative inline-block ${className}`}
      onKeyDown={(e) => {
        if (isOpen && searchable && searchInputRef.current && e.key.length === 1 && !e.metaKey && !e.ctrlKey) {
          searchInputRef.current.focus();
        }
      }}
    >
      {/* Chip Button */}
      <button
        onClick={() => !disabled && setIsOpen(!isOpen)}
        className={`
          inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm font-medium
          border transition-all duration-150
          ${disabled ? 'opacity-60 cursor-not-allowed' : ''}
          ${isActive
            ? 'bg-primary/10 border-primary text-primary'
            : 'bg-card border-border text-text-secondary hover:border-text-secondary dark:hover:border-border-strong hover:text-text-primary'
          }
        `}
      >
        <span>{displayText}</span>
        {isActive ? (
          <X
            className="w-3.5 h-3.5 hover:text-primary-hover cursor-pointer"
            onClick={handleClear}
          />
        ) : (
          <ChevronDown className={`w-3.5 h-3.5 transition-transform ${isOpen ? 'rotate-180' : ''}`} />
        )}
      </button>

      {/* Dropdown */}
      {isOpen && !disabled && (
        <Popover
          anchorRef={containerRef}
          open={isOpen}
          onClose={() => setIsOpen(false)}
          className={`${searchable ? 'min-w-[280px] max-w-[360px]' : 'min-w-[200px] max-w-[280px]'} bg-card border border-border rounded-lg shadow-lg`}
        >
          {/* Search input */}
          {searchable && (
            <div className="p-2 border-b border-border">
              <div className="relative">
                <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-text-tertiary" />
                <input
                  ref={searchInputRef}
                  type="text"
                  value={searchTerm}
                  onChange={(e) => setSearchTerm(e.target.value)}
                  placeholder="Search..."
                  className="w-full pl-8 pr-3 py-1.5 text-sm border border-border rounded-md bg-background text-text-primary placeholder:text-text-tertiary focus:outline-none focus:ring-1 focus:ring-primary focus:border-transparent"
                />
              </div>
            </div>
          )}

          {/* Scrollable checkbox list */}
          <div className="max-h-[300px] overflow-y-auto">
            <div className="p-1">
              {filteredOptions.map((option) => {
                const isSelected = selected.includes(option.value);
                const isExcluded = triState && excluded.includes(option.value);
                return (
                  <button
                    key={option.value}
                    onClick={() => handleToggle(option.value)}
                    className="w-full flex items-center gap-3 px-3 py-2 text-sm text-left hover:bg-card-hover rounded-md transition-colors"
                    title={triState ? (isSelected ? 'Click again to exclude' : isExcluded ? 'Click again to clear' : 'Include') : undefined}
                  >
                    {/* Checkbox: included, excluded, or neither */}
                    <div
                      className={`
                        w-4 h-4 rounded border flex items-center justify-center flex-shrink-0 transition-all duration-200
                        ${isSelected ? 'bg-primary border-primary scale-110' : isExcluded ? 'bg-red-500/80 border-red-600 scale-110' : 'border-border dark:border-border-strong'}
                      `}
                      style={
                        isSelected && option.color
                          ? { backgroundColor: option.color, borderColor: darkenColor(option.color, 30) }
                          : undefined
                      }
                    >
                      {isSelected && <Check className="w-3 h-3 text-on-primary" />}
                      {isExcluded && <Minus className="w-3 h-3 text-white" />}
                    </div>

                    {/* Icon */}
                    {option.icon && <span className="text-sm">{option.icon}</span>}

                    {/* Label */}
                    <span className={isSelected ? 'text-text-primary' : isExcluded ? 'text-text-secondary line-through' : 'text-text-secondary'}>
                      {option.label}
                    </span>
                  </button>
                );
              })}

              {/* Empty state */}
              {filteredOptions.length === 0 && searchTerm && (
                <div className="px-3 py-4 text-sm text-text-secondary dark:text-text-tertiary text-center">
                  No matches
                </div>
              )}
            </div>
          </div>

          {/* any of / all of the included values: always in place for a
              multi-valued relation, live once two are included, so nothing
              moves when the second one is clicked */}
          {onModeChange && (
            <div className={`border-t border-border px-3 py-2 flex items-center gap-2 text-xs ${selected.length >= 2 ? 'text-text-secondary' : 'text-text-tertiary'}`}>
              <span>match</span>
              <div className={`inline-flex rounded-md border border-border overflow-hidden ${selected.length >= 2 ? '' : 'opacity-50'}`}>
                {(['any', 'all'] as const).map((m) => (
                  <button key={m} type="button" onClick={() => onModeChange(m)} disabled={selected.length < 2}
                    className={`px-2 py-0.5 ${mode === m ? 'bg-primary/10 text-primary font-medium' : 'hover:bg-card-hover'} disabled:cursor-default`}>
                    {m}
                  </button>
                ))}
              </div>
              <span>of the included</span>
            </div>
          )}

          {/* Extra checkbox (e.g., "Needs review only" for the Quality filter) */}
          {extraCheckbox && (
            <div className="border-t border-border p-1">
              <button
                onClick={() => extraCheckbox.onChange(!extraCheckbox.checked)}
                className="w-full flex items-center gap-3 px-3 py-2 text-sm text-left hover:bg-card-hover rounded-md transition-colors"
              >
                <div
                  className={`
                    w-4 h-4 rounded border flex items-center justify-center flex-shrink-0 transition-all duration-200
                    ${extraCheckbox.checked ? 'bg-primary border-primary scale-110' : 'border-border dark:border-border-strong'}
                  `}
                >
                  {extraCheckbox.checked && <Check className="w-3 h-3 text-on-primary" />}
                </div>
                <span className={extraCheckbox.checked ? 'text-text-primary' : 'text-text-secondary'}>
                  {extraCheckbox.label}
                </span>
              </button>
            </div>
          )}

          {/* Shortcut button */}
          {shortcut && (
            <div className="border-t border-border p-2">
              <button
                onClick={() => {
                  if (onExcludedChange) onExcludedChange(shortcut.values, []); else onChange(shortcut.values);
                }}
                className="w-full px-3 py-1.5 text-sm text-primary hover:text-primary-hover hover:bg-primary/10 rounded-md text-left transition-colors font-medium"
              >
                {shortcut.label}
              </button>
            </div>
          )}

          {/* Clear all button */}
          {multiSelect && (triState || selected.length > 0 || excluded.length > 0 || extraCheckbox?.checked) && (
            <div className={`${shortcut ? 'px-2 pb-2' : 'border-t border-border p-2'}`}>
              <button
                onClick={() => {
                  if (onExcludedChange) onExcludedChange([], []); else onChange([]);
                  if (extraCheckbox?.checked) extraCheckbox.onChange(false);
                  setIsOpen(false);
                }}
                disabled={selected.length === 0 && excluded.length === 0 && !extraCheckbox?.checked}
                className="w-full px-3 py-1.5 text-sm text-text-secondary hover:text-text-primary hover:bg-card-hover rounded-md text-left transition-colors disabled:opacity-50 disabled:hover:bg-transparent disabled:cursor-default"
              >
                Clear all
              </button>
            </div>
          )}

          {/* Footer link */}
          {footerLink && (
            <div className="border-t border-border px-3 py-2">
              <Link
                href={footerLink.href}
                className="flex items-center gap-1.5 text-xs text-text-secondary dark:text-text-tertiary hover:text-primary transition-colors"
              >
                <HelpCircle className="w-3 h-3" />
                <span>{footerLink.label}</span>
              </Link>
            </div>
          )}
        </Popover>
      )}
    </div>
  );
};
