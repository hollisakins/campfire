'use client';

import React, { useState, useRef, useEffect } from 'react';
import { ChevronDown, X, MapPin } from 'lucide-react';
import { parseCoordinates, convertRadiusToDegrees } from '@/lib/utils/coordinate-parser';

export interface CoordinateSearchValue {
  ra: number;
  dec: number;
  radius: number;
  radius_unit: 'degrees' | 'arcmin' | 'arcsec';
}

interface CoordinateSearchChipProps {
  value?: CoordinateSearchValue | null;
  onChange: (value: CoordinateSearchValue | null) => void;
  className?: string;
}

export const CoordinateSearchChip: React.FC<CoordinateSearchChipProps> = ({
  value,
  onChange,
  className = '',
}) => {
  const [isOpen, setIsOpen] = useState(false);
  const [coordInput, setCoordInput] = useState('');
  const [radiusInput, setRadiusInput] = useState('1');
  const [unitInput, setUnitInput] = useState<'degrees' | 'arcmin' | 'arcsec'>('arcmin');
  const [validationError, setValidationError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // Sync local state with props
  useEffect(() => {
    if (value) {
      // Format coordinate for display
      setCoordInput(`${value.ra.toFixed(6)} ${value.dec.toFixed(6)}`);
      setRadiusInput(value.radius.toString());
      setUnitInput(value.radius_unit);
    } else {
      setCoordInput('');
      setRadiusInput('1');
      setUnitInput('arcmin');
    }
  }, [value]);

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

  // Validate coordinates on input change
  useEffect(() => {
    if (coordInput.trim() === '') {
      setValidationError(null);
      return;
    }

    const parsed = parseCoordinates(coordInput);
    if (parsed === null) {
      setValidationError('Invalid format. Use: "150.5 -2.3" or "10h02m30s -02d18m00s"');
    } else {
      setValidationError(null);
    }
  }, [coordInput]);

  const handleApply = () => {
    if (coordInput.trim() === '') {
      onChange(null);
      setIsOpen(false);
      return;
    }

    const parsed = parseCoordinates(coordInput);
    if (parsed === null) {
      setValidationError('Invalid coordinate format');
      return;
    }

    const radius = parseFloat(radiusInput);
    if (isNaN(radius) || radius <= 0) {
      setValidationError('Radius must be a positive number');
      return;
    }

    // Validate max radius (1 degree)
    const radiusDegrees = convertRadiusToDegrees(radius, unitInput);
    if (radiusDegrees > 1) {
      setValidationError('Maximum search radius is 1 degree');
      return;
    }

    onChange({
      ra: parsed.ra,
      dec: parsed.dec,
      radius,
      radius_unit: unitInput,
    });
    setIsOpen(false);
  };

  const handleClear = (e: React.MouseEvent) => {
    e.stopPropagation();
    setCoordInput('');
    setRadiusInput('1');
    setUnitInput('arcmin');
    setValidationError(null);
    onChange(null);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      handleApply();
    }
  };

  const isActive = value != null;

  const getDisplayText = () => {
    if (!isActive) return 'RA/Dec';
    const unitLabel = value.radius_unit === 'degrees' ? '°' :
                      value.radius_unit === 'arcmin' ? "'" : '"';
    return `RA/Dec: ${value.ra.toFixed(2)}°, ${value.dec.toFixed(2)}° (${value.radius}${unitLabel})`;
  };

  return (
    <div ref={containerRef} className={`relative inline-block ${className}`}>
      {/* Chip Button */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        className={`
          inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm font-medium
          border transition-all duration-150
          ${isActive
            ? 'bg-primary/10 border-primary text-primary'
            : 'bg-card border-border text-text-secondary hover:border-text-secondary hover:text-text-primary'
          }
        `}
      >
        <MapPin className="w-3.5 h-3.5 flex-shrink-0" />
        <span className="truncate max-w-[250px]">{getDisplayText()}</span>
        {isActive ? (
          <X
            className="w-3.5 h-3.5 hover:text-primary-hover cursor-pointer flex-shrink-0"
            onClick={handleClear}
          />
        ) : (
          <ChevronDown className={`w-3.5 h-3.5 transition-transform flex-shrink-0 ${isOpen ? 'rotate-180' : ''}`} />
        )}
      </button>

      {/* Dropdown */}
      {isOpen && (
        <div className="absolute z-50 mt-1 w-[320px] bg-background border border-border rounded-lg shadow-lg p-4">
          <div className="space-y-4">
            {/* Coordinate input */}
            <div>
              <label className="block text-xs text-text-secondary mb-1">
                Coordinates (RA Dec)
              </label>
              <input
                type="text"
                value={coordInput}
                onChange={(e) => setCoordInput(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="150.5 -2.3  or  10h02m30s -02d18m00s"
                className={`w-full px-3 py-2 text-sm border rounded-md bg-background text-text-primary focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent font-mono
                  ${validationError ? 'border-red-500' : 'border-border'}
                `}
              />
              {validationError && (
                <p className="text-xs text-red-500 mt-1">{validationError}</p>
              )}
              <p className="text-xs text-text-secondary mt-1">
                Decimal or hmsdms
              </p>
            </div>

            {/* Radius input with units */}
            <div>
              <label className="block text-xs text-text-secondary mb-1">
                Search radius (max 1 degree)
              </label>
              <div className="flex gap-2">
                <input
                  type="number"
                  value={radiusInput}
                  onChange={(e) => setRadiusInput(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder="1"
                  min="0"
                  step="0.1"
                  className="w-24 px-3 py-2 text-sm border border-border rounded-md bg-background text-text-primary focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
                />
                <select
                  value={unitInput}
                  onChange={(e) => setUnitInput(e.target.value as 'degrees' | 'arcmin' | 'arcsec')}
                  className="flex-1 px-3 py-2 text-sm border border-border rounded-md bg-background text-text-primary focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
                >
                  <option value="arcsec">arcseconds</option>
                  <option value="arcmin">arcminutes</option>
                  <option value="degrees">degrees</option>
                </select>
              </div>
            </div>

            {/* Action buttons */}
            <div className="flex gap-2 pt-2 border-t border-border">
              <button
                onClick={() => {
                  setCoordInput('');
                  setRadiusInput('1');
                  setUnitInput('arcmin');
                  setValidationError(null);
                }}
                className="flex-1 px-3 py-1.5 text-sm text-text-secondary hover:text-text-primary transition-colors"
              >
                Clear
              </button>
              <button
                onClick={handleApply}
                disabled={validationError !== null && coordInput.trim() !== ''}
                className="flex-1 px-3 py-1.5 text-sm bg-primary text-white rounded-md hover:bg-primary-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Apply
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
