'use client';

import React, { useState, useMemo } from 'react';
import Link from 'next/link';
import { ChevronDown, ChevronUp, Download, Copy, Check, Info, KeyRound } from 'lucide-react';
import type { NircamProductRow } from '@/lib/types';
import {
  API_KEYS_PATH,
  NIRCAM_DOWNLOAD_SCRIPT_FILENAME,
  buildNircamDownloadScript,
  formatFileSize,
  transferBytes,
} from '@/lib/nircam-download-script';

interface CurlScriptGeneratorProps {
  selectedImages: NircamProductRow[];
  className?: string;
}

// Where the script will send its API calls: this deployment. The component is
// client-only and the script is built after a click, so `window` exists by
// then; the env fallback only covers the (never-shown) server render.
function siteOrigin(): string {
  if (typeof window !== 'undefined') return window.location.origin;
  return process.env.NEXT_PUBLIC_APP_URL || 'https://campfire.hollisakins.com';
}

/**
 * Bulk-download panel for a NIRCam product selection: a shell script the user
 * runs locally.
 *
 * The script carries no urls and no credentials. Each file is fetched through
 * GET /api/v1/storage/download with the user's API key (read from
 * CAMPFIRE_API_KEY, or prompted for), which answers with a fresh presigned url
 * at download time — so the script never expires, however long a whole-field
 * download takes. Files already on disk are skipped and partial downloads
 * resume, so a failed run is simply re-run. See lib/nircam-download-script.ts.
 *
 * Building it needs no server call: the selection comes from the field page's
 * RLS-scoped listing, and the route re-authorizes every key when the script
 * runs, under the API key's own program scope.
 */
export const CurlScriptGenerator: React.FC<CurlScriptGeneratorProps> = ({
  selectedImages,
  className = '',
}) => {
  const [isExpanded, setIsExpanded] = useState(false);
  const [copied, setCopied] = useState(false);

  // Transfer estimate: stored (gzipped) bytes when recorded, logical
  // otherwise — what the downloads actually move.
  const totalSize = useMemo(
    () => selectedImages.reduce((sum, r) => sum + transferBytes(r), 0),
    [selectedImages],
  );

  // Only build once the panel is open: a whole-field selection is thousands
  // of lines, and the generation timestamp should be when the user looked.
  const script = useMemo(
    () => (isExpanded ? buildNircamDownloadScript(selectedImages, siteOrigin()) : ''),
    [isExpanded, selectedImages],
  );

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(script);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy script:', err);
    }
  };

  const handleDownload = () => {
    const blob = new Blob([script], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = NIRCAM_DOWNLOAD_SCRIPT_FILENAME;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  if (selectedImages.length === 0) {
    return null;
  }

  return (
    <div className={`bg-card border border-border rounded-lg ${className}`}>
      {/* Toggle header */}
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-card-hover transition-colors rounded-lg"
      >
        <div className="flex items-center gap-2">
          {isExpanded ? (
            <ChevronUp className="w-4 h-4 text-text-secondary" />
          ) : (
            <ChevronDown className="w-4 h-4 text-text-secondary" />
          )}
          <span className="text-sm font-medium text-text-primary">
            Bulk download {selectedImages.length} file{selectedImages.length === 1 ? '' : 's'}
          </span>
          <span className="text-sm text-text-secondary">
            ({formatFileSize(totalSize)} total)
          </span>
        </div>
      </button>

      {/* Expanded content */}
      {isExpanded && (
        <div className="border-t border-border">
          {/* How to run it: the script authenticates with an API key at
              download time, so it needs one and never expires. */}
          <div className="px-4 pt-4">
            <div className="flex items-start gap-2 bg-background border border-border rounded-lg p-3">
              <KeyRound className="w-4 h-4 text-text-secondary mt-0.5 shrink-0" />
              <p className="text-sm text-text-secondary">
                The script fetches each file&apos;s download link as it goes, so it
                never expires and can be re-run to resume after a failure. It needs
                an API key: create one at{' '}
                <Link href={API_KEYS_PATH} className="text-primary hover:underline">
                  API keys
                </Link>{' '}
                and run{' '}
                <code className="font-mono text-xs">CAMPFIRE_API_KEY=sk_… bash {NIRCAM_DOWNLOAD_SCRIPT_FILENAME}</code>
                {' '}(it prompts for the key otherwise).
              </p>
            </div>
          </div>

          {/* Script preview */}
          <div className="p-4">
            <div className="bg-gray-900 rounded-lg overflow-hidden">
              <div className="flex items-center justify-between px-4 py-2 bg-gray-800 border-b border-gray-700">
                <span className="text-sm text-gray-400 font-mono">{NIRCAM_DOWNLOAD_SCRIPT_FILENAME}</span>
                <div className="flex items-center gap-2">
                  {/* Plain buttons: the code panel is always dark, so the
                      theme-aware Button ghost variant is unreadable here. */}
                  <button
                    onClick={handleCopy}
                    disabled={!script}
                    className="inline-flex items-center rounded-md px-2.5 py-1.5 text-sm font-medium text-gray-300 hover:text-white hover:bg-gray-700 transition-colors disabled:opacity-50 disabled:pointer-events-none"
                  >
                    {copied ? (
                      <>
                        <Check className="w-4 h-4 mr-1.5" />
                        Copied
                      </>
                    ) : (
                      <>
                        <Copy className="w-4 h-4 mr-1.5" />
                        Copy
                      </>
                    )}
                  </button>
                  <button
                    onClick={handleDownload}
                    disabled={!script}
                    className="inline-flex items-center rounded-md px-2.5 py-1.5 text-sm font-medium text-gray-300 hover:text-white hover:bg-gray-700 transition-colors disabled:opacity-50 disabled:pointer-events-none"
                  >
                    <Download className="w-4 h-4 mr-1.5" />
                    Download
                  </button>
                </div>
              </div>
              <pre className="p-4 text-sm text-gray-300 font-mono overflow-x-auto max-h-96 overflow-y-auto">
                <code>{script}</code>
              </pre>
            </div>
          </div>

          {/* Programmatic access pointer */}
          <div className="px-4 pb-4">
            <div className="flex items-start gap-2 bg-background border border-border rounded-lg p-3">
              <Info className="w-4 h-4 text-text-secondary mt-0.5 shrink-0" />
              <p className="text-sm text-text-secondary">
                Regularly bulk-downloading CAMPFIRE data? See{' '}
                <Link href="/docs/api" className="text-primary hover:underline">
                  programmatic access
                </Link>{' '}
                for the CLI (<code className="font-mono text-xs">campfire pull --field</code>) and Python client.
              </p>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
