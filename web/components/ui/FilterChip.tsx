'use client';

import React, { useState, useRef, useEffect } from 'react';
import { ChevronDown, X, Check, Search, HelpCircle } from 'lucide-react';
import Link from 'next/link';

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
}) => {
  const [isOpen, setIsOpen] = useState(false);
  const [searchTerm, setSearchTerm] = useState('');
  const containerRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);

  // Close dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
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

  // Filter options by search term
  const filteredOptions = searchable && searchTerm
    ? options.filter(opt => opt.label.toLowerCase().includes(searchTerm.toLowerCase()))
    : options;

  const handleToggle = (value: string | number) => {
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
    onChange([]);
  };

  const isActive = selected.length > 0;
  const displayText = isActive
    ? `${label} (${selected.length})`
    : label;

  return (
    <div ref={containerRef} className={`relative inline-block ${className}`}>
      {/* Chip Button */}
      <button
        onClick={() => !disabled && setIsOpen(!isOpen)}
        className={`
          inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm font-medium
          border transition-all duration-150
          ${disabled ? 'opacity-60 cursor-not-allowed' : ''}
          ${isActive
            ? 'bg-primary/10 border-primary text-primary'
            : 'bg-card dark:bg-slate-800 border-border dark:border-slate-700 text-text-secondary dark:text-slate-400 hover:border-text-secondary dark:hover:border-slate-600 hover:text-text-primary dark:hover:text-slate-200'
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
        <div
          className={`absolute z-50 mt-1 ${searchable ? 'min-w-[280px] max-w-[360px]' : 'min-w-[200px] max-w-[280px]'} bg-background dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg shadow-lg`}
          onKeyDown={(e) => {
            if (searchable && searchInputRef.current && e.key.length === 1 && !e.metaKey && !e.ctrlKey) {
              searchInputRef.current.focus();
            }
          }}
        >
          {/* Search input */}
          {searchable && (
            <div className="p-2 border-b border-border dark:border-slate-700">
              <div className="relative">
                <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-text-secondary dark:text-slate-500" />
                <input
                  ref={searchInputRef}
                  type="text"
                  value={searchTerm}
                  onChange={(e) => setSearchTerm(e.target.value)}
                  placeholder="Search..."
                  className="w-full pl-8 pr-3 py-1.5 text-sm border border-border dark:border-slate-700 rounded-md bg-background dark:bg-slate-900 text-text-primary dark:text-slate-100 placeholder:text-text-secondary dark:placeholder:text-slate-500 focus:outline-none focus:ring-1 focus:ring-primary focus:border-transparent"
                />
              </div>
            </div>
          )}

          {/* Scrollable checkbox list */}
          <div className="max-h-[300px] overflow-y-auto">
            <div className="p-1">
              {filteredOptions.map((option) => {
                const isSelected = selected.includes(option.value);
                return (
                  <button
                    key={option.value}
                    onClick={() => handleToggle(option.value)}
                    className="w-full flex items-center gap-3 px-3 py-2 text-sm text-left hover:bg-card-hover dark:hover:bg-slate-700 rounded-md transition-colors"
                  >
                    {/* Checkbox */}
                    <div
                      className={`
                        w-4 h-4 rounded border flex items-center justify-center flex-shrink-0 transition-all duration-200
                        ${isSelected ? 'bg-primary border-primary scale-110' : 'border-border dark:border-slate-600'}
                      `}
                      style={
                        isSelected && option.color
                          ? { backgroundColor: option.color, borderColor: darkenColor(option.color, 30) }
                          : undefined
                      }
                    >
                      {isSelected && <Check className="w-3 h-3 text-white" />}
                    </div>

                    {/* Icon */}
                    {option.icon && <span className="text-sm">{option.icon}</span>}

                    {/* Label */}
                    <span className={isSelected ? 'text-text-primary dark:text-slate-100' : 'text-text-secondary dark:text-slate-400'}>
                      {option.label}
                    </span>
                  </button>
                );
              })}

              {/* Empty state */}
              {filteredOptions.length === 0 && searchTerm && (
                <div className="px-3 py-4 text-sm text-text-secondary dark:text-slate-500 text-center">
                  No matches
                </div>
              )}
            </div>
          </div>

          {/* Shortcut button */}
          {shortcut && (
            <div className="border-t border-border dark:border-slate-700 p-2">
              <button
                onClick={() => {
                  onChange(shortcut.values);
                }}
                className="w-full px-3 py-1.5 text-sm text-primary hover:text-primary-hover hover:bg-primary/10 rounded-md text-left transition-colors font-medium"
              >
                {shortcut.label}
              </button>
            </div>
          )}

          {/* Clear all button */}
          {multiSelect && selected.length > 0 && (
            <div className={`${shortcut ? 'px-2 pb-2' : 'border-t border-border dark:border-slate-700 p-2'}`}>
              <button
                onClick={() => {
                  onChange([]);
                  setIsOpen(false);
                }}
                className="w-full px-3 py-1.5 text-sm text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200 hover:bg-card dark:hover:bg-slate-700 rounded-md text-left transition-colors"
              >
                Clear all
              </button>
            </div>
          )}

          {/* Footer link */}
          {footerLink && (
            <div className="border-t border-border dark:border-slate-700 px-3 py-2">
              <Link
                href={footerLink.href}
                className="flex items-center gap-1.5 text-xs text-text-secondary dark:text-slate-500 hover:text-primary transition-colors"
              >
                <HelpCircle className="w-3 h-3" />
                <span>{footerLink.label}</span>
              </Link>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
