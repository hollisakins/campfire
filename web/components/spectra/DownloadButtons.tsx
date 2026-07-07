'use client';

import React, { useState } from 'react';
import { Button } from '@/components/ui/Button';
import { Download, Loader2 } from 'lucide-react';
import type { Spectrum } from '@/lib/types';
import { generateObjectFitsDownloadUrls } from '@/lib/actions/download';
import { downloadFilesAsZip } from '@/lib/utils/zip-download';

interface DownloadButtonsProps {
  spectra: Spectrum[];
  targetId: string;
}

export const DownloadButtons: React.FC<DownloadButtonsProps> = ({ spectra, targetId }) => {
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [batchProgress, setBatchProgress] = useState<string | null>(null);

  const handleDownloadAll = async () => {
    if (spectra.length === 0) return;

    setDownloading(true);
    setError(null);
    setBatchProgress(null);

    try {
      const allPaths = spectra.map(s => s.fits_path);

      // Authorize all of this object's spectra server-side (RLS) and get
      // ready-to-fetch proxy URLs, then bundle them into a single ZIP in the
      // browser. Fetching cross-origin presigned URLs through the proxy (which
      // supplies CORS) and zipping is the only reliable way to deliver multiple
      // files — sequential per-file anchor downloads get popup-blocked after the
      // first, so only one file ever saved.
      const result = await generateObjectFitsDownloadUrls(allPaths, targetId);

      if (result.error || !result.files) {
        setError(result.error || 'Failed to generate download URLs');
        return;
      }

      const { files, zipFilename } = result;
      setBatchProgress(`0/${files.length}`);

      const res = await downloadFilesAsZip(
        files,
        zipFilename || `${targetId}_spectra.zip`,
        (done, total) => setBatchProgress(`${done}/${total}`),
      );

      if (!res.ok) {
        setError(res.error || 'Download failed');
        return;
      }
      if (res.failed.length > 0) {
        setError(`${res.failed.length} file(s) failed to download`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Download failed');
    } finally {
      setDownloading(false);
      setBatchProgress(null);
    }
  };

  return (
    <div className="flex gap-4 items-center">
      <Button
        variant="primary"
        onClick={handleDownloadAll}
        disabled={downloading || spectra.length === 0}
      >
        {downloading ? (
          <>
            <Loader2 className="w-4 h-4 animate-spin mr-2" />
            Downloading{batchProgress ? ` (${batchProgress})` : ''}...
          </>
        ) : (
          <>
            <Download className="w-4 h-4 mr-2" />
            Download All ({spectra.length} files)
          </>
        )}
      </Button>

      {error && (
        <span className="text-sm text-red-600">{error}</span>
      )}
    </div>
  );
};

/**
 * Download a single FITS file
 */
export async function downloadSingleFile(fitsPath: string): Promise<void> {
  const response = await fetch(`/api/download?path=${encodeURIComponent(fitsPath)}`);

  if (!response.ok) {
    const data = await response.json();
    throw new Error(data.error || 'Failed to generate download URL');
  }

  const { url } = await response.json();
  const filename = fitsPath.split('/').pop() || 'spectrum.fits';

  await downloadFile(url, filename);
}

/**
 * Trigger browser download from a URL
 */
async function downloadFile(url: string, filename: string): Promise<void> {
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.target = '_blank';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}
