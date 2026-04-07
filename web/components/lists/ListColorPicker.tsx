'use client';

import React from 'react';

const PRESET_COLORS = [
  '#ffcccb', // soft red (LRD)
  '#c8e6c9', // soft green (Broad Line)
  '#bbdefb', // soft blue (Lyα)
  '#e1bee7', // soft purple (Balmer Break)
  '#fff59d', // soft yellow (OIII)
  '#f398ad', // pink (Hα)
  '#d7ccc8', // taupe (Passive)
  '#ffccbc', // peach (Dusty)
  '#b2dfdb', // teal
  '#f0f4c3', // lime
  '#d1c4e9', // lavender
  '#cfd8dc', // blue-grey
];

interface ListColorPickerProps {
  value: string | null;
  onChange: (color: string | null) => void;
}

export function ListColorPicker({ value, onChange }: ListColorPickerProps) {
  return (
    <div className="flex flex-wrap gap-1.5">
      <button
        type="button"
        onClick={() => onChange(null)}
        className={`w-6 h-6 rounded-full border-2 flex items-center justify-center text-[10px] ${
          value === null
            ? 'border-primary ring-2 ring-primary/30'
            : 'border-border dark:border-slate-600'
        } bg-background dark:bg-slate-700`}
        title="No color"
      >
        <span className="text-text-secondary dark:text-slate-400">&times;</span>
      </button>
      {PRESET_COLORS.map((color) => (
        <button
          key={color}
          type="button"
          onClick={() => onChange(color)}
          className={`w-6 h-6 rounded-full border-2 transition-all ${
            value === color
              ? 'border-primary ring-2 ring-primary/30 scale-110'
              : 'border-transparent hover:border-border dark:hover:border-slate-500'
          }`}
          style={{ backgroundColor: color }}
          title={color}
        />
      ))}
    </div>
  );
}
