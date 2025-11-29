'use client';

import React, { useState } from 'react';
import { Copy, Check } from 'lucide-react';
import { formatCoordinates } from '@/lib/utils/coordinates';

interface CoordinateDisplayProps {
  ra: number;
  dec: number;
}

export const CoordinateDisplay: React.FC<CoordinateDisplayProps> = ({ ra, dec }) => {
  const [copiedDecimal, setCopiedDecimal] = useState(false);
  const [copiedSexagesimal, setCopiedSexagesimal] = useState(false);

  const coords = formatCoordinates(ra, dec);

  const copyToClipboard = async (text: string, setCopied: (value: boolean) => void) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy:', err);
    }
  };

  return (
    <div className="flex items-center gap-2 text-sm">
      <span className="text-text-secondary">Coordinates:</span>

      {/* Decimal Degrees */}
      <button
        onClick={() => copyToClipboard(coords.decimal.combined, setCopiedDecimal)}
        className="inline-flex items-center gap-1 hover:bg-gray-100 px-2 py-1 rounded transition-colors group"
        title="Click to copy decimal coordinates"
      >
        <span className="font-mono text-text-primary">
          {coords.decimal.combined}
        </span>
        {copiedDecimal ? (
          <Check className="w-3 h-3 text-green-600" />
        ) : (
          <Copy className="w-3 h-3 text-gray-400 group-hover:text-gray-600" />
        )}
      </button>

      {/* Sexagesimal */}
      <button
        onClick={() => copyToClipboard(coords.sexagesimal.combined, setCopiedSexagesimal)}
        className="inline-flex items-center gap-1 hover:bg-gray-100 px-2 py-1 rounded transition-colors group"
        title="Click to copy sexagesimal coordinates"
      >
        <span className="font-mono text-text-primary">
          {coords.sexagesimal.combined}
        </span>
        {copiedSexagesimal ? (
          <Check className="w-3 h-3 text-green-600" />
        ) : (
          <Copy className="w-3 h-3 text-gray-400 group-hover:text-gray-600" />
        )}
      </button>
    </div>
  );
};
