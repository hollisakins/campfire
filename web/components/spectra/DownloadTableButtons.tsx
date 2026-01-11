'use client';

import React, { useState } from 'react';
import { Download, FileText, Package, Loader2, ChevronDown } from 'lucide-react';
import { Card } from '@/components/ui/Card';
import { generateCSV, generateCsvFilename, generateFitsDownloadUrl } from '@/lib/actions/download';
import type { SortColumn, SortDirection } from '@/lib/actions/spectra-types';
import { AdvancedFilterOptions } from './SpectraFilterBar';

interface DownloadTableButtonsProps {
  totalCount: number;
  filters: AdvancedFilterOptions;
  sortColumn: SortColumn;
  sortDirection: SortDirection;
  loading?: boolean;
}

export const DownloadTableButtons: React.FC<DownloadTableButtonsProps> = ({
  totalCount,
  filters,
  sortColumn,
  sortDirection,
  loading = false,
}) => {
  const [isExpanded, setIsExpanded] = useState(false);
  const [csvLoading, setCsvLoading] = useState(false);
  const [fitsLoading, setFitsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const FITS_LIMIT = 200;
  const fitsDisabled = totalCount > FITS_LIMIT || loading;

  const handleCsvDownload = async () => {
    setCsvLoading(true);
    setError(null);

    try {
      const result = await generateCSV(filters, sortColumn, sortDirection);

      if (result.error || !result.csv) {
        setError(result.error || 'Failed to generate CSV');
        return;
      }

      // Create blob and trigger download
      const blob = new Blob([result.csv], { type: 'text/csv;charset=utf-8;' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = await generateCsvFilename();
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error('CSV download error:', err);
      setError('Failed to download CSV file');
    } finally {
      setCsvLoading(false);
    }
  };

  const handleFitsDownload = async () => {
    setFitsLoading(true);
    setError(null);

    try {
      // Generate download URL with JWT token
      const result = await generateFitsDownloadUrl(filters, sortColumn, sortDirection);

      if (result.error || !result.url) {
        setError(result.error || 'Failed to generate download URL');
        return;
      }

      // Redirect to Worker URL (will trigger download)
      window.location.href = result.url;
    } catch (err) {
      console.error('FITS download error:', err);
      setError('Failed to download FITS files');
    } finally {
      // Keep loading state for a moment while redirect happens
      setTimeout(() => setFitsLoading(false), 2000);
    }
  };

  return (
    <Card className="mb-4">
      {/* Collapsible Header */}
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        disabled={loading}
        className="w-full p-3 min-h-[52px] flex items-center justify-between hover:bg-background-hover transition-colors disabled:cursor-not-allowed disabled:opacity-70"
      >
        <div className="flex items-center gap-2">
          {loading ? (
            <>
              <Loader2 className="w-4 h-4 animate-spin text-text-secondary" />
              <span className="text-sm text-text-secondary">Loading objects...</span>
            </>
          ) : (
            <>
              <span className="text-sm text-text-secondary">
                {totalCount.toLocaleString()} {totalCount === 1 ? 'object' : 'objects'} found
              </span>
              <span className="text-text-secondary">|</span>
              <Download className="w-4 h-4 text-text-secondary dark:text-slate-400" />
              <h3 className="text-sm font-semibold text-text-primary dark:text-slate-100">Download Results</h3>
            </>
          )}
        </div>
        <ChevronDown
          className={`w-4 h-4 text-text-secondary transition-transform ${isExpanded ? 'rotate-180' : ''}`}
        />
      </button>

      {/* Expandable Content */}
      {isExpanded && (
        <div className="px-4 pb-4 border-t border-border">
          <p className="text-xs text-text-secondary mt-3 mb-4">
            Export filtered results as CSV metadata or download FITS spectroscopic data files
          </p>

          <div className="flex gap-2">
            {/* CSV Download Button */}
            <button
              onClick={handleCsvDownload}
              disabled={csvLoading || loading}
              className="inline-flex items-center gap-2 px-4 py-2 bg-primary text-white rounded-md hover:bg-primary-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed text-sm font-medium"
              title={loading ? 'Please wait while objects are loading' : 'Download CSV table'}
            >
              {csvLoading ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Generating...
                </>
              ) : (
                <>
                  <FileText className="w-4 h-4" />
                  CSV Table
                </>
              )}
            </button>

            {/* FITS ZIP Download Button */}
            <button
              onClick={handleFitsDownload}
              disabled={fitsLoading || fitsDisabled}
              className="inline-flex items-center gap-2 px-4 py-2 bg-primary text-white rounded-md hover:bg-primary-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed text-sm font-medium"
              title={
                loading
                  ? 'Please wait while objects are loading'
                  : totalCount > FITS_LIMIT
                    ? `Limited to ${FITS_LIMIT} objects. Please refine filters.`
                    : 'Download FITS files as ZIP'
              }
            >
              {fitsLoading ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Preparing...
                </>
              ) : (
                <>
                  <Package className="w-4 h-4" />
                  FITS ZIP
                </>
              )}
            </button>
          </div>

          {/* Warning/Info Messages */}
          {fitsDisabled && (
            <div className="mt-3 flex items-start gap-2 text-xs text-amber-700 dark:text-amber-400 bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800 rounded-md p-2">
              <span className="flex-shrink-0 mt-0.5">⚠️</span>
              <span>
                FITS download is limited to {FITS_LIMIT} objects. Your current filters return{' '}
                {totalCount.toLocaleString()} objects. Please refine your filters to download spectroscopic data.
              </span>
            </div>
          )}

          {!fitsDisabled && totalCount > 0 && (
            <div className="mt-3 text-xs text-text-secondary">
              <span className="flex items-center gap-1">
                <span>✓</span>
                <span>
                  Ready to download {totalCount.toLocaleString()} {totalCount === 1 ? 'object' : 'objects'}
                </span>
              </span>
            </div>
          )}

          {/* Error Message */}
          {error && (
            <div className="mt-3 flex items-start gap-2 text-xs text-red-700 dark:text-red-400 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 rounded-md p-2">
              <span className="flex-shrink-0 mt-0.5">❌</span>
              <span>{error}</span>
            </div>
          )}
        </div>
      )}
    </Card>
  );
};
