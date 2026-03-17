'use client';

import React from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { GRATINGS, QUALITY_LABELS } from '@/lib/types';

interface FilterValues {
  programs: string[];
  fields: string[];
  gratings: string[];
  redshift_quality: number[];
}

interface SpectraFiltersProps {
  filters: FilterValues;
  onChange: (filters: FilterValues) => void;
  onClear: () => void;
  availablePrograms: { id: string; name: string }[];
  availableFields: string[];
}

export const SpectraFilters: React.FC<SpectraFiltersProps> = ({
  filters,
  onChange,
  onClear,
  availablePrograms,
  availableFields,
}) => {
  const handleCheckboxChange = (
    category: keyof FilterValues,
    value: string | number,
    checked: boolean
  ) => {
    const currentValues = filters[category] as (string | number)[];
    const newValues = checked
      ? [...currentValues, value]
      : currentValues.filter((v) => v !== value);

    onChange({
      ...filters,
      [category]: newValues,
    });
  };

  return (
    <Card className="p-6 sticky top-24">
      <h2 className="text-lg font-semibold text-text-primary mb-6">Filters</h2>

      {/* Programs */}
      <div className="mb-6">
        <h3 className="text-sm font-medium text-text-primary mb-3">Program</h3>
        <div className="space-y-2">
          {availablePrograms.map((program) => (
            <label key={program.id} className="flex items-center">
              <input
                type="checkbox"
                checked={filters.programs.includes(program.id)}
                onChange={(e) =>
                  handleCheckboxChange('programs', program.id, e.target.checked)
                }
                className="w-4 h-4 text-primary border-border rounded focus:ring-primary"
              />
              <span className="ml-2 text-sm text-text-primary">{program.name}</span>
            </label>
          ))}
        </div>
      </div>

      {/* Fields */}
      <div className="mb-6">
        <h3 className="text-sm font-medium text-text-primary mb-3">Field</h3>
        <div className="space-y-2">
          {availableFields.map((field) => (
            <label key={field} className="flex items-center">
              <input
                type="checkbox"
                checked={filters.fields.includes(field)}
                onChange={(e) =>
                  handleCheckboxChange('fields', field, e.target.checked)
                }
                className="w-4 h-4 text-primary border-border rounded focus:ring-primary"
              />
              <span className="ml-2 text-sm text-text-primary">{field}</span>
            </label>
          ))}
        </div>
      </div>

      {/* Gratings */}
      <div className="mb-6">
        <h3 className="text-sm font-medium text-text-primary mb-3">Grating</h3>
        <div className="space-y-2">
          {GRATINGS.map((grating) => (
            <label key={grating} className="flex items-center">
              <input
                type="checkbox"
                checked={filters.gratings.includes(grating)}
                onChange={(e) =>
                  handleCheckboxChange('gratings', grating, e.target.checked)
                }
                className="w-4 h-4 text-primary border-border rounded focus:ring-primary"
              />
              <span className="ml-2 text-sm font-mono text-text-primary">{grating}</span>
            </label>
          ))}
        </div>
      </div>

      {/* Redshift Quality */}
      <div className="mb-6">
        <h3 className="text-sm font-medium text-text-primary mb-3">Redshift Quality</h3>
        <div className="space-y-2">
          {QUALITY_LABELS.map((quality) => (
            <label key={quality.value} className="flex items-center">
              <input
                type="checkbox"
                checked={filters.redshift_quality.includes(quality.value)}
                onChange={(e) =>
                  handleCheckboxChange('redshift_quality', quality.value, e.target.checked)
                }
                className="w-4 h-4 text-primary border-border rounded focus:ring-primary"
              />
              <span className="ml-2 text-sm text-text-primary">{quality.label}</span>
            </label>
          ))}
        </div>
      </div>

      {/* Clear Filters Button */}
      <Button variant="secondary" size="sm" onClick={onClear} className="w-full">
        Clear Filters
      </Button>
    </Card>
  );
};
