'use client';

import React, { useState } from 'react';
import { Download } from 'lucide-react';
import type { NircamExpmap } from '@/lib/types';
import { Card } from '@/components/ui/Card';
import { generateNircamExpmapDownloadUrls } from '@/lib/actions/download';

interface NircamExpmapTableProps {
  expmaps: NircamExpmap[];
}

// Format a byte count for display (mirrors NircamTable).
const formatFileSize = (bytes: number | undefined): string => {
  if (!bytes) return '-';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
};

// Per-row download: authorizes + presigns the expmap key server-side, then
// navigates the browser to the credential-free proxy URL to start the download.
const DownloadCell: React.FC<{ expmap: NircamExpmap }> = ({ expmap }) => {
  const [busy, setBusy] = useState(false);

  const handleDownload = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const { urls } = await generateNircamExpmapDownloadUrls([expmap.storage_key]);
      const proxyUrl = urls[expmap.storage_key];
      if (proxyUrl) {
        const link = document.createElement('a');
        link.href = proxyUrl;
        link.download = expmap.storage_key.split('/').pop() || expmap.storage_key;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
      }
    } catch (err) {
      console.error('Failed to start NIRCam expmap download:', err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      type="button"
      onClick={handleDownload}
      disabled={busy}
      className="inline-flex items-center gap-1.5 text-sm text-primary hover:text-primary-hover hover:underline disabled:opacity-50 disabled:cursor-not-allowed"
    >
      <Download className="w-4 h-4" />
      <span>{busy ? 'Preparing…' : 'Download'}</span>
    </button>
  );
};

export const NircamExpmapTable: React.FC<NircamExpmapTableProps> = ({ expmaps }) => {
  if (expmaps.length === 0) return null;

  return (
    <Card className="overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead className="bg-table-header border-b border-border">
            <tr>
              {['Field', 'Filter', 'Size', 'Download'].map((h) => (
                <th
                  key={h}
                  className="px-4 py-3 text-left text-xs font-medium text-text-secondary uppercase tracking-wider"
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="bg-card divide-y divide-border">
            {expmaps.map((expmap) => (
              <tr
                key={expmap.storage_key}
                className="hover:bg-card-hover transition-colors"
              >
                <td className="px-4 py-3 whitespace-nowrap">
                  <span className="text-sm font-medium text-text-primary uppercase">
                    {expmap.field}
                  </span>
                </td>
                <td className="px-4 py-3 whitespace-nowrap">
                  <span className="text-sm font-mono text-text-primary uppercase">
                    {expmap.filter}
                  </span>
                </td>
                <td className="px-4 py-3 whitespace-nowrap">
                  <span className="text-sm text-text-secondary">
                    {formatFileSize(expmap.file_size)}
                  </span>
                </td>
                <td className="px-4 py-3 whitespace-nowrap">
                  <DownloadCell expmap={expmap} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
};
