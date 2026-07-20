'use client';

import React, { useState, useRef, useEffect } from 'react';
import { Download, FileText, Package, Loader2, ChevronDown } from 'lucide-react';
import { generateCSV, generateCsvFilename, generateFitsDownloadUrl } from '@/lib/actions/download';
import type { SortColumn, SortDirection, ViewMode } from '@/lib/actions/spectra-types';
import { FITS_DOWNLOAD_FILE_LIMIT, CSV_EXPORT_ROW_LIMIT } from '@/lib/actions/spectra-types';
import { downloadFilesAsZip } from '@/lib/utils/zip-download';
import { AdvancedFilterOptions } from './SpectraFilterBar';

interface DownloadDropdownProps {
  totalCount: number;
  filters: AdvancedFilterOptions;
  sortColumn: SortColumn;
  sortDirection: SortDirection;
  viewMode?: ViewMode;
  loading?: boolean;
}

export const DownloadDropdown: React.FC<DownloadDropdownProps> = ({
  totalCount,
  filters,
  sortColumn,
  sortDirection,
  viewMode = 'objects',
  loading = false,
}) => {
  const [isOpen, setIsOpen] = useState(false);
  const [csvLoading, setCsvLoading] = useState(false);
  const [fitsLoading, setFitsLoading] = useState(false);
  const [fitsProgress, setFitsProgress] = useState<{ done: number; total: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // Shared with the server action, which enforces the cap (generateCSV stops
  // its keyset pagination loop at this many rows).
  const CSV_LIMIT = CSV_EXPORT_ROW_LIMIT;
  // FITS_LIMIT is in spectra/file units. In spectra mode totalCount is already
  // the spectra count, so this gate is exact. In objects mode totalCount is the
  // object count — a loose lower bound on the spectra count (one object fans out
  // to 2-3 gratings), so the gate disables correctly once objects exceed the
  // limit but may stay enabled below it; the server action backstops that case
  // by refusing the download with a clear error rather than truncating.
  const FITS_LIMIT = FITS_DOWNLOAD_FILE_LIMIT;
  const csvWillTruncate = totalCount > CSV_LIMIT;
  const fitsDisabled = totalCount > FITS_LIMIT || loading;

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

  const handleCsvDownload = async () => {
    setCsvLoading(true);
    setError(null);

    try {
      const result = await generateCSV(filters, sortColumn, sortDirection, viewMode);

      if (result.error || !result.csv) {
        setError(result.error || 'Failed to generate CSV');
        return;
      }

      // Create blob and trigger download
      const blob = new Blob([result.csv], { type: 'text/csv;charset=utf-8;' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = await generateCsvFilename(viewMode);
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
    setFitsProgress(null);
    setError(null);

    try {
      const result = await generateFitsDownloadUrl(filters, sortColumn, sortDirection, viewMode);

      if (result.error || !result.files) {
        setError(result.error || 'Failed to generate download URL');
        return;
      }

      const { files, zipFilename } = result;
      setFitsProgress({ done: 0, total: files.length });

      // Fetch each file through the proxy and bundle into a single ZIP client-side.
      const res = await downloadFilesAsZip(
        files,
        zipFilename || 'campfire_download.zip',
        (done, total) => setFitsProgress({ done, total }),
      );

      if (!res.ok) {
        setError(res.error || 'Failed to download FITS files');
        return;
      }
      if (res.failed.length > 0) {
        setError(`${res.failed.length} file(s) failed to download`);
      }
    } catch (err) {
      console.error('FITS download error:', err);
      setError('Failed to download FITS files');
    } finally {
      setFitsLoading(false);
      setFitsProgress(null);
    }
  };

  return (
    <div ref={containerRef} className="relative">
      {/* Dropdown Toggle Button */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        disabled={loading}
        className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-text-secondary hover:text-text-primary hover:bg-card-hover rounded-md transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
      >
        <Download className="w-4 h-4" />
        <span>Download</span>
        <ChevronDown className={`w-3.5 h-3.5 transition-transform ${isOpen ? 'rotate-180' : ''}`} />
      </button>

      {/* Dropdown Panel */}
      {isOpen && (
        <div className="absolute right-0 z-50 mt-1 w-[320px] bg-background dark:bg-card border border-border rounded-lg shadow-lg">
          {/* Header */}
          <div className="px-4 py-3 border-b border-border bg-surface-2">
            <div className="text-sm font-medium text-text-primary">
              Download Results
            </div>
            <div className="text-xs text-text-secondary mt-0.5">
              {loading ? 'Loading...' : `${totalCount.toLocaleString()} ${viewMode === 'spectra' ? 'spectra' : (totalCount === 1 ? 'object' : 'objects')}`}
            </div>
          </div>

          {/* Download Buttons */}
          <div className="p-3 space-y-2">
            {/* CSV Download Button */}
            <button
              onClick={handleCsvDownload}
              disabled={csvLoading || loading}
              className="w-full flex items-center gap-3 px-3 py-2.5 text-sm text-left hover:bg-card-hover rounded-md transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              title={
                loading
                  ? 'Please wait while objects are loading'
                  : csvWillTruncate
                    ? `CSV will be limited to ${CSV_LIMIT.toLocaleString()} objects`
                    : 'Download CSV table'
              }
            >
              {csvLoading ? (
                <Loader2 className="w-4 h-4 animate-spin text-primary" />
              ) : (
                <FileText className="w-4 h-4 text-text-secondary" />
              )}
              <div className="flex-1">
                <div className="font-medium text-text-primary">
                  {csvLoading ? 'Generating...' : 'CSV Table'}
                </div>
                <div className="text-xs text-text-secondary">
                  Object metadata and properties
                </div>
              </div>
            </button>

            {/* FITS ZIP Download Button */}
            <button
              onClick={handleFitsDownload}
              disabled={fitsLoading || fitsDisabled}
              className="w-full flex items-center gap-3 px-3 py-2.5 text-sm text-left hover:bg-card-hover rounded-md transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              title={
                loading
                  ? 'Please wait while objects are loading'
                  : totalCount > FITS_LIMIT
                    ? `Limited to ${FITS_LIMIT} FITS files. Please refine filters.`
                    : 'Download FITS files as ZIP'
              }
            >
              {fitsLoading ? (
                <Loader2 className="w-4 h-4 animate-spin text-primary" />
              ) : (
                <Package className="w-4 h-4 text-text-secondary" />
              )}
              <div className="flex-1">
                <div className="font-medium text-text-primary">
                  {fitsProgress
                    ? `Downloading ${fitsProgress.done}/${fitsProgress.total}...`
                    : fitsLoading ? 'Preparing...' : 'FITS ZIP'}
                </div>
                <div className="text-xs text-text-secondary">
                  Spectroscopic data files
                </div>
              </div>
            </button>
          </div>

          {/* Warning/Info Messages */}
          {(csvWillTruncate || fitsDisabled || error) && (
            <div className="px-3 pb-3 space-y-2">
              {csvWillTruncate && (
                <div className="flex items-start gap-2 text-xs text-amber-700 dark:text-amber-400 bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800 rounded-md p-2">
                  <span className="flex-shrink-0 mt-0.5">⚠️</span>
                  <span>CSV limited to {CSV_LIMIT.toLocaleString()} objects</span>
                </div>
              )}

              {fitsDisabled && totalCount > FITS_LIMIT && (
                <div className="flex items-start gap-2 text-xs text-amber-700 dark:text-amber-400 bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800 rounded-md p-2">
                  <span className="flex-shrink-0 mt-0.5">⚠️</span>
                  <span>FITS download limited to {FITS_LIMIT} files</span>
                </div>
              )}

              {error && (
                <div className="flex items-start gap-2 text-xs text-red-700 dark:text-red-400 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 rounded-md p-2">
                  <span className="flex-shrink-0 mt-0.5">❌</span>
                  <span>{error}</span>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
};
