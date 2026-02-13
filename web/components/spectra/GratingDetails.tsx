'use client';

import React, { useState } from 'react';
import { ChevronDown, ChevronRight, Download, Loader2 } from 'lucide-react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Spectrum } from '@/lib/types';
import { downloadSingleFile } from './DownloadButtons';

interface GratingDetailsProps {
  spectrum: Spectrum;
}

export const GratingDetails: React.FC<GratingDetailsProps> = ({ spectrum }) => {
  const [isOpen, setIsOpen] = useState(true);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDownload = async () => {
    setDownloading(true);
    setError(null);

    try {
      await downloadSingleFile(spectrum.fits_path);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Download failed');
    } finally {
      setDownloading(false);
    }
  };

  return (
    <Card className="p-6 mb-6">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center w-full text-left"
      >
        {isOpen ? (
          <ChevronDown className="w-5 h-5 text-text-secondary dark:text-slate-400 mr-2" />
        ) : (
          <ChevronRight className="w-5 h-5 text-text-secondary dark:text-slate-400 mr-2" />
        )}
        <h3 className="text-sm font-semibold text-text-primary dark:text-slate-100 uppercase tracking-wide">
          Grating Details
        </h3>
      </button>

      {isOpen && (
        <div className="mt-4">
          <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
            <div>
              <div className="text-xs text-text-secondary dark:text-slate-400 uppercase tracking-wide mb-1">
                Configuration
              </div>
              <div className="text-sm font-mono text-text-primary dark:text-slate-100">
                {spectrum.grating}
              </div>
            </div>

            <div>
              <div className="text-xs text-text-secondary dark:text-slate-400 uppercase tracking-wide mb-1">
                Exposure Time
              </div>
              <div className="text-sm font-mono text-text-primary dark:text-slate-100">
                {spectrum.exposure_time != null
                  ? spectrum.exposure_time > 3600
                    ? `${spectrum.exposure_time.toFixed(0)} s (${(spectrum.exposure_time / 3600).toFixed(2)} hr)`
                    : spectrum.exposure_time > 1000
                      ? `${spectrum.exposure_time.toFixed(0)} s (${(spectrum.exposure_time / 60).toFixed(2)} min)`
                      : `${spectrum.exposure_time.toFixed(0)} s`
                  : 'N/A'}
              </div>
            </div>

            <div>
              <div className="text-xs text-text-secondary dark:text-slate-400 uppercase tracking-wide mb-1">
                Max S/N
              </div>
              <div className="text-sm font-mono text-text-primary dark:text-slate-100">
                {spectrum.signal_to_noise ? spectrum.signal_to_noise.toFixed(1) : 'N/A'}
              </div>
            </div>

            <div>
              <div className="text-xs text-text-secondary dark:text-slate-400 uppercase tracking-wide mb-1">
                Version
              </div>
              <div className="text-sm font-mono text-text-primary dark:text-slate-100">
                {spectrum.reduction_version || 'N/A'}
              </div>
            </div>

            <div>
              <div className="text-xs text-text-secondary dark:text-slate-400 uppercase tracking-wide mb-1">
                FITS File
              </div>
              <div className="text-sm font-mono text-text-primary dark:text-slate-100 truncate" title={spectrum.fits_path}>
                {spectrum.fits_path.split('/').pop() || spectrum.fits_path}
              </div>
            </div>
          </div>

          {/* Download button */}
          <div className="mt-4 flex items-center gap-3">
            <Button
              variant="secondary"
              size="sm"
              onClick={handleDownload}
              disabled={downloading}
            >
              {downloading ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin mr-2" />
                  Downloading...
                </>
              ) : (
                <>
                  <Download className="w-4 h-4 mr-2" />
                  Download {spectrum.grating} FITS
                </>
              )}
            </Button>
            {error && (
              <span className="text-sm text-red-600 dark:text-red-400">{error}</span>
            )}
          </div>
        </div>
      )}
    </Card>
  );
};
