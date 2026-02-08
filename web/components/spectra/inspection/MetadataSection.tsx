'use client';

import React from 'react';
import { Info } from 'lucide-react';
import type { SpectrumObject, Spectrum } from '@/lib/types';

interface MetadataSectionProps {
  spectrum: SpectrumObject;
  activeSpec: Spectrum;
}

const MetadataRow: React.FC<{ label: string; value: string; tooltip?: string }> = ({ label, value, tooltip }) => (
  <div className="flex justify-between items-center">
    <span className="text-text-secondary dark:text-slate-400" title={tooltip}>
      {label}:
    </span>
    <span className="font-mono text-text-primary dark:text-slate-100">
      {value}
    </span>
  </div>
);

export const MetadataSection: React.FC<MetadataSectionProps> = ({ spectrum, activeSpec }) => {
  return (
    <div className="p-4 border-b border-border dark:border-slate-700">
      <h3 className="text-xs font-semibold text-text-secondary dark:text-slate-400 uppercase mb-2 flex items-center gap-1">
        <Info className="w-3 h-3" />
        Metadata
      </h3>
      <div className="space-y-1.5 text-xs">
        <MetadataRow label="RA" value={spectrum.ra.toFixed(4) + '°'} />
        <MetadataRow label="Dec" value={spectrum.dec.toFixed(4) + '°'} />
        <MetadataRow label="Field" value={spectrum.field} />
        <MetadataRow label="Program" value={spectrum.program_name || '—'} />
        <MetadataRow
          label="SNR"
          value={activeSpec.signal_to_noise?.toFixed(1) || '—'}
          tooltip="Signal-to-noise ratio for active grating"
        />
        {spectrum.redshift && (
          <MetadataRow
            label="z_phot"
            value={spectrum.redshift.toFixed(3)}
            tooltip="Photometric redshift"
          />
        )}
      </div>
    </div>
  );
};
