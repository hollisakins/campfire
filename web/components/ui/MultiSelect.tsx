'use client';

import React from 'react';
import { Button } from '@/components/ui/Button';

interface MultiSelectOption {
  id: number;
  name: string;
}

interface MultiSelectProps {
  options: MultiSelectOption[];
  selected: number[];
  onChange: (ids: number[]) => void;
  label?: string;
  maxHeight?: string;
}

export const MultiSelect: React.FC<MultiSelectProps> = ({
  options,
  selected,
  onChange,
  label,
  maxHeight = 'max-h-64',
}) => {
  const allSelected = options.length > 0 && selected.length === options.length;
  const someSelected = selected.length > 0 && selected.length < options.length;

  const handleSelectAll = () => {
    onChange(options.map(opt => opt.id));
  };

  const handleSelectNone = () => {
    onChange([]);
  };

  const handleToggle = (id: number) => {
    if (selected.includes(id)) {
      onChange(selected.filter(selectedId => selectedId !== id));
    } else {
      onChange([...selected, id]);
    }
  };

  return (
    <div className="space-y-2">
      {label && (
        <label className="block text-sm font-medium text-text-primary mb-2">
          {label}
        </label>
      )}

      <div className="flex gap-2 mb-2">
        <Button
          type="button"
          variant="secondary"
          onClick={handleSelectAll}
          disabled={allSelected}
          className="text-xs px-3 py-1"
        >
          Select All
        </Button>
        <Button
          type="button"
          variant="secondary"
          onClick={handleSelectNone}
          disabled={selected.length === 0}
          className="text-xs px-3 py-1"
        >
          Clear
        </Button>
        {selected.length > 0 && (
          <span className="text-xs text-text-secondary self-center ml-auto">
            {selected.length} of {options.length} selected
          </span>
        )}
      </div>

      <div className={`border border-border rounded-lg ${maxHeight} overflow-y-auto p-2`}>
        {options.length === 0 ? (
          <p className="text-sm text-text-secondary text-center py-4">
            No options available
          </p>
        ) : (
          <div className="space-y-1">
            {options.map((option) => (
              <label
                key={option.id}
                className="flex items-center gap-2 px-3 py-2 rounded hover:bg-card-hover cursor-pointer transition-colors"
              >
                <input
                  type="checkbox"
                  checked={selected.includes(option.id)}
                  onChange={() => handleToggle(option.id)}
                  className="w-4 h-4 text-primary border-border rounded focus:ring-2 focus:ring-primary cursor-pointer"
                />
                <span className="text-sm text-text-primary flex-1">
                  {option.name}
                </span>
              </label>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};
