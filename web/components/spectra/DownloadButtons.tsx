'use client';

import React, { useState } from 'react';
import { Button } from '@/components/ui/Button';
import { Download, Loader2 } from 'lucide-react';
import type { Spectrum } from '@/lib/types';

interface DownloadButtonsProps {
  spectra: Spectrum[];
  objectId: string;
}

export const DownloadButtons: React.FC<DownloadButtonsProps> = ({ spectra, objectId }) => {
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDownloadAll = async () => {
    if (spectra.length === 0) return;

    setDownloading(true);
    setError(null);

    try {
      const paths = spectra.map(s => s.fits_path);

      const response = await fetch('/api/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Failed to generate download URLs');
      }

      const { urls } = await response.json();

      // Download each file
      for (const [path, url] of Object.entries(urls)) {
        const filename = path.split('/').pop() || `${objectId}.fits`;
        await downloadFile(url as string, filename);
        // Small delay between downloads to prevent browser blocking
        await new Promise(resolve => setTimeout(resolve, 300));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Download failed');
    } finally {
      setDownloading(false);
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
            Downloading...
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
